# Airbnb Scraper — Listings Search, Detail & Reviews

Scrape Airbnb listings into clean, structured JSON — search any location (or a lat/lng map area) with dates, guests, price, room-type and bedroom/bed filters, or resolve listing URLs to full detail records (optionally including an availability calendar and per-listing reviews). Built for reliability: automatic price-range splitting gets past Airbnb's ~270-result cap, blocks are classified and logged (never crash a run), and every record comes back in one flat, predictable shape.

> **Claude / Codex skill to describe and setup this actor: [SKILL.md](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md)**

## What Airbnb data does this scraper extract?

One flat JSON record per listing:

| Field | Meaning |
|---|---|
| `id` | Airbnb listing id |
| `url` | Direct `airbnb.com/rooms/...` link |
| `title` | Listing name |
| `propertyType` | e.g. "Entire rental unit", "Apartment in Lisbon" |
| `price` / `currency` / `pricePer` | Numeric price, ISO currency, qualifier (`total` / `night`) |
| `rating` / `reviewsCount` | Guest rating and review count |
| `roomType` | Entire home/apt, Private room, Shared room, Hotel room |
| `beds` / `bedrooms` / `baths` | Where Airbnb exposes them |
| `lat` / `lng` | Listing coordinates |
| `hostName` | Host first name (detail mode) |
| `thumbnail` | URL of the primary photo, where exposed |
| `imagesCount` | Number of photos |
| `amenitiesCount` | Number of amenities (detail mode) |
| `personCapacity` / `isSuperhost` / `description` | Detail mode extras |
| `calendar` | `[{month, year, availableDays}, ...]` — only populated when `calendarMonths` > 0 (detail mode); otherwise `null` |
| `reviews` | `[{id, author, date, rating, language, text, response}, ...]` — only populated when `includeReviews` is on (detail mode); otherwise `null` |
| `mode` | `search` or `detail` |

## Input

| Name | Type | Default | Description |
|---|---|---|---|
| `mode` | string (`search` \| `detail`) | `"search"` | Search a location, or resolve specific listing URLs/ids to full detail records. |
| `location` | string | — | Where to search, as typed on airbnb.com (e.g. `"Lisbon, Portugal"`). Required in search mode unless you use `latitude`/`longitude`. |
| `latitude` / `longitude` | number | — | Search mode. Centre of a coordinate/map search that bypasses named-location geocoding. Set both; `location` then becomes optional. Best-effort. |
| `maxLocationDeviation` | number | `0.05` | Search mode. Half-width in degrees of the map box around `latitude`/`longitude` (~0.05° ≈ 5.5 km). |
| `listingUrls` | array of strings | — | Listing URLs or bare numeric ids. Required in detail mode. |
| `checkIn` / `checkOut` | string (`YYYY-MM-DD`) | — | Optional dates. With both set, prices become stay totals. |
| `priceMin` / `priceMax` | integer | `0` | Nightly price filter bounds. `0` = no bound. |
| `adults` / `children` | integer | `0` | Guest counts. |
| `roomType` | string (enum) | `"any"` | `any`, `entire_home`, `private_room`, `shared_room`, `hotel_room`. |
| `minBedrooms` | integer | `0` | Only listings with at least this many bedrooms. `0` = no filter. |
| `minBeds` | integer | `0` | Only listings with at least this many beds. `0` = no filter. |
| `minBathrooms` | integer | `0` | Only listings with at least this many bathrooms. `0` = no filter. |
| `calendarMonths` | integer | `0` | Detail mode only. Fetch this many months (0–12) of availability into the `calendar` output field. `0` = skip. Best-effort (see Known limitations). |
| `includeReviews` | boolean | `false` | Detail mode only. Also fetch per-listing guest reviews into the `reviews` output field. Best-effort, cost-bounded by `maxReviewsPerListing`. |
| `maxReviewsPerListing` | integer | `10` | Cap on reviews fetched per listing when `includeReviews` is on (paged 50 at a time). |
| `maxItems` | integer | `100` | Hard cap on listings returned. |
| `currency` | string | `"USD"` | ISO currency code for returned prices. |
| `concurrency` *(Advanced)* | integer | `2` | Parallel listing fetches in detail mode. Values above `4` (up to `8`) are an aggressive opt-in — faster but more blocks. Keep low — Airbnb rate-limits aggressively. |
| `maxPriceSplits` *(Advanced)* | integer | `8` | How many times a search may auto-split its price range past the ~270-result cap. Lower it to cap cost on dense markets. |
| `proxyConfiguration` | object | Apify Residential | Proxy group. Residential is strongly recommended (its traffic is included in the per-event prices — see Pricing below). |

