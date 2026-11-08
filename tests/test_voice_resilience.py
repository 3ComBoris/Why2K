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


async def test_watch_for_resumed_chains_original_hook(state):
    # When state.hook is set (e.g. for diagnostics), our resume-watcher must
    # delegate to it so the chain isn't silently broken.
    st, vc = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    chained = []

    async def original_hook(ws, msg):
        chained.append(msg)

    st.hook = original_hook

    captured = {}
    new_ws = MagicMock()
    new_ws.close = AsyncMock()

    async def fake_from_connection_state(state, *, resume, hook, seq_ack):
        captured["hook"] = hook
        return new_ws

    async def fake_poll_event():
        await captured["hook"](new_ws, {"op": DiscordVoiceWebSocket.RESUMED, "d": {}})

    new_ws.poll_event = AsyncMock(side_effect=fake_poll_event)

    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=fake_from_connection_state
    ):
        result = await st._try_in_place_resume()

    assert result is True
    assert len(chained) == 1
    assert chained[0]["op"] == DiscordVoiceWebSocket.RESUMED


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


async def test_try_resume_bounds_whole_attempt_by_one_timeout(state):
    # The WSS open and the wait for OP 9 RESUMED share a single deadline, so a
    # slow open followed by a hang can't stretch the attempt to 2x the budget.
    st, _ = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    new_ws = MagicMock()
    new_ws.close = AsyncMock()

    async def slow_from_connection_state(state, *, resume, hook, seq_ack):
        await asyncio.sleep(st.RESUME_TIMEOUT * 0.8)
        return new_ws

    async def hang_forever():
        await asyncio.sleep(60)

    new_ws.poll_event = AsyncMock(side_effect=hang_forever)

    loop = asyncio.get_running_loop()
    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=slow_from_connection_state
    ):
        started = loop.time()
        result = await st._try_in_place_resume()
        elapsed = loop.time() - started

    assert result is False
    # One budget: ~1.0x RESUME_TIMEOUT. Per-phase budgets would take ~1.8x.
    assert elapsed < st.RESUME_TIMEOUT * 1.4
    new_ws.close.assert_awaited()


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


async def test_try_resume_closes_new_ws_and_propagates_on_cancel(state):
    # CancelledError is a BaseException (3.8+), so the helper's `except
    # Exception` never sees it. If the poller task is cancelled while waiting
    # for RESUMED, the half-open resume WS must still be closed — its
    # keep-alive thread starts on HELLO and only close() stops it — and the
    # cancellation must propagate rather than be turned into a False return.
    st, vc = state
    st.endpoint = "discord.media:8443"
    st.session_id = "sess"
    st.token = "tok"
    st.server_id = 12345
    st.ws = MISSING

    new_ws = MagicMock()
    new_ws.close = AsyncMock()
    polling = asyncio.Event()

    async def fake_from_connection_state(state, *, resume, hook, seq_ack):
        return new_ws

    async def hang_until_cancelled():
        polling.set()
        await asyncio.sleep(60)

    new_ws.poll_event = AsyncMock(side_effect=hang_until_cancelled)

    with patch.object(
        DiscordVoiceWebSocket, "from_connection_state", new=fake_from_connection_state
    ):
        task = asyncio.create_task(st._try_in_place_resume())
        await polling.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    new_ws.close.assert_awaited()
    vc.channel.guild.change_voice_state.assert_not_called()


def _closing_ws(code):
    ws = MagicMock()
    ws.close = AsyncMock()
    ws.poll_event = AsyncMock(
        side_effect=ConnectionClosed(MagicMock(), shard_id=None, code=code)
    )
    return ws


def _terminal_ws():
    # _poll_voice_ws returns when poll_event raises CancelledError, so
    # installing this WS ends the loop on its next iteration.
    ws = MagicMock()
    ws.close = AsyncMock()
    ws.poll_event = AsyncMock(side_effect=asyncio.CancelledError)
    return ws


def _wire_poller(st, *, close_code, resume_result):
    """Drive one voice WS close through _poll_voice_ws with every exit mocked.

    Both the resume path and the leave-rejoin path install a terminal WS, so
    the loop ends after the first close whichever branch runs — a routing
    regression fails the assertions instead of hanging the test.
    """
    st.ws = _closing_ws(close_code)

    async def fake_resume():
        if resume_result:
            st.ws = _terminal_ws()
        return resume_result

    async def fake_connect(**kwargs):
        st.ws = _terminal_ws()

    st._try_in_place_resume = AsyncMock(side_effect=fake_resume)
    st.disconnect = AsyncMock()
    st._connect = AsyncMock(side_effect=fake_connect)


@pytest.fixture
def no_backoff():
    # Upstream sleeps a random backoff before leave-rejoin; skip it.
    with patch("voice_resilience.ExponentialBackoff") as backoff_cls:
        backoff_cls.return_value.delay.return_value = 0
        yield


@pytest.mark.parametrize("code", [1001, 1006])
async def test_poll_resumes_in_place_on_transient_close(state, no_backoff, code):
    st, _ = state
    _wire_poller(st, close_code=code, resume_result=True)

    await st._poll_voice_ws(reconnect=True)

    st._try_in_place_resume.assert_awaited_once()
    # A confirmed resume must skip upstream's leave-rejoin entirely.
    st.disconnect.assert_not_awaited()
    st._connect.assert_not_awaited()


async def test_poll_falls_back_to_leave_rejoin_when_resume_fails(state, no_backoff):
    st, _ = state
    _wire_poller(st, close_code=1006, resume_result=False)

    await st._poll_voice_ws(reconnect=True)

    st._try_in_place_resume.assert_awaited_once()
    st.disconnect.assert_awaited_once_with(cleanup=False)
    st._connect.assert_awaited_once()
    assert st._connect.await_args.kwargs["resume"] is False


async def test_poll_skips_resume_for_other_close_codes(state, no_backoff):
    # 4006 isn't one of upstream's special-cased codes, so it reaches the same
    # generic fall-through as 1001/1006 — but must not attempt a resume.
    st, _ = state
    _wire_poller(st, close_code=4006, resume_result=True)

    await st._poll_voice_ws(reconnect=True)

    st._try_in_place_resume.assert_not_awaited()
    st.disconnect.assert_awaited_once_with(cleanup=False)
    st._connect.assert_awaited_once()


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
