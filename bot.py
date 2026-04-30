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
    channel = client.get_channel(CHANNEL_ID)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        print(f"Could not find voice channel with ID {CHANNEL_ID}")
        return
    try:
        await channel.connect()
        print(f"Joined voice channel: {channel.name}")
    except discord.ClientException as exc:
        print(f"Failed to join voice channel: {exc}")
    except discord.opus.OpusNotLoaded as exc:
        print(f"Opus library not available: {exc}")


client.run(TOKEN)
