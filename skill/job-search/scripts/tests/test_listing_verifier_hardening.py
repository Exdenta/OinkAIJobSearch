#!/usr/bin/env python3
"""Tests for the P6-T4 hardening of the web_search liveness verifier.

Covers three changes:
  A. Fail-safe gate in `prefilter_for_send` — drops on BOTH ws_status
     False (verifier says closed) AND None (verifier uncertain). Lets
     ws_status True through.
  B. Pre-LLM `_url_pattern_is_soft_404` helper — matches known ATS
     surrogate URL shapes (Workable /oops, ?not_found=true, Lever /
     Greenhouse / Ashby index pages, LinkedIn /jobs/search/, SmartRecruiters
     status=expired) without calling the LLM verifier.
  C. Telemetry — verifier calls now route through
     `wrapped_run_p_with_tools`, so each call writes a `claude_calls`
     row + a forensic `telegram.listing_verify` line for every verdict
     (open / closed / unknown), not just closed.

These tests are pytest-style (function name starts with `test_`) and
share a small set of helpers below. They can be invoked either by
running this file directly (`python test_listing_verifier_hardening.py`)
or via `pytest tests/test_listing_verifier_hardening.py`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


# ---------------------------------------------------------------------------
# Common fixtures / helpers
# ---------------------------------------------------------------------------

def _reload_telegram_client(
    state_dir: str | None = None, *, web_search_verify_off: str | None = "0",
):
    """Re-import telegram_client (and forensic) with optional fresh
    STATE_DIR. Keeps tests independent of each other's env tweaks."""
    if state_dir is not None:
        os.environ["STATE_DIR"] = state_dir
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORENSIC_FULL", None)
    os.environ.pop("URL_VALIDATION_OFF", None)
    os.environ.pop("WEB_SEARCH_VERIFY_OFF", None)
    if web_search_verify_off is not None:
        os.environ["WEB_SEARCH_VERIFY_OFF"] = web_search_verify_off
    os.environ.pop("WEB_SEARCH_SOFT_404_GATE_OFF", None)
    os.environ.pop("JOB_AGE_FILTER_OFF", None)
    os.environ.pop("FORUM_FILTER_OFF", None)
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client  # noqa: E402
    return telegram_client


def _make_web_search_job(url: str, ext_id: str = "ext1"):
    """Build a web_search Job whose only field that matters for the
    liveness gate is `source` + `url`. We bypass the age and forum
    gates so the test stays focused on the verifier path."""
    from dedupe import Job
    return Job(
        source="web_search", external_id=ext_id,
        title="Senior Engineer", company="Acme", location="",
        url=url,
        posted_at="2099-01-01",  # far future, sidesteps the age gate
    )


def test_liveness_default_disabled_without_env() -> None:
    """Default production stance: skip the send-time liveness verifier unless
    WEB_SEARCH_VERIFY_OFF=0 explicitly re-enables it."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td, web_search_verify_off=None)
    assert tc.WEB_SEARCH_VERIFY_OFF is True
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    calls: list[str] = []
    tc._web_search_listing_still_open = lambda job, timeout_s=90, **kw: (  # type: ignore[assignment]
        calls.append(job.url) or (False, "closed:would_drop_if_called")
    )
    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/default-off")
    alive, counts = tc.prefilter_for_send([job], chat_id=999, forensic=forensic)
    assert len(alive) == 1
    assert calls == []
    assert counts["web_search_closed_count"] == 0


# ---------------------------------------------------------------------------
# A. Fail-safe gate
# ---------------------------------------------------------------------------

def test_fail_open_on_infra_failure_none() -> None:
    """A None verdict from a verifier INFRASTRUCTURE failure (subprocess
    died / timed out / envelope unparseable — tags cli_unavailable,
    parse_failed, wrappers_import, claude_cli_import) now fails OPEN: the
    model never judged the page, so a flaky verifier must not silently drop
    an already-scored job. Job survives; the infra-error counter fires and
    the closed counter does not."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    # Skip the cheaper gates so only the verifier decides.
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    # Soft-404 gate must not pre-empt the LLM path for this test.
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._web_search_listing_still_open = lambda job, timeout_s=90, **kw: (
        None, "unknown:cli_unavailable",
    )  # type: ignore[assignment]
    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/123")
    alive, counts = tc.prefilter_for_send([job], chat_id=999, forensic=forensic)
    assert len(alive) == 1, f"infra-failure None must fail OPEN (keep), got {alive}"
    assert counts["web_search_infra_error_count"] == 1
    assert counts["web_search_closed_count"] == 0


