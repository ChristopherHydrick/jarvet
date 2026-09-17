# Jarvet

Jarvet is an agentic education and career facilitator for veterans. It pairs an
OpenAI-compatible tool-calling language model with authoritative local data — the
O*NET occupation graph and the VA GI Bill Comparison Tool index — so every
factual claim in a conversation is backed by a structured source rather than
model recall. The frontend renders only verified links: occupation facts,
school programs, approved providers, and official VA.gov actions each carry
their own trusted destination.

> Proof of concept. Jarvet does not make eligibility decisions or produce
> personalized benefit quotes; it surfaces official data and links for
> verification.

## Features

- **Occupation exploration** — search and inspect O*NET occupations, including
  Bright Outlook growth categories, related occupations, and work-activity
  context, via SPARQL over an embedded Oxigraph store.
- **Local training discovery** — exact-occupation school programs from My Next
  Move for Veterans / IPEDS, with a bounded crawl of each institution's own site
  to verify a real program page before promoting it.
- **VA provider search** — approved schools and employer/OJT providers near a
  resolved city/state or ZIP, with facility-level benefit facts, program
  summaries (degree, non-college, OJT, apprenticeship), and official VA
  Comparison Tool detail links.
- **VA program catalog search** — keyword search over every VA-approved
  school's own IHL (degree) and NCD (certificate/non-college) program list,
  nationwide or by state, independent of IPEDS's occupation classification.
  Complements local training discovery for proprietary trade schools (for
  example commercial diving academies) that IPEDS's CIP-to-SOC crosswalk does
  not always classify under the matching occupation.
- **Agentic tool calling** — the model decides which tools to call; Python
  validates arguments and returns structured facts. Geographic and occupational
  broadening are separate, explicit actions.
- **Direction memory** — opt-in browser-local profile, selected occupation, and
  bookmarked providers sent to the agent as soft comparison context.
- **Response caching** — successful chat turns are cached in SQLite for fast
  repeat demos, with TTL, LRU limit, and version controls.

## Architecture

| Component | Technology |
| --- | --- |
| Web app & API | Python 3.12, FastAPI, Uvicorn |
| Agent | OpenAI-compatible chat completions with native tool calling (e.g. OpenRouter) |
| Occupation graph | O*NET 31.0 N-Triples in an embedded Oxigraph store + FTS5 search index |
| School programs | IPEDS directory + completions + O*NET CIP-to-SOC crosswalk in SQLite |
| Provider & benefit data | VA GI Bill Comparison Tool workbook in SQLite + VA institution API (7-day cache) |
| VA program catalog | Bulk crawl of every approved school's IHL/NCD programs from the VA institution-programs API, indexed with FTS5 |
| Geography | Census 2025 ZCTA Gazetteer centroids for proximity and ZIP resolution |
| Frontend | Vanilla HTML/CSS/JS single page |
| Caching | SQLite response cache, VA API cache, My Next Move HTML parsing |
| Devcontainer | Docker, Cloudflare Tunnel (`cloudflared`), JupyterLab on port 7788 |

## Quick start

The devcontainer provisions everything on creation: it builds a Python virtual
environment, downloads the O*NET graph, VA workbook, and Census gazetteer,
bulk-loads the SPARQL store, and starts Jarvet, JupyterLab, and (if configured)
the Cloudflare Tunnel.

To run manually:

```bash
cp .env.example .env   # set LLM_API_KEY (OpenRouter or any OpenAI-compatible host)
./scripts/start-web.sh # serves http://localhost:8000
```

## O*NET graph data

