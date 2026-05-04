"""Telegram Bot API client + message formatting (with inline keyboards)."""
from __future__ import annotations

import email.utils
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from dedupe import Job

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


# ---------- Token-bucket rate limiter ----------
#
# Telegram's documented limits (https://core.telegram.org/bots/faq#broadcasting-to-users):
#   - 30 messages/second across ALL chats (global cap)
#   - 1 message/second to any individual user/chat (per-chat cap)
#   - Brief bursts above these limits are tolerated; sustained over-limit
#     yields a 429 with `parameters.retry_after = N` seconds.
#
# We model this as two token buckets per `_call`:
#   1. A single global bucket shared by every outbound call.
#   2. A per-chat bucket keyed by `chat_id` (lazily created).
# Calls that don't target a chat (`getUpdates`, `setMyCommands`, etc.) only
# consume the global bucket — that matches Telegram's accounting and avoids
# polluting the per-chat dict with sentinels.
#
# The limiter is opt-out via `TG_RATE_LIMIT_OFF=1` for tests / forensic
# replay where determinism matters more than safety.


# Env-coercion helpers used by both the rate limiter and URL-validator
# blocks below. Defined here so module-level constants can reference them at
# import time without forward-ref tricks.
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


TG_GLOBAL_RPS: float = _env_float("TG_GLOBAL_RPS", 30.0)
TG_PER_CHAT_RPS: float = _env_float("TG_PER_CHAT_RPS", 1.0)
TG_BURST_GLOBAL: int = _env_int("TG_BURST_GLOBAL", 30)
TG_BURST_PER_CHAT: int = _env_int("TG_BURST_PER_CHAT", 3)
TG_RATE_LIMIT_OFF: bool = os.environ.get("TG_RATE_LIMIT_OFF", "").strip() not in (
    "", "0", "false", "False",
)


class _TokenBucket:
    """Single token bucket with float-precision refill.

    Tokens accrue at `rate_per_sec` up to `capacity`. `acquire()` blocks until
    one token is available; returns the seconds slept (0.0 if it took a token
    immediately). Thread-safe — one lock per bucket.
    """

    __slots__ = ("capacity", "rate", "_tokens", "_last", "_lock")

    def __init__(self, capacity: float, rate_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(rate_per_sec)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self) -> float:
        """Block until a token is available; return seconds slept."""
        slept = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill_locked(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return slept
                # Need (1 - tokens) more; how long until that's true?
                deficit = 1.0 - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.05
            # Sleep outside the lock so other threads can still refill.
            time.sleep(wait)
            slept += wait


class _TelegramRateLimiter:
    """Two-tier limiter: global bucket + per-chat buckets.

    Acquisition order: global first, then per-chat. Per-chat buckets are
    created lazily so we don't pre-allocate for users we never message.
    `chat_id=None` (or 0) means "no chat" — only the global bucket is
    consumed. Both `acquire()` callers get back a tuple of
    (slept_global_ms, slept_per_chat_ms) so the caller can attribute the
    block to the right bucket in forensic output.
    """

    def __init__(
        self,
        global_rps: float,
        per_chat_rps: float,
        burst_global: int,
        burst_per_chat: int,
    ) -> None:
        self.global_bucket = _TokenBucket(burst_global, global_rps)
        self.per_chat_rps = per_chat_rps
        self.burst_per_chat = burst_per_chat
        self._chats: dict[Any, _TokenBucket] = {}
        self._chats_lock = threading.Lock()

    def _bucket_for(self, chat_id: Any) -> _TokenBucket:
        with self._chats_lock:
            b = self._chats.get(chat_id)
            if b is None:
                b = _TokenBucket(self.burst_per_chat, self.per_chat_rps)
                self._chats[chat_id] = b
            return b

    def acquire(self, chat_id: Any | None) -> tuple[float, float]:
        slept_global = self.global_bucket.acquire()
        slept_chat = 0.0
        if chat_id is not None:
            slept_chat = self._bucket_for(chat_id).acquire()
        return (slept_global, slept_chat)


_RATE_LIMITER = _TelegramRateLimiter(
    global_rps=TG_GLOBAL_RPS,
    per_chat_rps=TG_PER_CHAT_RPS,
    burst_global=TG_BURST_GLOBAL,
    burst_per_chat=TG_BURST_PER_CHAT,
)


def _extract_chat_id(payload: dict | None) -> Any | None:
    """Best-effort chat_id extraction from a Bot API payload.

    Methods that don't address a specific chat (getUpdates, setMyCommands,
    answerCallbackQuery, getFile, ...) return None so the per-chat bucket
    is bypassed.
    """
    if not payload:
        return None
    cid = payload.get("chat_id")
    if cid is None or cid == "":
        return None
    return cid


# ---------- URL liveness validation ----------
#
# Many job postings 404 by the time we send them — ATS systems unpublish
# closed reqs, redirects rot, and a few sources serve stale RSS for days.
# Sending a dead link wastes the user's tap and erodes trust. We HEAD-check
# the URL right before send and drop any job whose posting no longer
# resolves. The cost is small (one HEAD per job, ~100-300 ms) and gates
# inside the per-job loop so it covers EVERY chat regardless of source.

# Read once at import time so tests can monkey-patch via env before importing.
URL_VALIDATION_TIMEOUT_S: float = _env_float("URL_VALIDATION_TIMEOUT_S", 5.0)
URL_VALIDATION_OFF: bool = os.environ.get("URL_VALIDATION_OFF", "").strip() not in (
    "", "0", "false", "False",
)

# 429 retry policy. Some hosts (notably news.ycombinator.com) rate-limit our
# HEAD probe even when the page is fully live for a real user. A single 429
# was silently dropping otherwise-good candidates — the 2026-05-02 Alena
# regression (the only score-3 PostHog post vanished from her digest) is the
# canonical incident. Retry up to MAX_RETRIES times with exponential backoff
# (base, base*4) before declaring dead.
URL_VALIDATION_429_BACKOFF_BASE_S: float = _env_float(
    "URL_VALIDATION_429_BACKOFF_BASE_S", 1.0,
)
_URL_VALIDATION_429_MAX_RETRIES: int = 2  # 3 attempts total
# Cap on Retry-After: a server can ask us to wait an hour, but we have a
# digest to send — never block longer than this even if the header says so.
_URL_VALIDATION_429_RETRY_AFTER_CAP_S: float = 10.0

# Realistic browser UA — some ATS/CDN endpoints (Cloudflare in particular)
# 403 a generic bot UA on HEAD even when the page is fully public. We pose
# as Firefox so the liveness check matches what the user's tap would see.
_VALIDATION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) "
        "Gecko/20100101 Firefox/121.0"
    ),
    "Accept": "text/html,*/*",
    "Accept-Language": "en-US,en;q=0.5",
}

_ALIVE_STATUSES = frozenset({200, 301, 302, 303, 307, 308})
_HEAD_BLOCKED_STATUSES = frozenset({405, 501})


def _validation_request(
    method: str, url: str, timeout_s: float, *, headers: dict | None = None,
    verify: bool = True,
) -> requests.Response:
    """Single HTTP probe for URL validation. HEAD by default; allow_redirects
    on so 30x → canonical posting URLs follow correctly. Caller decides
    whether to retry under verify=False.
    """
    return requests.request(
        method,
        url,
        headers=headers or _VALIDATION_HEADERS,
        timeout=timeout_s,
        allow_redirects=True,
        verify=verify,
    )


