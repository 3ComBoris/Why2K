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


async def handle_health_check(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await reader.read(1024)
        body = b"ok\n"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: 3\r\n"
            b"Connection: close\r\n\r\n"
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

    try:
        channel = await client.fetch_channel(CHANNEL_ID)
    except discord.NotFound:
        await fail_startup(f"Error: Voice channel with ID {CHANNEL_ID} not found.")
        return
    except discord.Forbidden:
        await fail_startup(f"Error: Missing permissions to access channel {CHANNEL_ID}.")
        return
    except discord.HTTPException as exc:
        await fail_startup(f"Error: Failed to fetch channel {CHANNEL_ID}: {exc}")
        return

    if not isinstance(channel, discord.VoiceChannel):
        await fail_startup(f"Error: Channel {CHANNEL_ID} is not a voice channel.")
        return

    attempt = 1
    retry_delay = VOICE_CONNECT_INITIAL_RETRY_SECONDS

    while not client.is_closed():
        if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
            return

        try:
            await channel.connect(self_deaf=True, self_mute=True)
            print(f"Joined voice channel: {channel.name}")
            return
        except TimeoutError:
            print(
                f"Warning: Timed out connecting to {channel.name}. "
                f"Attempt {attempt}. Retrying in {retry_delay} seconds."
            )
        except discord.ClientException as exc:
            if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
                return
            print(
                f"Warning: Could not connect to voice channel: {exc}. "
                f"Attempt {attempt}. Retrying in {retry_delay} seconds."
            )
        except discord.Forbidden:
            await fail_startup(f"Error: Missing permissions to connect to {channel.name}.")
            return
        except discord.HTTPException as exc:
            print(
                f"Warning: Failed to connect to voice channel: {exc}. "
                f"Attempt {attempt}. Retrying in {retry_delay} seconds."
            )

        await asyncio.sleep(retry_delay)
        attempt += 1
        retry_delay = min(retry_delay * 2, VOICE_CONNECT_MAX_RETRY_SECONDS)


@client.event
async def on_ready():
    global voice_connect_task

    print(f"Logged in as {client.user}")

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
