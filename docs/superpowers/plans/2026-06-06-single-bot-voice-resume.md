# Single-Bot Voice WS Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the voice WebSocket closes with `1001` or `1006`, attempt an in-place `OP 7 RESUME` to the cached endpoint and only fall back to discord.py's leave-rejoin if the resume isn't positively confirmed.

**Architecture:** A `ResilientVoiceConnectionState(VoiceConnectionState)` subclass plus a matching `ResilientVoiceClient(VoiceClient)`. The state subclass adds a `_try_in_place_resume()` helper and overrides `_poll_voice_ws` with a near-verbatim copy of upstream that inserts a transient-code branch before the generic fall-through. The helper opens a fresh WSS via `DiscordVoiceWebSocket.from_connection_state(..., resume=True, hook=...)`, watches for `OP 9 RESUMED` via the hook, and returns `True` only when resume is positively confirmed. On any failure, the fall-through runs upstream's existing leave-rejoin — exactly the pre-PR-#9 baseline.

**Tech Stack:** Python 3.12, discord.py[voice]==2.7.1 (pinned — guarded by a tripwire test), pytest + pytest-asyncio for unit tests, systemd on a DigitalOcean droplet for deployment.

---

## Why `OP 7 RESUME` is the right mechanism (reference)

Verified against installed discord.py v2.7.1 source:

- `DiscordVoiceWebSocket.RESUME = 7`, `RESUMED = 9` (`gateway.py:832,834`).
- `from_connection_state(state, *, resume, hook, seq_ack)` opens `wss://{state.endpoint}/?v=8`, then **sends `OP 7 RESUME` synchronously inside the call** when `resume=True` (`gateway.py:929-930`). It returns the WS without waiting for any reply.
- `received_message` dispatches by opcode (`gateway.py:983-1032`). For `OP 9 RESUMED` it just logs (line 994-995) — no state mutation we can probe externally. **Every received message also fires `self._hook(self, msg)` at the end (line 1032).** That's our positive-confirmation seam.
- `_handshake_websocket` (`voice_state.py:654-660`) waits for `self.ip` and `self.ws.secret_key`. On a successful resume Discord sends neither, so calling `_handshake_websocket` post-resume would hang. **We do not call it.** The bot is self-deaf/self-mute and never needs `secret_key`.
- Cached session data needed for resume (`endpoint`, `session_id`, `token`, `server_id`) is preserved on the state object across our reconnect — `disconnect()` clears `ip`/`port` but not these (`voice_state.py:534-535`).

---

## File Structure

- **Create** `requirements-dev.txt` (repo root) — pytest + pytest-asyncio pinned on top of the prod requirements. (Not present on `main`.)
- **Create** `pytest.ini` (repo root) — pytest + asyncio config.
- **Create** `tests/conftest.py` — placeholder for shared fixtures.
- **Create** `voice_resilience.py` (repo root, next to `bot.py`) — `PINNED_DISCORD_VERSION`, `ResilientVoiceConnectionState`, `ResilientVoiceClient`, version-mismatch warning. One responsibility: try resume, otherwise let upstream run.
- **Create** `tests/test_voice_resilience.py` — unit tests for the helper, the close-code routing, and the factory.
- **Modify** `bot.py` — add `from voice_resilience import ResilientVoiceClient`, pass `cls=ResilientVoiceClient` to `channel.connect(...)`, and raise the `voice_resilience` logger to INFO so the resume-attempt logs reach journald.

---

## Task 1: Dev/test environment and infrastructure

**Files:**
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `requirements-dev.txt`**

Create with exact content:

```
-r requirements.txt
pytest==8.3.4
pytest-asyncio==0.25.2
```

- [ ] **Step 2: Create `pytest.ini`**

Create with exact content:

```ini
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Create `tests/conftest.py`**

Create with exact content:

```python
# Shared pytest fixtures and hooks for the test suite belong here.
# (The repo root is placed on sys.path via `pythonpath = .` in pytest.ini, so
# tests can `import voice_resilience` without this file doing anything special.)
```

- [ ] **Step 4: Create a virtualenv and install dev deps**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Expected: installs discord.py[voice]==2.7.1, python-dotenv, pytest, pytest-asyncio without error. (Linux/droplet equivalent: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.)

- [ ] **Step 5: Verify pytest collects zero tests**

```powershell
.venv\Scripts\python -m pytest -q
```

Expected: `no tests ran` (exit code 5). That exit code is SUCCESS — it proves the config is valid.

- [ ] **Step 6: Confirm `.venv/` is gitignored, then commit**

```powershell
git check-ignore .venv
```

Expected: prints `.venv`. If it prints nothing, add `.venv/` to `.gitignore` and include it in the commit.

```powershell
git add requirements-dev.txt pytest.ini tests/conftest.py
git commit -m "Add pytest dev infrastructure for voice resilience tests"
```

---

## Task 2: `voice_resilience.py` — resume helper + `_poll_voice_ws` override (TDD)

**Files:**
- Create: `tests/test_voice_resilience.py`
- Create: `voice_resilience.py`

This task implements the entire module under TDD. The implementation block in Step 3 is long — that's intentional: the override of `_poll_voice_ws` is a near-verbatim copy of discord.py 2.7.1's method with one new branch added, and copying it is the design choice.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_resilience.py` with EXACT content:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from discord.errors import ConnectionClosed
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from discord.utils import MISSING