## How to scrape Airbnb listings with this Actor

**Search mode** — set `location` (e.g. `"Lisbon, Portugal"`), optionally `checkIn`/`checkOut`, `priceMin`/`priceMax` (per night), `adults`, `roomType`, `minBedrooms`/`minBeds`/`minBathrooms` and `maxItems`. Instead of a named `location`, you can search a **map area** by setting `latitude`/`longitude` (with an optional `maxLocationDeviation` radius in degrees) — handy for programmatic, gridded market scans. With dates set, prices are stay totals for those dates. When a query hits Airbnb's ~270-result ceiling, the Actor automatically splits the price range into sub-ranges (up to `maxPriceSplits` times) and searches each — so large markets still come back complete up to your `maxItems`.

**Detail mode** — set `mode` to `detail` and pass `listingUrls` (full URLs or bare numeric ids). Each listing resolves to a full record including coordinates, per-category ratings source, host, photo and amenity counts, and a complete plain-text description. If the detail API omits its description section, the Actor falls back to the public page's canonical listing description. Set `calendarMonths` for a month-by-month availability summary; turn on `includeReviews` to also pull up to `maxReviewsPerListing` guest reviews per listing into the `reviews` field.

```python
from apify_client import ApifyClient

client = ApifyClient("<YOUR_APIFY_TOKEN>")
run = client.actor("nomad-agent/airbnb-scraper").call(run_input={
    "mode": "search",
    "location": "Lisbon, Portugal",
    "checkIn": "2026-09-23",
    "checkOut": "2026-09-28",
    "priceMax": 150,
    "maxItems": 200,
    "proxyConfiguration": {"useApifyProxy": True,
                           "apifyProxyGroups": ["RESIDENTIAL"]},
})
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item["price"], item["currency"], "|", item["title"], item["url"])
```

```bash
curl -X POST \
  "https://api.apify.com/v2/acts/nomad-agent~airbnb-scraper/run-sync-get-dataset-items?token=<YOUR_APIFY_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"mode": "detail", "listingUrls": ["https://www.airbnb.com/rooms/796929"]}'
```

## Output example

Real `push_data` shape (detail mode, live-fetched 2026-07 — the example description is abbreviated here for readability; emitted detail records retain the complete source body):

```json
{
  "id": "796929",
  "url": "https://www.airbnb.com/rooms/796929",
  "title": "Rental unit in Lisbon",
  "propertyType": "Entire rental unit",
  "price": 465.0,
  "currency": "EUR",
  "pricePer": "total",
  "rating": 4.92,
  "reviewsCount": 463,
  "roomType": "Entire home/apt",
  "beds": 2.0,
  "bedrooms": 1.0,
  "baths": 1.0,
  "lat": 38.71991,
  "lng": -9.13289,
  "hostName": "Bruno",
  "imagesCount": 44,
  "amenitiesCount": 63,
  "personCapacity": 4,
  "isSuperhost": true,
  "description": "A very cool flat in a great location! ...",
  "calendar": null,
  "mode": "detail"
}
```

With `calendarMonths` set (e.g. `2`), `calendar` is populated instead of `null`:

```json
{
"calendar": [
  {"month": 7, "year": 2026, "availableDays": 2},
  {"month": 8, "year": 2026, "availableDays": 8}
]
}
```

With `includeReviews` on, `reviews` is populated (each up to `maxReviewsPerListing`):

```json
{
"reviews": [
  {"id": "1234567890", "author": "Ana", "date": "May 2026", "rating": 5,
   "language": "en", "text": "Great location, spotless flat.", "response": null}
]
}
```

## Pricing

Pay per event — you pay per listing returned, search results and detail
records alike. Current rates are on this Actor's **Pricing** tab. No subscription — pay only for what you fetch.

**Platform usage is included.** Compute, storage, and proxy traffic — including
the default Residential proxy (`{"useApifyProxy": true, "apifyProxyGroups":
["RESIDENTIAL"]}`) — are covered by the per-event prices. You are never billed
separately for this Actor's platform usage.

## Integrations

Dataset results export straight to JSON, CSV, Excel/XLSX, or a plain HTML table from the Apify Console. Pull them into Make, Zapier or n8n via Apify's native integrations, call `run-sync-get-dataset-items` from any HTTP client (see the `curl` example above), or use the [Apify MCP server](https://apify.com/apify/actors-mcp-server) to let an LLM agent drive this Actor and read results directly.

## Why this Actor?

