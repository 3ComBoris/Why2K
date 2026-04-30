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

intents = discord.Intents.default()
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # Guard against reconnects triggering a second connect attempt
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

    try:
        await channel.connect(self_deaf=True, self_mute=True)
        print(f"Joined voice channel: {channel.name}")
    except discord.ClientException as exc:
        print(f"Error: Could not connect to voice channel: {exc}")
        await client.close()
    except discord.Forbidden:
        print(f"Error: Missing permissions to connect to {channel.name}.")
        await client.close()
    except discord.HTTPException as exc:
        print(f"Error: Failed to connect to voice channel: {exc}")
        await client.close()
    except discord.opus.OpusNotLoaded as exc:
        print(f"Error: Opus library not available: {exc}")
        await client.close()


client.run(TOKEN)
