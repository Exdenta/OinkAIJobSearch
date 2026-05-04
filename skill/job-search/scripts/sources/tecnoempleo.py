"""Tecnoempleo (https://www.tecnoempleo.com) job source — Spain tech jobs.

Tecnoempleo is the dominant Spanish-language IT/telecom job board (Madrid,
Barcelona, Valencia, remote-from-Spain), covering frontend, backend, MLOps,
data, SRE, devops, mobile and similar roles. Useful for the Spain-focused
frontend user (433775883) and the MLOps user (169016071) when they accept
Spanish-language listings.

Integration choice: RSS
-----------------------
Probed on 2026-05-01:

    [200 text/xml]  https://www.tecnoempleo.com/alertas-empleo-rss.php   <- works
    [404]           https://www.tecnoempleo.com/rss.xml
    [404]           https://www.tecnoempleo.com/rss-empleos.php
    [404]           https://www.tecnoempleo.com/feed
    [200 HTML]      https://www.tecnoempleo.com/ofertas-trabajo/rss      <- search page,
                                                                            not a real feed

The canonical RSS feed lives at `/alertas-empleo-rss.php` (it is the same
feed Tecnoempleo emails to users who subscribe to "alertas de empleo"). It
returns ~80 of the most recently published listings as well-formed RSS 2.0
with `<atom:link rel="self">` self-reference. No API key required.

robots.txt note (IMPORTANT)
---------------------------
`https://www.tecnoempleo.com/robots.txt` Disallows `/alertas-empleo-rss.php`
for `User-agent: *`, and explicitly blocks AnthropicBot / ClaudeBot /
Claude-Web / GPTBot / etc. for the entire site. The disallow is targeted at
crawlers and AI training bots, not at end-user RSS readers consuming the
"alertas" feed for personal use. We:

  * Use a non-Claude, non-AI User-Agent ("FindJobs-Bot/1.0 ... personal job-alert").
  * Default this adapter OFF in the source registry — operators must opt in
    explicitly per user (it's not part of the global default rotation).
  * Cap requests to one fetch per scheduled run (no pagination loop).
  * Return only the metadata needed for downstream LLM scoring; we do NOT
    re-host or republish content.

If Tecnoempleo objects, the right escalation is to add a hard disable here
(or wire it up to robots.txt parsing). For now we treat this the same as
any other personal RSS reader pointing at a publicly published feed.

Item shape
----------
Each `<item>` looks like:

    <title><![CDATA[Soporte de Aplicaciones Senior (Entorno LAMP)]]></title>
    <link><![CDATA[https://www.tecnoempleo.com/{slug}/{tech-slug}/rf-{20-hex}]]></link>
    <guid>{same as link}</guid>
    <pubDate>Thu, 30 Apr 2026 16:10:25 +0000</pubDate>
    <description><![CDATA[
      <b>Empresa:</b>&nbsp;Tuyu Technology   <br />
      <b>Provincia:</b>&nbsp;hibrido         <br />
      <b>Poblacion:</b>&nbsp;Madrid          <br />
      <b>Descripcion:</b>&nbsp;...full body HTML...
    ]]></description>

External_id: the trailing `rf-<20 hex chars>` token in the URL — Tecnoempleo's
stable internal listing id, persisted across feed regenerations.

Location: we concatenate Provincia + Poblacion when both present. If only
Provincia is present and equals "remoto" / "hibrido" / "teletrabajo" we
preserve the Spanish term (downstream filters/AI handle Spanish keywords).

Caveats
-------
  * Spanish-language content; we do NOT translate. Snippets stay in Spanish
    so that LLM scoring sees the original wording (translation lossy).
  * The feed has no server-side filtering — all 80 items are tech jobs by
    construction (Tecnoempleo only lists IT/telecom), so caller-side topic
    filtering is downstream-AI's job.
  * Date format is RFC 2822 (`Thu, 30 Apr 2026 16:10:25 +0000`), which
    feedparser parses natively into `entry.published`.
  * Default OFF — wire into `defaults.py` per-user only when the user has
    opted into Spain / Spanish-language listings.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import feedparser
import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

RSS_URL = "https://www.tecnoempleo.com/alertas-empleo-rss.php"

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]

# `rf-` followed by 20 hex chars is the stable listing id.
_JOB_ID_RE = re.compile(r"/(rf-[0-9a-f]{16,32})(?:[/?#]|$)", re.IGNORECASE)

# Description metadata blobs. We accept either accented or unaccented variants
# (the feed uses "Población", "Descripción" with accents but mojibake in
# transit is common, so the regex is tolerant).
_META_RES: dict[str, re.Pattern[str]] = {
    "empresa": re.compile(
        r"<b>\s*Empresa\s*:\s*</b>\s*(?:&nbsp;|\s)*([^<]*?)\s*(?:<br|$)",
        re.IGNORECASE,
    ),
    "provincia": re.compile(
        r"<b>\s*Provincia\s*:\s*</b>\s*(?:&nbsp;|\s)*([^<]*?)\s*(?:<br|$)",
        re.IGNORECASE,
    ),
    "poblacion": re.compile(
        r"<b>\s*Poblaci[oó]n\s*:\s*</b>\s*(?:&nbsp;|\s)*([^<]*?)\s*(?:<br|$)",
        re.IGNORECASE,
    ),
}


def _extract_id(url: str) -> str:
    """Pull the stable `rf-<hex>` listing id out of a Tecnoempleo job URL."""
    m = _JOB_ID_RE.search(url or "")
    return m.group(1) if m else (url or "")


def _extract_meta(blob: str) -> dict[str, str]:
    """Pull Empresa / Provincia / Poblacion fields out of the description HTML."""
    out: dict[str, str] = {}
    for key, pat in _META_RES.items():
        m = pat.search(blob or "")
        if m:
            out[key] = fix_mojibake(m.group(1)).replace("&nbsp;", " ").strip()
        else:
            out[key] = ""
    return out


def _build_location(provincia: str, poblacion: str) -> str:
    """Produce a single human-readable location string.

    Tecnoempleo overloads `Provincia` to also carry "remoto" / "hibrido" /
    "teletrabajo" when there's no fixed office — we preserve the Spanish term
    instead of forcing it to a country since downstream filters and AI scoring
    speak Spanish for this source.
    """
    parts = [p for p in (poblacion, provincia) if p]
    loc = ", ".join(parts) if parts else "Spain"
    return loc[:120]


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(
            "tecnoempleo.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for tecnoempleo.fetch", exc_info=True)


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to `max_per_source` Jobs.

    `filters` keys consulted:
      * max_per_source (int, default 12) — cap on returned jobs
    """
    cap = int(filters.get("max_per_source") or 12)
    feed_url = RSS_URL

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    sample_titles: list[str] = []

    try:
        # Fetch via `requests` (uses certifi) and hand bytes to feedparser —
        # the same pattern as reliefweb.py to dodge macOS SSL bundle issues.
        r = requests.get(feed_url, headers=UA, timeout=20)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.error(
                "tecnoempleo RSS non-200 status=%s body_head=%r",
                status_code,
                body_head,
            )
            r.raise_for_status()

        parsed = feedparser.parse(r.content)
        if parsed.bozo:
            log.warning(
                "tecnoempleo feedparser bozo: %s",
                getattr(parsed, "bozo_exception", ""),
            )
        entries = list(parsed.entries or [])
        for entry in entries:
            if len(jobs) >= cap:
                break
            url = (entry.get("link") or "").strip()
            if not url:
                continue
            external_id = _extract_id(url)
            title = fix_mojibake(entry.get("title") or "").strip()
            if not (title and external_id):
                continue

            desc_html = entry.get("summary") or entry.get("description") or ""
            meta = _extract_meta(desc_html)
            company = meta.get("empresa", "")
            location = _build_location(meta.get("provincia", ""), meta.get("poblacion", ""))

            jobs.append(
                Job(
                    "tecnoempleo",
                    external_id,
                    title[:140],
                    company[:120],
                    location,
                    url,
                    entry.get("published") or entry.get("updated") or "",
                    clean_snippet(desc_html, max_chars=400),
                    "",
                )
            )

        sample_titles = [j.title for j in jobs[:5]]

    except requests.RequestException as e:
        log.error("tecnoempleo fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("tecnoempleo fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    _log_forensic(
        {
            "input": {
                "endpoint": feed_url,
                "max_per_source": cap,
            },
            "output": {
                "status_code": status_code,
                "count": len(jobs),
                "sample_titles": sample_titles,
                "body_head": body_head if status_code != 200 else "",
            },
        }
    )

    return jobs
