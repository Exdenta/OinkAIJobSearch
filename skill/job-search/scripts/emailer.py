"""SMTP email delivery for web users — magic links + digest notifications.

Transport config comes entirely from env (the systemd units load
/home/oink/.env; local dev reads the repo .env via load_env or shell):

    OINK_SMTP_HOST       smtp.example.com — unset → emailer disabled
    OINK_SMTP_PORT       587 (default)
    OINK_SMTP_USERNAME   optional — unset → no AUTH (e.g. localhost relay)
    OINK_SMTP_PASSWORD   optional
    OINK_SMTP_FROM       From: header; falls back to OINK_SMTP_USERNAME
    OINK_SMTP_SECURITY   starttls (default) | ssl | none

Both the web backend (magic links, via the shared scripts sys.path) and
search_jobs (digest notifications for web-only users) import this module.
`send_email` never raises — email is a delivery channel, not a
transaction; callers decide what a False return means for them.

This module also owns the email *design*: every outgoing message is
rendered here (plain-text part + HTML part), so the visual language
lives in one place. The HTML mirrors the web app's theme (warm cream
background, white cards, forest-green accent) with inline styles only —
email clients strip <style> blocks and external assets.
"""

from __future__ import annotations

import html as _html
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


def smtp_configured() -> bool:
    """True when a transport host is set. Username/password stay optional
    so a localhost relay works."""
    return bool((os.environ.get("OINK_SMTP_HOST") or "").strip())


