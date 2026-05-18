#!/usr/bin/env bash
# Why2K droplet installer. Idempotent — safe to re-run after a git pull.
#
# Run as root on a fresh Ubuntu 22.04+ / Debian 12+ droplet:
#   curl -fsSL https://raw.githubusercontent.com/3ComBoris/Why2K/main/deploy/install.sh | bash
# or, after cloning:
#   sudo bash deploy/install.sh

set -euo pipefail

# Paths and identifiers below match deploy/why2k.service; don't override them
# without also editing the unit file (or the service will fail to start).
APP_USER="why2k"
APP_DIR="/opt/why2k"
ENV_DIR="/etc/why2k"
ENV_FILE="${ENV_DIR}/env"
SERVICE_NAME="why2k"

REPO_URL="${REPO_URL:-https://github.com/3ComBoris/Why2K.git}"
BRANCH="${BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root (sudo bash $0, or pipe into 'sudo bash')" >&2
  exit 1
fi

# runuser is part of util-linux (always present on Debian/Ubuntu); use it
# instead of sudo, which isn't installed on minimal Debian images.
as_app_user() {
  runuser -u "${APP_USER}" -- "$@"
}

# Abort if a path exists but isn't a real directory — a symlink at one of our
# managed paths would cause the subsequent recursive chown/chmod to operate
# on the symlink target, which is almost certainly not what the operator
# wants (and could clobber ownership on, e.g., /etc).
ensure_dir_is_safe() {
  local path="$1"
  if [[ -e "${path}" && (-L "${path}" || ! -d "${path}") ]]; then
    echo "ERROR: ${path} exists but is not a regular directory (symlink or non-dir)." >&2
    echo "Refusing to chown/chmod through it. Remove or replace it, then re-run." >&2
    exit 1
  fi
}

echo ">>> Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl git \
  python3 python3-venv python3-pip \
  libopus0

echo ">>> Creating service group/user '${APP_USER}'"
# Create the group first so chown ${APP_USER}:${APP_USER} and the unit's
# Group=why2k always resolve. --user-group on useradd does the same thing
# but isn't supported on all distros' useradd builds.
if ! getent group "${APP_USER}" >/dev/null; then
  groupadd --system "${APP_USER}"
fi
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${APP_USER}" \
    --create-home --home-dir "/home/${APP_USER}" \
    --shell /usr/sbin/nologin \
    "${APP_USER}"
fi

echo ">>> Preparing ${APP_DIR}"
ensure_dir_is_safe "${APP_DIR}"
mkdir -p "${APP_DIR}"
# Recursive: a previous root-owned manual clone here would leave files that
# the runuser-as-why2k git/venv/pip commands below can't touch.
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo ">>> Cloning / updating repo at ${APP_DIR} (branch ${BRANCH})"
if [[ -d "${APP_DIR}/.git" ]]; then
  # If REPO_URL changed between runs, retarget origin so the fetch pulls
  # from the new source instead of the stale remote.
  as_app_user git -C "${APP_DIR}" remote set-url origin "${REPO_URL}"
  as_app_user git -C "${APP_DIR}" fetch --depth=1 origin "${BRANCH}"
  as_app_user git -C "${APP_DIR}" reset --hard "origin/${BRANCH}"
else
  as_app_user git clone --depth=1 --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi

echo ">>> Building venv and installing requirements"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  as_app_user "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
fi
as_app_user "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
as_app_user "${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

echo ">>> Preparing env file at ${ENV_FILE}"
ensure_dir_is_safe "${ENV_DIR}"
mkdir -p "${ENV_DIR}"
chmod 750 "${ENV_DIR}"
chown root:"${APP_USER}" "${ENV_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<EOF
# Why2K runtime config. systemd loads this via EnvironmentFile=.
# Fill in real values, then: systemctl restart ${SERVICE_NAME}

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

# If the service is already running (this is an update, not a first install),
# restart it so the new code takes effect. On a fresh install the service
# isn't running yet, so we skip and let the operator start it after filling
# in secrets.
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo ">>> Restarting ${SERVICE_NAME} to pick up new code"
  systemctl restart "${SERVICE_NAME}.service"
fi

echo
echo "Install complete."
echo
echo "Next steps:"
echo "  1. Fill in secrets:    \$EDITOR ${ENV_FILE}"
echo "  2. Start the service:  systemctl start ${SERVICE_NAME}"
echo "  3. Tail logs:          journalctl -u ${SERVICE_NAME} -f"
