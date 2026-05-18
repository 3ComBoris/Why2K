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

VOICE_CONNECT_INITIAL_RETRY_SECONDS = 30
VOICE_CONNECT_MAX_RETRY_SECONDS = 300

intents = discord.Intents.default()
client = discord.Client(intents=intents)
voice_connect_task: Optional[asyncio.Task] = None
fatal_startup_error = False


def is_retryable_http_exception(exc: discord.HTTPException) -> bool:
    status = getattr(exc, "status", None)
    return status == 429 or (status is not None and status >= 500)


def is_retryable_client_exception(exc: discord.ClientException) -> bool:
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "already trying to connect",
            "connection closed",
            "not connected to voice",
            "voice websocket is not connected",
        )
    )


async def handle_health_check(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await reader.read(1024)
        if fatal_startup_error:
            body = b"startup failure\n"
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
    except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def fail_startup(message: str):
    global fatal_startup_error

    fatal_startup_error = True
    print(message)
    await client.close()


async def connect_to_voice():
    if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
        return

    attempt = 1
    retry_delay = VOICE_CONNECT_INITIAL_RETRY_SECONDS

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
        except TimeoutError:
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


@client.event
async def on_ready():
    global voice_connect_task

    print(f"Logged in as {client.user}")

    if voice_connect_task is not None and voice_connect_task.done():
        try:
            voice_connect_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await fail_startup(f"Error: Voice connection task failed unexpectedly: {exc}")
            return

    if voice_connect_task is None or voice_connect_task.done():
        voice_connect_task = asyncio.create_task(connect_to_voice())


async def main():
    health_server = await asyncio.start_server(handle_health_check, "0.0.0.0", PORT)
    print(f"Health check server listening on port {PORT}")

    try:
        async with client:
            await client.start(TOKEN)
    finally:
        health_server.close()
        await health_server.wait_closed()

    if fatal_startup_error:
        raise SystemExit(1)


asyncio.run(main())