def _smtp_settings() -> dict:
    host = (os.environ.get("OINK_SMTP_HOST") or "").strip()
    try:
        port = int(os.environ.get("OINK_SMTP_PORT") or _DEFAULT_PORT)
    except ValueError:
        port = _DEFAULT_PORT
    username = (os.environ.get("OINK_SMTP_USERNAME") or "").strip()
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": os.environ.get("OINK_SMTP_PASSWORD") or "",
        "from_addr": (os.environ.get("OINK_SMTP_FROM") or "").strip() or username,
        "security": (os.environ.get("OINK_SMTP_SECURITY") or _DEFAULT_SECURITY)
        .strip()
        .lower(),
    }


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Send an email — plain text, plus an HTML alternative when `html`
    is given. Returns True on accepted-by-relay, False on any failure
    (logged, never raised). No-op False when SMTP is unconfigured —
    callers gate on `smtp_configured()` for their fallback.
    """
    if not smtp_configured():
        log.debug("emailer: OINK_SMTP_HOST unset — send_email(%r) skipped", to)
        return False

    cfg = _smtp_settings()
    if not cfg["from_addr"]:
        log.error("emailer: OINK_SMTP_FROM and OINK_SMTP_USERNAME both empty")
        return False

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

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
# Shared design tokens + rendering helpers
# ---------------------------------------------------------------------------
#
# Hex approximations of the web app's theme (web/frontend/src/styles.css).
# The app uses oklch() for the accent; email clients don't, so these are
# pre-converted.

_C_BG = "#f5f1e7"          # warm cream page background
_C_CARD = "#ffffff"        # card surface
_C_INK = "#1f1d18"         # primary text
_C_INK2 = "#5b5648"        # secondary text
_C_INK3 = "#8d8775"        # tertiary / metadata
_C_RULE = "#e7e2d3"        # hairline borders
_C_ACCENT = "#2f7050"      # forest green
_C_ACCENT_INK = "#24503a"  # darker green for links
_C_ACCENT_SOFT = "#ddf0e3" # green tint fill
_C_WARN = "#7a4a16"        # mismatch note

_FONT = "'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif"
_MONO = "ui-monospace, SFMono-Regular, Menlo, monospace"


def _score_bar(score: int, cells: int = 5) -> str:
    """▰▰▰▱▱-style match bar; same glyphs as the Telegram cards."""
    n = max(0, min(cells, int(score)))
    return "▰" * n + "▱" * (cells - n)


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=True)


def _chip_row(pairs) -> str:
    """' · '-joined 'icon text' chips from (icon, text) pairs; empties drop."""
    parts = []
    for icon, text in pairs:
        t = (str(text or "")).strip()
        if t:
            parts.append(f"{icon} {t[:80]}")
    return "  ·  ".join(parts)


def _visa_label(v) -> str:
    s = (str(v or "")).strip().lower()
    return {"yes": "visa support", "no": "no visa support"}.get(s, "")


def _job_detail_rows(enr: dict) -> list[str]:
    """Two compact chip rows from key_details — same grouping as the
    Telegram card: role signal (stack · seniority · remote), then
    logistics (location · salary · visa · language)."""
    d = (enr or {}).get("key_details") or {}
    if not isinstance(d, dict):
        return []
    rows = []
    row1 = _chip_row([
        ("⚙️", d.get("stack")),
        ("📊", d.get("seniority")),
        ("🌐", d.get("remote_policy")),
    ])
    row2 = _chip_row([
        ("📍", d.get("location")),
        ("💰", d.get("salary")),
        ("🛂", _visa_label(d.get("visa_support"))),
        ("🗣", d.get("language")),
    ])
    if row1:
        rows.append(row1)
    if row2:
        rows.append(row2)
    return rows


def _render_job_text(job, enr: dict) -> str:
    """One job as a plain-text card — the full Telegram-card information:
    score bar, title, company/location, match rationale, detail chips,
    snippet, link, source."""
    enr = enr or {}
    title = (getattr(job, "title", "") or "Untitled role").strip()
    lines = []

    score = enr.get("match_score")
    if score is not None:
        s = int(score or 0)
        lines.append(f"{_score_bar(s)}  {s}/5 match")
    lines.append(title)

    meta = [
        (getattr(job, "company", "") or "").strip() or "Unknown company",
    ]
    loc = (getattr(job, "location", "") or "").strip()
    if loc:
        meta.append(loc)
    sal = (getattr(job, "salary", "") or "").strip()
    if sal and not enr.get("key_details"):
        meta.append(sal)
    lines.append("  ·  ".join(meta))

    why = (enr.get("why_match") or "").strip()
    if why:
        lines.append(f"✅ {why[:260]}")
    mism = (enr.get("why_mismatch") or "").strip()
    if mism:
        lines.append(f"⚠️ {mism[:260]}")

    lines.extend(_job_detail_rows(enr))

    standout = ((enr.get("key_details") or {}).get("standout") or "").strip() \
        if isinstance(enr.get("key_details"), dict) else ""
    if standout:
        lines.append(f"» {standout[:200]}")

    snip = (getattr(job, "snippet", "") or "").strip()
    if snip:
        if len(snip) > 240:
            snip = snip[:240].rstrip() + "…"
        lines.append(f'"{snip}"')

    url = (getattr(job, "url", "") or "").strip()
    if url:
        lines.append(f"Apply: {url}")
    source = (getattr(job, "source", "") or "").strip()
    if source:
        lines.append(f"via {source}")
    return "\n".join(lines)


def _render_job_html(job, enr: dict) -> str:
    """One job as a white HTML card, mirroring the web feed's look."""
    enr = enr or {}
    title = _esc((getattr(job, "title", "") or "Untitled role").strip())
    url = _esc((getattr(job, "url", "") or "").strip())

    title_html = (
        f'<a href="{url}" style="color:{_C_INK};text-decoration:none;">{title}</a>'
        if url else title
    )
    parts = [
        f'<div style="background:{_C_CARD};border:1px solid {_C_RULE};'
        f'border-radius:12px;padding:18px 20px;margin:0 0 12px;">'
    ]

    score = enr.get("match_score")
    if score is not None:
        s = int(score or 0)
        parts.append(
            f'<div style="font-size:13px;color:{_C_ACCENT};margin:0 0 6px;">'
            f'<span style="letter-spacing:2px;">{_score_bar(s)}</span>'
            f'&nbsp;&nbsp;<strong>{s}/5 match</strong></div>'
        )

    parts.append(
        f'<div style="font-size:17px;font-weight:600;color:{_C_INK};'
        f'line-height:1.35;margin:0 0 4px;">{title_html}</div>'
    )

    meta = [_esc((getattr(job, "company", "") or "").strip() or "Unknown company")]
    loc = (getattr(job, "location", "") or "").strip()
    if loc:
        meta.append(_esc(loc))
    sal = (getattr(job, "salary", "") or "").strip()
    if sal and not enr.get("key_details"):
        meta.append(_esc(sal))
    parts.append(
        f'<div style="font-size:14px;color:{_C_INK2};margin:0 0 10px;">'
        + "&nbsp;&nbsp;·&nbsp;&nbsp;".join(meta) + "</div>"
    )

    why = (enr.get("why_match") or "").strip()
    if why:
        parts.append(
            f'<div style="font-size:14px;color:{_C_ACCENT_INK};'
            f'line-height:1.5;margin:0 0 4px;">✅ {_esc(why[:260])}</div>'
        )
    mism = (enr.get("why_mismatch") or "").strip()
    if mism:
        parts.append(
            f'<div style="font-size:14px;color:{_C_WARN};'
            f'line-height:1.5;margin:0 0 4px;">⚠️ {_esc(mism[:260])}</div>'
        )

    detail_rows = _job_detail_rows(enr)
    if detail_rows:
        chips = "<br>".join(_esc(r) for r in detail_rows)
        parts.append(
            f'<div style="font-size:13px;color:{_C_INK2};line-height:1.7;'
            f'margin:8px 0 0;">{chips}</div>'
        )

    standout = ((enr.get("key_details") or {}).get("standout") or "").strip() \
        if isinstance(enr.get("key_details"), dict) else ""
    if standout:
        parts.append(
            f'<div style="font-size:13px;font-style:italic;color:{_C_INK2};'
            f'margin:8px 0 0;">{_esc(standout[:200])}</div>'
        )

    snip = (getattr(job, "snippet", "") or "").strip()
    if snip:
        if len(snip) > 240:
            snip = snip[:240].rstrip() + "…"
        parts.append(
            f'<div style="font-size:13px;font-style:italic;color:{_C_INK3};'
            f'line-height:1.5;margin:8px 0 0;">{_esc(snip)}</div>'
        )

    footer_bits = []
    if url:
        footer_bits.append(
            f'<a href="{url}" style="display:inline-block;background:{_C_ACCENT};'
            f'color:#fefcf6;font-size:13px;font-weight:600;text-decoration:none;'
            f'padding:7px 16px;border-radius:8px;">Open posting →</a>'
        )
    source = (getattr(job, "source", "") or "").strip()
    if source:
        footer_bits.append(
            f'<span style="font-family:{_MONO};font-size:12px;color:{_C_INK3};">'
            f'via {_esc(source)}</span>'
        )
    if footer_bits:
        parts.append(
            '<div style="margin:12px 0 0;">'
            + '&nbsp;&nbsp;&nbsp;'.join(footer_bits) + "</div>"
        )

    parts.append("</div>")
    return "".join(parts)


