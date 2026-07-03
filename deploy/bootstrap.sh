#!/usr/bin/env bash
# bootstrap.sh — fresh CX22 (Ubuntu 24.04) → running Hryu deployment.
#
# Run as root, ON THE SERVER, after you've rsynced or git-cloned the repo
# to /home/hryu/app/. Idempotent: re-running is a no-op when nothing has
# changed. Every step prints what it's doing and exits non-zero on failure.
#
#   sudo bash /home/hryu/app/deploy/bootstrap.sh
#
# Pre-flight assumptions:
#   * Ubuntu 24.04 LTS, root SSH access.
#   * /home/hryu/app/ already contains the repo (rsync from CI or git clone).
#   * Real /home/hryu/.env already filled in (or you'll fill it before
#     enabling services — bootstrap creates .env from env.example with 600
#     perms but does not invent secrets).

set -euo pipefail

# ----- Constants ---------------------------------------------------------
HRYU_USER="hryu"
HRYU_HOME="/home/${HRYU_USER}"
APP_DIR="${HRYU_HOME}/app"
STATE_DIR="${HRYU_HOME}/state"
LOGS_DIR="${HRYU_HOME}/logs"
VENV_DIR="${HRYU_HOME}/venv"
ENV_FILE="${HRYU_HOME}/.env"
ENV_EXAMPLE="${APP_DIR}/deploy/env.example"
SYSTEMD_SRC="${APP_DIR}/deploy/systemd"
CADDY_SRC="${APP_DIR}/deploy/caddy/Caddyfile"
SUDOERS_SRC="${APP_DIR}/deploy/sudoers.d/hryu-deploy"

NODE_MAJOR=20
PY_VERSION=python3.12

# ----- Helpers -----------------------------------------------------------
say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
run_as_hryu() { sudo -u "${HRYU_USER}" -H bash -lc "$*"; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "bootstrap.sh must run as root" >&2
        exit 1
    fi
}

# Idempotency guard for apt installs — only invoke when at least one
# package is missing. Avoids re-fetching package lists on every run.
ensure_apt_pkgs() {
    local missing=()
    for pkg in "$@"; do
        if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
            missing+=("${pkg}")
        fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        return 0
    fi
    say "Installing apt packages: ${missing[*]}"
    apt-get update
    apt-get install -y --no-install-recommends "${missing[@]}"
}

# ----- 0. Pre-flight -----------------------------------------------------
require_root

if [[ ! -d "${APP_DIR}" ]]; then
    echo "Expected ${APP_DIR} to exist (git clone / rsync first)." >&2
    exit 1
fi

# ----- 1. Base apt packages ---------------------------------------------
say "Step 1/8: base apt packages"
ensure_apt_pkgs \
    "${PY_VERSION}" \
    "${PY_VERSION}-venv" \
    python3-pip \
    git rsync curl ca-certificates \
    build-essential pkg-config \
    libxml2-dev libxslt1-dev \
    debian-keyring debian-archive-keyring apt-transport-https \
    sudo

# ----- 2. Caddy via Cloudsmith ------------------------------------------
say "Step 2/8: Caddy"
if ! command -v caddy >/dev/null 2>&1; then
    install -d -m 0755 /usr/share/keyrings
    if [[ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]]; then
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    fi
    if [[ ! -f /etc/apt/sources.list.d/caddy-stable.list ]]; then
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            -o /etc/apt/sources.list.d/caddy-stable.list
    fi
    apt-get update
    apt-get install -y --no-install-recommends caddy
fi
# Always make sure caddy is enabled and running — cheap and idempotent.
systemctl enable --now caddy.service

# ----- 3. Node.js 20 via NodeSource -------------------------------------
# Needed for `npm install -g @anthropic-ai/claude-code` below.
say "Step 3/9: Node.js ${NODE_MAJOR}"
need_node=true
if command -v node >/dev/null 2>&1; then
    cur="$(node --version | sed 's/^v//;s/\..*//')"
    if [[ "${cur}" -ge "${NODE_MAJOR}" ]]; then
        need_node=false
    fi
