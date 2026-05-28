# Single-Bot Continuous Voice Presence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop transient voice-WebSocket drops (close codes `1001`/`1006`) from making a lone Why2K bot leave its channel and reset the call timer, using a single bot.

**Architecture:** Subclass discord.py's `VoiceClient` and `VoiceConnectionState`. discord.py emits the gateway "leave" (`change_voice_state(channel=None)`) only from `_voice_disconnect()`, which is only reached via `disconnect()`. discord.py calls `disconnect(cleanup=False)` *only* for reconnect-intent teardowns (in `_poll_voice_ws` and `_inner_connect`). So we suppress the gateway leave whenever `cleanup=False`: the media socket is torn down and re-established, but our gateway voice state stays on the channel — the bot never disappears from the participant list, so the count never hits 0 and the timer holds. Genuine disconnects (normal close, kick/channel-deleted, ratelimit) use `cleanup=True` and are untouched.

**Tech Stack:** Python 3.12, discord.py[voice]==2.7.1 (pinned), pytest + pytest-asyncio for unit tests, systemd on a DigitalOcean droplet for deployment.

---

## Why `cleanup=False` is the right seam (reference)

Verified against discord.py v2.7.1 source (`discord/voice_state.py`):

- `_voice_disconnect()` (the only caller of `change_voice_state(channel=None)`) is called only by `VoiceConnectionState.disconnect()`.
- `disconnect(cleanup=False)` is called in exactly two places, both reconnect-intent:
  - `_poll_voice_ws` line 743 — the generic reconnect after an unhandled close code (this is the `1001`/`1006` path), immediately followed by `_connect(resume=False)`.
  - `_inner_connect` line 489 — retry within a connect attempt, followed by `sleep` + `continue`.
- Genuine disconnects call `disconnect()` with the default `cleanup=True`: normal close `1000`, kick/channel-deleted `4014`/`4022` (when `_disconnected` is set), ratelimit `4021`, failed `_potential_reconnect`, and the external `VoiceClient.disconnect()`.
- After a suppressed teardown, `_connect` → `_inner_connect` → `_voice_connect()` re-sends `change_voice_state(channel=channel)` (re-assert, **not** leave), so the media re-handshakes via the normal flow while the gateway voice state never left the channel.

**Outcome table (unchanged from the spec):**

| Close code | Path in discord.py | Leaves channel? |
|---|---|---|
| `1000` normal | `disconnect()` cleanup=True | Yes (correct) |
| `1001`, `1006`, other transient | `disconnect(cleanup=False)` → reconnect | **No (the fix)** |
| `4014`/`4022` kicked (disconnected set) | `disconnect()` cleanup=True | Yes (correct) |
| `4014`/`4022` moved | `_potential_reconnect()` (no disconnect) | No (unchanged) |
| `4021` ratelimit | `disconnect()` cleanup=True | Yes (unchanged) |
| `4015` server crashed | `_connect(resume=True)` (no disconnect) | No (unchanged) |

---

## File Structure

- **Create** `voice_resilience.py` (repo root, next to `bot.py`) — the `ResilientVoiceConnectionState` + `ResilientVoiceClient` subclasses and the discord.py version guard. One responsibility: keep voice presence continuous across reconnects.
- **Create** `tests/test_voice_resilience.py` — unit tests for the leave-suppression decision logic.
- **Create** `tests/conftest.py` — conventional home for shared pytest fixtures/hooks. (The repo root is placed on `sys.path` via `pythonpath = .` in `pytest.ini`, not by this file.)
- **Create** `pytest.ini` (repo root) — pytest + asyncio config.
- **Create** `requirements-dev.txt` (repo root) — dev/test deps on top of the prod pin.
- **Modify** `bot.py` — import `ResilientVoiceClient` and pass `cls=ResilientVoiceClient` to `channel.connect(...)`.
- **Modify** `docs/superpowers/specs/2026-05-29-single-bot-voice-presence-design.md` — refine Component 1 to the `cleanup=False` mechanism.

> **Note:** The code blocks in the tasks below are the plan-time snapshot. The
> committed files are authoritative and include refinements made during code
> review — e.g. the exported `PINNED_DISCORD_VERSION` constant, a
> flag-reset-on-exception test, and `_disconnected.clear()` in the suppressed
> branch.

---

## Task 1: Dev/test environment and infrastructure

**Files:**
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create the dev requirements file**

Create `requirements-dev.txt`:

```
-r requirements.txt
pytest==8.3.4
pytest-asyncio==0.25.2
```

- [ ] **Step 2: Create the pytest config**

