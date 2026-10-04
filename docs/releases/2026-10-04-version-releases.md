# Actor version updates - 4 October 2026

The versions below were published on Apify with `latest` as the production and default selector. Input and output schemas, event prices and public availability were preserved. A published build and a successful run are separate checks.

| Actor | Published version | Change |
| --- | --- | --- |
| [AI Menu Parser](https://apify.com/nomad-agent/ai-menu-parser) | 0.1.29 | Direct setup-skill link. |
| [Managed AI Menu Parser](https://apify.com/nomad-agent/ai-menu-parser-managed) | 0.1.21 | Direct setup-skill link. |
| [Flat AI Menu Parser](https://apify.com/nomad-agent/ai-menu-parser-flat) | 0.1.15 | Setup link and removal of an outdated price reference. Check the current Pricing tab. |
| [Dish Photo Matcher](https://apify.com/nomad-agent/dish-photo-matcher) | 0.1.27 | Direct setup-skill link. |
| [Managed Dish Photo Matcher](https://apify.com/nomad-agent/dish-photo-matcher-managed) | 0.1.18 | Direct setup-skill link. |
| [Google Maps Menu Scraper](https://apify.com/nomad-agent/google-maps-menu-scraper) | 0.1.34 | Direct setup-skill link. |
| [Managed Google Maps Menu Scraper](https://apify.com/nomad-agent/google-maps-menu-scraper-managed) | 0.1.25 | Direct setup-skill link. |
| [Devex Jobs](https://apify.com/nomad-agent/devex-jobs-scraper) | 0.90.2 | More reliable shared delivery handling, terminal summaries and setup instructions. |
| [Tecnoempleo](https://apify.com/nomad-agent/tecnoempleo-scraper) | 0.1.38 | Compatible runtime dependencies and corrected output-example validation. |
| [Normalized EuroBrussels](https://apify.com/nomad-agent/normalized-eurobrussels-jobs-scraper) | 1.90.2 | Improved runtime diagnostics and delivery handling. |
| [Job Atlas EuroBrussels](https://apify.com/jobatlas/normalized-eurobrussels-jobs-scraper) | 1.90.1 | Matching normalized runtime; a controlled stateless test returned two genuine jobs through the promoted default selector. |
| [Job Atlas LinkedIn](https://apify.com/jobatlas/linkedin-enrich-translate-normalize-scraper) | 1.0.9 | Direct setup-skill link and a JSON input example. Runtime unchanged. |

## What the default tests established

Native API runs using `{}` on the released job builds returned 12 Devex rows with complete bodies, including two incorrect titles requiring source-data repair. Tecnoempleo initially returned no rows while job data was unavailable; after its ordinary source refresh recovered, a new native default test returned 80 rows. EuroBrussels returned no new jobs because all 52 matching postings were suppressed as repeats. These observations are dated tests, not promises of future result counts.

Read `RUN-SUMMARY` as well as the platform status. An unavailable source, a valid empty search and repeat suppression have different meanings. Do not clear delivery history or silently broaden filters to force a result. Labelled demos and diagnostics are not source records. Optional AI enrichment and translation keep their documented controls.

Use the [public Actor setup skill](https://github.com/Exdenta/jobatlas/blob/main/.agents/skills/public-apify-actors/SKILL.md), select `latest`, and record the immutable build ID returned by each run. Menu nutrition and allergen estimates need confirmation from the venue.