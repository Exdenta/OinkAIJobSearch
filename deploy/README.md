# Hryu — Hetzner deployment

Turnkey deploy bundle. Target: a single Hetzner CX22 (or equivalent), Ubuntu
24.04 LTS. Goes from a fresh server to a running production install with

## What this deploys

```
                            Internet (HTTPS, Let's Encrypt via Caddy)
                                        │
                                        ▼
                ┌──────────────────────────────────────────────┐
                │  Caddy   (systemd service: caddy)            │
                │   :443 / :80                                 │
                │   /r*    → 127.0.0.1:8001   (redirect svr)   │
                └──────────────────────────────────────────────┘
                                        │
   ┌────────────────────────────────────┼────────────────────────────────┐
   ▼                                    ▼                                ▼
┌──────────────┐               ┌────────────────────┐         ┌─────────────────┐
│ 127.0.0.1    │               │  redirect server   │         │  via .timer)    │
│ :8000        │               │  on 127.0.0.1      │         │                 │
└──────┬───────┘               │  :8001)            │         └────────┬────────┘
       │                       └─────────┬──────────┘                  │
       │                                 │                             │
       └────────────────► /home/hryu/state/jobs.db ◄───────────────────┘
                          (SQLite — shared by all three)
```

Services:
- **hryu-bot.service** — `python skill/job-search/scripts/bot.py`. The bot
  starts the redirect server in-process on `127.0.0.1:8001` (see
  `bot.py:2723` and `redirect_server.py:start_redirect_server`).
  We do **not** run the redirect server as a separate systemd unit; it has
  no `__main__` entry point and is meant to live inside `bot.py`.
- **hryu-digest.timer** + **hryu-digest.service** — daily oneshot at 08:00
  Europe/Berlin. Runs `search_jobs.py`.
  server. Auto-provisions Let's Encrypt certificates from DNS.

Persistent state lives in `/home/hryu/state/` (SQLite + per-user resumes).
Everything else is throwaway and can be redeployed at will.

## First-time provisioning

1. **Hetzner console** — create a CX22 with Ubuntu 24.04 LTS, attach your
   SSH public key, note the IPv4/IPv6 addresses.
2. **DNS** — point `hryu.example.com` (and `www.hryu.example.com`) A/AAAA
   records at the server. Wait for propagation (`dig +short hryu.example.com`).
3. **SSH in as root** and create the unprivileged tree:
   ```bash
   ssh root@<server-ip>
   apt-get update && apt-get install -y git rsync
   ```
4. **Get the code onto the server**, two flavours:
   - From your laptop:
     ```bash
     rsync -azv --exclude='.git' --exclude='node_modules' \
         --exclude='state' ./ root@<server-ip>:/home/hryu/app/
     ```
   - Or `git clone` directly on the server (requires a deploy key for
     private repos):
     ```bash
     git clone https://github.com/your-org/FindJobs /home/hryu/app
     ```
5. **Edit `/home/hryu/.env`**. Bootstrap creates it from `env.example`
   with `chmod 600` if missing. Fill in:
   - `TELEGRAM_BOT_TOKEN` from @BotFather
   - `ANTHROPIC_API_KEY` (recommended) — see "Anthropic auth" below
   - `REDIRECT_HMAC_SECRET=$(openssl rand -hex 32)`
   - `HRYU_PUBLIC_URL` and `REDIRECT_BASE_URL` — set both to
     `https://hryu.example.com` (or your real domain).
   - `OPERATOR_CHAT_ID` — DM the bot from your operator account once it's
     up, then read `journalctl -u hryu-bot` to find your chat_id.
6. **Run bootstrap**:
   ```bash
   sudo bash /home/hryu/app/deploy/bootstrap.sh
   ```
   Prints each step and exits non-zero on failure. Re-runs are no-ops.
7. **Verify**:
   ```bash
   curl -fsS https://hryu.example.com/healthz   # 200 OK once cert is live
   ```
   Smoke-test the bot: send `/start` to your Telegram bot, confirm reply.

### Generating secrets

```bash
openssl rand -base64 24      # alternate format if base64 preferred
```

## Updating the deployment

### Via CI (recommended)

1. Move the workflow into place once:
   ```bash
   mkdir -p .github/workflows
   git mv deploy/github-actions/deploy.yml .github/workflows/deploy.yml
   git commit -m "ci: enable Hetzner deploy"
   git push
   ```
2. Add repo secrets in Settings → Secrets and variables → Actions:
   - `HETZNER_HOST` — `hryu.example.com` or raw IP.
   - `HETZNER_SSH_KEY` — private ED25519 key. Generate with
     `ssh-keygen -t ed25519 -C github-deploy -f hryu_deploy`. Public half
     goes into `~deploy/.ssh/authorized_keys` on the server.
