#!/usr/bin/env bash
# Why2K droplet installer. Idempotent — safe to re-run after a git pull.
#
# Run as root on a fresh Ubuntu 22.04+ / Debian 12+ droplet:
#   curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh | bash
# or, after cloning:
#   sudo bash deploy/install.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/3ComBoris/Why2K.git}"
APP_USER="${APP_USER:-why2k}"
APP_DIR="${APP_DIR:-/opt/why2k}"
ENV_DIR="${ENV_DIR:-/etc/why2k}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/env}"
SERVICE_NAME="${SERVICE_NAME:-why2k}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root (sudo bash $0)" >&2
  exit 1
fi

echo ">>> Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl git \
  python3 python3-venv python3-pip \
  libopus0

echo ">>> Creating service user '${APP_USER}'"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

echo ">>> Preparing ${APP_DIR}"
mkdir -p "${APP_DIR}"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo ">>> Cloning / updating repo at ${APP_DIR}"
if [[ -d "${APP_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch --depth=1 origin main
  sudo -u "${APP_USER}" git -C "${APP_DIR}" reset --hard origin/main
else
  sudo -u "${APP_USER}" git clone --depth=1 "${REPO_URL}" "${APP_DIR}"
fi

echo ">>> Building venv and installing requirements"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  sudo -u "${APP_USER}" "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
fi
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

echo ">>> Preparing env file at ${ENV_FILE}"
mkdir -p "${ENV_DIR}"
chmod 750 "${ENV_DIR}"
chown root:"${APP_USER}" "${ENV_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<'EOF'
# Why2K runtime config. systemd loads this via EnvironmentFile=.
# Fill in real values, then: systemctl restart why2k

DISCORD_TOKEN=
VOICE_CHANNEL_ID=

# Optional. The health endpoint binds 0.0.0.0:PORT; on a droplet you almost
# never need it exposed externally. Default 8080.
# PORT=8080
EOF
  echo "    (created template — edit ${ENV_FILE} with your DISCORD_TOKEN and VOICE_CHANNEL_ID)"
fi
chmod 640 "${ENV_FILE}"
chown root:"${APP_USER}" "${ENV_FILE}"

echo ">>> Installing systemd unit"
install -m 0644 "${APP_DIR}/deploy/why2k.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service" >/dev/null

echo
echo "Install complete."
echo
echo "Next steps:"
echo "  1. Fill in secrets:    sudoedit ${ENV_FILE}"
echo "  2. Start the service:  sudo systemctl start ${SERVICE_NAME}"
echo "  3. Tail logs:          sudo journalctl -u ${SERVICE_NAME} -f"