from voice_resilience import (
    PINNED_DISCORD_VERSION,
    ResilientVoiceClient,
    ResilientVoiceConnectionState,
)


def _make_state():
    """Build a ResilientVoiceConnectionState with a mocked voice client.

    VoiceConnectionState.__init__ only stores the voice_client and starts a
    daemon SocketReader thread; it never touches the network, so a MagicMock
    voice client is sufficient for testing the override logic.
    """
    vc = MagicMock()
    vc.channel.guild.change_voice_state = AsyncMock()
    return ResilientVoiceConnectionState(vc), vc


@pytest.fixture
def state():
    st, vc = _make_state()
    # Shrink resume timeout so failure-path tests don't burn 10s each.
    st.RESUME_TIMEOUT = 0.5
    try:
        yield st, vc
    finally:
        st._socket_reader.stop()


def test_targets_pinned_discord_version():
    # Tripwire: the installed discord.py must match the version the module's
    # internal-coupling was verified against (single source of truth).
    assert discord.__version__ == PINNED_DISCORD_VERSION


async def test_try_resume_returns_false_when_session_data_missing(state):
    st, vc = state
    # Defaults from VoiceConnectionState.__init__ are None/MISSING.
    assert st.endpoint is None
    result = await st._try_in_place_resume()
    assert result is False
    # No gateway message was emitted, no state change.
    vc.channel.guild.change_voice_state.assert_not_called()


async def test_try_resume_returns_true_on_resumed(state):
    st, vc = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    captured = {}
    new_ws = MagicMock()
    new_ws.close = AsyncMock()

    async def fake_from_connection_state(state, *, resume, hook, seq_ack):
        assert resume is True
        captured["hook"] = hook
        return new_ws

    async def fake_poll_event():
        # First poll: simulate Discord sending OP 9 RESUMED, which the WS
        # would normally hand to the hook.
        await captured["hook"](new_ws, {"op": DiscordVoiceWebSocket.RESUMED, "d": {}})

    new_ws.poll_event = AsyncMock(side_effect=fake_poll_event)

    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=fake_from_connection_state
    ):
        result = await st._try_in_place_resume()

    assert result is True
    assert st.ws is new_ws
    assert st.state is ConnectionFlowState.connected
    vc.channel.guild.change_voice_state.assert_not_called()
    new_ws.close.assert_not_called()


async def test_try_resume_returns_false_on_connection_closed(state):
    st, vc = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    new_ws = MagicMock()
    new_ws.close = AsyncMock()

    async def fake_from_connection_state(state, *, resume, hook, seq_ack):
        return new_ws

    async def fake_poll_event():
        raise ConnectionClosed(MagicMock(), shard_id=None, code=4006)

    new_ws.poll_event = AsyncMock(side_effect=fake_poll_event)

    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=fake_from_connection_state
    ):
        result = await st._try_in_place_resume()

    assert result is False
    # The new WS should be closed since resume failed.
    new_ws.close.assert_awaited()
    # No gateway leave was emitted.
    vc.channel.guild.change_voice_state.assert_not_called()


async def test_try_resume_returns_false_on_timeout(state):
    st, vc = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    new_ws = MagicMock()
    new_ws.close = AsyncMock()

    async def fake_from_connection_state(state, *, resume, hook, seq_ack):
        return new_ws

    async def hang_forever():
        await asyncio.sleep(60)  # longer than st.RESUME_TIMEOUT == 0.5

    new_ws.poll_event = AsyncMock(side_effect=hang_forever)

    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=fake_from_connection_state
    ):
        result = await st._try_in_place_resume()

    assert result is False
    new_ws.close.assert_awaited()
    vc.channel.guild.change_voice_state.assert_not_called()


async def test_try_resume_returns_false_on_open_failure(state):
    st, vc = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    async def fake_from_connection_state(state, *, resume, hook, seq_ack):
        raise OSError("connection refused")

    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=fake_from_connection_state
    ):
        result = await st._try_in_place_resume()

    assert result is False
    vc.channel.guild.change_voice_state.assert_not_called()


