"""SSRF guard for outbound fetches of attacker-controlled (scraped) URLs.

Job-posting URLs come from the open web (RSS feeds, LinkedIn, web-search,
scraped board pages) and are fetched server-side at send time — liveness
probes (`telegram_client._url_is_alive` / `_resolve_final_url` /
`_fetch_title_for_soft_404`) and detail-body fetches
(`sources/_detail_fetch.fetch_body_text`). Without a guard, a poisoned
listing URL like ``http://169.254.169.254/latest/meta-data/`` (cloud
metadata) or ``http://127.0.0.1:8000/`` (this host's own backend) would
let an attacker make the bot fetch internal targets (SSRF) and — because
the fetched body is echoed back into the user's Telegram card — partially
exfiltrate the response.

This module provides:
  * ``is_safe_url(url)``  — (safe, reason). Safe means an http(s) scheme
    AND every IP the host resolves to is a public, routable address.
  * ``safe_request(...)`` — a ``requests.request`` wrapper that validates
    the URL, follows redirects MANUALLY (``allow_redirects=False`` per hop)
    and re-validates every redirect target, so a public-looking URL cannot
    30x into an internal address. Raises ``SSRFBlocked`` (a
    ``requests.RequestException`` subclass) on a blocked hop, so existing
    ``except requests.RequestException`` handlers treat a block like any
    other transport failure (the job is dropped) — fail-closed.

This is a transport-security invariant — an allowed hardcoded guard per
CLAUDE.md (alongside User-Agent strings and the ATS-domain allowlist), not
a matching/scoring heuristic.

Residual note: there is a small TOCTOU window between our DNS resolution
in ``is_safe_url`` and ``requests``' own resolution at connect time, so a
determined DNS-rebinding attacker who controls a domain's nameserver could
still race it. That is a far more sophisticated attack than the static
internal-URL / redirect-to-internal vectors this closes; pinning the
validated IP would require connect-by-IP (breaking TLS SNI/cert checks)
and is deliberately out of scope.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5


class SSRFBlocked(requests.RequestException):
    """A URL (or one of its redirect hops) resolved to a blocked target.

    Subclasses ``requests.RequestException`` on purpose: every fetch site
    here already wraps calls in ``except requests.RequestException`` (or a
    broader ``except Exception``) and treats failures as "drop this job",
    so a blocked URL fails closed without any new handling at the call site.
    """


def _ip_is_blocked(ip: "ipaddress._BaseAddress") -> bool:
    """True if ``ip`` is not a public, routable unicast address."""
    # Unwrap IPv4-mapped IPv6 (``::ffff:127.0.0.1``) so a mapped internal
    # address is judged on its real IPv4 value, not the v6 wrapper.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local      # 169.254.0.0/16 — incl. cloud metadata 169.254.169.254
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified     # 0.0.0.0 / ::
    )


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return ``(safe, reason)``.

    Safe iff: scheme is http/https AND the host (literal IP or every IP it
    resolves to via DNS) is a public, routable address. Fail-closed — any
    parse/DNS error returns ``(False, reason)``. ``reason`` is a short,
    stable token for forensic logging.
    """
    if not url or not isinstance(url, str):
        return (False, "empty_url")
    try:
        parts = urlparse(url)
    except Exception:
        return (False, "unparseable")

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (False, f"bad_scheme:{scheme or 'none'}")

    host = parts.hostname
    if not host:
        return (False, "no_host")

    # Literal IP in the URL — check directly, no DNS.
    try:
        return (
            (False, f"blocked_ip:{host}")
            if _ip_is_blocked(ipaddress.ip_address(host))
            else (True, "ok")
        )
    except ValueError:
        pass  # not a literal IP — resolve the hostname below

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception as e:
        return (False, f"dns_error:{type(e).__name__}")
    if not infos:
        return (False, "dns_empty")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return (False, f"bad_addr:{addr}")
        if _ip_is_blocked(ip):
            return (False, f"blocked_ip:{addr}")
    return (True, "ok")


def safe_request(
    method: str,
    url: str,
    *,
    timeout: float,
    headers: dict | None = None,
    verify: bool = True,
    stream: bool = False,
    max_redirects: int = _MAX_REDIRECTS,
) -> requests.Response:
    """``requests.request`` with SSRF protection + per-hop redirect checks.

    Validates the initial URL and EVERY redirect target's resolved IPs
    before connecting (``allow_redirects=False`` per hop, followed
    manually). Raises ``SSRFBlocked`` if any hop is unsafe. The returned
    response's ``.url`` is the final URL (parity with the previous
    ``allow_redirects=True`` behavior). Method is preserved across hops —
    every call site here uses HEAD/GET, which ``requests`` never rewrites
    on redirect anyway.
    """
    current = url
    hops = 0
    while True:
        ok, reason = is_safe_url(current)
        if not ok:
            log.warning("safe_request: blocked %s (%s)", current, reason)
            raise SSRFBlocked(f"ssrf_blocked:{reason}")
        resp = requests.request(
            method,
            current,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            verify=verify,
            stream=stream,
        )
        code = int(getattr(resp, "status_code", 0) or 0)
        location = resp.headers.get("Location") if code in _REDIRECT_STATUSES else None
        if not location or hops >= max_redirects:
            # Final response (non-redirect, no Location, or redirect budget
            # spent). A leftover 3xx is fine — callers treat 3xx as alive.
            return resp
        # Re-validate the next hop before fetching it; close the interim body.
        try:
            resp.close()
        except Exception:
            pass
        current = urljoin(current, location)
        hops += 1
