---
name: ptab-trials-scraper
description: Set up nomad-agent/ptab-trials-scraper from its current Apify schema, run authorized requests and inspect trial records.
---

# PTAB trial search

Use this skill for [nomad-agent/ptab-trials-scraper](https://apify.com/nomad-agent/ptab-trials-scraper).
The public listing and current input schema define the supported controls; local development drafts may describe a future version.

Read the maintained [public Actor setup guide](../../.agents/skills/public-apify-actors/SKILL.md) before calling the API. This compatibility entrypoint preserves the setup link in existing Actor listings.

- Keep APIFY_TOKEN in the environment and use an Authorization header. Obtain authorization for paid executions.
- Resolve latest, select build=latest and record the immutable build returned by the run. Reconcile an ambiguous run-start response against recent history before retrying.
- Construct input from this Actor's current schema and requested filters. Job-reader fields are not universal across Actor families; do not enable optional AI unless requested.
- Inspect the dataset and any documented run summary. Count real trial records separately from warnings, diagnostics and demo records. Empty output can reflect filters or repeat suppression; preserve delivery history.
- Report source limitations, row count, run/build identity and charges. Do not promise a minimum result count or infer natural traffic from a test run.

[Source repository](https://github.com/Exdenta/OinkAIJobSearch) · [Current Actor documentation](https://apify.com/nomad-agent/ptab-trials-scraper)

When dryRun is enabled, example rows are demonstration data and do not prove USPTO retrieval.