def test_fail_closed_on_genuine_unknown_none() -> None:
    """A None verdict from a GENUINE model 'unknown' (the model read the
    page and honestly couldn't decide — free-text reason, not one of the
    infra tags) still DROPS. The fail-safe against shipping a possibly-dead
    URL is unchanged for real uncertainty."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._web_search_listing_still_open = lambda job, timeout_s=90, **kw: (
        None, "unknown:thin_body_and_fallback_search_inconclusive",
    )  # type: ignore[assignment]
    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/123")
    alive, counts = tc.prefilter_for_send([job], chat_id=999, forensic=forensic)
    assert alive == [], f"genuine unknown must be dropped (fail-closed), got {alive}"
    assert counts["web_search_closed_count"] == 1
    assert counts["web_search_infra_error_count"] == 0


def test_fail_safe_gate_drops_on_false() -> None:
    """Regression: existing closed-drop behavior still fires."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._web_search_listing_still_open = lambda job, timeout_s=90, **kw: (
        False, "closed:no_apply_button",
    )  # type: ignore[assignment]
    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/456")
    alive, counts = tc.prefilter_for_send([job], chat_id=999, forensic=forensic)
    assert alive == [], "expected job dropped on False verdict"
    assert counts["web_search_closed_count"] == 1


def test_fail_safe_gate_passes_on_true() -> None:
    """Verifier says open → job survives prefilter."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._web_search_listing_still_open = lambda job, timeout_s=90, **kw: (
        True, "ok",
    )  # type: ignore[assignment]
    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/789")
    alive, counts = tc.prefilter_for_send([job], chat_id=999, forensic=forensic)
    assert len(alive) == 1, f"expected job survives, got {len(alive)}"
    assert counts["web_search_closed_count"] == 0


def test_age_missing_policy_per_source() -> None:
    """Per-source missing-date policy: scrape-only/dateless sources
    (devex, web_search) reject a missing posted_at; feed sources fall back
    to the global `allow` default."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    assert tc._age_missing_policy_for("devex") == "reject"
    assert tc._age_missing_policy_for("web_search") == "reject"
    assert tc._age_missing_policy_for("DevEx") == "reject"  # case-insensitive
    assert tc._age_missing_policy_for("linkedin") == "allow"
    assert tc._age_missing_policy_for("ycombinator_was") == "allow"
    assert tc._age_missing_policy_for("") == "allow"


