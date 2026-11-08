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

## 3. Give the droplet read access to the repo

The repository is private, so `https://raw.githubusercontent.com/...` URLs return 404 and plain `git clone https://...` asks for credentials. The droplet reads the repo with a **deploy key**: an SSH key that can read this one repository and nothing else, held by the `why2k` service user.

Create the `why2k` user first, because the key lives in its home directory. These are the same commands the installer runs; it skips them when the user already exists.

```bash
groupadd --system why2k
useradd --system --gid why2k --create-home --home-dir /home/why2k --shell /usr/sbin/nologin why2k
```

Create the key and trust GitHub's host key:

```bash
install -d -m 700 -o why2k -g why2k /home/why2k/.ssh
runuser -u why2k -- ssh-keygen -t ed25519 -N "" -C "why2k droplet deploy key" -f /home/why2k/.ssh/id_ed25519
echo 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' > /home/why2k/.ssh/known_hosts
chown why2k:why2k /home/why2k/.ssh/known_hosts
cat /home/why2k/.ssh/id_ed25519.pub
```

The `known_hosts` line is GitHub's published ed25519 host key (fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`; compare it with [GitHub's SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)). Pinning it means the droplet never trusts whatever server answers first.

On GitHub, open the repository's **Settings → Deploy keys → Add deploy key**, paste the `.pub` line printed above, and leave **Allow write access** unchecked. Then check that it works:

```bash
runuser -u why2k -- ssh -T git@github.com
# Hi 3ComBoris/Why2K! You've successfully authenticated, but GitHub does not provide shell access.
```

(The command exits with status 1 even when it succeeds. The message is what matters.)

## 4. Install Why2K

> **`/opt/why2k` is managed by the installer.** Re-running the installer does `git reset --hard origin/${BRANCH}`, which discards any local edits in that tree. Make changes via the repo (commit, push, re-run the installer), never by editing files in `/opt/why2k` on the server. Secrets live in `/etc/why2k/env` and are never overwritten by the installer.

Clone the repo with the deploy key, review the installer, and run it from the clone. It's idempotent. On a fresh install it stops short of starting the service so you can fill in secrets first.

```bash
cd /tmp                                   # the why2k user can't read /root
apt-get update -qq && apt-get install -y git
runuser -u why2k -- git clone --depth=1 git@github.com:3ComBoris/Why2K.git /tmp/why2k-src
less /tmp/why2k-src/deploy/install.sh     # review it
bash /tmp/why2k-src/deploy/install.sh
rm -rf /tmp/why2k-src
```

The installer clones `git@github.com:3ComBoris/Why2K.git` (branch `main`) by default. To deploy a fork or another branch, set `REPO_URL` and/or `BRANCH`, for example `REPO_URL=git@github.com:your-fork/Why2K.git BRANCH=my-branch bash /tmp/why2k-src/deploy/install.sh`. A fork needs its own deploy key.

What it does:

1. Installs `python3`, `python3-venv`, `git`, `libopus0`.
2. Creates the system user `why2k` (no login shell) if it doesn't exist yet.
3. Clones the repo to `/opt/why2k` over SSH (owned by `why2k`, authenticating with its deploy key).
4. Builds a virtualenv at `/opt/why2k/.venv` and installs `requirements.txt`.
5. Creates a config skeleton at `/etc/why2k/env` (mode 640, owned by `root:why2k`).
6. Installs the `why2k.service` systemd unit and enables it.

## 5. Configure secrets

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

This is the only file the service reads. A `.env` in `/opt/why2k` is ignored (see [Troubleshooting](#10-troubleshooting)). If you replace the file rather than edit it, restore its permissions so the token isn't world-readable: `chown root:why2k /etc/why2k/env && chmod 640 /etc/why2k/env`.

## 6. Start the service

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

## 7. Operating the service

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

## 8. Updating

Re-run the installer — it does `git fetch` + `git reset --hard origin/${BRANCH}`, reinstalls deps, reloads systemd, and (if the service was already running) restarts it. Override `BRANCH` to deploy a non-`main` branch. Copy it out of `/opt/why2k` first, so the reset can't rewrite the script while it's running:

```bash
cd /tmp
cp /opt/why2k/deploy/install.sh /tmp/why2k-install.sh
bash /tmp/why2k-install.sh
```

A restart makes the bot leave and rejoin the channel once. If the channel must never be empty, update while someone else is in it.

Or update in place (`runuser` avoids the `sudo` dependency on minimal Debian):

```bash
runuser -u why2k -- git -C /opt/why2k pull
runuser -u why2k -- /opt/why2k/.venv/bin/pip install -r /opt/why2k/requirements.txt
systemctl restart why2k
```

**Droplets installed before the repo went private** still have an HTTPS `origin` and an older installer that defaults to HTTPS. Set up the deploy key (section 3), then run the installer once with the SSH URL; later runs default to it:

```bash
cd /tmp
cp /opt/why2k/deploy/install.sh /tmp/why2k-install.sh
REPO_URL=git@github.com:3ComBoris/Why2K.git bash /tmp/why2k-install.sh
```

## 9. Layout reference

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
/home/why2k/.ssh/id_ed25519   # read-only deploy key for the private repo
```

## 10. Troubleshooting

**`Permission denied (publickey)` while cloning or updating.** The deploy key isn't set up or isn't added on GitHub. Re-check section 3, and test with `runuser -u why2k -- ssh -T git@github.com`. **`Host key verification failed`** means `/home/why2k/.ssh/known_hosts` is missing or wrong.

**Service won't start.** `journalctl -u why2k -n 50` will show the Python error. Most common: `DISCORD_TOKEN is not set.` (or `VOICE_CHANNEL_ID is not set.`). Fill it in `/etc/why2k/env`, which the unit loads via `EnvironmentFile=`. A `.env` file in `/opt/why2k` does **not** work here: systemd sets every variable from `/etc/why2k/env` before the bot starts — including the installer's empty `DISCORD_TOKEN=` template line — and the bot's `.env` loader never overrides a variable that's already set, even to an empty value.

**Bot logs in but voice still drops every 30s.** Confirm with `ss -ulnp | grep python` that the bot has bound a local UDP port, and `iptables -L OUTPUT` doesn't reject UDP. On a standard droplet with default `ufw` outbound is unrestricted — if you've tightened it, allow outbound UDP to Discord's voice regions.

**`Voice sessions are dropping within 60s` followed by a 503 health response.** The circuit breaker tripped (5 consecutive short sessions). The droplet's outbound UDP isn't reaching Discord. Check firewall rules and any VPN/proxy on the host.