Create `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Create the conftest placeholder for shared fixtures**

Create `tests/conftest.py`. Imports work via `pytest.ini`'s `pythonpath = .`; this file is just the conventional home for shared fixtures/hooks:

```python
# Shared pytest fixtures and hooks for the test suite belong here.
# (The repo root is placed on sys.path via `pythonpath = .` in pytest.ini, so
# tests can `import voice_resilience` without this file doing anything special.)
```

- [ ] **Step 4: Create a virtualenv and install dev deps**

Run (Windows PowerShell, from repo root):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Expected: installs discord.py[voice]==2.7.1, python-dotenv, pytest, pytest-asyncio without error.
(Linux/droplet equivalent: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.)

- [ ] **Step 5: Verify pytest runs (collects zero tests)**

Run:

```powershell
.venv\Scripts\python -m pytest -q
```

Expected: `no tests ran` (exit code 5) — config is valid, no tests yet.

- [ ] **Step 6: Commit**

```powershell
git add requirements-dev.txt pytest.ini tests/conftest.py
git commit -m "Add pytest dev infrastructure for voice resilience tests"
```

---

## Task 2: `voice_resilience.py` — leave-suppressing voice client (TDD)

**Files:**
- Create: `tests/test_voice_resilience.py`
- Create: `voice_resilience.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_resilience.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord.voice_state import ConnectionFlowState