def test_missing_date_dropped_for_devex_kept_for_feed() -> None:
    """End-to-end through the age gate: a devex job with no posted_at is
    dropped (reject); a linkedin job with no posted_at survives (allow)."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    # Neutralize the other gates so only the age gate decides.
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._web_search_listing_still_open = lambda job, timeout_s=90, **kw: (True, "ok")  # type: ignore[assignment]
    from dedupe import Job

    def _mk(src: str):
        return Job(
            source=src, external_id="x", title="Researcher", company="NGO",
            location="", url="https://www.devex.com/jobs/role-123",
            posted_at="",  # missing date
        )
    import forensic  # noqa: E402
    devex_alive, _ = tc.prefilter_for_send([_mk("devex")], chat_id=1, forensic=forensic)
    feed_alive, _ = tc.prefilter_for_send([_mk("linkedin")], chat_id=1, forensic=forensic)
    assert devex_alive == [], "devex job with no posted_at must be dropped (reject)"
    assert len(feed_alive) == 1, "linkedin job with no posted_at must be kept (allow)"


# ---------------------------------------------------------------------------
# B. Pre-LLM soft-404 pattern check
# ---------------------------------------------------------------------------

def test_soft_404_pattern_workable_oops() -> None:
    tc = _reload_telegram_client(tempfile.mkdtemp())
    is_soft, reason = tc._url_pattern_is_soft_404("https://apply.workable.com/oops")
    assert is_soft is True
    assert reason == "closed:workable_oops"


def test_soft_404_pattern_workable_not_found() -> None:
    tc = _reload_telegram_client(tempfile.mkdtemp())
    is_soft, reason = tc._url_pattern_is_soft_404(
        "https://apply.workable.com/troop-1/?not_found=true",
    )
    assert is_soft is True
    assert reason == "closed:workable_not_found"


def test_soft_404_pattern_lever_index() -> None:
    tc = _reload_telegram_client(tempfile.mkdtemp())
    is_soft, reason = tc._url_pattern_is_soft_404("https://jobs.lever.co/foo")
    assert is_soft is True
    assert reason == "closed:lever_index"
    # And a legitimate Lever role URL with a uuid suffix must NOT fire.
    is_soft2, _ = tc._url_pattern_is_soft_404(
        "https://jobs.lever.co/foo/abc123-def456-7890",
    )
    assert is_soft2 is False, "legit lever role URL should not soft-404"


def test_soft_404_pattern_greenhouse_index() -> None:
    tc = _reload_telegram_client(tempfile.mkdtemp())
    is_soft, reason = tc._url_pattern_is_soft_404(
        "https://boards.greenhouse.io/foo/",
    )
    assert is_soft is True
    assert reason == "closed:greenhouse_index"
    # Legit greenhouse role URL must pass.
    is_soft2, _ = tc._url_pattern_is_soft_404(
        "https://boards.greenhouse.io/foo/jobs/12345",
    )
    assert is_soft2 is False


def test_soft_404_pattern_linkedin_search_redirect() -> None:
    tc = _reload_telegram_client(tempfile.mkdtemp())
    is_soft, reason = tc._url_pattern_is_soft_404(
        "https://linkedin.com/jobs/search/?keywords=react",
    )
    assert is_soft is True
    assert reason == "closed:linkedin_search_redirect"


def test_soft_404_legit_workable_passes() -> None:
    """A genuine `apply.workable.com/<co>/j/<slug>/` URL must NOT trip
    the soft-404 pattern, and when wired through prefilter_for_send the
    LLM verifier IS invoked."""
    tc = _reload_telegram_client(tempfile.mkdtemp())
    is_soft, reason = tc._url_pattern_is_soft_404(
        "https://apply.workable.com/reown/j/DEADBEEF99/",
    )
    assert is_soft is False, f"legit URL must not soft-404 (reason={reason!r})"

    # End-to-end: with a legit URL, _resolve_final_url returns the same
    # URL, the soft-404 check stays False, and the LLM verifier is
    # called exactly once.
    calls: list[str] = []

    def _fake_verifier(job, timeout_s=90, **kw):
        calls.append(job.url)
        return (True, "ok")

    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._web_search_listing_still_open = _fake_verifier  # type: ignore[assignment]

    import forensic  # noqa: E402
    job = _make_web_search_job("https://apply.workable.com/reown/j/DEADBEEF99/")
    alive, _ = tc.prefilter_for_send([job], chat_id=42, forensic=forensic)
    assert len(alive) == 1, "legit Workable URL should survive prefilter"
    assert len(calls) == 1, "LLM verifier must be called for legit URLs"


def test_soft_404_drops_without_llm_call() -> None:
    """When the URL matches a soft-404 pattern, the LLM verifier must
    NOT be called — that's the whole point of the structural gate."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    # Final URL = surrogate.
    tc._resolve_final_url = lambda u, timeout_s=5.0: (  # type: ignore[assignment]
        "https://apply.workable.com/troop-1/?not_found=true"
    )

    verifier_calls: list[Any] = []

    def _spy_verifier(job, timeout_s=90, **kw):
        verifier_calls.append(job.url)
        return (True, "ok")

    tc._web_search_listing_still_open = _spy_verifier  # type: ignore[assignment]

    import forensic  # noqa: E402
    # Input URL looks valid; final URL after redirect is the surrogate.
    job = _make_web_search_job("https://apply.workable.com/troop-1/j/5CCA262814")
    alive, counts = tc.prefilter_for_send([job], chat_id=42, forensic=forensic)
    assert alive == [], "soft-404 surrogate should be dropped"
    assert verifier_calls == [], (
        f"LLM verifier must NOT be invoked when soft-404 matches; got "
        f"{verifier_calls!r}"
    )
    assert counts["web_search_closed_count"] == 1


