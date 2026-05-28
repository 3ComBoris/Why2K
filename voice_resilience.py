"""Keep a single Why2K bot continuously present in its voice channel.

discord.py tears a voice connection down and rebuilds it on transient voice
WebSocket closes (e.g. close codes 1001 "going away" and 1006 "abnormal
closure"). That teardown sends a gateway OP4 voice-state update with
channel=None, which makes the bot *leave* the channel for ~2 seconds. When the
bot is the only occupant, the channel empties, the call session ends, and the
call timer resets.

The gateway leave originates in VoiceConnectionState._voice_disconnect(), which
is only reached via VoiceConnectionState.disconnect(). discord.py calls
disconnect(cleanup=False) *only* for reconnect-intent teardowns; genuine
disconnects use cleanup=True. So we suppress the gateway leave whenever
cleanup=False: the media socket is torn down and re-established, but the gateway
voice state stays on the channel, so a lone bot never empties it.

Coupled to discord.py internals (the disconnect(cleanup=...) contract and
_voice_disconnect). discord.py is pinned at 2.7.1 in requirements.txt; re-verify
this against any version bump.
"""

from __future__ import annotations

import logging

import discord
from discord.voice_client import VoiceClient
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

_log = logging.getLogger(__name__)

_PINNED_DISCORD_VERSION = "2.7.1"

if discord.__version__ != _PINNED_DISCORD_VERSION:
    _log.warning(
        "voice_resilience targets discord.py %s but %s is installed. The "
        "in-place voice reconnect relies on the "
        "VoiceConnectionState.disconnect(cleanup=False) contract and "
        "_voice_disconnect internals; re-verify against this version.",
        _PINNED_DISCORD_VERSION,
        discord.__version__,
    )


class ResilientVoiceConnectionState(VoiceConnectionState):
    """VoiceConnectionState that keeps the gateway voice state across reconnects."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._suppress_gateway_leave: bool = False

    async def _voice_disconnect(self) -> None:
        if self._suppress_gateway_leave:
            # Reconnect-in-place: drop the media connection but do NOT emit the
            # gateway OP4 channel=None. Our voice state stays on the channel, so
            # a lone bot never empties it and the call timer holds. The
            # subsequent _connect() re-handshakes the media connection.
            _log.info("Voice media dropped; reconnecting voice in place (no channel leave).")
            self.state = ConnectionFlowState.disconnected
            return
        await super()._voice_disconnect()

    async def disconnect(
        self, *, force: bool = True, cleanup: bool = True, wait: bool = False
    ) -> None:
        # discord.py uses cleanup=False only for reconnect-intent teardowns
        # (_poll_voice_ws and _inner_connect). Keep the gateway voice state in
        # those. cleanup=True is a genuine disconnect and must leave normally.
        if not cleanup:
            self._suppress_gateway_leave = True
            try:
                await super().disconnect(force=force, cleanup=cleanup, wait=wait)
            finally:
                self._suppress_gateway_leave = False
            return
        await super().disconnect(force=force, cleanup=cleanup, wait=wait)


class ResilientVoiceClient(VoiceClient):
    """VoiceClient that uses ResilientVoiceConnectionState."""

    def create_connection_state(self) -> VoiceConnectionState:
        return ResilientVoiceConnectionState(self)
