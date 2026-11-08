"""Keep a single Why2K bot continuously present in its voice channel.

When the voice media WebSocket closes with a transient code (1001 "server
going away" / 1006 "abnormal closure"), discord.py's default reconnect path
tears down the gateway voice state via change_voice_state(channel=None) and
asks Discord for a fresh voice server. That leave is what resets a call timer
when the bot is the sole occupant.

This module attempts a different recovery first: open a new voice WSS to the
SAME cached endpoint and send OP 7 RESUME with the cached session_id/token.
If Discord replies OP 9 RESUMED, the session continues, no gateway voice state
ever changed, and the participant count stays exactly where it was. If Discord
rejects the resume (e.g. close code 4006 "session no longer valid"), if the
WSS open fails, or if RESUMED doesn't arrive within RESUME_TIMEOUT seconds, the
helper cleans up and returns False — and the upstream `_poll_voice_ws` code
that we copied verbatim around the new branch falls through to discord.py's
existing leave-rejoin — the same ~2s blip the bot has had all along.

A failed attempt starts that fallback up to RESUME_TIMEOUT later than stock
discord.py would. That delay is deliberate: the attempt sends no gateway
voice-state change, so the bot stays in the channel throughout (production
logs show Discord keeping it listed for 30 s with no voice WS). Media is down
for longer, but the bot is self-deaf/self-mute; channel presence — the only
thing this module protects — is never worse than baseline.

Coupled to discord.py internals (the copied `_poll_voice_ws`, the
`DiscordVoiceWebSocket.from_connection_state` signature, the OP 9 RESUMED
opcode delivery via the WS hook). discord.py is pinned at 2.7.1 in
requirements.txt; re-verify against any version bump (the
`test_targets_pinned_discord_version` test fails CI on a mismatch).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import discord
from discord.backoff import ExponentialBackoff
from discord.errors import ConnectionClosed
from discord.gateway import DiscordVoiceWebSocket
from discord.utils import MISSING
from discord.voice_client import VoiceClient
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

_log = logging.getLogger(__name__)

# The discord.py version this module's internal-coupling was verified against.
# Exported so the test suite can assert the installed version matches (single
# source of truth — bump here when re-verifying against a new discord.py).
PINNED_DISCORD_VERSION = "2.7.1"

if discord.__version__ != PINNED_DISCORD_VERSION:
    _log.warning(
        "voice_resilience targets discord.py %s but %s is installed. The "
        "in-place resume relies on _poll_voice_ws's structure, "
        "DiscordVoiceWebSocket.from_connection_state's signature, and OP 9 "
        "RESUMED being delivered via the WS hook; re-verify against this version.",
        PINNED_DISCORD_VERSION,
        discord.__version__,
    )


class ResilientVoiceConnectionState(VoiceConnectionState):
    """VoiceConnectionState that attempts OP 7 RESUME before leave-rejoin."""

    # One budget for the whole attempt — opening the WSS, sending OP 7 and
    # waiting for OP 9 RESUMED all share a single deadline — so a failed
    # attempt delays the upstream leave-rejoin fallback by at most this long.
    RESUME_TIMEOUT: float = 10.0

    async def _try_in_place_resume(self) -> bool:
        """Try to recover the voice session via OP 7 RESUME.

        Returns True only after positively observing OP 9 RESUMED from Discord
        (caught via a one-shot hook on the new WS). Returns False on any
        failure — the caller is responsible for falling back to upstream's
        leave-rejoin. The helper never calls disconnect() and never emits any
        gateway voice-state change, so a False return leaves the gateway
        voice state untouched.
        """
        if not (
            self.endpoint and self.session_id and self.token and self.server_id
        ):
            return False

        resumed_event = asyncio.Event()
        original_hook = self.hook

        async def _watch_for_resumed(
            ws: DiscordVoiceWebSocket, msg: Dict[str, Any]
        ) -> None:
            if msg.get("op") == DiscordVoiceWebSocket.RESUMED:
                resumed_event.set()
            if original_hook is not None:
                await original_hook(ws, msg)

        previous_ws = self.ws
        entry_state = self.state
        new_ws: Optional[DiscordVoiceWebSocket] = None
        seq_ack = -1
        if previous_ws is not MISSING:
            seq_ack = previous_ws.seq_ack

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.RESUME_TIMEOUT
        try:
            # Open a fresh WSS to the cached endpoint and send OP 7 RESUME.
            # We call from_connection_state directly (bypassing
            # _connect_websocket) so we can install the resume-watching hook
            # for THIS attempt without mutating self.hook.
            #
            # Caveat: if wait_for cancels from_connection_state after its
            # internal ws_connect() succeeds but before it returns (e.g. during
            # the ws.resume() send), the aiohttp socket leaks until GC. This is
            # a limitation of the discord.py API — from_connection_state has no
            # cleanup path for that window. The bot still recovers via the
            # leave-rejoin fallback; only the orphan socket is the cost.
            new_ws = await asyncio.wait_for(
                DiscordVoiceWebSocket.from_connection_state(
                    self,
                    resume=True,
                    hook=_watch_for_resumed,
                    seq_ack=seq_ack,
                ),
                timeout=self.RESUME_TIMEOUT,
            )

            # Drive poll_event until either RESUMED is seen (success) or the
            # WS closes / we hit the shared deadline (failure).
            while not resumed_event.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError("OP 9 RESUMED not received in time")
                await asyncio.wait_for(new_ws.poll_event(), timeout=remaining)
        except asyncio.CancelledError:
            # CancelledError is a BaseException (3.8+), so the handler below
            # never sees it. Close the half-open resume WS — its keep-alive
            # thread starts on HELLO and only close() stops it — then let the
            # cancellation propagate.
            if new_ws is not None:
                try:
                    await new_ws.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            _log.info(
                "Voice WS resume attempt failed (%s: %s); falling back to leave-rejoin.",
                type(exc).__name__,
                exc,
            )
            if new_ws is not None:
                try:
                    await new_ws.close()
                except Exception:
                    pass
            return False

        if self.state is not entry_state or self.ws is not previous_ws:
            # Something else took over this connection while we waited — e.g.
            # VoiceClient.disconnect(), which marks the state disconnected but
            # does not cancel this poller. Installing the resumed WS now would
            # revive a connection that was deliberately torn down, so discard
            # it and leave the state as the other actor set it.
            _log.info(
                "Voice connection changed during resume attempt; discarding resumed WS."
            )
            try:
                await new_ws.close()
            except Exception:
                pass
            return False

        # Resume positively confirmed. Install the new WS, mark connected,
        # close the dead one. We do NOT call _handshake_websocket: on a
        # successful resume Discord does not re-send OP 4 SESSION DESCRIPTION,
        # so it would hang forever on `while self.ws.secret_key is None`. The
        # bot is self-deaf/self-mute and never uses secret_key.
        self.ws = new_ws
        self.state = ConnectionFlowState.connected
        if previous_ws is not None and previous_ws is not MISSING:
            try:
                await previous_ws.close()
            except Exception:
                pass
        _log.info(
            "Voice WS resumed in place (no channel leave); endpoint=%s",
            self.endpoint,
        )
        return True

    async def _poll_voice_ws(self, reconnect: bool) -> None:
        # COPIED VERBATIM from discord.py 2.7.1 VoiceConnectionState._poll_voice_ws,
        # with one addition: a 1001/1006 branch that tries _try_in_place_resume
        # before the generic fall-through. All other branches preserved so behavior
        # on close codes we don't target is identical to upstream.
        backoff = ExponentialBackoff()
        while True:
            try:
                await self.ws.poll_event()
            except asyncio.CancelledError:
                return
            except (ConnectionClosed, asyncio.TimeoutError) as exc:
                if isinstance(exc, ConnectionClosed):
                    # 1000 - normal closure
                    # 4014 - we were externally disconnected
                    # 4015 - voice server crashed, we should resume
                    # 4021 - rate limited, we should not reconnect
                    # 4022 - call terminated, similar to 4014
                    if exc.code == 1000:
                        if not self._expecting_disconnect:
                            _log.info(
                                "Disconnecting from voice normally, close code %d.",
                                exc.code,
                            )
                            await self.disconnect()
                        break

                    if exc.code in (4014, 4022):
                        if self._disconnected.is_set():
                            _log.info(
                                "Disconnected from voice by discord, close code %d.",
                                exc.code,
                            )
                            await self.disconnect()
                            break
                        _log.info(
                            "Disconnected from voice by force... potentially reconnecting."
                        )
                        successful = await self._potential_reconnect()
                        if not successful:
                            _log.info(
                                "Reconnect was unsuccessful, disconnecting from voice normally..."
                            )
                            if self.state is not ConnectionFlowState.disconnected:
                                await self.disconnect()
                            break
                        else:
                            continue

                    if exc.code == 4021:
                        _log.warning(
                            "We are being ratelimited while trying to connect to voice. Disconnecting..."
                        )
                        if self.state is not ConnectionFlowState.disconnected:
                            await self.disconnect()
                        break

                    if exc.code == 4015:
                        _log.info("Disconnected from voice, attempting a resume...")
                        try:
                            await self._connect(
                                reconnect=reconnect,
                                timeout=self.timeout,
                                self_deaf=(self.self_voice_state or self).self_deaf,
                                self_mute=(self.self_voice_state or self).self_mute,
                                resume=True,
                            )
                        except asyncio.TimeoutError:
                            _log.info(
                                "Could not resume the voice connection... Disconnecting..."
                            )
                            if self.state is not ConnectionFlowState.disconnected:
                                await self.disconnect()
                            break
                        else:
                            _log.info("Successfully resumed voice connection")
                            continue

                    # NEW BRANCH: transient transport-level codes. Try OP 7
                    # RESUME to the cached endpoint before falling through to
                    # the upstream leave-rejoin path. If resume succeeds we
                    # continue the loop on the new WS; if it fails we let the
                    # rest of upstream's logic run (identical to baseline).
                    # Gated on `reconnect` so reconnect=False keeps upstream's
                    # disconnect-and-raise behaviour.
                    if reconnect and exc.code in (1001, 1006):
                        _log.info(
                            "Attempting in-place voice WS resume for close code %d.",
                            exc.code,
                        )
                        if await self._try_in_place_resume():
                            continue
                        # else: fall through to upstream generic reconnect

                    _log.debug(
                        "Not handling close code %s (%s)",
                        exc.code,
                        exc.reason or "no reason",
                    )

                if not reconnect:
                    await self.disconnect()
                    raise

                retry = backoff.delay()
                _log.exception("Disconnected from voice... Reconnecting in %.2fs.", retry)
                await asyncio.sleep(retry)
                await self.disconnect(cleanup=False)

                try:
                    await self._connect(
                        reconnect=reconnect,
                        timeout=self.timeout,
                        self_deaf=(self.self_voice_state or self).self_deaf,
                        self_mute=(self.self_voice_state or self).self_mute,
                        resume=False,
                    )
                except asyncio.TimeoutError:
                    # at this point we've retried 5 times... let's continue the loop.
                    _log.warning("Could not connect to voice... Retrying...")
                    continue


class ResilientVoiceClient(VoiceClient):
    """VoiceClient that uses ResilientVoiceConnectionState."""

    def create_connection_state(self) -> VoiceConnectionState:
        return ResilientVoiceConnectionState(self)
