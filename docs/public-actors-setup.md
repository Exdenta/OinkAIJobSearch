# Set up a public Actor

Choose an exact endpoint from the [public Actor directory](public-actors.md). Each Actor has its own fields; property, legal, menu, job-search and fit-scoring inputs are not interchangeable.

1. Open the Actor's Input, Output and Pricing tabs. Build your input from its current published schema. A Console prefill can differ from API defaults.
2. Set `APIFY_TOKEN` in your environment and send it in the `Authorization: Bearer` header. Keep credentials out of URLs, examples and exports.
3. Select `latest`. For a first job search, request a few items, leave optional AI and translation off unless needed, and set an explicit cost cap and timeout. A result limit is not a guarantee of total processing cost.
4. Start a run only within your authorized spending budget. Poll the returned run ID to a terminal status; record its immutable `buildId`, build number and any charges.
5. Read the default dataset and, where provided, `RUN-SUMMARY`. Distinguish real data from diagnostic rows, partial results and a valid empty search. Preserve source IDs and original links.
6. Test any destination separately before adding a schedule. A successful Actor run does not prove that a Sheet, database or alert received data.

Simple job rows retain source descriptions and source-supplied facts. Normalized job rows use `nomad-agent-job-v1`; fit scores use a separate result format. `null` means unknown, while the meaning of an empty list depends on the documented field. Remote work does not imply worldwide eligibility: read the original description.

Use the [generic Actor skill](../.agents/skills/public-apify-actors/SKILL.md) for bounded API calls and result inspection. Do not silently broaden filters or clear repeat history to force results.

See [Actor version updates for 4 October 2026](releases/2026-10-04-version-releases.md) for published versions and the limits of their execution evidence.
