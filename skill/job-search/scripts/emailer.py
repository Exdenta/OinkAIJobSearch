"""SMTP email delivery for web users — magic links + digest notifications.

Transport config comes entirely from env (the systemd units load
/home/hryu/.env; local dev reads the repo .env via load_env or shell):

    HRYU_SMTP_HOST       smtp.example.com — unset → emailer disabled
    HRYU_SMTP_PORT       587 (default)
    HRYU_SMTP_USERNAME   optional — unset → no AUTH (e.g. localhost relay)
    HRYU_SMTP_PASSWORD   optional
    HRYU_SMTP_FROM       From: header; falls back to HRYU_SMTP_USERNAME
    HRYU_SMTP_SECURITY   starttls (default) | ssl | none

Both the web backend (magic links, via the shared scripts sys.path) and
search_jobs (digest notifications for web-only users) import this module.
`send_email` never raises — email is a delivery channel, not a
transaction; callers decide what a False return means for them.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.message import EmailMessage

log = logging.getLogger(__name__)

_DEFAULT_PORT = 587
_DEFAULT_SECURITY = "starttls"

# Web-only users get at most one "new matches" email per this interval.
# 20h ≈ daily without drifting later every day the way a strict 24h
# cooldown would.
_DEFAULT_NOTIFY_MIN_INTERVAL_S = 20 * 3600

# How many job lines the digest-notification email includes.
_DIGEST_EMAIL_TOP_N = 5


def smtp_configured() -> bool:
    """True when a transport host is set. Username/password stay optional
    so a localhost relay works."""
    return bool((os.environ.get("HRYU_SMTP_HOST") or "").strip())


def _smtp_settings() -> dict:
    host = (os.environ.get("HRYU_SMTP_HOST") or "").strip()
    try:
        port = int(os.environ.get("HRYU_SMTP_PORT") or _DEFAULT_PORT)
    except ValueError:
        port = _DEFAULT_PORT
    username = (os.environ.get("HRYU_SMTP_USERNAME") or "").strip()
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": os.environ.get("HRYU_SMTP_PASSWORD") or "",
        "from_addr": (os.environ.get("HRYU_SMTP_FROM") or "").strip() or username,
        "security": (os.environ.get("HRYU_SMTP_SECURITY") or _DEFAULT_SECURITY)
        .strip()
        .lower(),
    }


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on accepted-by-relay, False
    on any failure (logged, never raised). No-op False when SMTP is
    unconfigured — callers gate on `smtp_configured()` for their fallback.
    """
    if not smtp_configured():
        log.debug("emailer: HRYU_SMTP_HOST unset — send_email(%r) skipped", to)
        return False

    cfg = _smtp_settings()
    if not cfg["from_addr"]:
        log.error("emailer: HRYU_SMTP_FROM and HRYU_SMTP_USERNAME both empty")
        return False

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if cfg["security"] == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg["host"], cfg["port"], timeout=20,
            )
        else:
            client = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        try:
            if cfg["security"] == "starttls":
                client.starttls()
            if cfg["username"]:
                client.login(cfg["username"], cfg["password"])
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        log.info("emailer: sent %r to %s", subject, to)
        return True
    except Exception:
        log.exception("emailer: send to %s failed", to)
        return False


# ---------------------------------------------------------------------------
# Web-user digest notification
# ---------------------------------------------------------------------------

def _notify_min_interval_s() -> int:
    try:
        return int(
            os.environ.get("HRYU_EMAIL_NOTIFY_MIN_INTERVAL_S")
            or _DEFAULT_NOTIFY_MIN_INTERVAL_S
        )
    except ValueError:
        return _DEFAULT_NOTIFY_MIN_INTERVAL_S


def maybe_send_web_digest_email(db, chat_id: int, jobs, enrichments) -> bool:
    """Email a web-only user that a search run found fresh matches.

    Gates, in order: SMTP configured → user exists, has a verified email,
    and notify_email is on → at least one match this run → last email
    older than HRYU_EMAIL_NOTIFY_MIN_INTERVAL_S (default 20h, so the 2h
    continuous-searcher cadence doesn't turn into 12 emails a day).

    Returns True only when an email actually went out. Never raises.
    """
    try:
        if not jobs or not smtp_configured():
            return False
        row = db.get_user(chat_id)
        if row is None or not (row["email"] or "").strip():
            return False
        if not db.get_notify_email(chat_id):
            return False

        now = time.time()
        last = db.get_last_email_notified_at(chat_id)
        if last is not None and (now - float(last)) < _notify_min_interval_s():
            log.debug(
                "emailer: digest notify for chat=%s suppressed (last %.1fh ago)",
                chat_id, (now - float(last)) / 3600.0,
            )
            return False

        public_url = (os.environ.get("HRYU_PUBLIC_URL") or "").strip().rstrip("/")
        feed_url = f"{public_url}/" if public_url else ""

        scored = []
        for j in jobs:
            enr = (enrichments or {}).get(j.job_id) or {}
            scored.append((int(enr.get("match_score") or 0), j))
        scored.sort(key=lambda t: -t[0])

        lines = []
        for score, j in scored[:_DIGEST_EMAIL_TOP_N]:
            company = f" @ {j.company}" if getattr(j, "company", "") else ""
            lines.append(f"  [{score}/5] {j.title}{company}")
        more = len(scored) - _DIGEST_EMAIL_TOP_N
        if more > 0:
            lines.append(f"  …and {more} more")

        n = len(scored)
        subject = f"Hryu found {n} new job match{'es' if n != 1 else ''}"
        body = (
            f"Your latest search turned up {n} match{'es' if n != 1 else ''}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + (f"Open your feed: {feed_url}\n\n" if feed_url else "")
            + "You get at most one of these a day. "
            "Turn them off in Settings → Email me when new matches land."
        )

        sent = send_email(row["email"].strip(), subject, body)
        if sent:
            db.set_last_email_notified_at(chat_id, now)
        return sent
    except Exception:
        log.exception("emailer: maybe_send_web_digest_email crashed for chat=%s", chat_id)
        return False
