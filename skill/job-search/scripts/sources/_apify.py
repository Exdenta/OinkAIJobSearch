"""Thin Apify Actor-run transport, shared by source adapters that are
otherwise blocked (DataDome / Cloudflare Bot Fight Mode etc.).

Apify (https://apify.com) hosts the actual scraping infra (residential
proxies, headless browser, anti-bot bypass) for those sources. This module
is a plain `requests` wrapper around Apify API v2's "run Actor synchronously
and return its dataset items" endpoint — no `apify-client` dependency, per
the project's thin-transport convention.

Opt-in only: callers gate on `os.environ.get("APIFY_TOKEN")`. With no token
set, nothing in this module is ever called — zero behavior change.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

_API_BASE = "https://api.apify.com/v2/acts"


def run_actor(actor_slug: str, run_input: dict, token: str, timeout: int = 300) -> list[dict]:
    """Run an Apify Actor synchronously and return its dataset items.

    `actor_slug` is "username/actor-name"; Apify's REST API wants the slash
    tilde-encoded ("username~actor-name"). Never raises: any request error,
    non-200 status, or malformed response degrades to `[]` (a logged
    warning), matching how every other source adapter in this repo degrades.
    """
    encoded_slug = actor_slug.replace("/", "~")
    url = f"{_API_BASE}/{encoded_slug}/run-sync-get-dataset-items"
    try:
        r = requests.post(url, params={"token": token}, json=run_input, timeout=timeout)
    except requests.RequestException as e:
        log.warning("apify: %s request failed: %s", actor_slug, e)
        return []
    if r.status_code != 200:
        log.warning("apify: %s returned HTTP %d", actor_slug, r.status_code)
        return []
    try:
        data = r.json()
    except ValueError as e:
        log.warning("apify: %s returned non-JSON response: %s", actor_slug, e)
        return []
    if not isinstance(data, list):
        log.warning("apify: %s returned unexpected payload shape (%s)", actor_slug, type(data).__name__)
        return []
    return data