def _wrap_html(inner: str, preheader: str = "") -> str:
    """Shared outer frame: cream background, 600px column, Oink header,
    muted footer. `inner` is trusted HTML from our renderers."""
    pre = (
        f'<div style="display:none;max-height:0;overflow:hidden;">'
        f'{_esc(preheader)}</div>' if preheader else ""
    )
    return (
        f'<div style="background:{_C_BG};padding:28px 12px;font-family:{_FONT};">'
        f"{pre}"
        f'<div style="max-width:600px;margin:0 auto;">'
        f'<div style="font-size:20px;font-weight:700;color:{_C_INK};'
        f'margin:0 0 16px;">🐷 Oink <span style="font-weight:400;'
        f'color:{_C_INK3};font-size:14px;">— job alerts</span></div>'
        f"{inner}"
        f"</div></div>"
    )


# ---------------------------------------------------------------------------
# Sign-in (magic link + one-time code) email
# ---------------------------------------------------------------------------

def render_sign_in_email(link: str, code: str) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for the sign-in email.

    Code-first: the 6-digit code leads the subject (inbox preview shows
    it without opening the mail) and dominates the card; the magic link
    is the secondary path for same-device sign-in.
    """
    subject = f"{code} is your Oink sign-in code"
    text = (
        f"Your sign-in code: {code}\n\n"
        "Type it on the page where you asked to sign in — or click:\n\n"
        f"{link}\n\n"
        "Code and link are valid for 15 minutes and work once. "
        "If you didn't request this, ignore this email."
    )
    esc_link = _esc(link)
    inner = (
        f'<div style="background:{_C_CARD};border:1px solid {_C_RULE};'
        f'border-radius:12px;padding:28px 24px;text-align:center;">'
        f'<div style="font-size:15px;color:{_C_INK2};margin:0 0 14px;">'
        f"Your sign-in code</div>"
        f'<div style="font-family:{_MONO};font-size:34px;font-weight:700;'
        f"letter-spacing:8px;color:{_C_INK};background:{_C_ACCENT_SOFT};"
        f'border-radius:10px;padding:14px 8px;margin:0 0 18px;">{_esc(code)}</div>'
        f'<div style="font-size:14px;color:{_C_INK2};margin:0 0 18px;">'
        f"Type it on the page where you asked to sign in — or use the button:</div>"
        f'<a href="{esc_link}" style="display:inline-block;background:{_C_ACCENT};'
        f'color:#fefcf6;font-size:15px;font-weight:600;text-decoration:none;'
        f'padding:12px 28px;border-radius:10px;">Sign in to Oink</a>'
        f'<div style="font-size:13px;color:{_C_INK3};margin:18px 0 0;line-height:1.6;">'
        f"Code and link are valid for 15 minutes and work once.<br>"
        f"If you didn't request this, ignore this email.</div>"
        f'<div style="font-size:12px;color:{_C_INK3};margin:14px 0 0;'
        f'word-break:break-all;">Button not working? Paste this into your '
        f'browser:<br><a href="{esc_link}" style="color:{_C_ACCENT_INK};">'
        f"{esc_link}</a></div>"
        f"</div>"
    )
    return subject, text, _wrap_html(
        inner, f"Sign-in code {code} — valid 15 minutes"
    )


# ---------------------------------------------------------------------------
# Web-user digest notification
# ---------------------------------------------------------------------------

def _notify_min_interval_s() -> int:
    try:
        return int(
            os.environ.get("OINK_EMAIL_NOTIFY_MIN_INTERVAL_S")
            or _DEFAULT_NOTIFY_MIN_INTERVAL_S
        )
    except ValueError:
        return _DEFAULT_NOTIFY_MIN_INTERVAL_S


def maybe_send_web_digest_email(db, chat_id: int, jobs, enrichments) -> bool:
    """Email a web-only user that a search run found fresh matches.

    The email carries the FULL card for every job in the run — same
    information as the Telegram digest (score bar, match rationale,
    key-detail chips, snippet, link, source) — not just a top-N teaser,
    so the email alone is enough to triage.

    Gates, in order: SMTP configured → user exists, has a verified email,
    and notify_email is on → at least one match this run → last email
    older than OINK_EMAIL_NOTIFY_MIN_INTERVAL_S (default 20h, so the 2h
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

        public_url = (os.environ.get("OINK_PUBLIC_URL") or "").strip().rstrip("/")
        feed_url = f"{public_url}/" if public_url else ""

        scored = []
        for j in jobs:
            enr = (enrichments or {}).get(j.job_id) or {}
            scored.append((int(enr.get("match_score") or 0), j, enr))
        scored.sort(key=lambda t: -t[0])

        n = len(scored)
        es = "es" if n != 1 else ""
        top_title = (getattr(scored[0][1], "title", "") or "").strip()
        subject = f"🐷 {n} new job match{es}"
        if top_title:
            subject += f" — top pick: {top_title[:60]}"

        intro = f"Your latest search turned up {n} match{es}, best fit first."
        footer_note = (
            "You get at most one of these a day. "
            "Turn them off in Settings → Email me when new matches land."
        )

        divider = "\n\n" + "─" * 34 + "\n\n"
        body = (
            intro
            + divider
            + divider.join(_render_job_text(j, enr) for _, j, enr in scored)
            + "\n\n"
            + (f"Open your feed: {feed_url}\n\n" if feed_url else "")
            + footer_note
        )

        cards = "".join(_render_job_html(j, enr) for _, j, enr in scored)
        cta = (
            f'<div style="text-align:center;margin:20px 0 0;">'
            f'<a href="{_esc(feed_url)}" style="display:inline-block;'
            f"background:{_C_ACCENT};color:#fefcf6;font-size:15px;font-weight:600;"
            f'text-decoration:none;padding:12px 28px;border-radius:10px;">'
            f"Open your feed</a></div>"
        ) if feed_url else ""
        inner = (
            f'<div style="font-size:15px;color:{_C_INK2};margin:0 0 16px;">'
            f"{_esc(intro)}</div>"
            + cards
            + cta
            + f'<div style="font-size:12px;color:{_C_INK3};margin:20px 0 0;'
            f'line-height:1.6;text-align:center;">{_esc(footer_note)}</div>'
        )
        html_body = _wrap_html(inner, intro)

        sent = send_email(row["email"].strip(), subject, body, html=html_body)
        if sent:
            db.set_last_email_notified_at(chat_id, now)
        return sent
    except Exception:
        log.exception("emailer: maybe_send_web_digest_email crashed for chat=%s", chat_id)
        return False
