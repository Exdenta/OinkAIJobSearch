"""M1 — language-aware query generation.

The substance of M1 (translating queries, deciding query mutations) lives in
the Opus prompt, NOT in Python — per the repo's "prefer AI, avoid hardcoded
heuristics" design principle. So these tests cover the DETERMINISTIC scaffolding
around that prompt, without ever calling the real `claude -p`:

  (a) the active prompt actually carries the language-awareness instruction;
  (b) the rendered prompt input includes the candidate's resume + prefs verbatim
      (the wiring — so the CEFR / language lines reach the model);
  (c) the schema validator accepts multilingual (non-ASCII) LinkedIn queries and
      web-search seed phrases, and the profile round-trips through json without
      mangling them.

No test here invokes a model. The one place a stubbed builder runs uses an
injected `_run_p` that returns canned JSON.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import profile_builder as pb


# ---------------------------------------------------------------------------
# (a) The active prompt carries the language-awareness instruction.
# ---------------------------------------------------------------------------
#
# `profile_builder.txt` is the prompt that actually drives production output:
# `build_profile_sync` renders it via `_render_prompt`, and the seeds-only
# prompt (`profile_seeds.txt`) does not exist on disk, so the v3 builder is the
# live path. We assert against the loaded template text.

def _prompt_text() -> str:
    txt = pb._load_prompt_template()
    assert txt, "profile_builder.txt template should be non-empty"
    return txt


def test_prompt_has_language_awareness_block():
    txt = _prompt_text().lower()
    # CEFR parsing instruction present.
    assert "cefr" in txt
    for level in ("b2", "c2", "a2"):
        assert level in txt, f"prompt should reference CEFR level {level!r}"
    # The four-step working-language detection must be present.
    assert "working-language detection" in txt
    assert "emit languages" in txt
    # The native-language query directive must be present.
    assert "native-language" in txt


def test_prompt_instructs_native_query_variants_and_keeps_english():
    txt = _prompt_text().lower()
    # Must instruct idiomatic native phrasing, not a word-for-word gloss.
    assert "idiomatic" in txt
    assert "word-for-word" in txt  # explicitly warns against it
    # Must keep English variants alongside native ones.
    assert "60%" in _prompt_text() and "40%" in _prompt_text()


def test_prompt_says_below_b2_stays_english_only():
    txt = _prompt_text().lower()
    # A2 / below-B2 candidate must NOT get native queries — assert the rule
    # text exists (this is the Spanish-A2 example from rule 18a).
    assert "a2" in txt
    assert "below" in txt and "b2" in txt
    # Worked example showing a non-English emit set must be present.
    assert "spanish" in txt


# ---------------------------------------------------------------------------
# (a2) PREFERRED-COMMUNITY extension — a WORKABLE language the candidate
#      explicitly names as desired is emitted even when it is NOT geo-dominant
#      in any target market, and its native queries are paired with the
#      candidate's REMOTE geo, NEVER the language's home country.
# ---------------------------------------------------------------------------

def test_prompt_encodes_preferred_community_emit_rule():
    txt = _prompt_text().lower()
    # The new emit branch must be named and described.
    assert "preferred-community" in txt
    # It must key off the candidate EXPLICITLY naming a desired language /
    # environment / community (not mere proficiency).
    assert "explicitly" in txt
    assert ("desired working language" in txt) or (
        "environment / community" in txt) or ("speaking environments" in txt)
    # It must apply even when the language is NOT geo-dominant.
    assert "not geo-dominant" in txt or "not dominant" in txt


def test_prompt_pairs_preferred_community_with_remote_not_home_country():
    full = _prompt_text()
    txt = full.lower()
    # The critical geo rule: preferred-community native queries pair with the
    # REMOTE geo (European Union / remote_regions), NEVER the home country.
    assert "remote geo" in txt
    assert "home country" in txt
    assert "never the language's home country" in txt or (
        "never \"russia\"" in txt) or ("never the language’s home country" in txt)
    # The worked Russian example must be present and must NOT route to Russia.
    assert "russian" in txt
    assert "european union" in txt
    # Cyrillic native query example present (idiomatic, remote-geo paired).
    assert "разработчик" in full
    # And it must be paired with European Union, not Russia, in that example.
    assert '"q":"react разработчик","geo":"European Union"' in full


def test_prompt_does_not_overtrigger_or_rescue_subb2():
    txt = _prompt_text().lower()
    # A workable language that is NEITHER geo-dominant NOR explicitly desired
    # must STILL be dropped (no over-firing).
    assert "still dropped" in txt
    # Mere proficiency must NOT trigger the preferred-community branch.
    assert ("mere proficiency" in txt) or (
        "not merely that" in txt) or ("not mere" in txt)
    # Sub-B2 stays dropped even if explicitly named.
    assert "sub-b2" in txt and "still dropped" in txt
    # The French-C1/UK-US English-only example must still hold UNLESS prefs
    # explicitly name French.
    assert "french" in txt and "english only" in txt


def test_prompt_preserves_spanish_geodominant_pairing():
    full = _prompt_text()
    # Spanish-C2 targeting Spain still emits Spanish @ Spain (GEO-DOMINANT).
    assert '"q":"investigador migraciones","geo":"Spain"' in full
    # And the contrast is stated: geo-dominant pairs with country, not remote.
    low = full.lower()
    assert "geo-dominant" in low


def test_prompt_respects_linkedin_query_cap():
    # The prompt must not instruct exceeding the parser's hard cap.
    txt = _prompt_text()
    assert str(pb._MAX_LINKEDIN_QUERIES) in txt  # "10" appears as the hard cap


# ---------------------------------------------------------------------------
# (b) Wiring — the rendered prompt input includes resume + prefs verbatim,
#     so CEFR / language lines actually reach the model.
# ---------------------------------------------------------------------------

def test_render_prompt_includes_resume_and_prefs_language_lines():
    resume = (
        "Alena Maramygina — Anthropologist\n"
        "Languages: Espanol: nativo (C2). English: C1. Francais: A2.\n"
        "5 years migration research."
    )
    prefs = "Investigadora en ONG. Espana, remoto UE. Roles en espanol o ingles."
    rendered = pb._render_prompt(resume, prefs)
    assert rendered, "rendered prompt should be non-empty"
    # The CEFR lines from the resume must survive into the prompt.
    assert "C2" in rendered and "C1" in rendered and "A2" in rendered
    assert "nativo" in rendered
    # The prefs free-text must survive too.
    assert "Investigadora en ONG" in rendered
    # The template placeholders must be fully substituted.
    assert "{resume_text}" not in rendered
    assert "{user_description}" not in rendered


def test_render_prompt_preserves_non_ascii_input():
    # Accented / non-ASCII CEFR + role text must not be stripped or escaped.
    resume = "Idiomas: español C2, alemán B2, 投資 N/A"
    prefs = "técnico de proyectos, investigación migratoria"
    rendered = pb._render_prompt(resume, prefs)
    assert "español" in rendered
    assert "técnico de proyectos" in rendered
    assert "investigación" in rendered


# ---------------------------------------------------------------------------
# (c) The validator accepts multilingual queries / seed phrases.
# ---------------------------------------------------------------------------

def _profile_with_multilingual_seeds() -> dict:
    return {
        "schema_version": 3,
        "ideal_fit_paragraph": "Investigadora migraciones.",
        "primary_role": "qualitative researcher",
        "target_levels": ["mid"],
        "years_experience": 5,
        "stack_primary": ["qualitative research"],
        "stack_secondary": ["policy analysis"],
        "stack_adjacent": ["report writing"],
        "stack_antipatterns": ["market research"],
        "title_must_match": ["investigador", "researcher"],
        "title_exclude": ["director"],
        "exclude_keywords": ["bootcamp"],
        "exclude_companies": [],
        "onsite_locations": ["bilbao"],
        "remote_regions": ["spain", "europe"],
        "locations": ["bilbao", "spain", "europe"],
        "remote": "any",
        "time_zone_band": "UTC-1..UTC+3",
        "salary_min_usd": 0,
        "drop_if_salary_unknown": False,
        "language": "spanish",
        "max_age_hours": 0,
        "min_match_score": 1,
        "search_seeds": {
            "linkedin": {
                "queries": [
                    {"q": "investigador migraciones", "geo": "Spain", "f_TPR": "r86400"},
                    {"q": "técnico de proyectos cooperación", "geo": "Spain", "f_TPR": "r86400"},
                    {"q": "researcher migration", "geo": "European Union", "f_TPR": "r86400"},
                ]
            },
            "web_search": {
                "seed_phrases": [
                    "investigador migraciones españa",
                    "técnico de proyectos ong",
                    "qualitative researcher migration europe",
                ],
                "ats_domains": ["workable.com", "teamtailor.com"],
                "focus_notes": "Roles en español o inglés.",
            },
        },
        "free_text": "Investigadora en ONG. Roles en español o inglés.",
    }


def test_validator_accepts_multilingual_paired_queries():
    errs = pb.profile_schema_validate(_profile_with_multilingual_seeds())
    assert errs == [], f"multilingual paired-shape profile rejected: {errs}"


def test_validator_accepts_multilingual_separated_queries():
    p = _profile_with_multilingual_seeds()
    # SEPARATED v2.3 shape: queries = [str], geos = [str].
    p["search_seeds"]["linkedin"] = {
        "queries": [
            "investigador migraciones",
            "técnico de proyectos cooperación",
            "researcher migration",
        ],
        "geos": ["Spain", "European Union"],
        "f_TPR": "r86400",
    }
    errs = pb.profile_schema_validate(p)
    assert errs == [], f"multilingual separated-shape profile rejected: {errs}"


def test_validator_seeds_v4_accepts_multilingual():
    # The seeds-only (schema_version=4) validator must also accept non-ASCII.
    v4 = {
        "schema_version": 4,
        "primary_role": "qualitative researcher",
        "search_seeds": _profile_with_multilingual_seeds()["search_seeds"],
    }
    errs = pb.seeds_schema_validate(v4)
    assert errs == [], f"v4 multilingual seeds rejected: {errs}"


def test_multilingual_profile_round_trips_through_json():
    # Persistence uses ensure_ascii=False — non-ASCII queries must survive a
    # serialize/parse round-trip byte-for-byte.
    p = _profile_with_multilingual_seeds()
    s = json.dumps(p, ensure_ascii=False)
    back = json.loads(s)
    qs = [q["q"] for q in back["search_seeds"]["linkedin"]["queries"]]
    assert "técnico de proyectos cooperación" in qs
    phrases = back["search_seeds"]["web_search"]["seed_phrases"]
    assert "investigador migraciones españa" in phrases


# ---------------------------------------------------------------------------
# End-to-end-ish: a stubbed builder returning a multilingual profile is
# accepted and persisted (no real `claude -p`).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# (d) PRODUCTION WIRING LOCK — the language-aware prompt is only reachable via
#     build_profile_sync (renders prompts/profile_builder.txt). The seeds-only
#     build_search_seeds_sync renders prompts/profile_seeds.txt, which is NOT
#     on disk, so selecting it short-circuits every rebuild to an exception and
#     the language-aware prompt never runs. These tests lock the defaults that
#     production actually wires (ProfileBuilderQueue.__init__ and
#     rebuild_profile) to build_profile_sync, and prove that builder renders a
#     non-empty prompt.
# ---------------------------------------------------------------------------

def _default_of(func, param: str):
    return inspect.signature(func).parameters[param].default


def test_queue_default_builder_is_language_aware():
    # ProfileBuilderQueue (constructed by bot.py:1613 with NO sync_builder
    # override) must default to build_profile_sync.
    assert _default_of(pb.ProfileBuilderQueue.__init__, "sync_builder") is (
        pb.build_profile_sync
    ), "ProfileBuilderQueue must default to the language-aware build_profile_sync"


def test_rebuild_profile_default_builder_is_language_aware():
    # rebuild_profile (called by search_jobs.py:521 with NO override on the
    # auto-skip path) must default to build_profile_sync.
    assert _default_of(pb.rebuild_profile, "sync_builder") is (
        pb.build_profile_sync
    ), "rebuild_profile must default to the language-aware build_profile_sync"


def test_default_builder_renders_nonempty_prompt():
    # The whole point of the wiring fix: the production default builder must
    # render a NON-EMPTY prompt (i.e. its template exists). build_profile_sync
    # uses _render_prompt -> profile_builder.txt.
    assert pb._render_prompt("résumé", "prefs"), (
        "the production default builder's template must render non-empty"
    )


def test_seeds_builder_short_circuits_because_template_missing():
    # Documents WHY the default was swapped: build_search_seeds_sync renders
    # the absent profile_seeds.txt, so it short-circuits to status='exception'
    # WITHOUT ever calling the model. If someone restores profile_seeds.txt and
    # this test starts failing, the seeds builder is once again a viable
    # default and the wiring comment should be revisited.
    assert not pb._SEEDS_PROMPT_PATH.exists(), (
        "profile_seeds.txt unexpectedly present — revisit builder defaults"
    )

    def _must_not_run(prompt, **kwargs):  # pragma: no cover - asserts it's unreachable
        raise AssertionError("model must NOT be called when seeds template missing")

    res = pb.build_search_seeds_sync("résumé", "prefs", _run_p=_must_not_run)
    assert res.status == "exception"
    assert "seeds prompt template missing" in (res.error or "")


def test_rebuild_profile_end_to_end_emits_spanish_with_temp_db(tmp_path):
    # End-to-end through build_profile_sync (the PRODUCTION default builder —
    # locked by test_rebuild_profile_default_builder_is_language_aware), using a
    # temp DB — never state/jobs.db. We stub the MODEL by binding _run_p onto
    # build_profile_sync with functools.partial and passing it as sync_builder;
    # the bound function IS the production default, just with the `claude -p`
    # subprocess replaced. Proves: a Spanish-C2 user's rebuild routes through
    # build_profile_sync, the rendered prompt carries the CEFR lines, and the
    # Spanish queries persist to the DB.
    import functools

    import db as db_module

    database = db_module.DB(tmp_path / "test.db")
    chat_id = 4242
    database.upsert_user(chat_id)
    database.set_resume(
        chat_id,
        str(tmp_path / "cv.txt"),
        "Idiomas: español C2, English C1, français A2. Investigadora migración.",
    )
    database.set_prefs_free_text(chat_id, "Investigadora en ONG. España, remoto UE.")

    profile = _profile_with_multilingual_seeds()
    canned = json.dumps(
        {"type": "result", "result": json.dumps(profile, ensure_ascii=False)},
        ensure_ascii=False,
    )
    seen = {}

    def _fake_run_p(prompt, **kwargs):
        # Wiring proof: the candidate's CEFR lines reached the model.
        seen["had_cefr"] = "C2" in prompt and "C1" in prompt
        seen["had_native"] = "español" in prompt
        return canned

    # build_profile_sync IS the rebuild_profile default; here we bind its
    # _run_p to the stub so no real `claude -p` runs.
    stubbed_default_builder = functools.partial(pb.build_profile_sync, _run_p=_fake_run_p)
    res = pb.rebuild_profile(database, chat_id, sync_builder=stubbed_default_builder)
    assert res.status == "ok", f"expected ok, got {res.status}: {res.error}"
    assert seen.get("had_cefr"), "CEFR lines must reach the prompt (wiring)"
    assert seen.get("had_native"), "native-language prefs must reach the prompt"

    # The persisted profile (read back from the temp DB) keeps Spanish queries.
    stored = json.loads(database.get_user_profile(chat_id))
    qs = [q["q"] for q in stored["search_seeds"]["linkedin"]["queries"]]
    assert "investigador migraciones" in qs


def test_build_profile_sync_accepts_stubbed_multilingual_response():
    profile = _profile_with_multilingual_seeds()
    canned = json.dumps(
        {"type": "result", "result": json.dumps(profile, ensure_ascii=False)},
        ensure_ascii=False,
    )

    def _fake_run_p(prompt, **kwargs):
        # Sanity: the prompt the builder fed in must contain the candidate's
        # CEFR lines (wiring), proving the language info reached the model.
        assert "C2" in prompt
        return canned

    res = pb.build_profile_sync(
        "Idiomas: español C2, English C1.",
        "Investigadora en ONG, España.",
        _run_p=_fake_run_p,
    )
    assert res.status == "ok", f"expected ok, got {res.status}: {res.error}"
    assert res.profile is not None
    qs = [q["q"] for q in res.profile["search_seeds"]["linkedin"]["queries"]]
    assert "investigador migraciones" in qs