def test_create_connection_state_returns_resilient():
    # VoiceClient.__init__ requires PyNaCl/davey and a real client, so bypass
    # it with __new__ and call the factory directly — it only needs `self`
    # passed through as the connection state's voice_client.
    vc = ResilientVoiceClient.__new__(ResilientVoiceClient)
    st = vc.create_connection_state()
    try:
        assert isinstance(st, ResilientVoiceConnectionState)
    finally:
        st._socket_reader.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
.venv\Scripts\python -m pytest tests/test_voice_resilience.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'voice_resilience'`.

- [ ] **Step 3: Write the implementation**

Create `voice_resilience.py` at the repo root with EXACT content:

```python
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
existing leave-rejoin. That fallback is the same ~2s blip the bot has had all
along: no worse than baseline in any failure mode.

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

    # Total seconds to allow for the OP 7 RESUME → OP 9 RESUMED round trip
    # (covers WSS open + RESUME send + RESUMED receive). On timeout the
    # helper falls back to upstream leave-rejoin.
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
        if not (self.endpoint and self.session_id and self.token):
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
        new_ws: Optional[DiscordVoiceWebSocket] = None
        seq_ack = -1
        if previous_ws is not MISSING:
            seq_ack = previous_ws.seq_ack

        try:
            # Open a fresh WSS to the cached endpoint and send OP 7 RESUME.
            # We call from_connection_state directly (bypassing
            # _connect_websocket) so we can install the resume-watching hook
            # for THIS attempt without mutating self.hook.
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
            # WS closes / we hit the deadline (failure).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.RESUME_TIMEOUT
            while not resumed_event.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError("OP 9 RESUMED not received in time")
                await asyncio.wait_for(new_ws.poll_event(), timeout=remaining)
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
                    if exc.code in (1001, 1006):
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
                    _log.warning("Could not connect to voice... Retrying...")
                    continue


class ResilientVoiceClient(VoiceClient):
    """VoiceClient that uses ResilientVoiceConnectionState."""

    def create_connection_state(self) -> VoiceConnectionState:
        return ResilientVoiceConnectionState(self)
```

- [ ] **Step 4: Run tests to verify they pass**

```powershell
.venv\Scripts\python -m pytest tests/test_voice_resilience.py -q
```

Expected: PASS — 7 passed (a `DeprecationWarning` about pytest-asyncio's `asyncio.get_event_loop` policy is harmless and unrelated).

If any test fails, **do not adjust the assertions to make them pass** — the assertions encode the spec's invariants. If a test is genuinely wrong, stop and report `BLOCKED` with details.

- [ ] **Step 5: Commit**

```powershell
git add voice_resilience.py tests/test_voice_resilience.py
git commit -m "Add OP 7 RESUME helper and _poll_voice_ws override"
```

---

## Task 3: Wire `ResilientVoiceClient` into the bot

**Files:**
- Modify: `bot.py` — import line, logger level line, the `channel.connect(...)` call inside `connect_to_voice()`.

- [ ] **Step 1: Add the import**

In `bot.py`, immediately after the `from dotenv import load_dotenv` line near the top, add:

```python
from voice_resilience import ResilientVoiceClient
```

- [ ] **Step 2: Surface `voice_resilience` INFO logs to journald**

`bot.py` configures the root logger at WARNING. Without raising this logger explicitly, the `Attempting in-place voice WS resume` and `Voice WS resumed in place` lines would be filtered out, breaking the live-validation grep. After the existing `logging.getLogger("discord.gateway").setLevel(logging.INFO)` line, append:

```python
# voice_resilience logs its resume attempts at INFO; the root logger is
# WARNING, so raise this one explicitly or those lines would be filtered out
# of journald — which the live-validation grep relies on seeing.
logging.getLogger("voice_resilience").setLevel(logging.INFO)
```

- [ ] **Step 3: Pass the custom client class to `connect()`**

In `bot.py`, in `connect_to_voice()`, locate the line:

```python
                await channel.connect(self_deaf=True, self_mute=True)
```

Replace it with:

```python
                await channel.connect(
                    cls=ResilientVoiceClient, self_deaf=True, self_mute=True
                )
```

Preserve the existing indentation (this call is inside a `try` block in `connect_to_voice()`).

- [ ] **Step 4: Verify bot.py parses and the symbol is importable**

This compiles `bot.py` without running it (`bot.py` calls `sys.exit` on missing env vars and ends with `asyncio.run(main())` at module level, so a real import would try to start the bot):

```powershell
.venv\Scripts\python -c "import ast, importlib; importlib.import_module('voice_resilience').ResilientVoiceClient; ast.parse(open('bot.py', encoding='utf-8').read()); print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 5: Run the full test suite**