def _parse_retry_after(resp: "requests.Response") -> float | None:
    """Parse the Retry-After header on a 429 response.

    The header is either delta-seconds (e.g. ``"3"``) or an HTTP-date.
    We only honor the delta-seconds form — HTTP-date is rare for 429 and
    parsing it would pull in extra deps. Returns None on any parse failure
    so the caller falls back to its exponential schedule. Caller is
    responsible for clamping to the cap.
    """
    try:
        raw = resp.headers.get("Retry-After")
    except Exception:
        return None
    if not raw:
        return None
    try:
        v = float(str(raw).strip())
        if v < 0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def _head_with_429_retry(
    url: str, timeout_s: float,
) -> tuple["requests.Response | None", str | None]:
    """HEAD probe with 429-aware retries and SSL fallback.

    Returns ``(response, error_reason)``. On success, ``response`` is the
    final HEAD response (which may itself be a 429 if all retries were
    exhausted) and ``error_reason`` is None. On a non-retryable transport
    failure (timeout, connect error, generic exception) returns
    ``(None, reason)`` and the caller propagates the reason directly.

    Why retry only on 429: 4xx/5xx other than 429 are real failures (404
    means gone, 410 is gone, 500 is broken). 429 means "you're going too
    fast" — a transient signal where backing off is the correct response.
    """
    last_resp: "requests.Response | None" = None
    # Total attempts = 1 + MAX_RETRIES.
    for attempt in range(_URL_VALIDATION_429_MAX_RETRIES + 1):
        try:
            resp = _validation_request("HEAD", url, timeout_s)
        except requests.exceptions.SSLError:
            # Same fallback as math_ku_phd.py — public posting URL, the
            # liveness signal is what matters, not chain completeness.
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
            try:
                resp = _validation_request("HEAD", url, timeout_s, verify=False)
            except requests.exceptions.Timeout:
                return (None, "timeout")
            except requests.exceptions.ConnectionError:
                return (None, "connect_error")
            except Exception as e:
                return (None, f"exception:{type(e).__name__}")
        except requests.exceptions.Timeout:
            return (None, "timeout")
        except requests.exceptions.ConnectionError:
            return (None, "connect_error")
        except Exception as e:
            return (None, f"exception:{type(e).__name__}")

        code = int(getattr(resp, "status_code", 0) or 0)
        last_resp = resp
        if code != 429:
            return (resp, None)

        # 429 path: short-circuit if we've spent our retry budget.
        if attempt >= _URL_VALIDATION_429_MAX_RETRIES:
            break

        # Backoff: honor Retry-After if present and sane, else
        # exponential (base, base*4). Cap at the configured ceiling so
        # one slow server can't wedge the digest.
        ra = _parse_retry_after(resp)
        if ra is not None:
            sleep_s = min(ra, _URL_VALIDATION_429_RETRY_AFTER_CAP_S)
        else:
            # attempt=0 → base; attempt=1 → base*4.
            sleep_s = URL_VALIDATION_429_BACKOFF_BASE_S * (4 ** attempt)
        # Defensive: never negative, never above the cap.
        sleep_s = max(0.0, min(sleep_s, _URL_VALIDATION_429_RETRY_AFTER_CAP_S))
        try:
            time.sleep(sleep_s)
        except Exception:
            pass

    return (last_resp, None)


def _url_is_alive(url: str, timeout_s: float = URL_VALIDATION_TIMEOUT_S) -> tuple[bool, str]:
    """Probe a posting URL right before sending it to the user.

    Returns ``(is_alive, reason)``. Treat 30x as alive — many ATS systems
    redirect to a canonical posting URL and the user's tap would follow.
    Falls back from HEAD → ranged GET when the server rejects HEAD (405/501),
    and from strict-TLS → ``verify=False`` on SSLError (some servers ship
    incomplete certificate chains; same defensive pattern as
    ``sources/math_ku_phd.py``).

    On HTTP 429 we retry up to ``_URL_VALIDATION_429_MAX_RETRIES`` times
    with exponential backoff (see ``_head_with_429_retry``). After all
    retries fail the reason becomes ``http_429_after_retries`` so forensic
    queries can distinguish "rate-limited and gave up" from "rate-limited
    on the very first try" (the LinkedIn anti-bot path below).

    All exceptions are caught — a single bad URL must never crash the
    digest. The reason string is small and stable so forensic queries can
    aggregate by it.
    """
    if not url or not isinstance(url, str):
        return (False, "empty_url")
    try:
        resp, err = _head_with_429_retry(url, timeout_s)
        if err is not None:
            return (False, err)
        # resp is non-None when err is None (invariant of the helper).
        assert resp is not None  # narrows for type-checkers
        code = int(getattr(resp, "status_code", 0) or 0)
        if code in _ALIVE_STATUSES:
            return (True, "ok")

        # LinkedIn (and a few other anti-bot CDNs) 429/403 our validator IP
        # while serving the page just fine to a real browser. The user's tap
        # would work; treat as alive with a distinct reason so forensic
        # queries can audit it. The investigation report on chat 433775883
        # confirmed 3 LinkedIn 429s where the body was a real listing page.
        if code in (401, 403, 429) and "linkedin.com" in (url or "").lower():
            return (True, f"linkedin_anti_bot_{code}")

        # 2) Server rejected HEAD — retry with a tiny ranged GET.
        if code in _HEAD_BLOCKED_STATUSES:
            try:
                ranged_headers = dict(_VALIDATION_HEADERS)
                ranged_headers["Range"] = "bytes=0-1023"
                try:
                    g = _validation_request(
                        "GET", url, timeout_s, headers=ranged_headers,
                    )
                except requests.exceptions.SSLError:
                    g = _validation_request(
                        "GET", url, timeout_s, headers=ranged_headers, verify=False,
                    )
                gcode = int(getattr(g, "status_code", 0) or 0)
                # 206 Partial Content is the expected reply to Range; treat
                # it as alive alongside the normal alive set.
                if gcode in _ALIVE_STATUSES or gcode == 206:
                    return (True, "head_method_blocked")
                if gcode in (404, 410, 451):
                    return (False, str(gcode))
                if 400 <= gcode < 600:
                    return (False, f"http_{gcode}")
                return (False, f"http_{gcode}")
            except Exception as e:
                return (False, f"exception:{type(e).__name__}")

        # 3) Hard-dead status codes: be specific so forensic queries can
        # bucket them. Everything else 4xx/5xx falls into http_<code>.
        if code in (404, 410, 451):
            return (False, str(code))
        # If we still see 429 here it means the retry loop exhausted its
        # budget. Distinct reason so forensic dashboards can split
        # "rate-limited and we gave up" from "rate-limited on first try"
        # (the LinkedIn anti-bot path returned True earlier).
        if code == 429:
            return (False, "http_429_after_retries")
        if 400 <= code < 600:
            return (False, f"http_{code}")
        # Unexpected status (e.g. 1xx / 0). Treat as not-alive.
        return (False, f"http_{code}")

    except requests.exceptions.Timeout:
        return (False, "timeout")
    except requests.exceptions.ConnectionError:
        return (False, "connect_error")
    except Exception as e:  # pragma: no cover — defensive catch-all
        return (False, f"exception:{type(e).__name__}")

# ---------- Age-window gate ----------
#
# Job feeds occasionally serve weeks-old listings (RSS caches, infrequent
# crawler refreshes, ATS systems re-publishing closed reqs). Sending a
# stale posting wastes a tap and trains users to ignore the digest. We
# parse `posted_at` right before send and drop anything older than
# ``MAX_JOB_AGE_DAYS`` (default 7). Sources that don't expose a date
# (LinkedIn, web_search) are admitted by default — `JOB_AGE_MISSING_POLICY`
# can flip that to "reject" if the operator wants strict gating.

MAX_JOB_AGE_DAYS: int = _env_int("MAX_JOB_AGE_DAYS", 7)
JOB_AGE_MISSING_POLICY: str = (
    os.environ.get("JOB_AGE_MISSING_POLICY", "allow").strip().lower() or "allow"
)
JOB_AGE_FILTER_OFF: bool = os.environ.get("JOB_AGE_FILTER_OFF", "").strip() not in (
    "", "0", "false", "False",
)


