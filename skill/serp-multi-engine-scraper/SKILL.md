---
name: serp-multi-engine-scraper
description: Set up the nomad-agent SERP Actor for Bing, DuckDuckGo, Baidu or Yahoo search-result exports.
---

# Multi-engine SERP setup

Use [the Actor](https://apify.com/nomad-agent/serp-multi-engine-scraper) for search-engine results. For other public products, use the [general setup skill](https://github.com/Exdenta/OinkAIJobSearch/blob/main/.agents/skills/public-apify-actors/SKILL.md).

Fetch `GET /v2/acts/nomad-agent~serp-multi-engine-scraper`, then `GET /v2/actor-builds/<buildId>` using `taggedBuilds.latest.buildId`. Inspect the build input schema or decoded `.actor/input_schema.json` source file. `queries` is required: a Console prefill is not an API default. Keep API credentials in an environment variable, outside dataset exports.

A bounded first search, after checking the schema and current Pricing tab:

```json
{"queries":["open source search engine"],"engines":["bing","duckduckgo"],"maxPagesPerQuery":1,"maxItems":5}
```

Run with `latest` and record the immutable build and run IDs returned. Read the complete dataset and run log: blocked engines can be skipped while the run still succeeds. Inspect result URLs, ranks and engine labels; a nonzero count does not establish coverage of every requested engine. Yahoo may need the supported residential proxy option; do not enable paid proxy options without authorization. Do not treat empty output or an error envelope as search results.

The [public repository](https://github.com/Exdenta/OinkAIJobSearch) hosts this compatibility entrypoint for the link in older published Actor descriptions. The Actor's current input form and README remain the source of supported controls.
