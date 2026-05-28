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
