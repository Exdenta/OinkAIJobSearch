"""View-posting redirect server — stdlib HTTP listener that logs clicks.

Why a redirector
-----------------
Telegram URL buttons (the "View posting ↗" link under each job card) open
the destination directly in the user's browser. Telegram NEVER notifies
the bot when a URL button is tapped — there's no callback for url-type
buttons. To get any click signal we have to put a server in the middle:

    [Telegram] → tap → [our redirect URL] → 302 → [job board posting]

Each redirect link encodes the chat_id + job_id and an HMAC signature so
nobody can forge a click event by hand-crafting URLs.

Public exposure
---------------
The bot listens on `127.0.0.1:<port>` only — the operator is responsible
for fronting it with HTTPS via a tunnel (cloudflared, ngrok, a reverse
proxy on a VPS). We never bind 0.0.0.0 directly: this saves us from
TLS-on-the-bot, opens-port-on-laptop firewall headaches, and DoS exposure.

Configuration (env)
-------------------
  REDIRECT_BIND_HOST       — default 127.0.0.1
  REDIRECT_BIND_PORT       — default 8765
  REDIRECT_BASE_URL        — public base URL (e.g. https://abc.trycloudflare.com)
                             When unset, callers should keep using raw URL
                             buttons (no analytics path).
  REDIRECT_HMAC_SECRET     — secret for HMAC-SHA256 signature; required
                             when REDIRECT_BASE_URL is set. If empty,
                             button-builder falls back to raw URLs.

URL shape
---------
  GET <REDIRECT_BASE_URL>/r?j=<job_id>&u=<chat_id>&s=<hex_sig>
    j  — stable 16-hex job_id (matches DB)
    u  — chat_id (Telegram numeric)
    s  — first 16 hex chars of HMAC-SHA256(secret, "j=<j>&u=<u>")

Server validates `s`, looks up `jobs.url` by job_id, records a click,
issues a 302 to the canonical URL. On HMAC fail → 403. On unknown
job_id → 410 (gone). On any other path → 404.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)

# Length of the hex-encoded HMAC slice we keep in URLs. 16 hex chars =
# 64 bits of signature space — comfortably collision-resistant for our
# threat model (forgery, not preimage attack on the secret).
SIG_LEN = 16


def sign_url_payload(secret: str, job_id: str, chat_id: int) -> str:
    """Return the 16-hex signature for `(job_id, chat_id)`.

    The canonical message is `j=<job_id>&u=<chat_id>` — same byte order
    the URL carries. Keep this stable: changing it invalidates every
    in-flight Telegram button on every previously-sent digest message.
    """
    msg = f"j={job_id}&u={chat_id}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return digest[:SIG_LEN]


def build_redirect_url(
    base_url: str,
    secret: str,
    job_id: str,
    chat_id: int,
) -> str:
    """Construct the public redirect URL for one (job, chat) tuple.

    `base_url` should NOT have a trailing slash. We always emit `/r?j=...`.
    """
    sig = sign_url_payload(secret, job_id, chat_id)
    return f"{base_url.rstrip('/')}/r?j={job_id}&u={chat_id}&s={sig}"


class _Handler(BaseHTTPRequestHandler):
    """Single-method handler — only `GET /r?…` does anything useful."""

    # Wired by `start_redirect_server` after class creation.
    db = None  # type: ignore[assignment]
    secret = ""
    on_click: Callable[[int], None] | None = None

    # Quiet the default `127.0.0.1 - - [...]` noise on stderr; we log
    # via the project's `logging` setup instead.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib override
        log.debug("redirect_server: " + fmt, *args)

    def _send(self, status: int, body: str = "", headers: dict | None = None) -> None:
        body_bytes = body.encode("utf-8") if body else b""
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def do_GET(self) -> None:  # noqa: N802 - stdlib spelling
        parsed = urlparse(self.path)
        if parsed.path != "/r":
            self._send(404, "not found")
            return

        params = parse_qs(parsed.query)
        job_id = (params.get("j") or [""])[0]
        chat_id_raw = (params.get("u") or [""])[0]
        sig = (params.get("s") or [""])[0]

        if not job_id or not chat_id_raw or not sig:
            self._send(400, "bad request")
            return

        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            self._send(400, "bad chat id")
            return

        expected = sign_url_payload(self.secret, job_id, chat_id)
        if not hmac.compare_digest(expected, sig):
            log.warning("redirect_server: HMAC fail for job=%s chat=%s",
                        job_id, chat_id)
            self._send(403, "forbidden")
            return

        url = self.db.get_job_url(job_id) if self.db else None
        if not url:
            self._send(410, "posting no longer available")
            return

        # Defensive: never redirect to non-http(s) — protects against
        # someone poisoning the jobs table with javascript:/data: URIs.
        if not (url.startswith("http://") or url.startswith("https://")):
            self._send(410, "invalid posting url")
            return

        try:
            self.db.record_posting_click(
                chat_id=chat_id,
                job_id=job_id,
                user_agent=self.headers.get("User-Agent"),
                referer=self.headers.get("Referer"),
            )
        except Exception:  # noqa: BLE001
            log.exception("redirect_server: record_posting_click failed")

        self._send(302, headers={"Location": url})

        # Value-event hook: fires after the redirect is already on the
        # wire, so a slow/failing callback can't delay the user.
        if self.on_click is not None:
            try:
                self.on_click(chat_id)
            except Exception:  # noqa: BLE001
                log.exception("redirect_server: on_click callback failed")


def start_redirect_server(
    db,  # DB instance
    *,
    host: str | None = None,
    port: int | None = None,
    secret: str | None = None,
    on_click: Callable[[int], None] | None = None,
) -> ThreadingHTTPServer | None:
    """Spin up the redirect server in a daemon thread.

    Returns the running server on success, None when the feature is
    disabled (REDIRECT_BASE_URL or REDIRECT_HMAC_SECRET unset). Caller is
    free to ignore the return value — the server is daemon-threaded and
    dies with the parent process.
    """
    base_url = os.environ.get("REDIRECT_BASE_URL", "").strip()
    secret = (secret or os.environ.get("REDIRECT_HMAC_SECRET", "")).strip()
    if not base_url or not secret:
        log.info("redirect_server: disabled (REDIRECT_BASE_URL or "
                 "REDIRECT_HMAC_SECRET not set)")
        return None

    host = host or os.environ.get("REDIRECT_BIND_HOST", "127.0.0.1")
    try:
        port = int(port if port is not None
                   else os.environ.get("REDIRECT_BIND_PORT", "8765"))
    except (TypeError, ValueError):
        port = 8765

    # Bind handler class with shared state. We create a subclass per
    # invocation so multiple bots in the same process (tests) don't
    # collide on class-level attrs.
    class _BoundHandler(_Handler):
        pass
    _BoundHandler.db = db
    _BoundHandler.secret = secret
    _BoundHandler.on_click = on_click

    server = ThreadingHTTPServer((host, port), _BoundHandler)
    t = threading.Thread(
        target=server.serve_forever,
        name="redirect-server",
        daemon=True,
    )
    t.start()
    log.info("redirect_server: listening on http://%s:%d (public base: %s)",
             host, port, base_url)
    return server
