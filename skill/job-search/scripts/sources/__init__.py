"""Source adapters.

Each module exposes:

    def fetch(filters: dict) -> list[Job]

except `linkedin` (per-user only — see `linkedin.fetch_for_user(filters, seeds)`)
and `web_search` (per-user only — see `web_search.fetch(filters, ...)`).

`filters` is the runtime operational config dict (see `defaults.DEFAULTS` —
sources toggles, max_age_hours, max_per_source, AI timeouts, message format).
The dict no longer carries any per-user matching fields; the AI score gate
in `job_enrich.enrich_jobs_ai` is the sole matching pass and reads each
recipient's profile separately.
"""
