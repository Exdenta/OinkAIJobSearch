# SPAC Redemptions Scraper — SEC EDGAR Lifecycle Tracker

> **Claude / Codex skill to describe and setup this actor: [SKILL.md](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md)**

Track the full SPAC lifecycle straight from **SEC EDGAR**: new blank-check S-1 IPO filings, Rule 425 merger communications, DEFM14A merger-vote proxies (meeting date parsed out of the proxy) and 8-K redemption disclosures (redeemed share count, per-share redemption price and trust-account withdrawal parsed out of the filing). Official public SEC data — no login, no proxies, no API key.

Use this structured feed to research SPAC events from their original filings. Point it at a date range, a single SPAC (by name, ticker or CIK), or run it on a schedule and compare snapshots downstream.

## Input

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | select | `"redemptions"` | Which lifecycle stage to track: `ipos` (S-1 blank-check filings), `mergers` (Form 425 communications), `votes` (DEFM14A proxies), `redemptions` (8-K redemption disclosures) or `all` (all four in sequence, sharing one `maxItems` budget). |
| `entity` | string | *(empty)* | Filter to one SEC filer — company name (e.g. `"AlphaVest"`), ticker or CIK. Empty = all SPACs. Ignored when `watchlist` is set. |
| `watchlist` | string[] | *(empty)* | Named basket of SPACs to track — one company name, ticker or CIK per entry. Each lifecycle search runs once per entry, sharing the `maxItems` budget; a filer matched under both its name and ticker is emitted once. Takes precedence over `entity`. See [Watchlists](#watchlists). |
| `watchlistName` | string | *(empty)* | Label for the basket — echoed into every record as `watchlistName`. |
| `postedWithin` | string | *(absent in API; form prefill `"any"`)* | An explicit relative window overrides `fromDate`/`toDate`. `any` disables the date filter; durations such as `24h`, `3d`, `2w`, `6m` start at the UTC cutoff calendar date, through today. SEC filing dates have day precision, so sub-day windows cannot provide hour-level filtering. |
| `fromDate` | string | *(empty)* | Inclusive absolute start date (`YYYY-MM-DD`); preserved for saved runs and API calls when `postedWithin` is absent or blank. A missing end bound becomes today. |
| `toDate` | string | *(empty)* | Inclusive absolute end date (`YYYY-MM-DD`); a missing start bound becomes SEC's 2001 index epoch. An explicitly supplied `postedWithin` overrides both bounds. |
| `parseFilingText` | boolean | `true` | Fetch each proxy/8-K document and extract meeting date, redeemed shares, price per share and trust withdrawal (see [Parsed fields](#parsed-fields--parse-confidence)). Applies to `votes` and `redemptions`; `ipos` and `mergers` records are index metadata only. |
| `maxItems` | integer | `100` | Hard cap on filings returned per run (1–2000; out-of-range values are clamped). You get the **newest** filings in the window: EDGAR's own search ranks by keyword relevance, so the Actor scans the whole window and sorts by filing date before applying this cap. |
| `concurrency` *(Advanced)* | integer | `3` | Parallel document fetches when `parseFilingText` is on (1–5). All SEC requests are rate-spaced internally to respect EDGAR fair-access limits. |

## What SPAC data does this scraper extract?

One flat JSON record per filing (one per accession number — exhibit hits are deduplicated). Normal filing records carry the fields described below; diagnostic records use the smaller warning shape described by `eventType`. Fields that do not apply to an event type, or that could not be extracted with confidence, are `null` — never guessed.

| Field | Meaning |
|---|---|
| `id` | SEC accession number, e.g. `0001493152-25-014682` — stable and unique per filing |
| `source` | Always `"sec-edgar"` |
| `watchlistName` | The `watchlistName` input echoed onto the record, or `null` when none was set |
| `eventType` | `ipo_filing` \| `merger_communication` \| `merger_vote_proxy` \| `redemption_disclosure` (or `diagnostic` on the single unbilled row emitted if a run errors) |
| `company` | Filer name as registered with the SEC |
| `cik` | SEC CIK, zero-padded 10 digits |
| `ticker` | Primary listed ticker, or `null` when the filer has none on record |
| `tickers` | All listed tickers (common/rights/units/warrants), possibly empty |
| `formType` | Root form: `S-1`, `425`, `DEFM14A` or `8-K` |
| `matchedFileType` | The specific file the search matched (`8-K`, `EX-99.1`, ...) |
| `filedAt` | Filing date `YYYY-MM-DD` |
| `accessionNumber` | Same as `id` |
| `filingUrl` | Direct URL of the matched document on sec.gov |
| `indexUrl` | Filing folder on sec.gov (all documents + exhibits) |
| `redeemedShares` | Parsed share count redeemed *(redemptions only)* |
| `redeemedSharesCandidates` | When one 8-K states several rival share counts (e.g. it covers two meetings), `redeemedShares` stays `null` and every candidate is listed here as a number — so you can reconcile them without re-parsing the document *(redemptions only)* |
| `redemptionPricePerShare` | Parsed per-share redemption price in USD *(redemptions only)* |
| `trustAmountRemoved` | Parsed total USD removed from the trust account *(redemptions only)* |
| `hasVoteResults` | `true` when the 8-K contains Item 5.07 vote results *(redemptions only)* |
| `meetingDate` | Parsed shareholder-meeting date `YYYY-MM-DD` *(votes only)* |
| `excerpt` | ≤300-char snippet of the filing text the numbers were read from — verify at a glance |
| `parse_confidence` | `"high"` / `"medium"` / `"low"` — see below; `null` when text parsing was skipped |
| `warnings` | Human-readable notes on anything ambiguous or unparsed (empty array when clean) |

## Parsed fields & parse confidence

SEC filings state redemption facts in prose. The parser extracts them with strict rules and reports how sure it is:

- **`high`** — all primary facts for the event type found unambiguously (redemptions: share count *and* per-share price; votes: one consistent meeting date; `ipos`/`mergers`: always `high`, the record is index metadata).
- **`medium`** — some facts found; the rest are `null` with a `warnings` entry.
- **`low`** — nothing recognized; all parsed fields `null`, `warnings` says why (unusual phrasing, data in an unfetched exhibit, document fetch failed).

When a filing mentions **multiple conflicting values** (e.g. per-meeting redemption counts plus a combined total that can't be told apart), the field is left `null` and the candidates are listed in `warnings` — the actor never picks a number for you. The `excerpt` field shows the exact sentence so you can verify in one glance.

## How to track SPAC redemptions with this Actor

1. Pick a **lifecycle stage** — `redemptions` for trust redemptions, `votes` for upcoming merger votes, `ipos` for new blank-check registrations, `mergers` for de-SPAC announcements, or `all`.
2. Optionally narrow to a single **entity** (one SPAC) or a **watchlist** (a named basket of SPACs — see [Watchlists](#watchlists)), and a **freshness window**.
3. Leave **Parse filing text** on to get the structured numbers, not just filing metadata.
4. Put the Actor on a daily schedule and compare snapshots downstream to identify new SPAC events — new redemption results land on EDGAR the same day they're filed.
5. Run and export JSON, CSV or Excel — or call it over the API:

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_APIFY_TOKEN>")
run = client.actor("nomad-agent/spac-redemptions-scraper").call(run_input={
    "mode": "redemptions",
    "postedWithin": "90d",
    "maxItems": 100,
})
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item["filedAt"], item["company"], item["redeemedShares"],
          item["redemptionPricePerShare"], item["filingUrl"])
```

```bash
curl -X POST \
  "https://api.apify.com/v2/acts/nomad-agent~spac-redemptions-scraper/run-sync-get-dataset-items?token=<YOUR_APIFY_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"mode": "redemptions", "postedWithin": "90d", "maxItems": 50}'
```

## Output example

See the [observed filing example](#observed-output-example) below for a shareholder-redemption record and its original SEC source.

## Watchlists

Set a **Watchlist** (`watchlist`) to track a fixed basket of SPACs instead of a single filer or the whole market. Add one company name, ticker or CIK per entry:

- Each lifecycle search runs **once per entry**, sharing the single `maxItems` budget.
- A filer that matches under more than one entry (e.g. its name *and* its ticker) is **de-duplicated** — one record per filing.
- The watchlist **takes precedence** over `entity`.
- Give it a **Watchlist name** (`watchlistName`) to label the output; it is echoed onto every record.

```json
{
  "mode": "all",
  "watchlist": ["ClimateRock", "AlphaVest", "0001903392"],
  "watchlistName": "Active de-SPACs"
}
```

## How the four searches work

Each stage is one EDGAR full-text search using the standard terms of art in SPAC filings, verified against live filings:

| Stage | Form filter | Matches filings containing |
|---|---|---|
| `ipos` | S-1 | "blank check company" |
| `mergers` | 425 | "business combination" |
| `votes` | DEFM14A | "special meeting" + "business combination" |
| `redemptions` | 8-K | "exercised their right to redeem" + "business combination" |

The redemption search requires both phrases to reduce matches on corporate
bond and note indentures. This qualification applies even when filing-text
parsing is off. It is a search-based SPAC event feed: filings with different
wording can be missed, and matching documents still need review before use.

EDGAR full-text search covers filings from **2001 onwards** and indexes exhibits too — redemption numbers announced in an EX-99.1 press release attached to an 8-K are found and parsed just the same.

## Integrations

Export results as JSON, CSV or Excel, or wire this Actor into [Make](https://make.com), [Zapier](https://zapier.com) or [n8n](https://n8n.io); call it programmatically with `run-sync-get-dataset-items`; or use it from AI agents via the [Apify MCP server](https://mcp.apify.com).

## Pricing

See the Actor's [Pricing tab](https://apify.com/nomad-agent/spac-redemptions-scraper/pricing) for current Actor-start and result-event prices and any platform charges that apply. Date and entity filters narrow the returned filings; `maxItems` limits the result count. Set a maximum total charge when configuring a run.

## Use cases

- SPAC arbitrage: daily feed of redemption results, trust withdrawals and per-share redemption prices
- Merger-arb calendars: upcoming de-SPAC votes with parsed meeting dates
- Deal-flow monitoring: new blank-check S-1 registrations and 425 merger announcements as they hit EDGAR
- Law firms / advisors: track redemption levels and extension votes across the active SPAC universe
- Quant/data teams: derive event-date datasets from primary SEC filings

## FAQ

**Is it legal to scrape this data?**
The Actor reads public SEC search and archive endpoints without login. Requests use an identified User-Agent and a throttled rate. Review the SEC's access guidance and the requirements for your intended use.

**Do I need an API key or login?**
No. All endpoints used are unauthenticated public SEC services.

**How fresh is the data?**
Every run queries EDGAR's current search index. A newly accepted filing may not appear until SEC indexes it; the Actor does not guarantee an indexing delay.

**Why is a numeric field null when the filing clearly states the number?**
The parser only reports values it can extract unambiguously. If a filing states several conflicting counts (common when one 8-K covers two meetings), the field is `null` and `warnings` lists the candidates — check `excerpt` and `filingUrl`. That's deliberate: no silent guessing.

**Does it cover redemptions announced only in press-release exhibits?**
Yes — EDGAR full-text search indexes exhibits (EX-99.x), and the parser runs on whichever document matched.

**Something broken or missing?**
Open an issue on the Actor's **Issues** tab — it is monitored and fixes ship fast.

## Fast setup and source code

[Public repository](https://github.com/Exdenta/OinkAIJobSearch) · [Agent setup skill](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md).

Use the Actor’s current input form for filters, or start with its documented API example. Select `latest` for `spac-redemptions-scraper` and record the immutable build ID and number returned by your run. Inspect the dataset and any run summary the Actor documents together; a successful status alone does not establish complete source coverage.

Restrictive filters or repeat-delivery suppression can produce zero results. Diagnostics and demo records are not source records. Optional AI or translation stays explicit where supported.

### Observed output example

Selected fields from a real source record inspected on 2026-10-03; consult the output schema for the full contract. Values change with the source.

```json
{
  "id": "0001829126-26-010583",
  "source": "sec-edgar",
  "watchlistName": null,
  "eventType": "redemption_disclosure",
  "company": "Invest Acquisition Corp",
  "cik": "0001857410",
  "ticker": null,
  "tickers": [],
  "formType": "8-K",
  "matchedFileType": "8-K",
  "filedAt": "2026-09-30",
  "accessionNumber": "0001829126-26-010583",
  "filingUrl": "https://www.sec.gov/Archives/edgar/data/1857410/000182912626010583/investacq_8k.htm",
  "indexUrl": "https://www.sec.gov/Archives/edgar/data/1857410/000182912626010583/",
  "redeemedShares": 1801349,
  "redeemedSharesCandidates": [],
  "redemptionPricePerShare": 12.34,
  "trustAmountRemoved": null,
  "hasVoteResults": false,
  "meetingDate": null,
  "excerpt": "n deadline of 5:00 p.m. Eastern Time on September 22, 2026, holders of 1,801,349 Class A ordinary shares exercised their right to redeem such shares for cash at a redemption price of approximately $12.34 per share, for an aggregate redemption payment of approximately $22.2 million funded from the Co",
  "parse_confidence": "high",
  "warnings": []
}
```

## Product guides and related tools

- [Product overview and public links](https://jobatlas.dev/actors/directory/public-records#nomad-agent-spac-redemptions-scraper)
- [Public setup guide](https://github.com/Exdenta/jobatlas/blob/main/docs/public-actors-setup.md)
