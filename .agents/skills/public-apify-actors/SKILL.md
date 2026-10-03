---
name: public-apify-actors
description: Set up and run public Nomad Agent or Job Atlas Apify Actors from their current input schema, inspect real output and verify the immutable build returned through latest.
---

Use this guide for Actor setup, API calls, result inspection and repeatable exports. Ask for the Actor URL or choose one from the public catalogue that serves the requested data.

1. Read the Actor README and current input schema before constructing input. Job readers, AI scorers, property, social, legal and menu Actors have different contracts. Do not send job filters to other Actor families. A form prefill is a Console convenience; inspect runtime defaults before assuming an empty API input is useful.
2. Keep APIFY_TOKEN in the environment. Never place it in a URL, source file, output dataset or shared transcript. Use Authorization: Bearer for HTTPS API requests.
3. Resolve GET /v2/acts/OWNER~NAME and record taggedBuilds.latest.buildId. Start POST /v2/acts/OWNER~NAME/runs?build=latest&timeout=300&maxTotalChargeUsd=0.20 with the requested JSON input. Get user authorization before starting a paid run. For a first look at job readers, request a small maxItems; enable AI or translation only when requested. Never add BYOK fields to owner-funded Actors.
4. Poll GET /v2/actor-runs/RUN_ID until terminal with a finite deadline. Record status, exitCode, buildId and buildNumber. Compare the run buildId with the previously resolved latest; if promotion raced, read the current tag and report the exact returned build instead of asserting the expected release ran. Never blindly retry a run-start POST after an ambiguous response; find that run in recent history first.
5. Read GET /v2/datasets/DATASET_ID/items and the RUN-SUMMARY record in the run key-value store. A SUCCEEDED status, a diagnostic row and a demo row are different from real source output. For jobs require source identity, title, original description and source URL. Preserve schemaVersion and source-specific fields; null means unknown and [] means explicitly empty.
6. Respect filters and repeat-delivery suppression. Zero rows can mean no matching or new records; inspect summary candidate/suppression counts. For an explicitly requested stateless job search set dedupe.enabled=false where supported. Do not clear delivery history, widen filters or synthesize results to satisfy a count.
7. Save credentials separately from exports. Report the exact Actor/build/run, real row count, source limitations and charges. A bounded Actor run verifies this route; destination writes and optional modes need their own tests.

Example first call for a simple job reader (confirm its current schema first):

```json
{"schemaVersion":"nomad-agent-simple-inventory-search-v2","maxItems":3,"postedWithin":"any","dedupe":{"enabled":false}}
```

Normalized readers use their own input and nested job output. Existing LinkedIn, EURAXESS, YC and scorer skills provide richer source-specific setup. Prefer their maintained examples when targeting those products.
