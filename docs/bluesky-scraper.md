# Bluesky Scraper — Posts, Profiles, Threads & Followers

Extract Bluesky data in one Actor: search posts by keyword or hashtag, pull any account's post feed, scrape a post's reply thread to the requested depth, read a custom/algorithmic feed, fetch profile details, or list followers and following — all through the official AT Protocol public API. No login, no cookies, no proxies.

> **Claude / Codex skill to describe and setup this actor: [SKILL.md](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md)**

## What Bluesky data does this scraper extract?

**Post records** (`search`, `authorFeed`, `thread` and `feed` modes) — one flat JSON record per post:

| Field | Meaning |
|---|---|
| `uri` / `cid` | Stable AT Protocol identifiers of the post |
| `url` | Direct bsky.app link to the post |
| `authorHandle` / `authorDid` / `authorDisplayName` | Who posted it |
| `text` | Full post text |
| `createdAt` / `indexedAt` | When it was posted / indexed |
| `likeCount` / `repostCount` / `replyCount` / `quoteCount` / `bookmarkCount` | Engagement counts |
| `langs` | Declared post languages |
| `tags` / `mentions` / `links` | Hashtags, @-mention DIDs and hyperlinks parsed from the post's richtext facets |
| `embedType` / `mediaUrls` / `mediaAlt` / `externalUrl` / `externalTitle` / `quotedUri` | Media & embeds: images/video/gallery URLs and alt text, external link cards, and quoted-post URIs |
| `replyParentUri` / `replyRootUri` | The post this replies to, and the thread root (null if not a reply) |
| `isRepost` / `feedOf` | Repost flag and source handle/feed (`authorFeed`, `feed` modes) |
| `depth` / `parentUri` / `rootUri` / `isRoot` / `threadUri` | Thread structure (`thread` mode) |
| `mode` | Which mode produced the record |

**Profile records** (`profile`, `followers`, `following` modes): `did`, `handle`, `url`, `displayName`, `description`, `avatar`, `createdAt`, plus `followersCount` / `followsCount` / `postsCount` in `profile` mode and `subjectHandle` in follower/following modes.

## Modes

| Mode | What it returns | Key inputs |
|---|---|---|
| `search` | Posts matching a keyword/hashtag, with date, author, mention, language, domain/url/tag and engagement filters | `query`, `postedWithin`, `fromAuthor`, `mentionsAuthor`, `language`, `domain`, `url`, `tag`, `sort`, `minLikes`, `minReposts` |
| `authorFeed` | Posts by one or more accounts | `handles`, `includeReplies`, `minLikes`, `minReposts` |
| `thread` | A post plus replies to the requested depth (and optional ancestors) | `threadUris`, `threadDepth`, `parentHeight` |
| `feed` | Posts from a custom/algorithmic feed generator | `feedUris` |
| `profile` | Profile details (bio, counts) per account | `handles` |
| `followers` | Accounts following the given handles | `handles` |
| `following` | Accounts the given handles follow | `handles` |

Search supports Bluesky query operators typed straight into `query` — `#hashtag`, `"exact phrase"`, `from:user.bsky.social`, `mentions:user.bsky.social`, `lang:en`, and more. The most common ones also have dedicated inputs so you don't have to remember the operator syntax:

- **`fromAuthor`** — only posts written by this handle (same as `from:`). Uses `searchPosts`' own `author` filter param, not just a query-string trick.
- **`mentionsAuthor`** — only posts that mention this handle (same as `mentions:`). Uses the `mentions` filter param.
- **`language`** — only posts declared in this ISO 639-1 language code (same as `lang:`). Uses the `lang` filter param.

Search requires a non-empty `query`, including when using dedicated filters: they narrow that query. An author-only request belongs in `authorFeed` mode with `handles`. The `#ai` fallback applies only to an otherwise plain search; it does not replace an explicitly empty query or choose a topic for a filter-only request.

These compose safely with `query`, `postedWithin` and `sort` in the same request. The `searchPosts` endpoint's dedicated `domain`/`url`/`tag` filters are also wired up as first-class inputs:

- **`domain`** — only posts linking to this domain (e.g. `nytimes.com`).
- **`url`** — only posts linking to this exact URL.
- **`tag`** — only posts carrying these hashtags (enter each without the leading `#`).

Bluesky's unauthenticated search API does not allow cursor paging, so the Actor transparently paginates `latest` searches by walking the time window backwards — you still just set `maxItems`. `top` sort is not time-ordered and returns up to 100 posts per run.

`minLikes` / `minReposts` filter posts client-side, after they're fetched from the API — set either to skip low-engagement noise in the `search`, `authorFeed`, `thread` and `feed` post modes. Filtered-out posts are never pushed to the dataset, so you aren't billed for them.

## Thread & custom-feed modes

