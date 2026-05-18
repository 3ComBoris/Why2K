import asyncio
import discord
import os
import sys

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

PORT = int(os.getenv("PORT", "8080"))
VOICE_CONNECT_RETRY_SECONDS = 30

intents = discord.Intents.default()
client = discord.Client(intents=intents)
client.voice_connect_task = None


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
    finally:
        writer.close()
        await writer.wait_closed()


async def connect_to_voice():
    if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
        return

    try:
        channel = await client.fetch_channel(CHANNEL_ID)
    except discord.NotFound:
        print(f"Error: Voice channel with ID {CHANNEL_ID} not found.")
        await client.close()
        return
    except discord.Forbidden:
        print(f"Error: Missing permissions to access channel {CHANNEL_ID}.")
        await client.close()
        return
    except discord.HTTPException as exc:
        print(f"Error: Failed to fetch channel {CHANNEL_ID}: {exc}")
        await client.close()
        return

    if not isinstance(channel, discord.VoiceChannel):
        print(f"Error: Channel {CHANNEL_ID} is not a voice channel.")
        await client.close()
        return

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
                f"Retrying in {VOICE_CONNECT_RETRY_SECONDS} seconds."
            )
        except discord.ClientException as exc:
            if any(vc.channel.id == CHANNEL_ID for vc in client.voice_clients):
                return
            print(
                f"Warning: Could not connect to voice channel: {exc}. "
                f"Retrying in {VOICE_CONNECT_RETRY_SECONDS} seconds."
            )
        except discord.Forbidden:
            print(f"Error: Missing permissions to connect to {channel.name}.")
            await client.close()
            return
        except discord.HTTPException as exc:
            print(
                f"Warning: Failed to connect to voice channel: {exc}. "
                f"Retrying in {VOICE_CONNECT_RETRY_SECONDS} seconds."
            )
        except discord.opus.OpusNotLoaded as exc:
            print(f"Error: Opus library not available: {exc}")
            await client.close()
            return

        await asyncio.sleep(VOICE_CONNECT_RETRY_SECONDS)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    if client.voice_connect_task is None or client.voice_connect_task.done():
        client.voice_connect_task = asyncio.create_task(connect_to_voice())

async def main():
    health_server = await asyncio.start_server(handle_health_check, "0.0.0.0", PORT)
    print(f"Health check server listening on port {PORT}")

    try:
        async with client:
            await client.start(TOKEN)
    finally:
        health_server.close()
        await health_server.wait_closed()


asyncio.run(main())