def _parse_posted_at(s: str) -> datetime | None:
    """Parse a free-form posted_at string into a tz-aware UTC datetime.

    Sources hand us mismatched formats:
      * ISO 8601 short date: ``"2026-04-30"`` (HackerNews, RemoteOK date)
      * ISO 8601 datetime, tz-aware or naive: ``"2026-04-25T12:00:00+00:00"`` /
        ``"2026-04-25T12:00:00Z"`` / ``"2026-04-25T12:00:00"`` (Remotive,
        Euraxess, ReliefWeb)
      * Unix epoch seconds: ``"1714512000"`` (some scraped sources)
      * RFC 2822: ``"Tue, 23 Apr 2026 12:00:00 +0000"`` (WeWorkRemotely feed)

    Naive datetimes are assumed to be UTC — pragmatic given the source
    landscape; ATS feeds rarely encode tz on the publish date. Returns
    None for empty / unparseable input so the caller can apply
    ``JOB_AGE_MISSING_POLICY``.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None

    # 1) Unix epoch seconds — pure-numeric strings only. Guards against
    #    accidentally consuming a year like "2024" (which would parse as
    #    1970-01-01 plus 33 minutes — clearly wrong). Length ≥ 9 covers
    #    any timestamp from ~1973 onward.
    if s.isdigit() and len(s) >= 9:
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass

    # 2) ISO 8601 (date or datetime). `fromisoformat` handles "2026-04-25",
    #    "2026-04-25T12:00:00", "2026-04-25T12:00:00+00:00". It does NOT
    #    accept the trailing "Z" prior to Python 3.11 — normalize first.
    iso_candidate = s
    if iso_candidate.endswith("Z"):
        iso_candidate = iso_candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # 3) RFC 2822 — `parsedate_to_datetime` returns naive when no tz
    #    information is present; coerce to UTC in that case.
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    return None


def _is_within_age_window(
    posted_at_str: str,
    max_days: int,
    missing_policy: str = "allow",
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the age gate.

    * ``(True, "ok")`` — parsed and within ``max_days``.
    * ``(False, "too_old:<N>d")`` — parsed but older than ``max_days``;
      ``N`` is the integer age in days, useful for forensic bucketing.
    * Missing / unparseable: respects ``missing_policy`` —
        - ``"allow"`` → ``(True, "missing_posted_at")``
        - ``"reject"`` → ``(False, "missing_posted_at")``

    Future-dated postings (clock skew, source bug) are treated as fresh
    (age clamped to 0). We never want to drop a fresh listing because a
    feed got the timezone wrong.
    """
    dt = _parse_posted_at(posted_at_str or "")
    if dt is None:
        if (missing_policy or "allow").lower() == "reject":
            return (False, "missing_posted_at")
        return (True, "missing_posted_at")
    now = datetime.now(timezone.utc)
    age_seconds = (now - dt).total_seconds()
    age_days = max(0, int(age_seconds // 86400))
    if age_days > int(max_days):
        return (False, f"too_old:{age_days}d")
    return (True, "ok")


# ---------- Forum / discussion-page URL filter ----------
#
# The web_search source (Claude sub-agent doing open-web discovery) sometimes
# returns URLs pointing at a FORUM or COMMENT THREAD instead of a real job
# posting — e.g. a Reddit /r/cscareerquestions thread, a Hacker News comments
# page, a Twitter status, or a github.com/<org>/<repo>/issues/123. Those URLs
# are alive (so the liveness gate passes) but they're not what the user
# wanted. We reject them at send-time using a blocklist of known
# discussion-style hosts plus a small set of path patterns.
#
# IMPORTANT: legitimate sources whose URLs are EXPECTED to be discussion-style
# by design (the canonical case being `hackernews` — every "Who is hiring?"
# job's URL points to news.ycombinator.com/item?id=...) are exempted by
# `job_source`, NOT by host. The exemption is source-scoped so a web_search
# leak that happens to hit news.ycombinator.com is still rejected.

_FORUM_HOST_BLOCKLIST_DEFAULT: frozenset[str] = frozenset({
    "reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com",
    "news.ycombinator.com", "ycombinator.com",
    "twitter.com", "x.com", "mobile.twitter.com",
    "stackoverflow.com", "stackexchange.com", "superuser.com",
    "github.com",  # path-conditional — see _url_is_real_posting
    "medium.com", "dev.to", "substack.com", "levels.fyi",
    "quora.com",
    "discord.com", "t.me",
})


def _load_forum_host_blocklist() -> frozenset[str]:
    """Read the blocklist from env (additive by default; replace with `=`).

    `FORUM_HOST_BLOCKLIST` env var:
      - empty/unset → use _FORUM_HOST_BLOCKLIST_DEFAULT.
      - "host1,host2" → ADD host1+host2 to the default set.
      - "=host1,host2" (leading equals) → REPLACE the default with these.
    """
    raw = os.environ.get("FORUM_HOST_BLOCKLIST", "").strip()
    if not raw:
        return _FORUM_HOST_BLOCKLIST_DEFAULT
    if raw.startswith("="):
        items = {h.strip().lower() for h in raw[1:].split(",") if h.strip()}
        return frozenset(items)
    extras = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return frozenset(_FORUM_HOST_BLOCKLIST_DEFAULT | extras)


_FORUM_HOST_BLOCKLIST: frozenset[str] = _load_forum_host_blocklist()

# Sources whose Job URLs are LEGITIMATELY discussion-style by design. A job
# from one of these sources is exempted from forum-host filtering even if
# the host appears in the blocklist. Keep this set tight — anything new we
# add here MUST guarantee its URLs are real apply targets, not random
# threads.
_FORUM_SOURCE_EXEMPT: frozenset[str] = frozenset({
    "hackernews",
})

# Generic prefix match: any host starting with "forums." (e.g. forums.adobe.com)
# is treated as a forum regardless of the rest of the domain.
_FORUM_HOST_PREFIXES: tuple[str, ...] = ("forums.",)

# Compiled path patterns matched against URL.path (case-insensitive). Hits
# any of these → reject. Order isn't load-bearing; the first match wins.
_FORUM_PATH_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("/comments/",       re.compile(r"/comments?/", re.IGNORECASE)),
    ("/discuss/",        re.compile(r"/discuss(ion)?s?/", re.IGNORECASE)),
    ("/threads/",        re.compile(r"/threads?/", re.IGNORECASE)),
    ("/r/<sub>",         re.compile(r"/r/[^/]+/?$", re.IGNORECASE)),
    ("/issues/<n>",      re.compile(r"/issues?/\d+", re.IGNORECASE)),
    ("/forum/",          re.compile(r"/forum/", re.IGNORECASE)),
    ("/topics/",         re.compile(r"/topics?/", re.IGNORECASE)),
]

FORUM_FILTER_OFF: bool = os.environ.get("FORUM_FILTER_OFF", "").strip() not in (
    "", "0", "false", "False",
)


def _host_matches_blocklist(host: str) -> str | None:
    """Return the matched blocklist entry (str) if `host` is on the blocklist
    or a subdomain of one of its entries; otherwise None.

    Subdomain match: `careers.reddit.com` matches `reddit.com` because the
    parent domain is in the blocklist. We walk parent labels rather than
    relying on naive endswith — that avoids `notreddit.com` matching
    `reddit.com`.
    """
    if not host:
        return None
    host = host.lower()
    # Direct match
    if host in _FORUM_HOST_BLOCKLIST:
        return host
    # Generic prefix match (e.g. forums.adobe.com)
    for prefix in _FORUM_HOST_PREFIXES:
        if host.startswith(prefix):
            return f"prefix:{prefix}"
    # Parent-domain match: split labels and walk upward.
    labels = host.split(".")
    for i in range(1, len(labels) - 1):
        parent = ".".join(labels[i:])
        if parent in _FORUM_HOST_BLOCKLIST:
            return parent
    return None


def _github_path_is_allowed(path: str) -> bool:
    """github.com special-case: allow `/<org>/<repo>/blob/...` (often used as
    a careers page) but reject `/issues/...` and `/discussions/...`. Anything
    else (homepage, /<org>/<repo> root, /<org>/<repo>/tree/...) is
    conservatively rejected — career listings on github tend to live at
    /blob/ paths to a markdown file.
    """
    p = (path or "").lower()
    # Reject explicitly-discussion paths first.
    if re.search(r"/issues?(/|$)", p) or re.search(r"/discussions?(/|$)", p):
        return False
    # Allow blob/raw paths (typical for CAREERS.md / HIRING.md).
    if re.search(r"/(blob|raw)/", p):
        return True
    return False


def _url_is_real_posting(url: str, job_source: str) -> tuple[bool, str]:
    """Return (is_real, reason). Identifies forum / discussion URLs that
    leaked through despite passing liveness validation.

    - Sources in `_FORUM_SOURCE_EXEMPT` (e.g. hackernews) bypass the filter
      entirely with reason="source_exempt".
    - Host-blocklist match → (False, f"forum_host:{matched}").
    - github.com is special-cased: /blob/ allowed; /issues/ and /discussions/
      rejected as forum_host:github.com (they're discussion threads).
    - Path-pattern match → (False, f"forum_path:{pattern}").
    - Otherwise → (True, "ok").
    """
    if (job_source or "").lower() in _FORUM_SOURCE_EXEMPT:
        return (True, "source_exempt")
    if not url or not isinstance(url, str):
        # Empty URL: liveness gate already handled this; defer to ok so we
        # don't double-report. If a caller wired this in without a liveness
        # gate, an empty URL would also fail send anyway.
        return (True, "ok")
    try:
        parsed = urlparse(url)
    except Exception:
        return (True, "ok")
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    matched = _host_matches_blocklist(host)
    if matched is not None:
        # github.com gets a path-conditional carve-out for /blob/ pages.
        # Any other matched host is rejected outright.
        if matched == "github.com" or host == "github.com":
            if _github_path_is_allowed(path):
                # Still apply the generic path-pattern check below — a
                # /blob/.../issues/123 path would be weird but we'd still
                # want to reject it. Falls through.
                pass
            else:
                return (False, f"forum_host:{host}")
        else:
            return (False, f"forum_host:{host}")

    for label, pat in _FORUM_PATH_PATTERNS:
        if pat.search(path):
            return (False, f"forum_path:{label}")

    return (True, "ok")


# ---------- MarkdownV2 helpers ----------

_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_RE = re.compile(f"([{re.escape(_MDV2_SPECIALS)}])")


def mdv2_escape(text: str) -> str:
    if not text:
        return ""
    return _MDV2_RE.sub(r"\\\1", text)


# Shared icon set. Kept small and functional — no decorative emoji. Each
# symbol has one consistent meaning across the bot so users learn to
# pattern-match after a couple of digests.
ICON_JOB       = "💼"   # role / posting
ICON_LOCATION  = "📍"
ICON_SALARY    = "💰"
ICON_COMPANY   = "🏢"
ICON_REMOTE    = "🌐"   # remote policy
ICON_STACK     = "⚙️"   # tech stack
ICON_SENIORITY = "📊"   # level
ICON_LANGUAGE  = "🗣"
ICON_VISA      = "🛂"
ICON_APPLIED   = "✅"
ICON_SKIPPED   = "⊘"    # lighter than 🚫 for the "not-applied" tag
ICON_NEW       = "•"    # neutral bullet, used for fresh-post emphasis


def _score_bar(score: int, cells: int = 5) -> str:
    """Return a horizontal match-score bar.

    Uses block elements ('▰' filled, '▱' empty) instead of stars — reads more
    like a progress/rating widget and less like a kids' game. Accepts any int;
    clamps to [0, cells].
    """
    n = max(0, min(cells, int(score)))
    return "▰" * n + "▱" * (cells - n)


# ---------- onboarding / layout primitives ----------

def progress_dots(step: int, total: int) -> str:
    """Render a 'Step N of M' progress indicator as filled/empty dots.

    Example: progress_dots(3, 6) → '●●●○○○  Step 3 of 6'. Cheap, scannable,
    no external assets. Clamps to sane bounds so off-by-one callers don't
    produce visual nonsense.
    """
    total = max(1, int(total))
    step = max(0, min(total, int(step)))
    dots = "●" * step + "○" * (total - step)
    return f"{dots}  Step {step} of {total}"


def hr_mdv2() -> str:
    """Horizontal rule made of MDv2-safe chars. Useful as a section divider."""
    return "─" * 22


def section_header_mdv2(title: str, subtitle: str | None = None) -> str:
    """Render a two-line section header: bold title + optional italic subtitle.

    Both lines are MDv2-escaped here so callers can hand in raw text.
    """
    out = [f"*{mdv2_escape(title)}*"]
    if subtitle:
        out.append(f"_{mdv2_escape(subtitle)}_")
    return "\n".join(out)


def chip_line_mdv2(chips: Iterable[tuple[str, str]]) -> str:
    """Render a row of 'icon  text' chips joined by ' · '.

    Each chip is a (icon, text) tuple. Empty/whitespace text values are
    dropped. The text is MDv2-escaped; the icon is emitted verbatim.
    """
    parts: list[str] = []
    for icon, text in chips:
        s = (text or "").strip()
        if not s:
            continue
        parts.append(f"{icon} {mdv2_escape(s)}")
    return "  ·  ".join(parts)


def _render_key_details_mdv2(d: dict) -> list[str]:
    """Compact two-line 'chip' block for the key_details dict, MDV2-escaped.

    Group 1 (role signal):  stack · seniority · remote policy
    Group 2 (logistics):    location · salary · visa · language

    Each group renders as one ' · '-joined line; empty fields drop out. Groups
    with zero surviving chips are omitted entirely so short cards stay short.
    The old per-field emoji list produced 5-8 lines of visual noise — this
    brings it down to ≤2 lines while keeping the same information density.
    """
    if not isinstance(d, dict):
        return []
    out: list[str] = []

    role_chips = [
        (ICON_STACK,     d.get("stack")),
        (ICON_SENIORITY, d.get("seniority")),
        (ICON_REMOTE,    d.get("remote_policy")),
    ]
    log_chips = [
        (ICON_LOCATION, d.get("location")),
        (ICON_SALARY,   d.get("salary")),
        (ICON_VISA,     _visa_label(d.get("visa_support"))),
        (ICON_LANGUAGE, d.get("language")),
    ]
    row1 = chip_line_mdv2((icon, (val or "")[:80]) for icon, val in role_chips)
    row2 = chip_line_mdv2((icon, (val or "")[:80]) for icon, val in log_chips)
    if row1:
        out.append(row1)
    if row2:
        out.append(row2)
    # Standout gets its own italic line if present — it's a free-text pitch,
    # not a tag, so it doesn't belong in the chip row.
    standout = (d.get("standout") or "").strip()
    if standout:
        out.append("_" + mdv2_escape(standout[:200]) + "_")
    return out


def _visa_label(v) -> str:
    s = (v or "").strip().lower()
    return {"yes": "visa support", "no": "no visa support"}.get(s, "")


def format_job_mdv2(
    job: Job,
    include_snippet: bool = True,
    snippet_chars: int = 240,
    applied_status: str | None = None,
    enrichment: dict | None = None,
) -> str:
    """Render one job as a Telegram MarkdownV2 card.

    If `enrichment` is provided (from job_enrich.enrich_jobs_ai), we render:
      - a ⭐ score bar right under the title
      - a resume-aware `why_match` line
      - a compact list of key_details (stack, seniority, remote, salary, …)

    Without enrichment the card falls back to title / company / snippet / source.
    """
    title = mdv2_escape(job.title or "Untitled role")
    company = mdv2_escape(job.company or "Unknown company")
    location = mdv2_escape(job.location or "")
    salary = mdv2_escape(job.salary or "")
    source = mdv2_escape(job.source)
    url = (job.url or "").replace(")", "\\)").replace("(", "\\(")

    # Line 1 — prominent role title (linked).
    lines = [f"*[{title}]({url})*"]
    # Line 2 — company · location [· salary]. Drop salary here when enrichment
    # is present because it re-appears in the logistics chip row.
    meta_bits = [company]
    if location:
        meta_bits.append(location)
    if salary and not enrichment:
        meta_bits.append(salary)
    lines.append("  ·  ".join(meta_bits))

    # Match score + resume-aware rationale come next so users can triage in
    # one glance. The score bar uses block elements (▰▱) so it reads like a
    # progress bar rather than a star rating.
    if enrichment:
        score = int(enrichment.get("match_score") or 0)
        lines.append("")
        lines.append(f"{_score_bar(score)}  *{score}/5 match*")
        why = (enrichment.get("why_match") or "").strip()
        if why:
            lines.append("_" + mdv2_escape(why[:260]) + "_")
        detail_lines = _render_key_details_mdv2(enrichment.get("key_details") or {})
        if detail_lines:
            lines.append("")
            lines.extend(detail_lines)

    if include_snippet and job.snippet:
        snip = job.snippet.strip()
        if len(snip) > snippet_chars:
            snip = snip[:snippet_chars].rstrip() + "…"
        lines.append("")
        lines.append(f"_{mdv2_escape(snip)}_")

    # Status chip + source footer. "Applied" / "Skipped" use the canonical
    # icon pair from the top of the module; "Saved" stays as a plain
    # star-flavoured tag. Source lives in monospace so it reads as metadata,
    # not body text.
    badge_map = {
        "applied":    f"{ICON_APPLIED} *Applied*",
        "skipped":    f"{ICON_SKIPPED} *Skipped*",
        "interested": "★ *Saved*",
    }
    badge = badge_map.get(applied_status or "", "")
    footer = f"`via {source}`"
    if badge:
        footer = f"{badge}  ·  {footer}"
    lines.append("")
    lines.append(footer)
    return "\n".join(lines)


# ---------- Inline keyboards ----------

def job_keyboard(job_id: str, applied_status: str | None = None, url: str | None = None) -> dict:
    """Build the inline keyboard under each job message.

    callback_data is capped at 64 bytes; our job_id is 16 hex chars → plenty of room.
    Prefixes:
        a:<job_id>  → mark applied
        n:<job_id>  → mark not applied / skipped
        r:<job_id>  → rewrite resume for this position

    The top row is a direct URL button (Telegram opens the posting in-browser).
    """
    rows: list[list[dict]] = []
    if url:
        rows.append([{"text": "View posting ↗", "url": url}])
    if applied_status == "applied":
        status_row = [{"text": "✓ Applied", "callback_data": f"n:{job_id}"}]
    elif applied_status == "skipped":
        status_row = [{"text": "⊘ Skipped", "callback_data": f"a:{job_id}"}]
    else:
        status_row = [
            {"text": "✓ Applied", "callback_data": f"a:{job_id}"},
            {"text": "⊘ Not a fit", "callback_data": f"n:{job_id}"},
        ]
    rows.append(status_row)
    # Two AI actions on one row — paired because they tackle the same job
    # but serve different intents:
    #   fit:<id>  → evaluate alignment & surface gaps (this doesn't rewrite anything)
    #   r:<id>    → produce a tailored resume draft
    # Keeping them adjacent makes the tradeoff visible: read before rewrite.
    rows.append([
        {"text": "Analyze fit →",   "callback_data": f"fit:{job_id}"},
        {"text": "Tailor resume →", "callback_data": f"r:{job_id}"},
    ])
    return {"inline_keyboard": rows}


def min_score_keyboard(current: int = 0) -> dict:
    """Inline keyboard for picking the minimum match score (0 = any, 1..5 = gate).

    Callback data shape:  ms:<n>   where n ∈ {0, 1, 2, 3, 4, 5}

    The currently-selected tier is marked with a dot in its label so the user
    can see what they already have. The layout is two rows of three so it
    stays readable on narrow phone screens.
    """
    def _lbl(n: int) -> str:
        # Use '●' as the "selected" marker so the UI stays tonally consistent
        # with the onboarding progress dots. The empty/filled block pattern
        # doubles as a miniature score bar so users can see the tier at a
        # glance without reading the number.
        marker = "● " if n == current else ""
        if n == 0:
            return f"{marker}Any"
        return f"{marker}{n}+  {_score_bar(n)}"

    rows = [
        [{"text": _lbl(0), "callback_data": "ms:0"},
         {"text": _lbl(1), "callback_data": "ms:1"},
         {"text": _lbl(2), "callback_data": "ms:2"}],
        [{"text": _lbl(3), "callback_data": "ms:3"},
         {"text": _lbl(4), "callback_data": "ms:4"},
         {"text": _lbl(5), "callback_data": "ms:5"}],
    ]
    return {"inline_keyboard": rows}


# Clean-my-data cleanup categories. Kept here (rather than in bot.py) so the
# inline keyboard and the handler reference the same canonical labels +
# callback codes. Ordered for rendering — lightest first, destructive "all"
# last. The code value ends up in the callback_data after the `cd:` / `cdc:`
# prefix, so keep it URL-safe and short (Telegram caps callback_data at 64
# bytes).
CLEAN_DATA_KINDS: tuple[tuple[str, str, str], ...] = (
    # (code,       emoji + label,               one-line description — used on confirm screen)
    ("resume",    "📄 Resume",                  "Your uploaded CV (PDF + extracted text)."),
    ("history",   "📋 Job history",             "Applied/skipped marks + digest sent-log."),
    ("tailored",  "✍️ Tailored resumes",        "Markdown files from ✍️ Tailor my resume."),
    ("profile",   "🤖 Profile",                 "Your /prefs free-text + AI-built profile + ⭐ min-score."),
    ("research",  "🔬 Research",                "Market-research history and saved .docx files."),
    ("all",       "⚠️ Everything",              "Full wipe — starts you back at /start."),
)


def clean_data_menu_keyboard() -> dict:
    """Inline keyboard for the 🧹 Clean my data menu.

    Layout: two-per-row for the five scoped options, then the destructive
    "Everything" on its own row (visual separation — tapping it shouldn't
    feel like a small-adjustment button), then a Cancel row.

    Callback data shape:  cd:<code>  where <code> is one of CLEAN_DATA_KINDS.
    """
    scoped = [k for k in CLEAN_DATA_KINDS if k[0] != "all"]
    all_btn = next(k for k in CLEAN_DATA_KINDS if k[0] == "all")

    rows: list[list[dict]] = []
    for i in range(0, len(scoped), 2):
        pair = scoped[i:i + 2]
        rows.append([
            {"text": label, "callback_data": f"cd:{code}"}
            for (code, label, _desc) in pair
        ])
    # Destructive row — alone, with the warning emoji the label carries.
    rows.append([{"text": all_btn[1], "callback_data": f"cd:{all_btn[0]}"}])
    rows.append([{"text": "✖️ Cancel", "callback_data": "cdx:"}])
    return {"inline_keyboard": rows}


def clean_data_confirm_keyboard(kind: str) -> dict:
    """Inline keyboard for the second-step confirm after picking a category.

    Callback data shape:
        cdc:<code>  → execute the deletion
        cdx:        → cancel / back to menu
    """
    return {"inline_keyboard": [[
        {"text": "✅ Yes, delete",   "callback_data": f"cdc:{kind}"},
        {"text": "✖️ Cancel",        "callback_data": "cdx:"},
    ]]}


def suggestions_keyboard(job_id: str, url: str | None = None,
                         decided: str | None = None) -> dict:
    """Inline keyboard for the tailor-suggestions dialog.

    Callback data prefixes:
        ra:<job_id>  → user accepted; attach the tailored resume file
        rd:<job_id>  → user dismissed the suggestions
    """
    rows: list[list[dict]] = []
    if url:
        rows.append([{"text": "🔗 View posting", "url": url}])
    if decided == "applied":
        rows.append([{"text": "✅ Applied — see attachment", "callback_data": f"noop:{job_id}"}])
    elif decided == "dismissed":
        rows.append([{"text": "✖️ Dismissed", "callback_data": f"noop:{job_id}"}])
    else:
        rows.append([
            {"text": "✅ Apply", "callback_data": f"ra:{job_id}"},
            {"text": "✖️ Dismiss", "callback_data": f"rd:{job_id}"},
        ])
    return {"inline_keyboard": rows}


# Per-change-type emoji. Keys are normalized lower-case verbs.
_CHANGE_EMOJI = {
    "add":      "➕",
    "remove":   "➖",
    "rephrase": "✏️",
    "reorder":  "🔀",
    "reframe":  "🎯",
    "rewrite":  "✏️",
    "edit":     "✏️",
    "update":   "✏️",
}
_DEFAULT_CHANGE_EMOJI = "✏️"
_RULE = "─" * 22  # visual divider between suggestions — contains no MDv2-reserved chars


def _change_emoji(change: str) -> str:
    return _CHANGE_EMOJI.get((change or "").strip().lower(), _DEFAULT_CHANGE_EMOJI)


def _balance_mdv2_entities(text: str) -> str:
    """Strip UNESCAPED trailing `*`, `_`, backtick tokens if their count is odd.

    Telegram rejects MDv2 messages with an unterminated bold/italic/code entity.
    If we had to truncate mid-block, we may be left with an odd number of a
    given marker — walk the text, count only unescaped occurrences, and if the
    count is odd, strip the LAST unescaped occurrence. Escaped markers (`\\*`,
    `\\_`, `\\\\``) don't open/close entities so they don't count.

    This is a last-resort safety net; the primary mitigation is truncating at
    `_RULE` boundaries (which contain no reserved chars) so entities can't be
    split in the first place.
    """
    for tok in ("*", "_", "`"):
        # Find positions of UNESCAPED tok (preceding char is not a backslash).
        positions: list[int] = []
        for i, ch in enumerate(text):
            if ch != tok:
                continue
            if i > 0 and text[i - 1] == "\\":
                continue
            positions.append(i)
        if len(positions) % 2 == 1:
            last = positions[-1]
            text = text[:last] + text[last + 1:]
    return text


def render_suggestions_mdv2(job, plan: dict, max_chars: int = 3500) -> str:
    """Render the AI plan as a MarkdownV2 dialog body.

    Goal: make this scannable. Three-line header, one suggestion block per
    change, horizontal separators between blocks, and emoji-tagged labels for
    Current / Suggested / Why so the user can pattern-match without reading
    every word.

    The full rewritten resume is intentionally omitted — it arrives as a
    sendDocument after the user clicks Apply. Truncates to stay under
    Telegram's 4096-char cap for editMessageText.
    """
    title = mdv2_escape(job.title or "Role")
    company = mdv2_escape(job.company or "")

    # Three-line header: prominent, easy to skim.
    lines: list[str] = [
        "🎯 *Tailor plan*",
        f"📄 *{title}*",
    ]
    if company:
        lines.append(f"🏢 {company}")
    lines.append("")

    summary = (plan.get("summary") or "").strip()
    if summary:
        lines.append("💬 _" + mdv2_escape(summary) + "_")
        lines.append("")

    suggestions = plan.get("suggestions") or []
    if not suggestions:
        lines.append("✨ " + mdv2_escape("Your resume already aligns — no concrete edits suggested."))
    else:
        n = len(suggestions)
        plural = "s" if n != 1 else ""
        lines.append(f"📝 *{n} suggested change{plural}*")
        lines.append("")
        for i, s in enumerate(suggestions, 1):
            section = str(s.get("section") or "").strip() or "Resume"
            change = str(s.get("change") or "Rephrase").strip() or "Rephrase"
            emoji = _change_emoji(change)
            # Block header: "─────  1. Experience  ➕ Add"
            lines.append(f"{_RULE}")
            lines.append(
                f"*{i}\\.* *{mdv2_escape(section)}*  ·  {emoji} _{mdv2_escape(change)}_"
            )

            before = (s.get("before") or "").strip()
            after = (s.get("after") or "").strip()
            why = (s.get("why") or "").strip()

            if before:
                lines.append("")
                lines.append("❌ *Current*")
                lines.append("> " + mdv2_escape(before[:280]))
            if after:
                lines.append("")
                lines.append("✅ *Suggested*")
                lines.append("> *" + mdv2_escape(after[:280]) + "*")
            if why:
                lines.append("")
                lines.append("💡 _" + mdv2_escape(why[:240]) + "_")
            lines.append("")
        lines.append(_RULE)
        lines.append("")

    lines.append("👇 " + mdv2_escape("Tap ✅ Apply to receive the rewritten resume as a file."))
    body = "\n".join(lines)
    if len(body) > max_chars:
        # Safe-boundary truncation: slice at the last `_RULE` (the horizontal
        # separator between suggestion blocks). Since `_RULE` contains no
        # MDv2-reserved chars, we're guaranteed the prefix has no mid-entity
        # cut. Leave room for a trailing note + the closing footer.
        note = "…\n\n" + mdv2_escape(
            "Plan trimmed to fit Telegram's size limit — the full rewritten "
            "resume will still include every change when you tap ✅ Apply."
        )
        room = max_chars - len(note) - 16
        cutoff = body.rfind(_RULE, 0, room)
        if cutoff > 0:
            body = body[:cutoff].rstrip() + "\n\n" + note
        else:
            # Fallback: cut at the last newline before `room` so at least we
            # don't split a line in half, then let the balancer fix stragglers.
            nl = body.rfind("\n", 0, room)
            body = body[: nl if nl > 0 else room].rstrip() + "\n\n" + note
        # Defensive: if any odd-count bold/italic/code marker survived, strip it.
        body = _balance_mdv2_entities(body)
    return body


# ---------- Client ----------

class TelegramClient:
    def __init__(self, token: str, timeout: int = 20):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, payload: dict | None = None, files: dict | None = None,
              http_timeout: int | None = None) -> dict:
        url = API_BASE.format(token=self.token, method=method)
        effective_timeout = http_timeout if http_timeout is not None else self.timeout
        chat_id = _extract_chat_id(payload)

        # Lazy import — avoid a hard dependency in environments where forensic
        # is unavailable (e.g. unit tests stubbing telegram_client out).
        try:
            import forensic as _forensic
        except Exception:  # pragma: no cover
            _forensic = None

        # Acquire rate-limit tokens before issuing the HTTP request. Skipped
        # entirely when TG_RATE_LIMIT_OFF=1 — the static-pacing fallback path.
        if not TG_RATE_LIMIT_OFF:
            slept_global, slept_chat = _RATE_LIMITER.acquire(chat_id)
            # Only emit forensic when blocking actually mattered (>100ms).
            # The check covers either bucket — whichever caused the wait wins
            # the `reason` label so dashboards can bucket by cause.
            total_slept = slept_global + slept_chat
            if _forensic is not None and total_slept > 0.1:
                if slept_chat >= slept_global:
                    reason = "per_chat_bucket"
                else:
                    reason = "global_bucket"
                _forensic.log_step(
                    "telegram.rate_limit",
                    input={"chat_id": chat_id, "method": method},
                    output={
                        "slept_ms": int(total_slept * 1000),
                        "reason": reason,
                    },
                    chat_id=chat_id if isinstance(chat_id, int) else None,
                )

        # Up to 2 attempts: the second one only fires if the first hit a 429.
        # Any other error (4xx, 5xx, timeout, JSON parse) surfaces immediately.
        # Token-leak guard: ProxyError / ConnectionError tracebacks include
        # the full URL, which contains the bot token. Catch + re-raise with a
        # token-redacted message so log scrapers / shared screenshots don't
        # leak the secret. The original exception is logged at debug for
        # local diagnosis.
        attempts = 0
        while True:
            attempts += 1
            try:
                if files:
                    resp = requests.post(url, data=payload or {}, files=files, timeout=effective_timeout)
                else:
                    resp = requests.post(url, json=payload or {}, timeout=effective_timeout)
            except (requests.exceptions.ProxyError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                log.debug("telegram %s network error (full): %r", method, e)
                msg = str(e)
                # Belt + suspenders: scrub the token even if it slipped into
                # the exception's args via some intermediary's repr().
                if self.token:
                    msg = msg.replace(self.token, "<TOKEN_REDACTED>")
                # Strip the /bot.../ path entirely from any URL in the message.
                msg = re.sub(r"/bot[A-Za-z0-9_:.-]+/", "/bot<REDACTED>/", msg)
                raise RuntimeError(
                    f"Telegram {method} network error: {msg[:300]}"
                ) from None

            # Telegram returns 200 with `ok: false` for most logical errors;
            # 429 is the one case where the HTTP status itself signals
            # "back off". Some intermediaries also raise on 429 via
            # raise_for_status(), so we check both code and parsed body.
            status = getattr(resp, "status_code", 0)
            try:
                data = resp.json()
            except Exception:
                if status == 429 and attempts == 1:
                    # No JSON body — fall back to default retry_after.
                    retry_after = 5.0
                    self._sleep_after_429(retry_after, method, chat_id, _forensic)
                    continue
                raise RuntimeError(f"Telegram {method} non-JSON response: {resp.text[:200]}")

            error_code = data.get("error_code")
            is_429 = status == 429 or error_code == 429
            if is_429 and attempts == 1:
                params = data.get("parameters") or {}
                retry_after = float(params.get("retry_after") or 5.0)
                self._sleep_after_429(retry_after, method, chat_id, _forensic)
                continue

            if not data.get("ok"):
                raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
            return data.get("result", {})

    @staticmethod
    def _sleep_after_429(
        retry_after: float, method: str, chat_id: Any | None, forensic_mod,
    ) -> None:
        """Sleep `retry_after + jitter` and record a forensic line.

        The +0.5s jitter keeps multiple workers from synchronising their
        retries at the exact same boundary (thundering-herd guard); we add
        a small random nudge on top so two clients hitting 429 in lockstep
        diverge on their second attempt.
        """
        jitter = 0.5 + random.uniform(0.0, 0.25)
        wait = max(0.0, float(retry_after)) + jitter
        if forensic_mod is not None:
            forensic_mod.log_step(
                "telegram.rate_limit",
                input={"chat_id": chat_id, "method": method},
                output={
                    "slept_ms": int(wait * 1000),
                    "reason": "429_retry_after",
                },
                chat_id=chat_id if isinstance(chat_id, int) else None,
            )
        log.warning(
            "Telegram %s hit 429; sleeping %.2fs (retry_after=%.2f)",
            method, wait, retry_after,
        )
        time.sleep(wait)

    # ----- sending -----

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = self._call("sendMessage", payload)
        return int(res.get("message_id", 0))

    def send_plain(self, chat_id: int | str, text: str) -> int:
        return self.send_message(chat_id, text, parse_mode="")

    def send_document(
        self,
        chat_id: int | str,
        path: Path,
        caption: str | None = None,
    ) -> int:
        path = Path(path)
        with path.open("rb") as f:
            files = {"document": (path.name, f)}
            payload = {"chat_id": str(chat_id)}
            if caption:
                payload["caption"] = caption
            res = self._call("sendDocument", payload, files=files)
        return int(res.get("message_id", 0))

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", payload)

    def edit_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: dict | None) -> None:
        self._call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        })

    def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        """Delete a message the bot sent. Best-effort.

        Telegram only lets bots delete their own messages within the last 48
        hours. After that — or if the chat/message is otherwise unreachable —
        the API returns a BadRequest we swallow here, returning False so
        callers can fall back to e.g. clearing the reply keyboard.

        Returns True on success, False on any failure (older than 48h, already
        deleted, chat not found, network error, etc). Deletion is purely
        cosmetic so we never want it to take down the callback handler.
        """
        try:
            self._call("deleteMessage", {
                "chat_id": chat_id,
                "message_id": message_id,
            })
            return True
        except RuntimeError as e:
            # _call wraps Telegram's `description` into the RuntimeError msg.
            # Common shapes: "message can't be deleted for everyone",
            # "message to delete not found", "chat not found",
            # "MESSAGE_ID_INVALID". All best-effort failures.
            log.info("delete_message swallowed: %s", e)
            return False
        except Exception as e:  # pragma: no cover — network / JSON errors
            log.warning("delete_message unexpected error: %s", e)
            return False

    def answer_callback(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        self._call("answerCallbackQuery", payload)

    # ----- receiving (used by bot.py) -----

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        """Long-poll for updates. The HTTP read timeout MUST be longer than the
        long-poll timeout — otherwise the client gives up before Telegram has
        anything to return.
        """
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        # +10s buffer for the server's own request processing.
        res = self._call("getUpdates", payload, http_timeout=timeout + 10)
        return res if isinstance(res, list) else []

    def get_file_path(self, file_id: str) -> str:
        res = self._call("getFile", {"file_id": file_id})
        return res["file_path"]

    def download_file(self, file_path: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        return dest


# ---------- High-level digest helpers (for search_jobs.py) ----------

def digest_header_mdv2() -> str:
    today = mdv2_escape(time.strftime("%A, %d %B %Y"))
    # Two lines: primary heading + date subtitle. Reads cleaner than one
    # dense dash-joined line on narrow phone screens.
    return f"*Daily Job Digest*\n_{today}_"


def digest_header_keyboard(
    run_id: int | None,
    current_floor: int,
    lower_count: int = 0,
) -> dict | None:
    """Inline keyboard for the daily digest header.

    Two buttons max: ⬇ one step lower (with admit count) and ⬆ one step higher.
    The lower button is hidden at floor 0 or when no cached unsent job sits at
    or above floor-1. The raise button is hidden at floor 5. With no buttons
    left, returns None so callers can omit `reply_markup` entirely.

    Callback data:
      flt:lwr:<run_id>:<new_floor>   → re-send unsent cached jobs with score
                                        ≥ new_floor (inclusive-upward — any
                                        late-arriving higher-tier rows also
                                        replay)
      flt:rse:<new_floor>            → bump profile floor for next run only
    """
    rows: list[list[dict]] = []
    cur = max(0, min(5, int(current_floor)))
    btns: list[dict] = []
    if cur > 0 and run_id is not None and lower_count > 0:
        new_floor = cur - 1
        label = f"⬇ ≥{new_floor} (+{int(lower_count)})"
        btns.append({"text": label, "callback_data": f"flt:lwr:{int(run_id)}:{new_floor}"})
    if cur < 5:
        new_floor = cur + 1
        label = f"⬆ ≥{new_floor}"
        btns.append({"text": label, "callback_data": f"flt:rse:{new_floor}"})
    if not btns:
        return None
    rows.append(btns)
    return {"inline_keyboard": rows}


# ---------- Sort priority for per-job digest ----------
#
# Tier map ranks sources by "user-perceived relevance": agent-shaped
# findings (web_search) and direct-from-employer feeds beat broad
# firehose listings. Lower number = higher priority. Sources not listed
# fall back to ``SOURCE_TIER_DEFAULT`` so a new source doesn't sort
# above the curated ones until we explicitly tier it.
SOURCE_TIER: dict[str, int] = {
    "web_search":      0,
    "linkedin":        1,
    "euraxess":        2,
    "reliefweb":       3,
    "remotive":        4,
    "remoteok":        5,
    "weworkremotely":  6,
    "hackernews":      7,
}
SOURCE_TIER_DEFAULT = 99


def _sort_key_with_enrichments(job: Job, enrichments: dict[str, dict]) -> tuple:
    """Build a deterministic sort key honoring match score, freshness,
    source tier, and finally job_id as a stable tie-break.

    Used with ``sorted(...)`` (ascending). Under ascending order:
      * higher match_score sorts LAST  (None → -1 sinks to the front)
      * fresher posted_at sorts LAST   (lex compare on ISO YYYY-MM-DD…)
      * lower source-tier number sorts LAST → we negate so smaller tier
        (web_search=0) becomes larger negative-tier (0) and lands later
      * job_id ascending — purely for determinism
    """
    enr = enrichments.get(job.job_id) or {}
    raw_score = enr.get("match_score")
    try:
        score = int(raw_score) if raw_score is not None else -1
    except (TypeError, ValueError):
        score = -1
    posted_at = job.posted_at or ""
    tier = SOURCE_TIER.get((job.source or "").lower(), SOURCE_TIER_DEFAULT)
    # Negate tier so ascending sort puts low-numbered (better) tiers LAST.
    return (score, posted_at, -tier, job.job_id)


def _sort_key_no_enrichments(job: Job) -> tuple:
    """Fallback sort key when AI enrichment is missing.

    Priority (best LAST under ascending sort):
      source tier → posted_at → job_id.
    """
    tier = SOURCE_TIER.get((job.source or "").lower(), SOURCE_TIER_DEFAULT)
    return (-tier, job.posted_at or "", job.job_id)


def sort_jobs_for_digest(
    jobs: list[Job],
    enrichments: dict[str, dict] | None,
) -> list[Job]:
    """Return ``jobs`` ordered for the digest feed — best LAST.

    See ``send_per_job_digest`` docstring for the UX rationale (Telegram
    renders newest-at-bottom, so the highest-priority job should be the
    last message sent so it lands closest to the user's gaze). The order
    is fully deterministic given the same ``(jobs, enrichments)``.
    """
    if enrichments:
        return sorted(jobs, key=lambda j: _sort_key_with_enrichments(j, enrichments))
    return sorted(jobs, key=_sort_key_no_enrichments)


def send_per_job_digest(
    tg: TelegramClient,
    chat_id: int,
    jobs: list[Job],
    cfg: dict,
    on_sent,  # callable(message_id:int, job:Job) -> None
    enrichments: dict[str, dict] | None = None,
    min_score: int = 0,
    top_n: int | None = None,
    *,
    run_id: int | None = None,
    enriched_count: int = 0,
    dropped_below_score: int = 0,
    lower_count_at_step: int = 0,
    skip_header: bool = False,
) -> int:
    """Send one message per job, each with its own inline keyboard.

    `enrichments`, if provided, is a map keyed by Job.job_id → {match_score,
    why_match, key_details}. Each matching job's message will include the
    resume-aware card (⭐ score, why-match line, key details).

    `min_score` (0-5) is informational — when >0 the count line mentions the
    active gate so the user knows their digest is filtered. The actual score
    filtering happens upstream in search_jobs.py.

    `top_n`, when set (>0), truncates the digest to the strongest N matches
    AFTER sorting. Useful for ``/jobs`` interactive runs where the user only
    wants a handful. Default behavior (``None``) is unchanged — every job
    that survives upstream filtering is sent.

    Sort order (best-LAST UX choice — see below):
      Priority when ``enrichments`` is present:
        1. match_score DESC (5 first when reading the chat top-down; ``None``
           is treated as -1 so Haiku batch failures sink to the bottom of the
           priority list — they don't crash and don't mis-sort).
        2. posted_at DESC (fresher first).
        3. SOURCE_TIER (web_search > linkedin > euraxess > reliefweb >
           remotive > remoteok > weworkremotely > hackernews > others).
           Configurable via the module-level ``SOURCE_TIER`` constant.
        4. job_id (stable final tie-break — same input set ⇒ same order
           on every run).
      Fallback when ``enrichments`` is None or empty:
        SOURCE_TIER → posted_at DESC → job_id.

    UX RATIONALE — best-LAST:
      Telegram renders the newest message at the BOTTOM of the chat,
      right above the input box where the user's gaze starts when they
      open the conversation. If we send strongest-first, the best match
      scrolls to the TOP of the chat — furthest from the eye and only
      reachable via an explicit scroll-up. Sending strongest-LAST means
      the just-arrived message (what the user sees first on open) IS the
      best match. This matches the mental model "what just appeared =
      what to look at first." So we sort ASCENDING by the relevance
      keys: worst sent first, best sent last.

    Calls `on_sent` after every successful send so the caller can persist the
    message_id → job_id mapping in the DB.

    URL liveness gate: each job's posting URL is HEAD-checked right before
    its message goes out. Dead/broken URLs (404/410/timeout/etc.) are
    DROPPED — no message sent, ``on_sent`` is not called, and a forensic
    line ``telegram.url_dead`` is written with the reason. The summary
    forensic line includes ``dead_url_count``. Set ``URL_VALIDATION_OFF=1``
    to skip the gate entirely (e.g. for offline tests or sources where the
    HEAD cost outweighs the benefit). Tune ``URL_VALIDATION_TIMEOUT_S`` to
    change the per-URL probe budget (default 5.0s).

    Forum-URL gate: after liveness, each job's URL is checked against a
    blocklist of discussion-style hosts (reddit, news.ycombinator.com,
    twitter/x, github issues, stackoverflow, …) and a small set of forum
    path patterns (``/comments/``, ``/threads/``, ``/r/<sub>``,
    ``/issues/N``, …). Hits are DROPPED with a forensic ``job.forum_url``
    line and counted in summary as ``forum_url_count``. Sources whose URLs
    are legitimately discussion-style (``hackernews``) are exempted by
    source. Set ``FORUM_FILTER_OFF=1`` to skip the gate entirely.

    Returns number of messages sent.
    """
    msg_cfg = cfg.get("message", {})
    inc_snip = bool(msg_cfg.get("include_snippet", True))
    snip_chars = int(msg_cfg.get("snippet_chars", 240))
    enrichments = enrichments or {}

    # Sort once — best-LAST so the strongest job is the most recent
    # message in the chat (closest to the input box, most visible).
    # See docstring above for why ascending = best-last.
    sort_mode = "with_enrichments" if enrichments else "fallback"
    jobs = sort_jobs_for_digest(list(jobs), enrichments or None)

    # Optional truncation: keep only the top-N strongest matches. Because
    # we sort ascending (worst → best), "top" lives at the END of the
    # list. Slice from the right so the best-N survive.
    truncated_from: int | None = None
    if top_n is not None and top_n > 0 and len(jobs) > top_n:
        truncated_from = len(jobs)
        jobs = jobs[-top_n:]

    # Lazy import — telegram_client is loaded by search_jobs which already
    # imports forensic; this keeps the dependency one-way and tolerates
    # the rare environment where forensic is unavailable.
    try:
        import forensic as _forensic
    except Exception:  # pragma: no cover
        _forensic = None

    # Header send. Failures here are rare but if Telegram is down we want
    # to surface the reason post-hoc. `skip_header=True` is the "Lower
    # floor" replay path — caller already has a header in the chat and
    # only needs the additional per-job cards appended.
    header_status = "ok"
    header_err = None
    if not skip_header:
        try:
            kb = digest_header_keyboard(
                run_id=run_id,
                current_floor=int(min_score or 0),
                lower_count=int(lower_count_at_step or 0),
            )
            header_text = digest_header_mdv2() + "\n\n" + _count_line(
                jobs,
                min_score=min_score,
                enriched_count=enriched_count,
                dropped_below_score=dropped_below_score,
            )
            tg.send_message(chat_id, header_text, reply_markup=kb)
        except Exception as e:
            header_status = "error"
            header_err = {"class": type(e).__name__, "message": str(e)[:300]}
            log.error("send_per_job_digest: header send failed for %s: %s", chat_id, e)
            if _forensic is not None:
                _forensic.log_step(
                    "telegram.send_header",
                    input={"chat_id": chat_id, "job_count": len(jobs), "min_score": min_score},
                    output={"status": header_status},
                    error=header_err,
                    chat_id=chat_id,
                )
            return 0
        if _forensic is not None:
            _forensic.log_step(
                "telegram.send_header",
                input={"chat_id": chat_id, "job_count": len(jobs), "min_score": min_score},
                output={"status": header_status},
                chat_id=chat_id,
            )

    if not jobs:
        return 1 if not skip_header else 0
    sent = 1 if not skip_header else 0
    fail_count = 0
    dead_url_count = 0
    too_old_count = 0
    forum_url_count = 0
    for job in jobs:
        enr = enrichments.get(job.job_id)

        # Age-window gate. Runs BEFORE the URL-liveness probe so we don't
        # waste a HEAD request on postings we're already going to drop.
        # Sources that don't surface posted_at (LinkedIn, web_search) are
        # admitted by default; flip JOB_AGE_MISSING_POLICY=reject to be
        # strict. JOB_AGE_FILTER_OFF=1 disables the gate entirely.
        if not JOB_AGE_FILTER_OFF:
            allowed, reason = _is_within_age_window(
                job.posted_at or "",
                max_days=MAX_JOB_AGE_DAYS,
                missing_policy=JOB_AGE_MISSING_POLICY,
            )
            if not allowed:
                too_old_count += 1
                age_days_val: int | None = None
                if reason.startswith("too_old:"):
                    try:
                        age_days_val = int(reason.split(":", 1)[1].rstrip("d"))
                    except (ValueError, IndexError):
                        age_days_val = None
                if _forensic is not None:
                    _forensic.log_step(
                        "job.too_old",
                        input={
                            "job_id": job.job_id,
                            "source": job.source,
                            "posted_at": job.posted_at or "",
                            "title": (job.title or "")[:120],
                        },
                        output={"age_days": age_days_val, "reason": reason},
                        chat_id=chat_id,
                    )
                log.info(
                    "send_per_job_digest: dropping %s (%s) — too old: %s",
                    job.job_id, job.source, reason,
                )
                continue

        # URL liveness gate. Skip the HEAD probe entirely when the
        # operator has disabled validation (URL_VALIDATION_OFF=1) or the
        # job has no URL to check. Otherwise: drop dead URLs BEFORE we
        # build the message / call on_sent, so the user never sees a
        # broken posting and the DB mapping isn't poisoned with a
        # message_id pointing at a 404.
        if not URL_VALIDATION_OFF and (job.url or "").strip():
            alive, reason = _url_is_alive(job.url, timeout_s=URL_VALIDATION_TIMEOUT_S)
            if not alive:
                dead_url_count += 1
                if _forensic is not None:
                    _forensic.log_step(
                        "telegram.url_dead",
                        input={
                            "job_id": job.job_id,
                            "source": job.source,
                            "url": job.url,
                            "title": (job.title or "")[:120],
                        },
                        output={"reason": reason},
                        chat_id=chat_id,
                    )
                log.info(
                    "send_per_job_digest: dropping %s (%s) — dead URL: %s",
                    job.job_id, job.source, reason,
                )
                continue

        # Forum / discussion-page filter. Runs AFTER the liveness gate so
        # dead-and-forum URLs are still bucketed as dead (cheaper signal,
        # surfaces source-side rot). hackernews jobs are exempt by source —
        # their URLs legitimately point at news.ycombinator.com threads.
        # FORUM_FILTER_OFF=1 disables the gate entirely.
        if not FORUM_FILTER_OFF and (job.url or "").strip():
            real, reason = _url_is_real_posting(job.url, job.source or "")
            if not real:
                forum_url_count += 1
                if _forensic is not None:
                    _forensic.log_step(
                        "job.forum_url",
                        input={
                            "job_id": job.job_id,
                            "source": job.source,
                            "url": job.url,
                            "title": (job.title or "")[:120],
                        },
                        output={"reason": reason},
                        chat_id=chat_id,
                    )
                log.info(
                    "send_per_job_digest: dropping %s (%s) — forum URL: %s",
                    job.job_id, job.source, reason,
                )
                continue

        text = format_job_mdv2(
            job, include_snippet=inc_snip, snippet_chars=snip_chars, enrichment=enr,
        )
        kb = job_keyboard(job.job_id, url=job.url or None)
        send_status = "ok"
        msg_err = None
        msg_id = None
        try:
            msg_id = tg.send_message(chat_id, text, reply_markup=kb)
            on_sent(msg_id, job)
            sent += 1
            # NOTE: pacing now lives inside TelegramClient._call via the
            # token-bucket rate limiter (global 30 rps + per-chat 1 rps with
            # burst). The old static 0.35s sleep here was a blunt
            # approximation that (a) only covered this digest path and not
            # callbacks/replies and (b) didn't react to actual 429s. The new
            # limiter covers ALL outbound calls and retries on 429 — this
            # site is now a no-op so small digests aren't artificially
            # slowed.
            time.sleep(0)
        except Exception as e:
            send_status = "error"
            fail_count += 1
            msg_err = {"class": type(e).__name__, "message": str(e)[:300]}
            log.error("send_message failed for %s: %s", job.job_id, e)
        if _forensic is not None:
            _forensic.log_step(
                "telegram.send_per_job",
                input={
                    "job_id": job.job_id,
                    "title": (job.title or "")[:80],
                    "company": (job.company or "")[:60],
                    "source": job.source,
                    "match_score": int((enr or {}).get("match_score") or 0) if enr else None,
                    "text_chars": len(text),
                },
                output={
                    "status": send_status,
                    "message_id": msg_id,
                },
                error=msg_err,
                chat_id=chat_id,
            )
    if _forensic is not None:
        _forensic.log_step(
            "telegram.send_per_job_digest.summary",
            input={"chat_id": chat_id, "attempted": len(jobs)},
            output={
                "sent": sent - (0 if skip_header else 1),
                "failed": fail_count,
                "dead_url_count": dead_url_count,
                "too_old_count": too_old_count,
                "forum_url_count": forum_url_count,
                "header_sent": not skip_header,
                "sort_mode": sort_mode,
                "sort_direction": "best_last",
                "top_n": top_n,
                "truncated_from": truncated_from,
            },
            chat_id=chat_id,
        )
    return sent


def _count_line(
    jobs: Iterable[Job],
    min_score: int = 0,
    enriched_count: int = 0,
    dropped_below_score: int = 0,
) -> str:
    """Render the digest header's "what came in" summary.

    Layout (all parts conditional):
        *N* new postings  ·  filter ≥ M/5
        E enriched · D below floor
        `source 1  ·  source 2`

    `dropped_below_score` is jobs that survived enrichment but failed the
    score gate. `too_old_count` is intentionally NOT shown here — that
    number is post-send and lands in forensic logs only.
    """
    jobs = list(jobs)
    gate = ""
    if min_score and min_score > 0:
        gate = f"  ·  filter ≥ *{int(min_score)}*/5"
    sub_parts: list[str] = []
    if enriched_count and enriched_count > 0:
        sub_parts.append(f"{int(enriched_count)} enriched")
    if dropped_below_score and dropped_below_score > 0:
        sub_parts.append(f"{int(dropped_below_score)} below floor")
    sub_line = ("\n_" + mdv2_escape(" · ".join(sub_parts)) + "_") if sub_parts else ""
    if not jobs:
        return f"_No new postings today_{gate}{sub_line}\\."
    by_src: dict[str, int] = {}
    for j in jobs:
        by_src[j.source] = by_src.get(j.source, 0) + 1
    parts = "  ·  ".join(f"{mdv2_escape(k)} {v}" for k, v in sorted(by_src.items()))
    noun = "posting" if len(jobs) == 1 else "postings"
    return f"*{len(jobs)}* new {noun}{gate}{sub_line}\n`{parts}`"