Airbnb scrapers are notorious for flaky runs and messy nested output. This one is built around three choices: (1) it reads the same structured payloads Airbnb's own site renders from, not brittle HTML selectors; (2) every record is one flat object with stable field names — no digging through `sections[12].section.cardData`; (3) blocks and rate limits are detected and reported with the partial results you already paid for, instead of a crashed run.

## Anti-blocking notes

Airbnb aggressively blocks datacenter and shared IPs. **Residential proxies are strongly recommended** — set `proxyConfiguration` to Apify residential (the default prefill). The Actor uses a fixed browser User-Agent, randomized 1–2.5 s delays between requests, and low concurrency (default 2; up to 8 as an aggressive opt-in). A 403/429/challenge response stops the run gracefully and keeps everything collected so far.

## Use cases

- Market and pricing research for short-term rentals (STR analytics)
- Competitive sets for property managers and revenue managers
- Feeding availability/pricing data into investment models
- Monitoring specific listings (price, rating, review count over time)

## FAQ

**Is it legal to scrape Airbnb?**
The Actor only reads publicly visible listing data — the same data any logged-out visitor sees. Review Airbnb's terms and your local regulations for your use case; you are responsible for how you use the data.

**Why do I get fewer results than Airbnb claims exist?**
Airbnb itself only serves ~15 pages (~270 listings) per query. The Actor splits price ranges to go past that; extremely dense markets may still need narrower filters (dates, room type).

**Why are prices totals, not per night?**
With `checkIn`/`checkOut` set (or when Airbnb picks default dates), listing cards show stay totals — `pricePer` tells you which one you got. Divide by nights for a nightly rate.

**The run says "blocked" — what now?**
Switch to residential proxies, lower concurrency to 1, and retry. Partial results are kept.

**Can I filter by property type (house, apartment, guesthouse, ...) instead of room type?**
Not currently. `roomType` (entire home / private room / shared room / hotel room) is a confirmed, documented-by-reverse-engineering raw filter on Airbnb's search page. A separate "property type" facet (house vs. apartment vs. guesthouse) does not have a similarly discoverable raw query param on the public search page — Airbnb only exposes that facet through its interactive UI, not a stable URL param we could verify. `propertyType` is still returned per-listing in the output (e.g. `"Entire rental unit"`) — it just isn't a request-side filter.

**How reliable is `calendarMonths`?**
It replays an internal, unauthenticated Airbnb GraphQL persisted query (`PdpAvailabilityCalendar`) using a known-good operation hash — live-verified working as of 2026-07. Unlike the amenities count (whose operation hash is rediscovered from a JS bundle on every run), this hash is not currently re-discoverable at runtime from any bundle we could find referenced by the listing page, so it's a static snapshot. If Airbnb ever rotates it, calendar fetches fail closed (best-effort — the rest of the record is unaffected) and `calendar` comes back `null` for that run until the Actor is updated.

**How reliable are `reviews`?**
Same best-effort footing as `calendarMonths`: reviews come from an internal, unauthenticated Airbnb GraphQL persisted query using a known-good operation hash. If Airbnb rotates that hash the review fetch fails closed (`reviews` comes back `null` for that run) — the rest of each record is unaffected, and the run still succeeds. Reviews are opt-in (`includeReviews`) and capped by `maxReviewsPerListing` to keep runs cost-bounded.

**Something broken or missing?**
Open an issue on the Actor's **Issues** tab — it is monitored and fixes ship fast.

## Related Actors

- [Greenhouse Jobs Scraper — Company Job Boards](https://apify.com/nomad-agent/greenhouse-jobs-scraper)
- [Lever Jobs Scraper — Company Job Postings API](https://apify.com/nomad-agent/lever-jobs-scraper)
- [LinkedIn Jobs Scraper — No Login, No Cookies](https://apify.com/nomad-agent/linkedin-scraper)

## Attribution

See `NOTICE`. Price-range-splitting pagination strategy inspired by [digital-engineering/airbnb-scraper](https://github.com/digital-engineering/airbnb-scraper) (GPL-3.0) — **algorithm inspiration only, no code copied, translated or derived**. All code in this Actor was written from scratch against Airbnb's current site (2026) and is MIT-licensed.

## Fast setup and source code

[Public repository](https://github.com/Exdenta/OinkAIJobSearch) · [Agent setup skill](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md).

Use the Actor’s current input form for filters, or start with its documented API example. Select `latest` for `airbnb-scraper` and record the immutable build ID and number returned by your run. Inspect the dataset and any run summary the Actor documents together; a successful status alone does not establish complete source coverage.

Restrictive filters or repeat-delivery suppression can produce zero results. Diagnostics and demo records are not source records. Optional AI or translation stays explicit where supported.