The O*NET N-Triples graph database is downloaded from the
[O*NET Resource Center](https://www.onetcenter.org/database.html#graph) during
devcontainer setup. The extracted database is intentionally excluded from Git.

To initialize or restore it manually, run:

```bash
./scripts/init-onet-data.sh
```

The script defaults to O*NET 31.0. Set `ONET_VERSION` using underscores to
download another published version, for example `ONET_VERSION=30_2`.

The devcontainer then bulk-loads every N-Triples file into an embedded,
disk-backed Oxigraph store. Jarvet queries occupation relationships and features
with SPARQL rather than loading the 2.4 GB graph into Python memory. Rebuild the
store after changing datasets with:

```bash
.venv/bin/python scripts/init-onet-store.py
```

Initialization also downloads O*NET OnLine's official Bright Outlook CSV and
joins its current growth, openings, and new/emerging categories to occupations
by O*NET-SOC code.

## VA provider and benefit data

Initialization downloads the official VA GI Bill Comparison Tool workbook and
the Census Bureau's 2025 ZIP Code Tabulation Area Gazetteer. The VA workbook is
streamed into a compact SQLite index rather than loaded wholly into memory. The
Census coordinates let Jarvet estimate proximity from a supplied ZIP-area
centroid to the facility coordinates published in the VA workbook.

Rebuild the VA index manually with:

```bash
.venv/bin/python scripts/init-va-data.py
```

To download the latest published VA workbook and Census file before rebuilding,
run:

```bash
REFRESH_VA_DATA=1 ./scripts/init-onet-data.sh
.venv/bin/python scripts/init-va-data.py
```

### VA program catalog

`scripts/init-va-programs-data.py` bulk-crawls every approved
`school_provider` facility's IHL and NCD program list from the same public VA
institution-programs API that provider detail lookups already use for one
facility at a time, then builds an FTS5 index over the results
(`va_program_search` in `.cache/va-comparison.sqlite`). This is what backs
`VaComparison.programs_for()` and the agent's `find_va_programs` tool for
nationwide/state program search independent of IPEDS. It is not run by
`postCreateCommand.sh`: a full crawl makes roughly two API calls per approved
school (tens of thousands of requests) and can take a while. Run it manually,
and re-run it periodically to refresh:

```bash
.venv/bin/python scripts/init-va-programs-data.py
```

The crawl is resumable — facility codes already recorded are skipped on a
re-run, so an interrupted crawl can simply be restarted.

## School program data (IPEDS)

Initialization downloads the NCES IPEDS institutional directory (HD2024) and
completions (C2024_A) files plus the official O*NET Education CIP-to-SOC
crosswalk, and joins them into `.cache/ipeds.sqlite`: institutions with
coordinates and websites, and program rows keyed by O*NET-SOC code with
recent-award counts. Rebuild manually with:

```bash
.venv/bin/python scripts/init-ipeds-data.py
```

The generated source files and SQLite index are excluded from Git. The index
contains provider identity, approval and provider type, location, the published
monthly housing/living-allowance rate, Post-9/11 usage/payment aggregates,
Yellow Ribbon fields, accreditation, military-credit policy, and VA caution
flags. Jarvet presents the workbook's housing rate as a facility-level reference,
not a personalized payment quote. Actual payments depend on the veteran's
eligibility, benefit chapter and tier, rate of pursuit, training modality, and
applicable dates.

Provider cards supplement the workbook with the public VA institution API's
school certifying official, current comparison fields, and complete approved
IHL, non-college-degree, or combined OJT/apprenticeship inventories. VA returns
apprenticeships through its OJT program endpoint and identifies them with a
per-program subtype, which Jarvet preserves in card labels. Raw API responses
are cached by facility code in `.cache/va-comparison.sqlite` for seven days;
stale data is used if VA is temporarily unavailable. Program lists are filtered
against the current career and study direction and summarized in the card, with
the full official VA list linked separately.

## Web application

Jarvet runs at `http://localhost:8000` in the devcontainer. Copy `.env.example`
to `.env`, configure the host LLM, then start the service:

```bash
./scripts/start-web.sh
```

The browser never receives the LLM key. For Cloudflare Tunnel, route `jarvet.ai`
to `http://localhost:8000`. Add the remotely managed tunnel token to `.env` as
`TUNNEL_TOKEN`; the devcontainer starts `cloudflared` automatically alongside
Jarvet. Tunnel credentials and logs remain outside Git.

Jarvet uses the configured OpenAI-compatible model as a tool-calling agent. The
model decides when to search O*NET, inspect one occupation, resolve a named area
or ZIP, query exact-occupation My Next Move programs, search relevant VA
providers over a chosen radius, or attach official resources. Python validates
tool arguments and returns structured source facts; it does not automatically
switch occupations or inject the nearest unrelated provider when a search is
empty. Geographic broadening and occupational broadening are separate actions,
and related occupations are available only through an explicit agent tool. Each
recommended VA provider includes a facility-specific VA Comparison Tool detail
link derived from its official facility code. Exact provider-name or code lookup
also supports follow-up requests for the link to a previously named provider.

Benefit, school, vocational, and on-the-job-training starting points link to
official VA.gov guidance and the GI Bill Comparison Tool. Jarvet does not make
eligibility decisions or treat O*NET occupation data as a school inventory.

When a career and location are known, Jarvet loads school programs from its
local IPEDS index: the NCES institutional directory and completions files joined
to occupations through the official O*NET Education CIP-to-SOC crosswalk, built
by `scripts/init-ipeds-data.py`. This replaces scraping My Next Move, whose
local-training table derives from the same sources. Searches support three
scopes — near a city or ZIP (ranked by distance), across a state, or nationwide
— and each result reports the total program count for the scope so the agent can
say how many more exist beyond those shown. Jarvet shows recent-award counts as
evidence of program activity, not as a quality ranking. For the closest few
results, Jarvet performs a bounded crawl of the institution's own site and
verifies subject terms before promoting a program, degree, certificate,
curriculum, or catalog page. If no official program page can be verified, the
action is labeled as a source listing instead of presenting the institution
homepage as program details. Trusted program and provider actions are linked at
their names in the response and repeated in the resource list below it.
The IPEDS index identifies occupation-related school programs; the VA index
separately verifies approved facilities and supplies benefit comparison facts.
Nearby approved employer records are proximity leads, not proof that an employer
offers training for the selected O*NET occupation.

Jarvet treats OJT, apprenticeships, and other paid training as one family, since
VA publishes apprenticeships inside its OJT program data and users use the terms
interchangeably. Employer searches match the trade against provider names
semantically: approved employer/OJT names are embedded with the
`BAAI/bge-small-en-v1.5` model (via fastembed, ONNX, no external API) when the
VA index is built, and a query is embedded at search time and ranked by cosine
similarity with a small exact-word bonus. This finds relevant sponsors whose
names never mention the trade — "car repair training" matches "Automotive
Apprenticeship Group" — without canned stemming rules. When nothing matches,
the search also returns the closest approved OJT/apprenticeship sponsors
regardless of name, and the agent is instructed to inspect their approved
program lists before reporting that an area has no OJT options.

Users can bookmark a school or employer from its provider card. Saved providers
use the existing opt-in browser direction memory and are sent to the agent as
soft comparison context; they do not restrict later answers or searches unless
the user explicitly asks to search only those providers.

Successful chat turns are cached in `.cache/chat-responses.sqlite`. To preheat a
demo, walk through the intended paths once; repeating the same choices will reuse
the complete response, including profile state, suggestions, and trusted links,
across browser refreshes and server restarts. Cache entries expire after seven
days and the 500 least-recently-used limit is configurable with
`JARVET_CACHE_TTL_SECONDS` and `JARVET_CACHE_MAX_ENTRIES`. Increment
`JARVET_CACHE_VERSION` when response behavior changes and old warm entries should
be ignored. `/api/health` reports cache entries, hits, and misses, while each chat
response includes `X-Jarvet-Cache: HIT` or `MISS`.

## Performance

Slow agent turns are dominated by sequential LLM tool-call rounds, so Jarvet
caches and parallelizes everything else:

- **Shared HTTP cache** — My Next Move training tables, crawled institution
  pages, and other raw GET responses are stored in
  `.cache/http-responses.sqlite` for seven days, so repeat questions about the
  same occupation and area skip the web entirely.
- **Parallel crawling** — program-page verification fetches a school's frontier
  pages concurrently instead of one at a time, and provider detail lookups for
  a shortlist run concurrently as well.
- **VA API cache** — per-facility provider payloads are cached for seven days
  and reused when VA is temporarily unavailable.
- **Rotating status messages** — while the agent works, the frontend cycles a
  status line every three seconds so users can tell the request is progressing
  rather than stalled.

The remaining latency is the model itself: each turn can take several
tool-calling rounds against the configured LLM. Choosing a faster model in
`LLM_MODEL` is the most effective way to shorten responses further.

## API endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Serves the single-page frontend |
| `/api/health` | GET | Reports store counts, model, and cache statistics |
| `/api/chat` | POST | Runs the agent for one conversation turn |

## License

Copyright © 2026 Jarvet contributors.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See the [LICENSE](LICENSE) file for the full text.

This project uses data from sources with their own terms:

- **O*NET® 31.0 Database** by the U.S. Department of Labor, Employment and
  Training Administration (USDOL/ETA), used under the
  [CC BY 4.0 license](https://creativecommons.org/licenses/by/4.0/). O*NET® is
  a trademark of USDOL/ETA; Jarvet has modified or added to some information,
  and USDOL/ETA has not approved, endorsed, or tested these modifications.
- **VA GI Bill Comparison Tool** data and the public VA institution API,
  U.S. Department of Veterans Affairs.
- **U.S. Census Bureau Gazetteer** files, public domain.