from voice_resilience import (
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
    try:
        yield st, vc
    finally:
        st._socket_reader.stop()


def test_targets_pinned_discord_version():
    assert discord.__version__ == "2.7.1"


async def test_voice_disconnect_suppressed_skips_gateway_leave(state):
    st, vc = state
    st._suppress_gateway_leave = True

    await st._voice_disconnect()

    vc.channel.guild.change_voice_state.assert_not_called()
    assert st.state is ConnectionFlowState.disconnected


async def test_voice_disconnect_normal_emits_gateway_leave(state):
    st, vc = state
    st._suppress_gateway_leave = False

    await st._voice_disconnect()

    vc.channel.guild.change_voice_state.assert_awaited_once_with(channel=None)
    assert st.state is ConnectionFlowState.disconnected


async def test_disconnect_cleanup_false_does_not_leave(state):
    st, vc = state

    await st.disconnect(cleanup=False)

    vc.channel.guild.change_voice_state.assert_not_called()
    assert st._suppress_gateway_leave is False
    assert st.state is ConnectionFlowState.disconnected


async def test_disconnect_cleanup_true_leaves(state):
    st, vc = state

    await st.disconnect(cleanup=True, wait=False)

    vc.channel.guild.change_voice_state.assert_awaited_once_with(channel=None)
    assert st._suppress_gateway_leave is False


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

Run:

```powershell
.venv\Scripts\python -m pytest tests/test_voice_resilience.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'voice_resilience'`.

- [ ] **Step 3: Write the implementation**

Create `voice_resilience.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
.venv\Scripts\python -m pytest tests/test_voice_resilience.py -q
```

Expected: PASS — 6 passed. (During code review a 7th test — `test_disconnect_cleanup_false_resets_flag_on_exception` — was added, so the file settles at 7 passing.)

- [ ] **Step 5: Commit**

```powershell
git add voice_resilience.py tests/test_voice_resilience.py
git commit -m "Add leave-suppressing voice client for in-place reconnects"
```

---

## Task 3: Wire `ResilientVoiceClient` into the bot

**Files:**
- Modify: `bot.py` (import near the top with the other imports; the `channel.connect(...)` call at line 174)

- [ ] **Step 1: Add the import**

In `bot.py`, after the `import discord` / `from dotenv import load_dotenv` block (around line 8-9), add:

```python
from voice_resilience import ResilientVoiceClient
```

- [ ] **Step 2: Pass the custom client class to connect()**

In `bot.py`, in `connect_to_voice()`, change the connect call (currently around line 174):

```python
                await channel.connect(self_deaf=True, self_mute=True)
```

to:

```python
                await channel.connect(
                    cls=ResilientVoiceClient, self_deaf=True, self_mute=True
                )
```

- [ ] **Step 3: Verify the module still parses and imports the class**

Run (does not start the bot — only checks the symbol is importable and bot.py compiles):

```powershell
.venv\Scripts\python -c "import ast, importlib; importlib.import_module('voice_resilience').ResilientVoiceClient; ast.parse(open('bot.py', encoding='utf-8').read()); print('ok')"
```

Expected: prints `ok`. (We compile `bot.py` rather than import it because importing executes module-level startup that calls `sys.exit` without env vars and runs `asyncio.run(main())`.)

- [ ] **Step 4: Run the full test suite**

Run:

```powershell
.venv\Scripts\python -m pytest -q
```

Expected: PASS — 7 passed.

- [ ] **Step 5: Commit**

```powershell
git add bot.py
git commit -m "Use ResilientVoiceClient so voice reconnects keep channel presence"
```

---

## Task 4: Refine the design spec to match the implemented mechanism

**Files:**
- Modify: `docs/superpowers/specs/2026-05-29-single-bot-voice-presence-design.md` (the "Component 1" subsection under "## Design")

- [ ] **Step 1: Replace the Component 1 body**

Find the `### Component 1 — In-place voice reconnect (the core fix)` section and replace its body (the bullet list under it, up to but not including `### Component 2`) with:

```markdown
Suppress the gateway "leave" for reconnect-intent teardowns so the media
connection is rebuilt without the bot ever leaving the channel.

- Provide a custom `VoiceClient` subclass (`ResilientVoiceClient`) whose
  `create_connection_state()` returns a custom
  `ResilientVoiceConnectionState`, and connect with
  `channel.connect(cls=ResilientVoiceClient, self_deaf=True, self_mute=True)`.
- In `ResilientVoiceConnectionState`, override `disconnect()`: discord.py calls
  `disconnect(cleanup=False)` **only** for reconnect-intent teardowns
  (`_poll_voice_ws` after an unhandled close code such as `1001`/`1006`, and
  `_inner_connect` retries). For those, set a flag so the gateway leave is
  suppressed; `cleanup=True` (genuine disconnects) is delegated unchanged.
- Override `_voice_disconnect()`: when the flag is set, set the connection state
  to `disconnected` but skip `change_voice_state(channel=None)`. The following
  `_connect()` re-sends `change_voice_state(channel=channel)`, re-handshaking
  the media while the gateway voice state never left the channel.

**Scope:** This naturally covers `1001`, `1006`, and any other transient code
that falls through to discord.py's generic reconnect, while leaving genuine
disconnects untouched: normal close (`1000`), kick / channel-deleted
(`4014`/`4022` with the disconnect confirmed), and ratelimit (`4021`) all use
`cleanup=True` and still leave the channel as they should.
```

- [ ] **Step 2: Commit**

```powershell
git add docs/superpowers/specs/2026-05-29-single-bot-voice-presence-design.md
git commit -m "Refine voice-presence spec to the cleanup=False suppression mechanism"
```

---

## Task 5: Live validation (manual, on the droplet) — REQUIRED before declaring success

Unit tests prove the leave-suppression decision logic. They cannot prove Discord keeps the bot in the channel across a real media reconnect. This must be validated live.

- [ ] **Step 1: Deploy the branch to the droplet**

From the droplet (as root), deploy this branch (replace `<branch>`):

```bash
BRANCH=<branch> bash <(curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh)
systemctl restart why2k
```

- [ ] **Step 2: Confirm the bot joined and is alone**

```bash
journalctl -u why2k -n 20 --no-pager
```

Expected: a recent `Joined voice channel: ...` line. From a separate Discord account (an observer who does NOT join the channel), confirm the bot is the only member and the call timer is running.

- [ ] **Step 3: Watch for an in-place reconnect (opportunistic — primary method)**

Leave it running and tail the log, filtering for the relevant events:

```bash
journalctl -u why2k -f | grep -iE "reconnecting voice in place|Disconnected from voice|channel .* -> None|Voice connection complete"
```

When a natural `1001`/`1006` occurs (roughly twice per 16h based on history), confirm:
- a `Voice media dropped; reconnecting voice in place (no channel leave).` line appears, and
- **no** `Voice state changed: channel ... -> None` line appears for that event, and
- the observer account sees the bot **never disappear** from the member list, and
- the call timer does **not** reset.

- [ ] **Step 4 (optional, advanced): Force a reconnect instead of waiting**

To induce a voice WS close on demand, briefly block outbound TCP/443 to the current voice endpoint, then restore. First find the endpoint:

```bash
EP=$(journalctl -u why2k | grep -oE 'Endpoint found: [^:]+' | tail -1 | awk '{print $3}')
IP=$(getent hosts "$EP" | awk '{print $1}' | head -1)
echo "Blocking $EP ($IP) for 10s"
iptables -A OUTPUT -p tcp -d "$IP" --dport 443 -j DROP
sleep 10
iptables -D OUTPUT -p tcp -d "$IP" --dport 443 -j DROP
```

Then apply the same checks as Step 3. (The endpoint can change between sessions; re-read it right before blocking.)

- [ ] **Step 5: Confirm the circuit breaker did not trip**

```bash
journalctl -u why2k --since "1 hour ago" | grep -iE "Voice session lasted|circuit|503" || echo "clean"
```

Expected: `clean`. The in-place reconnect should keep sessions long-lived, so the short-session breaker in `bot.py` should not fire.

---

## Self-Review notes

- **Spec coverage:** Goals (eliminate `1001`/`1006` leave; keep gateway state continuous; single bot) → Tasks 2-3. Non-goals respected (no second presence, no media/audio work, deployment topology unchanged). Validation section → Task 5. Risks (internal coupling, version pin) → version guard in Task 2 Step 3 + the `test_targets_pinned_discord_version` test.
- **Placeholder scan:** none — all code and commands are concrete.
- **Type/name consistency:** `ResilientVoiceConnectionState`, `ResilientVoiceClient`, `_suppress_gateway_leave`, `create_connection_state`, and `_voice_disconnect` are used identically across `voice_resilience.py`, the tests, and `bot.py`.
- **Residual risk (documented, accepted):** a hard non-resumable gateway disconnect can still cause a brief leave; if a transient reconnect ultimately fails after retries, the bot's gateway state remains in-channel without media until Discord drops it (then `on_voice_state_update` in `bot.py` re-spawns the connect). Only a second presence is fully bulletproof, which the operator declined.
```
