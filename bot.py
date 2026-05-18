import asyncio
import discord
import os
import sys
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    sys.exit("Error: DISCORD_TOKEN is not set. Please add it to your .env file.")

_raw_channel_id = os.getenv("VOICE_CHANNEL_ID")
if not _raw_channel_id:
    sys.exit("Error: VOICE_CHANNEL_ID is not set. Please add it to your .env file.")
try:
    CHANNEL_ID = int(_raw_channel_id)
except ValueError:
    sys.exit(f"Error: VOICE_CHANNEL_ID must be a numeric ID, got: {_raw_channel_id!r}")

_raw_port = os.getenv("PORT", "8080")
try:
    PORT = int(_raw_port)
except ValueError:
    sys.exit(f"Error: PORT must be a numeric port, got: {_raw_port!r}")
if not 1 <= PORT <= 65535:
    sys.exit(f"Error: PORT must be in range 1-65535, got: {PORT}")

VOICE_CONNECT_INITIAL_RETRY_SECONDS = 30
VOICE_CONNECT_MAX_RETRY_SECONDS = 300
HEALTH_CHECK_READ_TIMEOUT_SECONDS = 5


class Why2KClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_connect_task: Optional[asyncio.Task] = None
        self.fatal_startup_error: bool = False
        self.discord_ready: bool = False


intents = discord.Intents.default()
client = Why2KClient(intents=intents)


# These phrases are matched against str(exc) for discord.ClientException to
# decide whether a voice-connect failure is transient. They come from
# discord.py 2.x internal exception messages and are NOT part of its public
# API. discord.py is pinned in requirements.txt; revisit this list whenever
# that pin is bumped.
_RETRYABLE_CLIENT_EXCEPTION_PHRASES = (
    "already trying to connect",
    "connection closed",
    "not connected to voice",
    "voice websocket is not connected",
)


def is_retryable_http_exception(exc: discord.HTTPException) -> bool:
    status = getattr(exc, "status", None)
    return status == 429 or (status is not None and status >= 500)


def is_retryable_client_exception(exc: discord.ClientException) -> bool:
    message = str(exc).lower()
    return any(phrase in message for phrase in _RETRYABLE_CLIENT_EXCEPTION_PHRASES)