def test_hackernews_liveness_exempt_skips_verifier() -> None:
    """`hackernews` is liveness-exempt: a 'Who is hiring?' comment is not
    ATS-shaped (no Apply button, apply info is inline text), so the text-only
    verifier systematically false-closes it. The gate must skip the LLM
    verifier entirely and keep the job — its liveness is already covered by
    the dead-URL HTTP probe (404 catches deleted/flagged items) and the
    current-month-only fetch. Mirrors the soft-404 'without_llm_call' guard."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    assert "hackernews" in tc._LIVENESS_EXEMPT_SOURCES, "hackernews must be exempt"
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]

    verifier_calls: list[Any] = []

    def _spy_verifier(job, timeout_s=90, **kw):
        verifier_calls.append(job.url)
        return (False, "closed:no visible Apply button")  # would drop if called

    tc._web_search_listing_still_open = _spy_verifier  # type: ignore[assignment]

    from dedupe import Job
    import forensic  # noqa: E402
    job = Job(
        source="hackernews", external_id="hn1",
        title="Founding Engineers", company="Talk Machine", location="",
        url="https://news.ycombinator.com/item?id=48749307",
        posted_at="2099-01-01",  # far future, sidesteps the age gate
    )
    alive, counts = tc.prefilter_for_send([job], chat_id=42, forensic=forensic)
    assert len(alive) == 1, f"hackernews job must be kept, got {alive}"
    assert verifier_calls == [], (
        f"LLM verifier must NOT be invoked for liveness-exempt hackernews; "
        f"got {verifier_calls!r}"
    )
    assert counts["web_search_closed_count"] == 0


def test_un_careers_routes_to_api_not_llm() -> None:
    """UN Careers is a JS-SPA the text verifier false-closes. `prefilter_for_send`
    must route it to the per-job API check (`_un_careers_listing_still_open`), NOT
    the LLM verifier. Here the API says open → job kept, LLM never invoked."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._fetch_title_for_soft_404 = lambda u, timeout_s=5.0: None  # type: ignore[assignment]
    # UN API check stubbed OPEN (no network); the LLM verifier must never fire.
    tc._un_careers_listing_still_open = lambda job, timeout_s=90: (True, "ok")  # type: ignore[assignment]
    llm_calls: list[Any] = []
    tc._web_search_listing_still_open_claude = lambda job, timeout_s=90: (  # type: ignore[assignment]
        llm_calls.append(job.url) or (False, "closed:generic UN portal")
    )

    from dedupe import Job
    import forensic  # noqa: E402
    job = Job(
        source="un_careers", external_id="u1",
        title="Individual Contractor — YPS study", company="United Nations",
        location="New York",
        url="https://careers.un.org/jobSearchDescription/280387?language=en",
        posted_at="2099-01-01",  # far future, sidesteps the age gate
    )
    alive, counts = tc.prefilter_for_send([job], chat_id=7, forensic=forensic)
    assert len(alive) == 1, f"UN job (API=open) must be kept, got {alive}"
    assert llm_calls == [], f"LLM verifier must NOT run for UN Careers; got {llm_calls!r}"
    assert counts["web_search_closed_count"] == 0


def test_un_careers_api_closed_drops_without_llm() -> None:
    """The converse: when the UN API says closed (deadline passed / removed), the
    job is dropped — again without ever consulting the LLM verifier."""
    td = tempfile.mkdtemp()
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]
    tc._fetch_title_for_soft_404 = lambda u, timeout_s=5.0: None  # type: ignore[assignment]
    tc._un_careers_listing_still_open = lambda job, timeout_s=90: (  # type: ignore[assignment]
        False, "closed:un_deadline_passed"
    )
    llm_calls: list[Any] = []
    tc._web_search_listing_still_open_claude = lambda job, timeout_s=90: (  # type: ignore[assignment]
        llm_calls.append(job.url) or (True, "ok")
    )

    from dedupe import Job
    import forensic  # noqa: E402
    job = Job(
        source="un_careers", external_id="u2", title="Expired role",
        company="United Nations", location="Geneva",
        url="https://careers.un.org/jobSearchDescription/111111?language=en",
        posted_at="2099-01-01",
    )
    alive, counts = tc.prefilter_for_send([job], chat_id=7, forensic=forensic)
    assert alive == [], f"UN job (API=closed) must be dropped, got {alive}"
    assert llm_calls == [], f"LLM verifier must NOT run for UN Careers; got {llm_calls!r}"
    assert counts["web_search_closed_count"] == 1


