# Why2K

A minimal Discord bot that joins a voice channel and holds the call open indefinitely.

## Requirements

- Python 3.8+
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- The numeric ID of the voice channel you want the bot to occupy

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables**

   Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

   | Variable | Description |
   |---|---|
   | `DISCORD_TOKEN` | Your bot's token from the Discord Developer Portal |
   | `VOICE_CHANNEL_ID` | Numeric ID of the voice channel to join |

3. **Invite the bot to your server**

   When creating the bot in the Developer Portal, enable the following OAuth2 scopes:
   - `bot`

   And the following bot permissions:
   - `Connect`
   - `Speak`

4. **Run the bot**
   ```bash
   python bot.py
   ```

   The bot will log in, join the configured voice channel, and stay there indefinitely.