async def handle_health_check(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        try:
            await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=HEALTH_CHECK_READ_TIMEOUT_SECONDS,
            )
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            TimeoutError,
        ):
            # Best-effort drain; respond with current status regardless.
            # asyncio.TimeoutError and builtin TimeoutError are distinct on
            # Python <3.11 (unified to the same alias in 3.11+); catch both
            # so the supported Python 3.8+ range is honored.
            pass

        if client.fatal_startup_error:
            body = b"startup failure\n"
            status_line = b"HTTP/1.1 503 Service Unavailable\r\n"
        elif not client.discord_ready:
            body = b"not ready\n"
            status_line = b"HTTP/1.1 503 Service Unavailable\r\n"
        else:
            body = b"ok\n"
            status_line = b"HTTP/1.1 200 OK\r\n"
        writer.write(
            status_line
            + b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def fail_startup(message: str):
    client.fatal_startup_error = True
    print(message)
    await client.close()


async def connect_to_voice():
    attempt = 1
    retry_delay = VOICE_CONNECT_INITIAL_RETRY_SECONDS

    try:
        while not client.is_closed():
            if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
                return

            try:
                channel = await client.fetch_channel(CHANNEL_ID)
                if not isinstance(channel, discord.VoiceChannel):
                    await fail_startup(f"Error: Channel {CHANNEL_ID} is not a voice channel.")
                    return
                await channel.connect(self_deaf=True, self_mute=True)
                print(f"Joined voice channel: {channel.name}")
                return
            except discord.NotFound:
                await fail_startup(f"Error: Voice channel with ID {CHANNEL_ID} not found.")
                return
            except discord.Forbidden:
                await fail_startup(f"Error: Missing permissions to access channel {CHANNEL_ID}.")
                return
            except (asyncio.TimeoutError, TimeoutError):
                # asyncio.wait_for inside discord.py raises asyncio.TimeoutError,
                # which is only an alias for builtin TimeoutError on 3.11+; catch
                # both to keep Python 3.8+ behavior consistent.
                print(
                    f"Warning: Timed out connecting to channel {CHANNEL_ID}. "
                    f"Attempt {attempt}. Retrying in {retry_delay} seconds."
                )
            except discord.ClientException as exc:
                if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
                    return
                if not is_retryable_client_exception(exc):
                    await fail_startup(f"Error: Could not connect to voice channel: {exc}")
                    return
                print(
                    f"Warning: Could not connect to voice channel: {exc}. "
                    f"Attempt {attempt}. Retrying in {retry_delay} seconds."
                )
            except discord.HTTPException as exc:
                if not is_retryable_http_exception(exc):
                    await fail_startup(
                        f"Error: Failed to fetch or connect to voice channel {CHANNEL_ID}: {exc}"
                    )
                    return
                print(
                    f"Warning: Failed to fetch or connect to voice channel {CHANNEL_ID}: {exc}. "
                    f"Attempt {attempt}. Retrying in {retry_delay} seconds."
                )
            except discord.opus.OpusNotLoaded as exc:
                await fail_startup(f"Error: Opus library not available: {exc}")
                return

            await asyncio.sleep(retry_delay)
            attempt += 1
            retry_delay = min(retry_delay * 2, VOICE_CONNECT_MAX_RETRY_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Surface unexpected failures (programming errors, third-party exceptions
        # without a discord.py base class) through fail_startup so they don't get
        # buried in asyncio's "Task exception was never retrieved" GC warning.
        await fail_startup(f"Error: Voice connection task crashed: {exc!r}")


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    # discord_ready stays sticky-true after first on_ready: this is a readiness
    # (not liveness) probe, and discord.py auto-reconnects on transient
    # disconnects. Toggling on every disconnect would make orchestrators flap
    # routing for failures the library is already recovering from. Fatal
    # conditions still flip fatal_startup_error and return 503.
    client.discord_ready = True

    if client.voice_connect_task is not None and client.voice_connect_task.done():
        try:
            client.voice_connect_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await fail_startup(f"Error: Voice connection task failed unexpectedly: {exc}")
            return

    # on_ready fires on every reconnect; if we're already in the target channel
    # there's nothing to do, so skip spawning a task that would just no-op.
    if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
        return

    if client.voice_connect_task is None or client.voice_connect_task.done():
        client.voice_connect_task = asyncio.create_task(connect_to_voice())


@client.event
async def on_voice_state_update(member, before, after):
    # discord.py's VoiceClient runs its own internal reconnect loop on voice
    # WS drops; once it gives up, the VoiceClient is removed from
    # client.voice_clients and nothing re-triggers our connect logic
    # (on_ready only fires on full gateway reconnect, not voice-only drops).
    # Watch our own voice state and re-spawn connect_to_voice whenever we
    # leave the target channel.
    if client.user is None or member.id != client.user.id:
        return

    before_channel_id = before.channel.id if before.channel else None
    after_channel_id = after.channel.id if after.channel else None

    if before_channel_id != after_channel_id:
        print(
            f"Voice state changed: channel {before_channel_id} -> "
            f"{after_channel_id} (target {CHANNEL_ID})"
        )

    if after_channel_id == CHANNEL_ID:
        return

    if before_channel_id == CHANNEL_ID:
        print(f"Voice disconnected from channel {CHANNEL_ID}; reconnecting.")
        if client.voice_connect_task is None or client.voice_connect_task.done():
            client.voice_connect_task = asyncio.create_task(connect_to_voice())


async def main():
    # Opus is loaded lazily by discord.py on first voice use. Probe at startup
    # so a misconfigured deployment surfaces the issue immediately. We warn
    # rather than fail: the bot connects mute/deaf, so it can run without
    # actually exercising Opus.
    if not discord.opus.is_loaded():
        print(
            "Warning: Opus library is not loaded. Voice connect may fail "
            "(install libopus and ensure it is on the library search path)."
        )

    health_server = await asyncio.start_server(handle_health_check, "0.0.0.0", PORT)
    print(f"Health check server listening on port {PORT}")

    try:
        async with client:
            await client.start(TOKEN)
    finally:
        health_server.close()
        await health_server.wait_closed()

    if client.fatal_startup_error:
        raise SystemExit(1)


asyncio.run(main())
