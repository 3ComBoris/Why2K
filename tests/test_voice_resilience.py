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