# ---------------------------------------------------------------------------
# C. Telemetry — claude_calls + per-verdict forensic
# ---------------------------------------------------------------------------

def _read_forensic_lines(state_dir: str) -> list[dict]:
    """Slurp all `state/forensic_logs/log.*.jsonl` lines."""
    log_dir = Path(state_dir) / "forensic_logs"
    out: list[dict] = []
    for f in sorted(log_dir.glob("log.*.jsonl")):
        for raw in f.read_text().splitlines():
            raw = raw.strip()
            if raw:
                out.append(json.loads(raw))
    return out


def _setup_telemetry_capture(state_dir: str) -> list[dict]:
    """Wire the wrappers module so each `record_claude_call` is captured
    into a list we can assert on. Returns the list.

    The mutation lives on the live `instrumentation.wrappers` module —
    don't reload it, because telegram_client's lazy `from
    instrumentation.wrappers import wrapped_run_p_with_tools` would
    cache a stale reference. We just overwrite `_resolve_store` and
    reset `_DEFAULT_STORE`; subsequent wrapped calls pick up the new
    behavior immediately.
    """
    captured: list[dict] = []

    class _FakeStore:
        def record_claude_call(self, **kwargs):
            captured.append(kwargs)
            return 1

    from instrumentation import wrappers as _w  # noqa: E402
    _w._DEFAULT_STORE = None
    _w._resolve_store = lambda store: _FakeStore()
    return captured


def test_verifier_telemetry_records_open() -> None:
    """Verifier returns True → a claude_calls row is written AND a
    forensic `telegram.listing_verify` line with status=open."""
    td = tempfile.mkdtemp()
    captured = _setup_telemetry_capture(td)
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]

    # Force the verifier to invoke wrapped_run_p_with_tools but with a
    # stub stdout that decodes to status=open. `extract_assistant_text`
    # expects the `{"result": "<inner>"}` envelope that `claude -p
    # --output-format json` emits.
    import claude_cli  # noqa: E402
    fake_stdout = json.dumps(
        {"result": '{"status": "open", "reason": "apply button visible"}'}
    )
    claude_cli.run_p_with_tools = lambda *a, **kw: fake_stdout  # type: ignore[assignment]
    # Also short-circuit the wrappers module's run_p_with_tools symbol
    # (it imports run_p_with_tools at module import time).
    from instrumentation import wrappers as _w  # noqa: E402
    _w.run_p_with_tools = lambda *a, **kw: fake_stdout  # type: ignore[assignment]

    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/open-1")
    alive, _ = tc.prefilter_for_send([job], chat_id=77, forensic=forensic)
    assert len(alive) == 1, "open-verdict job should pass through"

    # claude_calls captured.
    assert len(captured) == 1, f"one claude_calls row expected, got {len(captured)}"
    row = captured[0]
    assert row["caller"] == "web_search_liveness", (
        f"caller tag (got {row.get('caller')!r})"
    )
    assert row["status"] == "ok"

    # Forensic listing_verify entry with status=open.
    lines = _read_forensic_lines(td)
    verify_lines = [r for r in lines if r.get("op") == "telegram.listing_verify"]
    assert len(verify_lines) == 1, (
        f"one listing_verify line, got {len(verify_lines)}"
    )
    assert verify_lines[0]["output"]["status"] == "open"


