#!/usr/bin/env bash
# deploy.sh — push working tree to the Hryu server and restart services.
#
# Usage:
#   deploy/deploy.sh <host>
#       host = hostname or IP, e.g. hryu.example.com or 1.2.3.4
#
# Designed to run from CI (see deploy/github-actions/deploy.yml) and from a
# developer's laptop. Idempotent — re-running is safe; rsync only ships
# diffs. Skips first-boot bootstrap (run deploy/bootstrap.sh on the box once
# before this).
#
# Exit codes:
#   0  success, all services Active=active
#   2  bad args
#   3  rsync failed
#   4  remote install failed
#   5  service is not active after restart

set -euo pipefail

HOST="${1:-}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
SSH_TARGET="${DEPLOY_USER}@${HOST}"
APP_DIR="/home/hryu/app"

if [[ -z "${HOST}" ]]; then
    echo "Usage: $0 <host>" >&2
    exit 2
fi

# Resolve repo root from this script's location so it works from any cwd.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

# ----- 1. rsync working tree --------------------------------------------
say "rsync → ${SSH_TARGET}:${APP_DIR}/"
# --rsync-path uses sudo -u hryu so files land owned by hryu:hryu without
# needing root for the rsync receive end. The deploy user must be allowed
# to `sudo -u hryu rsync` (configure in /etc/sudoers.d/hryu-deploy if you
# want this; or run rsync as deploy and let bootstrap chown afterwards).
#
# Excludes: VCS, build artifacts, runtime state, dotenv. .env on the
# server stays untouched — never overwritten by deploy.
rsync -azv --delete \
    --exclude='.git/' \
    --exclude='node_modules/' \
    --exclude='__pycache__/' \
    --exclude='.pytest_cache/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='dist/' \
    --exclude='state/' \
    --exclude='logs/' \
    --exclude='.env' \
    --exclude='.env.local' \
    ./ "${SSH_TARGET}:${APP_DIR}/" \
    || { echo "rsync failed" >&2; exit 3; }

# ----- 2. install deps + rebuild frontend -------------------------------
say "install Python deps + rebuild frontend"
ssh "${SSH_TARGET}" 'bash -se' <<'REMOTE' || { echo "remote install failed" >&2; exit 4; }
set -euo pipefail
sudo -u hryu /home/hryu/venv/bin/pip install -r /home/hryu/app/requirements.txt
# The dist/ output is what production serves; node_modules never ships.
REMOTE

# ----- 3. restart services + reload Caddy -------------------------------
say "restart services"
ssh "${SSH_TARGET}" 'bash -se' <<'REMOTE'
set -euo pipefail
sudo /bin/systemctl restart hryu-bot.service
sudo /bin/systemctl reload  caddy.service
REMOTE

# ----- 4. wait + verify -------------------------------------------------
say "verifying services (10s journal tail)"
ssh "${SSH_TARGET}" 'bash -se' <<'REMOTE' || { echo "post-restart verify failed" >&2; exit 5; }
set -euo pipefail
sleep 10
    state="$(systemctl is-active "${svc}" || true)"
    if [[ "${state}" != "active" ]]; then
        echo "FAIL: ${svc} is ${state}" >&2
        sudo /bin/journalctl -u "${svc}" -n 50 --no-pager
        exit 1
    fi
    echo "OK: ${svc}"
done
# Sanity-check the in-process redirect listener too (bot.py spawns it).
if ! ss -ltn | grep -q '127.0.0.1:8001'; then
    echo "WARN: redirect server not listening on 127.0.0.1:8001 — REDIRECT_BASE_URL/SECRET probably unset"
fi
REMOTE

say "deploy complete"
