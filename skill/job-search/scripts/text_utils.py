"""Shared text cleanup helpers used by every source adapter.

Three jobs:

  1. `strip_html(html)` — remove tags + unescape entities + collapse whitespace.
     Job-board APIs (RemoteOK, Remotive) return HTML blobs in their description
     fields; Telegram digest messages should show plain text, not markup.

  2. `fix_mojibake(text)` — heal the classic UTF-8-decoded-as-Latin-1 damage
     (e.g. `weâre` → `we're`, `donât` → `don't`). Uses `ftfy` when available;
     otherwise falls back to a conservative round-trip heuristic.

  3. `html_links_to_text(html, base_url=...)` — like `strip_html`, but
     preserves link targets as inline `label (URL)` text instead of dropping
     them. `strip_html` is right for job-DETAIL pages (only the description
     text matters); this is for job-LISTING/index pages where the model
     needs to recover per-posting URLs (and "next page" links) that live in
     `<a href>` — which `strip_html`'s tag-stripping regex silently drops.
"""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urljoin

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Signature sequences of the classic Latin-1-encoded-UTF-8 bug.
_MOJIBAKE_MARKERS = ("â€™", "â€œ", "â€", "Ã©", "Ã¨", "Ã¢", "Ãª", "â ", "âs", "ât")

try:
    import ftfy  # optional dep; pip install ftfy
    _HAVE_FTFY = True
except ImportError:
    _HAVE_FTFY = False


def strip_html(html: str) -> str:
    """Turn an HTML fragment into plain text."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    return _WS_RE.sub(" ", text).strip()


def html_links_to_text(html: str, *, base_url: str = "", max_chars: int = 8000) -> str:
    """Turn an HTML page into plain text, inlining each `<a href>`'s target
    as `label (absolute-URL)` so a text-only LLM prompt can still recover
    per-posting/next-page links. Relative hrefs are resolved against
    `base_url`. Falls back to `strip_html` if BeautifulSoup can't parse the
    input (never raises).
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return strip_html(html)[:max_chars]
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return strip_html(html)[:max_chars]
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        abs_url = urljoin(base_url, href) if base_url else href
        label = a.get_text(" ", strip=True)
        a.replace_with(f" {label} ({abs_url}) " if label else f" ({abs_url}) ")
    text = _WS_RE.sub(" ", soup.get_text(" ", strip=True)).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _fallback_fix(text: str) -> str:
    """Heuristic mojibake fix when ftfy isn't installed. Only touches strings
    that look visibly broken, so well-formed Unicode passes through unchanged."""
    if not text or not any(m in text for m in _MOJIBAKE_MARKERS):
        return text
    try:
        # The usual bug: UTF-8 bytes were decoded as Latin-1 somewhere upstream.
        repaired = text.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
        return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def fix_mojibake(text: str) -> str:
    """Return the text with any mojibake repaired, if we can."""
    if not text:
        return ""
    if _HAVE_FTFY:
        try:
            return ftfy.fix_text(text)
        except Exception:
            pass
    return _fallback_fix(text)


def clean_snippet(raw: str, max_chars: int = 400) -> str:
    """One-shot: strip HTML → fix mojibake → trim whitespace → cap length."""
    text = fix_mojibake(strip_html(raw or ""))
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text
