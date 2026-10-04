---
name: newsletter-sponsors-scraper
description: Set up nomad-agent/newsletter-sponsors-scraper from its current Apify schema, run authorized requests and inspect newsletter sponsorship signals.
---

# Newsletter sponsor discovery

Use this skill for [nomad-agent/newsletter-sponsors-scraper](https://apify.com/nomad-agent/newsletter-sponsors-scraper).
The public listing and current input schema define the supported controls; local development drafts may describe a future version.

Read the maintained [public Actor setup guide](../../.agents/skills/public-apify-actors/SKILL.md) before calling the API. This compatibility entrypoint preserves the setup link in existing Actor listings.

- Keep APIFY_TOKEN in the environment and use an Authorization header. Obtain authorization for paid executions.
- Resolve latest, select build=latest and record the immutable build returned by the run. Reconcile an ambiguous run-start response against recent history before retrying.
- Construct input from this Actor's current schema and requested filters. Job-reader fields are not universal across Actor families; do not enable optional AI unless requested.
- Inspect the dataset and any documented run summary. Count real newsletter directory records separately from warnings, diagnostics and demo records. Empty output can reflect filters or repeat suppression; preserve delivery history.
- Report source limitations, row count, run/build identity and charges. Do not promise a minimum result count or infer natural traffic from a test run.

[Source repository](https://github.com/Exdenta/OinkAIJobSearch) · [Current Actor documentation](https://apify.com/nomad-agent/newsletter-sponsors-scraper)

A public sponsor-contact flag reports an available contact or advertise route. It does not establish a subscriber count, a campaign placement or an extracted email address. Check the current Pricing tab for charges.