3. Future pushes to `master` deploy automatically. Or trigger manually
   with the "Deploy to Hetzner" workflow's *Run workflow* button.

### Manually from a laptop

```bash
deploy/deploy.sh hryu.example.com
```

The script rsyncs the working tree, reinstalls Python deps, rebuilds the
frontend, restarts services, and verifies all are `active` before exiting.

## Rollback

Two strategies:

- **Local**: `git checkout <previous-tag-or-sha>` and re-run `deploy.sh`.
- **On the server** (faster, no CI roundtrip):
  ```bash
  ssh deploy@hryu.example.com 'cd /home/hryu/app && sudo -u hryu git fetch && sudo -u hryu git checkout <sha>'
  ```
  Note: server-side checkout requires the deploy method to be `git clone`
  rather than rsync. If you went the rsync route, only the laptop rollback
  applies.

## Logs

```bash
journalctl -u hryu-bot    -f       # Telegram bot + redirect server
journalctl -u hryu-digest -n 200   # Last digest run
journalctl -u caddy       -f       # TLS / proxy
tail -f /var/log/caddy/hryu-access.log
```

## Backups

Daily state is small (SQLite + a few resume PDFs). Recommended: `restic`
against a Hetzner Storage Box. Sketch (not scripted — pick your own
schedule and credentials):

```cron
# 0 3 * * *  /usr/local/bin/restic -r sftp:user@u123.your-storagebox.de:/backups \
#              --password-file /home/hryu/.restic-pw \
#              backup --tag nightly /home/hryu/state /home/hryu/.env
```

What to back up: `/home/hryu/state/` (DB + per-user files) and
`/home/hryu/.env` (secrets). Skip `/home/hryu/app/` — that's redeployable.

## Anthropic auth

The bot's profile builder, market_research, and fit_analyzer all shell out
to the `claude` CLI. Two modes:

- **Recommended — API key in `.env`**:
  ```env
  ANTHROPIC_API_KEY=sk-ant-...
  ```
  Non-interactive, works from a fresh boot. Get a key at
  <https://console.anthropic.com/settings/keys>.
- **Interactive OAuth**:
  ```bash
  sudo -u hryu -H bash -lc 'claude'
  ```
  …and follow the device-flow prompts once. Stores credentials in
  `/home/hryu/.claude/`. Survives reboots; breaks if the user is rotated.

If neither is configured, the bot keeps running but every Claude-backed
feature falls back to its heuristic (still useful, just less smart).

## Domain swap

Replace the placeholder once DNS is real:

```bash
sudo sed -i 's/hryu\.example\.com/realdomain.tld/g' /etc/caddy/Caddyfile
sudo systemctl reload caddy
# Then in /home/hryu/.env, update HRYU_PUBLIC_URL and REDIRECT_BASE_URL,
# and restart the affected services:
```

## Limitations / known issues

  token in the query string. Real SMTP delivery is Phase 3. Do not expose
  the web frontend to untrusted users yet — anyone with a chat_id can sign
  in as that user.
- **Web profile builder is a stub.** The bot's full Opus profile pipeline
  for `HRYU_BUILD_DELAY` seconds and writes a placeholder profile.
- **No HA or failover.** Single CX22, single SQLite file. Fine for the MVP;
  plan a managed Postgres + 2nd app box before you outgrow it.
- **Redirect server is in-process inside `bot.py`.** If you restart only
  the bot, click-tracking is briefly down (typically < 1s). Caddy will
  502 on `/r*` during that window.
- **No Docker.** Out of scope for solo Hetzner — one Python venv, one
  systemd, no orchestration.

## File reference

| File                                          | Purpose                                  |
|-----------------------------------------------|------------------------------------------|
| `deploy/bootstrap.sh`                         | Fresh-server provisioning (root)         |
| `deploy/deploy.sh`                            | Push-from-laptop / CI deploy             |
| `deploy/env.example`                          | Template for `/home/hryu/.env`           |
| `deploy/systemd/hryu-bot.service`             | Long-poll bot + in-process redirector    |
| `deploy/systemd/hryu-digest.service`          | Oneshot — daily digest                   |
| `deploy/systemd/hryu-digest.timer`            | 08:00 Europe/Berlin trigger              |
| `deploy/sudoers.d/hryu-deploy`                | NOPASSWD restart/reload for deploy group |
| `deploy/github-actions/deploy.yml`            | GHA workflow — move to `.github/workflows/` to enable |