def test_verifier_telemetry_records_unknown() -> None:
    """Verifier returns None via the parse-failure path → telemetry rows
    still get written for both claude_calls and forensic listing_verify
    (status=unknown). parse_failed is an INFRASTRUCTURE failure, so the job
    now fails OPEN (kept) and a `telegram.web_search_infra_error` forensic
    line is emitted alongside the listing_verify line."""
    td = tempfile.mkdtemp()
    captured = _setup_telemetry_capture(td)
    tc = _reload_telegram_client(td)
    tc._url_is_alive = lambda url, timeout_s=5.0: (True, "ok")  # type: ignore[assignment]
    tc._url_is_real_posting = lambda url, src: (True, "ok")  # type: ignore[assignment]
    tc._url_pattern_is_soft_404 = lambda u: (False, "")  # type: ignore[assignment]
    tc._resolve_final_url = lambda u, timeout_s=5.0: u  # type: ignore[assignment]

    # Drive the verifier to return None via a stdout that fails parse.
    import claude_cli  # noqa: E402
    bad_stdout = "not-valid-json"
    claude_cli.run_p_with_tools = lambda *a, **kw: bad_stdout  # type: ignore[assignment]
    from instrumentation import wrappers as _w  # noqa: E402
    _w.run_p_with_tools = lambda *a, **kw: bad_stdout  # type: ignore[assignment]

    import forensic  # noqa: E402
    job = _make_web_search_job("https://example.com/role/unk-1")
    alive, counts = tc.prefilter_for_send([job], chat_id=77, forensic=forensic)
    # parse_failed is an INFRASTRUCTURE failure (unparseable envelope) — the
    # model never judged the page — so the gate fails OPEN: job is kept.
    assert len(alive) == 1, "parse-failure (infra) job must be kept (fail-open)"
    assert counts["web_search_infra_error_count"] == 1
    assert counts["web_search_closed_count"] == 0

    assert len(captured) == 1, "one claude_calls row expected"
    assert captured[0]["caller"] == "web_search_liveness"

    lines = _read_forensic_lines(td)
    verify_lines = [r for r in lines if r.get("op") == "telegram.listing_verify"]
    assert len(verify_lines) == 1
    assert verify_lines[0]["output"]["status"] == "unknown"
    # The fail-open keep is recorded distinctly for operator visibility.
    infra_lines = [r for r in lines if r.get("op") == "telegram.web_search_infra_error"]
    assert len(infra_lines) == 1, "expected one web_search_infra_error forensic line"
    assert infra_lines[0]["output"]["verdict"] == "kept_despite_verifier_failure"
    # No `web_search_uncertain` drop line: infra failure fails OPEN now, so
    # the uncertain-DROP path (genuine model unknown / closed) is not taken.
    uncertain = [r for r in lines if r.get("op") == "telegram.web_search_uncertain"]
    assert len(uncertain) == 0, (
        f"infra failure must not emit an uncertain-drop line; got {len(uncertain)}"
    )


# ---------------------------------------------------------------------------
# Runner (so plain `python` invocation still works alongside pytest).
# ---------------------------------------------------------------------------

_TESTS = [
    test_liveness_default_disabled_without_env,
    test_fail_open_on_infra_failure_none,
    test_fail_closed_on_genuine_unknown_none,
    test_age_missing_policy_per_source,
    test_missing_date_dropped_for_devex_kept_for_feed,
    test_fail_safe_gate_drops_on_false,
    test_fail_safe_gate_passes_on_true,
    test_soft_404_pattern_workable_oops,
    test_soft_404_pattern_workable_not_found,
    test_soft_404_pattern_lever_index,
    test_soft_404_pattern_greenhouse_index,
    test_soft_404_pattern_linkedin_search_redirect,
    test_soft_404_legit_workable_passes,
    test_soft_404_drops_without_llm_call,
    test_hackernews_liveness_exempt_skips_verifier,
    test_un_careers_routes_to_api_not_llm,
    test_un_careers_api_closed_drops_without_llm,
    test_verifier_telemetry_records_open,
    test_verifier_telemetry_records_unknown,
]


def main() -> int:
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  OK   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {t.__name__}: {e!r}")
            import traceback
            traceback.print_exc()
    if failed:
        print(f"\n{failed} test(s) failed.")
        return 1
    print(f"\nAll {len(_TESTS)} listing-verifier hardening tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