```powershell
.venv\Scripts\python -m pytest -q
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```powershell
git add bot.py
git commit -m "Wire ResilientVoiceClient and raise voice_resilience logger to INFO"
```

---

## Task 4: Live validation (manual, on the droplet) — REQUIRED before declaring success

Unit tests prove the resume mechanism wires up correctly; they cannot prove Discord accepts `OP 7 RESUME` for our failure modes. This must be observed live.

- [ ] **Step 1: Deploy the branch to the droplet**

From the droplet (as root or via `sudo env`), deploy the feature branch — note the installer's existing bug means we have to use `FETCH_HEAD` after the fetch:

```bash
sudo runuser -u why2k -- git -C /opt/why2k fetch --depth=1 origin feat/voice-in-place-resume
sudo runuser -u why2k -- git -C /opt/why2k reset --hard FETCH_HEAD
sudo runuser -u why2k -- git -C /opt/why2k log --oneline -1
sudo systemctl restart why2k
```

The middle command should print the HEAD commit SHA — confirm it matches the branch tip you pushed.

- [ ] **Step 2: Confirm startup and channel join**

```bash
sudo systemctl status why2k
sudo journalctl -u why2k -n 30 --no-pager
```

Expected: `Logged in as ...` and `Joined voice channel: ...` lines, no exceptions.

- [ ] **Step 3: Force a voice WS drop with `ss -K`**

The previous test method is now **valid** for this design because the fallback is graceful. In one SSH session, tail the journal:

```bash
sudo journalctl -u why2k -f | grep -iE "Attempting in-place|resumed in place|resume attempt failed|Disconnected from voice|-> None|Voice connection complete"
```

In a second SSH session, identify the live voice peer and kill the socket:

```bash
sudo ss -tnp | grep -i python
# Look for the peer ending in :8443 — that's the voice WS. Then:
sudo ss -K dst <that_ipv4> dport = 8443
```

- [ ] **Step 4: Read the result**

| Journal pattern | Verdict |
|---|---|
| `Attempting in-place voice WS resume for close code 1006.` followed by `Voice WS resumed in place (no channel leave); endpoint=...` and **no** `Voice state changed: channel ... -> None` | ✅ Resume works for hard kills. Strong signal it'll work for natural drops. |
| `Attempting in-place ...` followed by `Voice WS resume attempt failed (...)` then upstream's `Voice handshake is being terminated` + `channel ... -> None` + `Joined voice channel` | Fallback worked. Bot leaves+rejoins as baseline. Resume doesn't help for `ss -K` specifically — wait for a natural drop to learn if it helps there. |
| Anything else (hang, exception, etc.) | `BLOCKED` — gather logs and re-evaluate. |

- [ ] **Step 5: Wait for a natural `1001` or `1006`** (only required if Step 4 shows fallback)

```bash
sudo journalctl -u why2k --since "<deploy timestamp>" --no-pager | grep -iE "Not handling close code|Attempting in-place|resumed in place|resume attempt failed|-> None"
```

Same verdict table as Step 4. Natural events are ~2 per 16 h; expect to wait up to a day.

- [ ] **Step 6: Decide**

- If Step 4 OR Step 5 shows ✅ on resume success: merge the PR.
- If both show only fallback: the resume mechanism doesn't help in practice for this deployment. Close the PR unmerged like PR #9 — the bot returns to baseline ~2 s blip, known and acceptable.

---

## Self-Review notes

- **Spec coverage:** All four spec components map to tasks:
  - Component 1 (`_try_in_place_resume`) → Task 2 Step 3 (the helper definition) + Task 2 Step 1 (the success/failure/timeout/cleanup tests).
  - Component 2 (`_poll_voice_ws` override) → Task 2 Step 3 (the override) + Task 2 Step 1 (`test_targets_pinned_discord_version` guards the coupling; the new branch is exercised end-to-end via Task 4 live test).
  - Component 3 (`ResilientVoiceClient` + `bot.py` wiring) → Task 2 Step 3 (`ResilientVoiceClient` class) + Task 3 (wire-up) + `test_create_connection_state_returns_resilient`.
  - The "what is explicitly removed compared to PR #9" line is satisfied by never recreating the `_suppress_gateway_leave` flag, the `disconnect` override, or the `_voice_disconnect` override.
- **Placeholder scan:** none — every code block is complete, every command has an expected output.
- **Type/name consistency:** `ResilientVoiceConnectionState`, `ResilientVoiceClient`, `_try_in_place_resume`, `RESUME_TIMEOUT`, `PINNED_DISCORD_VERSION`, `create_connection_state` used identically across module, tests, and `bot.py`.
- **Honest residual risk (carried from the spec, documented for the implementer):** resume may not be accepted by Discord for `ss -K` or natural 1006. If both `ss -K` and natural drops show the fallback path winning, this fix doesn't help in practice and the PR should be closed without merge. The implementation is structured so that outcome leaves the bot at exactly baseline — no regression in any case.