**`thread`** mode returns a post and available replies to the requested depth via the AT Protocol `getPostThread` endpoint. Put post links or `at://` URIs in `threadUris`, set `threadDepth` for how many reply levels to fetch, and `parentHeight` to also pull ancestor posts above the requested one. Every record carries `depth` (0 = the requested post, 1+ = replies, negative = ancestors), `parentUri`, `rootUri` and `isRoot`, so you can rebuild the returned conversation tree. Count, deadline, charge and source limits can stop collection before the requested depth is reached.

**`feed`** mode reads a custom/algorithmic feed generator via `getFeed`. Put feed links (`https://bsky.app/profile/<handle>/feed/<rkey>`) or `at://.../app.bsky.feed.generator/...` URIs in `feedUris`; handles are resolved to DIDs automatically. Records include `feedOf` (the feed) and `isRepost`.

## How to scrape Bluesky with this Actor

1. Click **Try for free** / **Run** — no Bluesky account needed, only public data is read.
2. Pick a `mode`, set a `query` or `handles`, adjust `maxItems` or keep the defaults.
3. Run it and export the dataset as JSON, CSV or Excel, or read it over the [API](https://docs.apify.com/api/v2).

Run it from your own code with `apify-client>=3.2,<4`:

```python
from apify_client import ApifyClient
from datetime import timedelta
from decimal import Decimal

import os

client = ApifyClient(os.environ["APIFY_TOKEN"])
run = client.actor("nomad-agent/bluesky-scraper").call(build="latest", run_timeout=timedelta(seconds=300),
    wait_duration=timedelta(seconds=360), max_total_charge_usd=Decimal("0.20"), run_input={
    "mode": "search",
    "query": "#ai",
    "postedWithin": "7d",
    "maxItems": 200,
})
if run is None or run["status"] != "SUCCEEDED":
    raise RuntimeError("The Actor did not complete successfully within the deadline")
print({"runId": run["id"], "buildId": run["buildId"]})
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    if item.get("mode") != "search" or not item.get("uri") or not item.get("authorHandle"):
        continue  # Diagnostic rows are not posts.
    print(item["authorHandle"], "—", (item.get("text") or "")[:80], item.get("url"))
```

Or a single HTTP call that runs the Actor and returns items in one response:

```bash
curl -X POST \
  "https://api.apify.com/v2/acts/nomad-agent~bluesky-scraper/run-sync-get-dataset-items?build=latest&timeout=300&maxTotalChargeUsd=0.20" \
  -H "Authorization: Bearer ${APIFY_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mode": "authorFeed", "handles": ["bsky.app"], "maxItems": 50}'
```

## Input

General fields (apply across modes):

| Field | Type | Default | Notes |
|---|---|---|---|
| `mode` | string | `search` | One of `search`, `authorFeed`, `thread`, `feed`, `profile`, `followers`, `following`. |
| `handles` | array | — | Handles or DIDs for `authorFeed`/`profile`/`followers`/`following`. Leading `@` optional. |
| `includeReplies` | boolean | `false` | Include the author's replies in `authorFeed` mode. |
| `maxItems` | integer | `100` | Maximum source records for the run. Set 0 to remove only the count limit; deadline, charge and source limits still apply. |

Thread & feed fields:

| Field | Type | Default | Notes |
|---|---|---|---|
| `threadUris` | array | — | Post URLs / `at://` URIs whose reply thread to scrape (`thread` mode). |
| `threadDepth` | integer | `6` | Levels of replies to fetch below the post (`thread` mode). |
| `parentHeight` | integer | `0` | Ancestor posts above the requested one to include (`thread` mode). |
| `feedUris` | array | — | Feed URLs / `at://` generator URIs to read (`feed` mode). |

Search fields (`search` mode; `minLikes`/`minReposts` also apply to `authorFeed`/`thread`/`feed`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | string | `#ai` when omitted in a plain search | Keyword/hashtag for `search` mode. Supports Bluesky search operators. The advertised `#ai` example is used for empty API input; an explicit blank query or an alternate search target is preserved. |
| `postedWithin` | string | *(omitted; form prefill `any`)* | Only posts created inside this window. `any` for no date filter, or a duration — `1h`, `24h`, `3d`, `2w`, `6m`. |
| `fromAuthor` | string | — | Only posts written by this handle (`from:` operator / `author` param). |
| `mentionsAuthor` | string | — | Only posts that mention this handle (`mentions:` operator / `mentions` param). |
| `language` | string | — | Only posts in this ISO 639-1 language code (`lang:` operator / `lang` param). |
| `domain` | string | — | Only posts linking to this domain (`domain` param). |
| `url` | string | — | Only posts linking to this exact URL (`url` param). |
| `tag` | array | — | Only posts carrying these hashtags, entered without `#` (`tag` param). |
| `sort` | string | `latest` | `latest` (newest first) or `top` (by relevance) for `search` mode. |
| `minLikes` | integer | `0` | Drop posts with fewer likes than this (compares `likeCount`). 0 = no filter. |
| `minReposts` | integer | `0` | Drop posts with fewer reposts than this (compares `repostCount`). 0 = no filter. |

## Output example

```json
{
  "mode": "search",
  "uri": "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/3lb2abcxyz",
  "cid": "bafyreib2rxk3rw6...",
  "url": "https://bsky.app/profile/bsky.app/post/3lb2abcxyz",
  "authorHandle": "bsky.app",
  "authorDid": "did:plc:z72i7hdynmk6r22z27h6tvur",
  "authorDisplayName": "Bluesky",
  "text": "Introducing new moderation tools...",
  "createdAt": "2026-06-15T18:02:11.000Z",
  "indexedAt": "2026-06-15T18:02:12.345Z",
  "langs": ["en"],
  "replyCount": 412,
  "repostCount": 1287,
  "likeCount": 9034,
  "quoteCount": 187,
  "bookmarkCount": 54,
  "replyParentUri": null,
  "replyRootUri": null,
  "tags": ["moderation"],
  "mentions": [],
  "links": ["https://bsky.social/about/blog"],
  "embedType": "external",
  "mediaUrls": [],
  "mediaAlt": [],
  "externalUrl": "https://bsky.social/about/blog",
  "externalTitle": "Bluesky Blog",
  "quotedUri": null
}
```

## Compatibility

This release returns source data without built-in AI analysis. The former `aiSentiment`, `aiTopics` and `aiSummary` fields are no longer emitted; old AI settings are ignored, and source text remains available for your own analysis. `postedWithin` is the date control shown in the input form. Saved API inputs using `since` and `until` retain their absolute date bounds when `postedWithin` is absent; an explicitly supplied `postedWithin`, including `any`, takes precedence.

## Pricing

Check the Actor's current Apify pricing panel for per-run and per-record rates. Your run's maximum total charge limits billed output.
`minLikes` and `minReposts` are applied before records are pushed, so filtered-out posts are never billed.

## Integrations

Export the dataset as JSON, CSV, Excel or HTML, pull it over the [Apify API](https://docs.apify.com/api/v2) (including `run-sync-get-dataset-items` for a single blocking call), wire it into Make, Zapier or n8n, or call it as a tool from an AI agent via the [Apify MCP server](https://docs.apify.com/platform/integrations/mcp).

## Use cases

- Brand, keyword and hashtag monitoring on Bluesky
- Social listening and sentiment datasets for AI/LLM pipelines
- Influencer discovery via follower/following graphs and engagement counts
- Exporting an account's available posts within run and source limits
- Trend and virality analysis with like/repost/reply counts

## FAQ

**Is it legal to scrape Bluesky?**
This Actor reads only publicly available data through Bluesky's own public AT Protocol API (`public.api.bsky.app`) — the same data any logged-out visitor can see. No authentication is used and no private data is touched. Review Bluesky's terms and your local regulations for your specific use case.

**Do I need a Bluesky account?**
No. All seven modes use unauthenticated public endpoints.

**How fresh is the data?**
Every run reads the live API. `search` with `sort: latest` requests the newest indexed matches; source indexing and pagination determine which posts are available.

**How many records can I get?**
`maxItems` caps the record count; set 0 to remove that count limit. Pagination still stops at the Actor's 200-second collection deadline, the run's charge cap, or source pagination limits. A note row reports when the collection deadline stops a run; it is not a post. Full account or follower archives are not guaranteed.

**What about deleted or blocked accounts?**
Handles that cannot be resolved (typos, deactivated accounts) are logged and skipped — one bad handle never fails the whole run.

**Which search operators are supported as dedicated inputs?**
`fromAuthor`, `mentionsAuthor`, `language`, `domain`, `url` and `tag` map to `searchPosts`' own `author`, `mentions`, `lang`, `domain`, `url` and `tag` filter params (verified against the live API) — equivalent to `from:`, `mentions:`, `lang:`, `domain:` and `#tag` in `query`, but composable with it via proper params instead of string-building.

**Something broken or missing?**
Open an issue on the Actor's **Issues** tab — it is monitored and reliability fixes ship fast.

## Related Actors

- [Hacker News Who Is Hiring Scraper — HN Jobs](https://apify.com/nomad-agent/hackernews-scraper)
- [Web Search Scraper](https://apify.com/nomad-agent/web-search-scraper)
- [AI & ML Engineer Jobs Scraper — 8 Boards in One](https://apify.com/nomad-agent/ml-ai-dev-bundle)

## Credits

Independent implementation on the official AT Protocol public API. Design informed by the MIT-licensed [deepfates/bsky-scraper](https://github.com/deepfates/bsky-scraper) firehose archiver — see `LICENSE` for the attribution note.

## Fast setup and source code

[Public repository](https://github.com/Exdenta/OinkAIJobSearch) · [Agent setup skill](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md).

Use the Actor’s current input form for filters, or start with its documented API example. Select `latest` for `bluesky-scraper` and record the immutable build ID and number returned by your run. Inspect the dataset and any run summary the Actor documents together; a successful status alone does not establish complete source coverage.

Restrictive filters or source availability can produce zero results. Diagnostic and note rows are uncharged status records and do not count as posts. This Actor returns source data without built-in AI analysis or translation.
