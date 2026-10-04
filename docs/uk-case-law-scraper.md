# UK Case Law Scraper — Find Case Law Judgments API

> **Claude / Codex skill to describe and setup this actor: [SKILL.md](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md)**

Search UK court judgments and tribunal decisions from The National Archives' **Find Case Law** service (caselaw.nationalarchives.gov.uk) through its public API. Filter by court, party, judge, date or exact neutral citation. Export structured records with optional full judgment text and legislation references. No login, no proxies, no HTML parsing.

Covers the UK Supreme Court, Privy Council, Court of Appeal, High Court, Family Court, Upper and First-tier Tribunals, Employment Appeal Tribunal and more — judgments from 2001 onwards (older for some courts), updated as courts publish.

### Why this Actor

- **Structured filters.** Target a specific court or tribunal (Supreme Court, Court of Appeal, a named chamber), a named **party**, a named **judge**, a date range, or one judgment by **exact neutral citation**.
- **Citation lookup** with graceful fallback — set a neutral citation and get exactly that judgment (`citationExactMatch`).
- **Legislation references** parsed straight from the official judgment XML — statutes cited, with `legislation.gov.uk` links.
- **`contentHash`** to detect when a previously-seen judgment has been revised.

## Input

Use `fromDate` and `toDate` for an absolute judgment date range, or `postedWithin` to compute a lower date bound from a relative duration. Find Case Law supplies judgment dates by calendar day: the UTC cutoff's whole calendar day is included, so hour durations do not give hour-level precision. A supplied nonempty `postedWithin` value takes precedence over both date bounds; omit it or leave it empty to preserve a saved absolute range. Citation lookup ignores all date filters.

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | `"negligence"` | Free-text search across judgments. Leave empty to browse latest judgments matching the other filters. Ignored when `citation` is set. |
| `citation` | string | *(empty)* | Look up one judgment by its exact neutral citation, e.g. `"[2024] UKSC 1"`. When set, this takes over the search: `query`, `courts`, `party`, `judge` and `postedWithin` are all ignored. Returns the exact citation match(es); falls back to raw (unfiltered) search hits if nothing matches exactly. See [Citation lookup](#citation-lookup) below. |
| `courts` | array (multi-select) | *(empty = all)* | Court/tribunal codes to filter by — all 37 collections on Find Case Law, see [Court codes](#court-codes). Ignored when `citation` is set. |
| `party` | string | *(empty)* | Filter on a party's name (claimant, defendant, appellant, respondent...). Ignored when `citation` is set. |
| `judge` | string | *(empty)* | Filter on the judge's name. Ignored when `citation` is set. |
| `postedWithin` | string | *(omitted; form prefill `"any"`)* | Relative duration converted to an inclusive UTC calendar-date lower bound: `24h`, `3d`, `2w`, `6m`, or `any` to clear date filtering. The whole cutoff day is included, even for hour durations. Explicit values override both absolute bounds. Ignored when `citation` is set. |
| `fromDate` / `toDate` | string | *(empty)* | Inclusive lower / upper judgment date in `YYYY-MM-DD`. Preserved for saved configurations and API clients; ignored when `postedWithin` is nonempty or `citation` is set. |
| `includeFullText` | boolean | `false` | Fetch each judgment's XML and include parties, judges, the full judgment text and structured legislation references. Slower and heavier output. |
| `maxItems` | integer | `50` | Hard cap on judgments considered per run. `0` = no cap (use with care). |
| `concurrency` *(Advanced)* | integer | `4` | Parallel detail fetches when `includeFullText` is on. Keep low — this is a public government service. |

## What UK case-law data does this scraper extract?

One flat JSON record per judgment:

| Field | Meaning |
|---|---|
| `citation` | Neutral citation, e.g. `[2024] UKSC 1` |
| `title` | Case name (parties as titled by the court) |
| `court` | Court / chamber name, e.g. `Court of Appeal (Civil Division)` |
| `date` | Judgment date (`YYYY-MM-DD`) |
| `url` | Judgment page on Find Case Law |
| `pdfUrl` | Official PDF |
| `xmlUrl` | Akoma Ntoso (LegalDocML) XML |
| `slug` | Court/year/number path, e.g. `uksc/2024/1` |
| `fclId` | Find Case Law document ID |
| `contentHash` | Content hash — detect judgment revisions |
| `parties` | Party names (with **Include full text**) |
| `judges` | Judge names where marked up (with **Include full text**) |
| `fullText` | Plain-text judgment body (with **Include full text**) |
| `legislationRefs` | Statutes cited in the judgment — array of `{text, uri, canonical}`, e.g. `{"text": "Data Protection Act 1998", "uri": "http://www.legislation.gov.uk/id/ukpga/1998/29", "canonical": "1998 c. 29"}` (with **Include full text**) |
| `citationExactMatch` | `true`/`false` — only present when **Citation** is set; see [Citation lookup](#citation-lookup) |

## How to scrape UK judgments with this Actor

1. Enter a **search query** (`negligence`, `data protection`, a party name...) — or leave it empty and filter by court/date only. Or set **Citation** to look up one specific judgment (see below).
2. Optionally restrict by **courts** (`uksc`, `ewca/civ`, `ewhc/kb`, `eat`, ...), **party**, **judge**, **postedWithin**.
3. Turn on **Include full text** if you need parties, judges and the judgment body.
4. Schedule the Actor and compare snapshots downstream when polling the same query for new judgments.
5. Run and export JSON, CSV or Excel — or call it over the API:

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_APIFY_TOKEN>")
run = client.actor("nomad-agent/uk-case-law-scraper").call(build="latest", run_input={
    "query": "negligence",
    "courts": ["uksc", "ewca/civ"],
    "maxItems": 100,
})
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item["citation"], "|", item["title"], item["url"])
```

```bash
curl -X POST \
  "https://api.apify.com/v2/acts/nomad-agent~uk-case-law-scraper/run-sync-get-dataset-items?token=<YOUR_APIFY_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"query": "negligence", "courts": ["uksc"], "maxItems": 50}'
```

## Output example

```json
{
  "source": "find-case-law",
  "citation": "[2024] UKSC 1",
  "title": "Paul and another v Royal Wolverhampton NHS Trust",
  "court": "United Kingdom Supreme Court",
  "date": "2024-01-11",
  "url": "https://caselaw.nationalarchives.gov.uk/uksc/2024/1",
  "pdfUrl": "https://assets.caselaw.nationalarchives.gov.uk/uksc/2024/1/uksc_2024_1.pdf",
  "xmlUrl": "https://caselaw.nationalarchives.gov.uk/uksc/2024/1/data.xml",
  "slug": "uksc/2024/1",
  "fclId": "gxdrmpqf",
  "parties": ["Paul and another", "Royal Wolverhampton NHS Trust"],
  "judges": ["Lord Leggatt", "Lady Rose"],
  "legislationRefs": [
    {"text": "Fatal Accidents Act 1976", "uri": "http://www.legislation.gov.uk/id/ukpga/1976/30", "canonical": "1976 c. 30"}
  ],
  "fullText": "Hilary Term [2024] UKSC 1 On appeal from..."
}
```

`citationExactMatch` is added on top of this shape when **Citation** is set — see [Citation lookup](#citation-lookup).

## Court codes

All **37** collections published on Find Case Law — every court and tribunal the service carries, with the years each one covers. The **Courts & tribunals** input is a multi-select of exactly these codes: pick as many as you need, or leave empty for all.

| Code | Court | Coverage |
|---|---|---|
| `uksc` | Supreme Court | 2009– |
| `ukpc` | Privy Council | 2009– |
| `ewca/civ` | Court of Appeal, Civil Division | 2001– |
| `ewca/crim` | Court of Appeal, Criminal Division | 2003– |
| `ewhc/admin` | High Court, Administrative Court | 2003– |
| `ewhc/admlty` | High Court, Admiralty Court | 2003– |
| `ewhc/ch` | High Court, Chancery Division | 2003– |
| `ewhc/comm` | High Court, Commercial Court | 2003– |
| `ewhc/fam` | High Court, Family Division | 2003– |
| `ewhc/ipec` | High Court, Intellectual Property Enterprise Court | 2013– |
| `ewhc/kb` | High Court, King's / Queen's Bench Division | 2003– |
| `ewhc/mercantile` | High Court, Mercantile Court | 2008–2014 |
| `ewhc/pat` | High Court, Patents Court | 2003– |
| `ewhc/scco` | High Court, Senior Courts Costs Office | 2003– |
| `ewhc/tcc` | High Court, Technology and Construction Court | 2003– |
| `ewcr` | Crown Court | 2020– |
| `ewcc` | County Court | 2019– |
| `ewfc` | Family Court | 2014– |
| `ewcop` | Court of Protection | 2009– |
| `eat` | Employment Appeal Tribunal | 2021– |
| `ukut/aac` | Upper Tribunal, Administrative Appeals Chamber | 2011– |
| `ukut/iac` | Upper Tribunal, Immigration and Asylum Chamber | 2007– |
| `ukut/lc` | Upper Tribunal, Lands Chamber | 2014– |
| `ukut/tcc` | Upper Tribunal, Tax and Chancery Chamber | 2016– |
| `ukftt/tc` | First-tier Tribunal, Tax Chamber | 2019– |
| `ukftt/grc` | First-tier Tribunal, General Regulatory Chamber | 2009– |
| `ukftt/hesc` | First-tier Tribunal, Care Standards | 1985– |
| `ftt/pc` | First-tier Tribunal, Land Registration Division | 2024– |
| `ftt/phl` | First-tier Tribunal, Primary Health Lists | 2025– |
| `ukiptrib` | Investigatory Powers Tribunal | 2023–2024 |
| `siac` | Special Immigration Appeals Commission | 2003– |
| `ftt/claims` | Claims Management Services Tribunal | 2010–2011 |
| `ukftt/credit` | Consumer Credit Appeals Tribunal | 2008–2013 |
| `ukftt/estate` | Estate Agents Tribunal | 2010 |
| `ukit` | Information Tribunal | 1990–2010 |
| `ukist` | Immigration Services Tribunal | 2001–2009 |
| `ftt/transport` | Transport Tribunal | 2000–2009 |

## Citation lookup

Set **Citation** to a neutral citation (e.g. `[2024] UKSC 1`) to look up one specific judgment instead of running a broad search. When set:

- `query`, `courts`, `party`, `judge` and `postedWithin` are all ignored.
- The Actor searches Find Case Law for that citation text, then keeps only the result(s) whose citation matches **exactly** (whitespace/case-insensitive) — tagged `"citationExactMatch": true`.
- If nothing matches exactly (e.g. a slightly malformed citation), it falls back to returning the raw, unfiltered search hits for that text instead, tagged `"citationExactMatch": false`, so you still get something useful to inspect. The fallback is capped at 25 records, so a mistyped citation can never run up a large bill.

## Integrations

Export results as JSON, CSV or Excel, or wire this Actor into [Make](https://make.com), [Zapier](https://zapier.com) or [n8n](https://n8n.io); call it programmatically with `run-sync-get-dataset-items`; or use it from AI agents via the [Apify MCP server](https://mcp.apify.com).

## Pricing

Check the Actor's current Apify pricing panel for metadata and full-text rates. Your run's maximum total charge limits billed output.

## Use cases

- Legal research: track new judgments by topic, court or judge
- Litigation intelligence: monitor cases naming a company or party
- Feed RAG / AI legal assistants with authoritative primary law
- Academic analysis of UK courts and tribunals
- Compliance and news alerting on precedent-setting decisions

## FAQ

**Is it legal to scrape this data?**
Yes — Find Case Law is The National Archives' official open service and this Actor uses only its documented public API. Judgments are Crown copyright, re-usable under the [Open Justice Licence](https://caselaw.nationalarchives.gov.uk/open-justice-licence). Note: the licence requires attribution and does **not** cover programmatic *computational analysis* (e.g. bulk text/data mining) — that needs a (free) licence application to The National Archives. Retrieval, research, republication with attribution are fine; check the licence for your use case.

**Do I need an API key or login?**
No. The Find Case Law public API is unauthenticated.

**How far back does coverage go?**
Consistently from 2001–2003 onwards depending on court; the archive grows as The National Archives ingests older judgments.

**How fresh is the data?**
Every run hits the live API — new judgments appear as courts publish them, often same-day.

**Something broken or missing?**
Open an issue on the Actor's **Issues** tab — it is monitored and fixes ship fast.

## Fast setup and source code

[Public repository](https://github.com/Exdenta/OinkAIJobSearch) · [Agent setup skill](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md).

Use the Actor’s current input form for filters, or start with its documented API example. Select `latest` for `uk-case-law-scraper` and record the immutable build ID and number returned by your run. Inspect the dataset and any run summary the Actor documents together; a successful status alone does not establish complete source coverage.

Restrictive filters or repeat-delivery suppression can produce zero results. Diagnostics and demo records are not source records. Optional AI or translation stays explicit where supported.

### Observed output example

Selected fields from a real source record inspected on 2026-10-03; consult the output schema for the full contract. Values change with the source.

```json
{
  "source": "find-case-law",
  "title": "Sarah Douglas v Ronald Channon",
  "citation": "[2026] EWHC 2475 (KB)",
  "court": "High Court (King's Bench Division)",
  "date": "2026-10-02",
  "url": "https://caselaw.nationalarchives.gov.uk/ewhc/kb/2026/2475",
  "xmlUrl": "https://caselaw.nationalarchives.gov.uk/ewhc/kb/2026/2475/data.xml",
  "pdfUrl": "https://assets.caselaw.nationalarchives.gov.uk/d-31654798-dd10-4a9c-8170-97785f6ddb6b/d-31654798-dd10-4a9c-8170-97785f6ddb6b.pdf",
  "slug": "ewhc/kb/2026/2475",
  "fclId": "vp7np8np",
  "documentUri": "d-31654798-dd10-4a9c-8170-97785f6ddb6b",
  "contentHash": "5d15acf3ec5469f8ca83f390a71b7627a0b73c7a903eb6fba8c8e44396e0560b",
  "updatedAt": "2026-10-02T09:45:53+00:00"
}
```
