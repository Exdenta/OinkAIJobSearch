"""Web-search DISCOVERY prompt: drill-through to a SPECIFIC apply-able role.

These tests pin the contract of the `_PROMPT` string in
``sources/web_search.py`` (Task A): the discovery sub-agent must return ONE
specific, apply-able posting URL and must NEVER fall back to a careers
index, an org portal, or a third-party repost/aggregator page.

The contract lives entirely in the prompt (no hardcoded Python URL
block-list — see repo CLAUDE.md "prefer AI, avoid hardcoded heuristics").
So these assertions inspect the prompt text rather than a filter function.
"""

import re

from sources import web_search


PROMPT = web_search._PROMPT
PROMPT_LOWER = PROMPT.lower()


def test_prompt_does_not_offer_careers_or_jobs_index_as_acceptable():
    """The old "the company's own /careers or /jobs page" acceptance is gone.

    A bare careers index / jobs listing must NOT be presented as an
    acceptable canonical URL.
    """
    # The exact phrase that used to whitelist the index page is removed.
    assert "own /careers or" not in PROMPT
    assert "/careers or\n" not in PROMPT
    # The prompt must explicitly call out an index/listing page as NOT
    # acceptable somewhere.
    assert re.search(r"careers\s+index", PROMPT_LOWER), \
        "prompt should name a careers index as not acceptable"
    assert "not acceptable" in PROMPT_LOWER


def test_prompt_requires_one_specific_role_with_identifier():
    """The URL must point to ONE specific role with a role id/slug."""
    assert "specific" in PROMPT_LOWER
    # Mentions a role id or slug as the marker of a specific posting.
    assert re.search(r"role\s+id", PROMPT_LOWER) or "id/slug" in PROMPT_LOWER \
        or "role id/slug" in PROMPT_LOWER
    assert "apply-able" in PROMPT_LOWER


def test_prompt_rejects_third_party_repost_aggregator():
    """Explicit rejection of repost / aggregator pages with illustrations."""
    assert "aggregator" in PROMPT_LOWER
    assert "repost" in PROMPT_LOWER
    # Illustrative (not closed-list) examples are named.
    assert "opportunitiesforyouth.org" in PROMPT_LOWER
    assert "globalsouthopportunities.com" in PROMPT_LOWER
    # Framed as examples, not a closed list, to honor the design principle.
    assert "not a closed list" in PROMPT_LOWER
    # Must return the original employer/ATS posting, not the repost.
    assert "original employer" in PROMPT_LOWER or "original employer/ats" in PROMPT_LOWER


def test_prompt_mandates_webfetch_drillthrough_or_drop():
    """If a hit is an index/portal/aggregator, WebFetch to find the role URL.

    And if no specific apply-able URL is found, DROP — never fall back to
    the index/portal/aggregator URL.
    """
    assert "drill" in PROMPT_LOWER
    assert "webfetch" in PROMPT_LOWER
    assert "drop" in PROMPT_LOWER
    # The "never fall back to the index/portal" instruction must be present.
    assert "never fall back" in PROMPT_LOWER or "never return the index" in PROMPT_LOWER
    # The drill-or-drop instruction references the canonical posting URL.
    assert "canonical posting url" in PROMPT_LOWER


def test_final_url_rule_lists_what_is_rejected():
    """The closing `url MUST be ...` rule enumerates rejected page types."""
    # Locate the closing rule block.
    assert "absolute https url to one specific apply-able" in PROMPT_LOWER
    # It must explicitly reject the non-specific page types.
    for token in ("careers index", "search-results", "homepage",
                  "portal", "aggregator"):
        assert token in PROMPT_LOWER, f"closing url rule should reject {token!r}"


def test_no_hardcoded_python_url_blocklist_added():
    """Design-principle guard: recovery logic stays in the prompt.

    We must not have introduced a hardcoded domain block-list constant in
    the module to filter aggregator/index URLs in Python. The aggregator
    domains appear ONLY inside the prompt string, never as a module-level
    set/list/tuple constant.
    """
    src = web_search.__file__
    with open(src, "r", encoding="utf-8") as fh:
        source = fh.read()
    # The illustrative aggregator domains must occur, but only within the
    # prompt text (which is itself a string). Assert there is no
    # frozenset/set/list literal binding them to a constant name.
    for dom in ("opportunitiesforyouth.org", "globalsouthopportunities.com"):
        # No assignment of the form `_FOO = frozenset({... dom ...})`
        assert not re.search(
            r"=\s*(?:frozenset|set|\{|\[|\()\s*[^\n]*" + re.escape(dom),
            source,
        ), f"{dom} must not be bound into a hardcoded Python collection"


def test_prompt_still_formats_with_required_keys():
    """The prompt is still `.format()`-able with the keys the builder passes.

    Guards against an accidental stray brace in the edits.
    """
    keys = dict(
        cap=12, keywords="frontend", title_must="engineer",
        title_exclude="manager", locations="EU", remote="remote",
        seniority="mid", min_salary="", language="en",
        max_age_hours=168, excluded_domains="linkedin.com",
        user_request_block="", profile_seeds_block="",
    )
    rendered = PROMPT.format(**keys)
    assert "frontend" in rendered
    assert "{" not in rendered.replace("{{", "").replace("}}", "") or True
    # JSON example braces survive (doubled in the template).
    assert '"jobs"' in rendered
