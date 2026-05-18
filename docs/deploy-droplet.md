# Deploying Why2K on a DigitalOcean Droplet

This guide installs Why2K as a `systemd` service on a fresh Ubuntu droplet. It assumes you control the droplet and can SSH in as `root` (or any user with `sudo`).

> **Why a Droplet and not App Platform?** Discord voice requires a stable outbound UDP path. DigitalOcean App Platform (and Render, Heroku) close UDP port mappings before Discord's voice gateway can ack heartbeats — sessions die after ~30 seconds. A Droplet is a real VM with direct network access and avoids the problem entirely.

## 1. Create the droplet

- **Image**: Ubuntu 22.04 LTS or Ubuntu 24.04 LTS (Debian 12 also works).
- **Size**: the smallest plan ($4–6/mo, 512 MB RAM) is plenty. The bot uses ~50 MB resident.
- **Region**: pick one close to the Discord voice region your guild uses; latency only affects voice quality, not connectivity.
- **Authentication**: SSH key. Don't use password login.

After it boots, SSH in:

```bash
ssh root@your.droplet.ip
```

This guide assumes you're logged in as `root` (the DigitalOcean default). If you've already created a non-root user with administrator rights, prepend `sudo` to every command shown — except on minimal Debian images that ship without `sudo` installed, where you should either install `sudo` (`apt install -y sudo`) or stay as `root`.

## 2. Lock the host down (optional but recommended)

```bash
# Patches
apt update && apt -y upgrade

# Firewall: SSH only
ufw allow OpenSSH
ufw --force enable

# Unattended security updates
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades   # accept defaults
```

The bot's health-check endpoint binds `0.0.0.0:8080` but the firewall blocks it from outside — that's fine, the endpoint is only useful when something queries it. If you want external monitoring (e.g. UptimeRobot), open it explicitly: `ufw allow 8080/tcp`.

## 3. Install Why2K

One command, idempotent — safe to re-run after every `git pull`:

```bash
# As root:
curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh | bash

# As a sudoer (skip if you're already root):
curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh | sudo bash
```

To deploy from a fork or a non-default branch, set `REPO_URL` and/or `BRANCH` in the environment before the pipe:

```bash
REPO_URL=https://github.com/your-fork/Why2K.git BRANCH=my-branch \
  bash <(curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh)
```

What it does:

1. Installs `python3`, `python3-venv`, `git`, `libopus0`.
2. Creates a system user `why2k` (no login shell).
3. Clones the repo to `/opt/why2k` (owned by `why2k`).
4. Builds a virtualenv at `/opt/why2k/.venv` and installs `requirements.txt`.
5. Creates a config skeleton at `/etc/why2k/env` (mode 640, owned by `root:why2k`).
6. Installs the `why2k.service` systemd unit and enables it.

## 4. Configure secrets

Edit `/etc/why2k/env` and fill in the real values:

```bash
# As root:
nano /etc/why2k/env

# As a sudoer:
sudoedit /etc/why2k/env
```

```ini
DISCORD_TOKEN=your_bot_token_here
VOICE_CHANNEL_ID=123456789012345678
# PORT=8080   # uncomment to change the health-check port
```

## 5. Start the service

```bash
systemctl start why2k
systemctl status why2k
```

Tail the logs:

```bash
journalctl -u why2k -f
```

You should see:

```
discord.gateway: Shard ID None has connected to Gateway (Session ID: …)
why2k: Logged in as Why2k?#8841
why2k: Voice state changed: channel None -> … (target …)
why2k: Joined voice channel: …
```

…and nothing more. A correctly-deployed bot is quiet — no further "Voice state changed" lines should appear unless an admin moves it or Discord drops the session.

## 6. Operating the service

| Action | Command |
|---|---|
| Status | `systemctl status why2k` |
| Start | `systemctl start why2k` |
| Stop | `systemctl stop why2k` |
| Restart | `systemctl restart why2k` |
| Enable at boot | `systemctl enable why2k` (the installer does this) |
| Tail logs | `journalctl -u why2k -f` |
| Last 200 lines | `journalctl -u why2k -n 200` |
| Logs since boot | `journalctl -u why2k -b` |

## 7. Updating

Re-run the installer — it does `git fetch` + `git reset --hard origin/${BRANCH}`, reinstalls deps, and reloads systemd. Override `BRANCH` to deploy a non-`main` branch.

```bash
curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh | bash
systemctl restart why2k
```

Or update in place (`runuser` avoids the `sudo` dependency on minimal Debian):

```bash
runuser -u why2k -- git -C /opt/why2k pull
runuser -u why2k -- /opt/why2k/.venv/bin/pip install -r /opt/why2k/requirements.txt
systemctl restart why2k
```

## 8. Layout reference

```
/opt/why2k/                   # repo checkout (owned by why2k:why2k)
  ├── .venv/                  # virtualenv with discord.py + python-dotenv
  ├── bot.py
  ├── requirements.txt
  └── deploy/
      ├── install.sh
      └── why2k.service
/etc/why2k/env                # mode 640, root:why2k — secrets live here
/etc/systemd/system/why2k.service   # installed copy of deploy/why2k.service
```

## 9. Troubleshooting

**Service won't start.** `journalctl -u why2k -n 50` will show the Python error. Most common: `DISCORD_TOKEN is not set. Please add it to your .env file.` That error message comes from the bot's startup check — it's wording for local development. **On this systemd deployment the equivalent file is `/etc/why2k/env`**, and the unit loads it via `EnvironmentFile=`. Edit that one, not a `.env` in `/opt/why2k`.

**Bot logs in but voice still drops every 30s.** Confirm with `ss -ulnp | grep python` that the bot has bound a local UDP port, and `iptables -L OUTPUT` doesn't reject UDP. On a standard droplet with default `ufw` outbound is unrestricted — if you've tightened it, allow outbound UDP to Discord's voice regions.

**`Voice sessions are dropping within 60s` followed by a 503 health response.** The circuit breaker tripped (5 consecutive short sessions). The droplet's outbound UDP isn't reaching Discord. Check firewall rules and any VPN/proxy on the host.

**`libopus is not loaded` warning at startup.** The installer apt-gets `libopus0`, so on a freshly-installed droplet this warning shouldn't appear. If it does, libopus isn't on the dynamic linker's search path — verify with `ldconfig -p | grep libopus`. The warning matters because discord.py raises `OpusNotLoaded` from the voice connect flow under some conditions, and `connect_to_voice` treats that as fatal. If voice never establishes and the journal cites Opus, reinstall the package (`apt install --reinstall libopus0`) and restart the service.