fi
if ${need_node}; then
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y --no-install-recommends nodejs
fi

# ----- 4. Claude CLI ------------------------------------------------------
say "Step 4/9: Claude CLI"
if ! command -v claude >/dev/null 2>&1; then
    npm install -g @anthropic-ai/claude-code
fi
# Ownership of npm's global prefix is fine — claude is invoked by the
# hryu user via PATH; the binary itself need not be owned by hryu.

# ----- 5. hryu user + dirs ---------------------------------------------
say "Step 5/9: hryu user and directory tree"
if ! id -u "${HRYU_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${HRYU_USER}"
fi
install -d -o "${HRYU_USER}" -g "${HRYU_USER}" -m 0755 "${APP_DIR}"
install -d -o "${HRYU_USER}" -g "${HRYU_USER}" -m 0750 "${STATE_DIR}"
install -d -o "${HRYU_USER}" -g "${HRYU_USER}" -m 0755 "${LOGS_DIR}"

# Make sure the rsynced app tree is owned by hryu (idempotent chown).
chown -R "${HRYU_USER}:${HRYU_USER}" "${APP_DIR}"

# /home/hryu/.env — created from env.example if missing (operator fills in).
if [[ ! -f "${ENV_FILE}" ]]; then
    install -o "${HRYU_USER}" -g "${HRYU_USER}" -m 0600 \
        "${ENV_EXAMPLE}" "${ENV_FILE}"
    say "Created ${ENV_FILE} from env.example. Edit it before enabling services."
fi
chmod 600 "${ENV_FILE}"
chown "${HRYU_USER}:${HRYU_USER}" "${ENV_FILE}"

# Ensure Caddy can write its access log dir.
install -d -o caddy -g caddy -m 0755 /var/log/caddy 2>/dev/null \
    || install -d -m 0755 /var/log/caddy

# ----- 6. Python venv + deps -------------------------------------------
say "Step 6/9: Python venv + dependencies"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    run_as_hryu "${PY_VERSION} -m venv '${VENV_DIR}'"
fi
run_as_hryu "'${VENV_DIR}/bin/pip' install -U pip wheel setuptools"
run_as_hryu "'${VENV_DIR}/bin/pip' install -r '${APP_DIR}/requirements.txt'"

# ----- 7. systemd units -------------------------------------------------
say "Step 7/9: systemd units"
install -m 0644 "${SYSTEMD_SRC}/hryu-bot.service"     /etc/systemd/system/
install -m 0644 "${SYSTEMD_SRC}/hryu-digest.service"  /etc/systemd/system/
install -m 0644 "${SYSTEMD_SRC}/hryu-digest.timer"    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hryu-bot.service
systemctl enable --now hryu-digest.timer

# ----- 8. Caddyfile -----------------------------------------------------
say "Step 8/9: Caddyfile"
install -m 0644 "${CADDY_SRC}" /etc/caddy/Caddyfile
# `caddy validate` on the installed file before reloading — abort the
# whole bootstrap if it's syntactically broken.
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy.service

# ----- 9. Sudoers ------------------------------------------------------
say "Step 9/9: sudoers"
# Make sure the hryu-deploy group exists; the file references it.
groupadd -f hryu-deploy
# Install with strict perms; visudo -cf validates BEFORE we put the file
# in /etc/sudoers.d/, so a bad rule cannot lock root out.
TMPFILE="$(mktemp)"
trap 'rm -f "${TMPFILE}"' EXIT
install -m 0440 "${SUDOERS_SRC}" "${TMPFILE}"
visudo -cf "${TMPFILE}"
install -o root -g root -m 0440 "${TMPFILE}" /etc/sudoers.d/hryu-deploy

say "Done. Verify with:"
cat <<'EOF'
  systemctl status hryu-bot hryu-digest.timer caddy
  curl -sf https://hryu.example.com/healthz   # once DNS + cert are live
  journalctl -u hryu-bot -n 50
EOF
