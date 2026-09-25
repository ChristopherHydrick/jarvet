from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from app.ipeds import IpedsIndex
from app.onet import OnetGraph
from app.pathways import build_pathway, summary_for_model
from app.programs import discover_admissions_page
from app.va import VaComparison

# uvicorn's own logger, so tool calls show in `docker logs jarvet`.
logger = logging.getLogger("uvicorn.error")

TrainingFetcher = Callable[[str, str], Awaitable[list[dict[str, str]] | None]]

# A city resolves to a single point (the centroid of its most common school
# ZIP -- see VaComparison.resolve_area), not to where the veteran actually
# lives within it, so a strict cutoff drops schools the veteran would
# reasonably count as "within 20 miles" (a "20 miles of Redwood City" search
# lost San Jose State at 20.3 mi purely from where the center point landed).
# Every distance cutoff is quietly widened by this much; the reply still
# talks about the radius the veteran asked for.
RADIUS_BUFFER_MILES = 5.0
AFSC_SKILL_LEVEL = re.compile(r"\s+(Helper|Apprentice|Journeyman|Craftsman|Superintendent)$")
# Cap on "Related programs" schools per search: each one costs a live VA API
# call to enrich its card, and past this many the section stops advising and
# starts burying the exact matches.
RELATED_FACILITY_LIMIT = 15
# The "Your path forward" ladder keeps a program scoring this low against
# its field only when the same school has a confident program on the same
# track (see app/pathways.py build_pathway).
PATHWAY_SAME_SCHOOL_MIN_SCORE = 0.65

# The system prompt already tells the model to call find_va_programs directly
# for a named-program/trade search, skipping search_occupations/get_occupation
# as a prerequisite -- but in practice the model still sometimes resolves an
# occupation first anyway, spending an extra O*NET round trip the request
# never needed. Rather than trust the instruction alone, the first tool call
# of a turn is pinned to find_va_programs (see force_program_search below)
# whenever the latest message looks like a named-program search and no
# occupation is already selected. Deliberately conservative in both
# directions: NAMED_PROGRAM_HINTS requires program/school/training/certificate
# wording (not just any mention of a trade), and CAREER_EXPLORATION_HINTS
# backs off for phrasing that actually is about the occupation itself, so a
# genuine "what does an electrician do" question still reaches O*NET. A false
# positive here only costs one extra find_va_programs call before the model
# is free to call O*NET on later rounds -- much cheaper than the O*NET call
# this is meant to skip.
NAMED_PROGRAM_HINTS = re.compile(
    r"\b(?:programs?|certificates?|certifications?|schools?|diploma|"
    r"training\s+for|courses?\s+in)\b",
    re.I,
)
CAREER_EXPLORATION_HINTS = re.compile(
    r"\bwhat\s+(?:career|job|kind\s+of\s+work|does\s+an?|is\s+an?)\b|"
    r"\bjob\s+outlook\b|\bbright\s+outlook\b|\brelated\s+occupations\b|"
    r"\bexplore\s+career|\bwhich\s+career|\bcareer\s+path|degree.to.career",
    re.I,
)


def wants_named_program_search(message: str, selected_occupation: dict[str, str] | None) -> bool:
    if selected_occupation is not None or not message:
        return False
    return bool(NAMED_PROGRAM_HINTS.search(message)) and not CAREER_EXPLORATION_HINTS.search(message)


# The model has been observed inventing a radius_miles value (often the
# schema's own maximum, 500) for find_va_programs even when the user asked
# for nationwide/entire-country results and never named a distance at all --
# which then either wrongly narrows a nationwide search to "within 500
# miles of nowhere" or, since no location was given either, trips the
# radius-needs-a-location guard and makes the model ask for a ZIP the user
# never needed to give. Rather than trust the model not to do this, a
# nationwide-scope request from the user's own latest wording strips any
# radius_miles/location the model passed for find_va_programs on that call,
# the same defense-in-depth approach NAMED_PROGRAM_HINTS/force_program_search
# already uses above.
# "nationwide"/"nation-wide" always mean nationwide on their own. Everything
# else needs BOTH a broadening word (all/every/entire/whole) AND a country
# reference SOMEWHERE in the message -- not necessarily adjacent to each
# other, since real phrasing rarely puts them next to each other ("show me
# all diver programs in usa" has "diver programs in" sitting between "all"
# and "usa", which an earlier adjacency-only version of this check missed).
# Deliberately excludes the bare pronoun "us" ("show us...", "near us...")
# from the country references -- far too common in ordinary phrasing to use
# as a signal, unlike "usa" or the punctuated abbreviation "u.s.".
# Above this many programs, a "list every program" reply summarizes instead of
# retyping each name; the school's card shows the complete list.
FULL_LIST_REPLY_LIMIT = 40
NATIONWIDE_SCOPE_HINTS = re.compile(r"\bnationwide\b|\bnation\s*-?\s*wide\b", re.I)
NATIONWIDE_QUALIFIER_WORDS = re.compile(r"\b(?:entire|whole|all|every)\b", re.I)
COUNTRY_REFERENCE_WORDS = re.compile(
    r"\busa\b|\bu\.s\.a\.\b|\bunited\s+states\b|\bu\.s\.\b|\bamerica\b|\bcountry\b|\bnation\b",
    re.I,
)


def wants_nationwide_scope(message: str) -> bool:
    if not message:
        return False
    if NATIONWIDE_SCOPE_HINTS.search(message):
        return True
    return bool(NATIONWIDE_QUALIFIER_WORDS.search(message)) and bool(COUNTRY_REFERENCE_WORDS.search(message))

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_occupations",
            "description": "Search O*NET occupations by a user's work goal, tasks, interests, or job title. Also resolves a military job code (Army MOS, Air Force AFSC, Navy rating/NEC, Marine Corps MOS, Space Force code) directly to its matching civilian occupation(s) -- pass the code as-is, no need to expand it into words first. Use this before choosing an occupation unless a current selected occupation still matches the user's goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Concrete work goal or tasks, preserving the user's words."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
                    "bright_outlook_only": {"type": "boolean", "default": False, "description": "Set true when the user asks for high-demand, growing, in-demand, or 'Bright Outlook' jobs. Widens the search and returns only occupations O*NET flags as Bright Outlook (projected rapid growth, many openings, or a new/emerging field)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_occupation",
            "description": "Get authoritative O*NET facts for one occupation code. Calling this selects that occupation as the current direction.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "O*NET-SOC code returned by search_occupations."}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_related_occupations",
            "description": "Get O*NET-related occupations. Use only when the user explicitly asks for alternatives or agrees to broaden the occupation (asking for high-demand/growing/Bright Outlook jobs 'related to' a military code or interest counts as this), never merely because local results are empty.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_location",
            "description": "Resolve a city and state, state name, or ZIP to a geographic search anchor. When the user says near me, pass their known profile location instead of the words near me.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_local_training",
            "description": "Find exact-occupation school programs from the local IPEDS index near a city/state or ZIP, across a state, or nationwide. This never changes occupations. The result includes total_programs for the scope; when it exceeds shown, tell the user how many more exist. An empty result means keep the occupation and consider a wider scope or OJT source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "occupation_code": {"type": "string"},
                    "location": {"type": "string", "description": "Known city/state or ZIP, not 'near me'. Omit for nationwide."},
                    "scope": {"type": "string", "enum": ["near", "state", "nationwide"], "description": "near ranks by distance from location; state filters to the location's state; nationwide ignores location. Default near."},
                },
                "required": ["occupation_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_va_facilities",
            "description": "Find VA-approved schools or employer/OJT providers near a location. For employer searches, describe the trade in plain words; providers are matched semantically by name meaning, so related sponsors are found even when their names differ from the trade. Empty results should trigger a larger radius for the same occupation, not unrelated nearby employers. The result also includes nearest_ojt_providers when nothing matched.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Known city/state or ZIP, not 'near me'."},
                    "provider_type": {"type": "string", "enum": ["school", "employer"]},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Plain trade words describing the exact career, such as automotive mechanic, car repair, painter. Matched by meaning, not exact words."},
                    "radius_miles": {"type": "number", "minimum": 5, "default": 50},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 6},
                },
                "required": ["location", "provider_type", "keywords"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_va_programs",
            "description": "Primary source for a specific named program or trade search (nationwide, by state, or within a mile radius of a location): search VA's own approved IHL (degree) and NCD (certificate/non-college) program catalog directly by keyword. Requires no occupation code and no prior search_occupations/get_occupation call. VA approves many proprietary trade schools (for example commercial diving academies) that IPEDS's CIP crosswalk misses, so this covers more ground than find_local_training alone. Ranked by relevance unless location is given, in which case results are ranked by distance. A location with no radius_miles still sorts by distance but does not exclude far results. Returns up to 100 results per call; when the result's remaining_facilities is above 0, offer the user a 'show N more' suggestion and, if chosen, call this again with the same program/state/location/radius_miles plus offset set to continue past what was already shown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "program": {"type": "string", "description": "Plain program or trade keywords, e.g. 'commercial diver', 'HVAC', 'dental assisting'. To scope by degree level, include its abbreviation as it appears at the start of VA program titles (aa, as, ba, bs, ma, ms, gc, jd, do), e.g. 'bs marketing' -- every matched program title must then contain that degree-level word too, so this is the only way to actually exclude lower/higher-level results from both the answer and the result tiles; narrowing to a degree level in your reply text alone does not filter which tiles are shown."},
                    "state": {"type": "string", "description": "Two-letter state code to narrow results. Omit for nationwide. Ignored if location is given."},
                    "location": {"type": "string", "description": "Known city/state or ZIP to search near, not 'near me'. Omit for a state or nationwide search."},
                    "radius_miles": {"type": "number", "minimum": 5, "description": "Only with location: excludes results farther than this. Omit to just sort by distance with no cutoff."},
                    "offset": {"type": "integer", "minimum": 0, "description": "How many top-ranked results to skip, to continue a previous search of the same program/state/location/radius_miles past what was already shown to the user. 0 for a first search."},
                },
                "required": ["program"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_va_programs_for_military_job",
            "description": "Primary source when the user gives a military job code (Army MOS, Air Force AFSC, Navy rating/NEC, Marine Corps MOS, Space Force code) and wants schools or programs for it: goes from the code to its civilian career(s) in the official crosswalk, to the fields of study that lead there, to every VA-approved program in those fields -- no keyword guessing. Also returns related programs for closely related civilian careers (for example nursing for a combat medic). Same location/state/radius_miles/offset rules as find_va_programs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "military_code": {"type": "string", "description": "The code exactly as the user gave it, e.g. 68W, 25B, 0311, 3D1X2, HM."},
                    "state": {"type": "string", "description": "Two-letter state code to narrow results. Omit for nationwide. Ignored if location is given."},
                    "location": {"type": "string", "description": "Known city/state or ZIP to search near, not 'near me'. Omit for a state or nationwide search."},
                    "radius_miles": {"type": "number", "minimum": 5, "description": "Only with location, and only when the user named a distance."},
                    "offset": {"type": "integer", "minimum": 0, "description": "0 for a first search; to show more, the previous offset plus the number of facilities already shown."},
                },
                "required": ["military_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_va_facility",
            "description": "Find one previously named VA-approved provider by exact facility code or institution name and attach its official VA detail-page link. Use for follow-ups asking for a provider link or details, or to list every VA-approved program at one already-named school.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Facility code or full provider name."},
                    "full_program_list": {
                        "type": "boolean",
                        "description": "True when the user explicitly asked to see all/every/the full list of this school's VA-approved programs (not just examples or a relevant sample). Returns every program in each category instead of a 6-item sample, which can be long.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_official_resources",
            "description": "Attach trusted official action links relevant to the user's need.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topics": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["compare", "eligibility", "remaining", "other", "ojt", "apprenticeship", "vocational", "vre", "bright"]},
                    }
                },
                "required": ["topics"],
            },
        },
    },
]


class JarvetTools:
    def __init__(
        self, onet: OnetGraph, va: VaComparison, ipeds: IpedsIndex,
        official_resources: dict[str, dict[str, str]], selected: dict[str, str] | None,
        provider_context: str, nationwide_requested: bool = False,
    ) -> None:
        self.onet = onet
        self.va = va
        self.ipeds = ipeds
        self.official_resources = official_resources
        self.selected = selected
        self.matches: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.resolved_location: dict[str, Any] | None = None
        self.location_candidates: list[str] = []
        self.training_facilities: list[dict[str, Any]] = []
        self.provider_context = provider_context
        self.nationwide_requested = nationwide_requested
        # The schools the latest find_va_programs call returned, for
        # arrange_listings() -- see there.
        self.listed_facilities: dict[str, list[dict[str, Any]]] = {}
        # "Your path forward" section from the latest military-job search,
        # which the page renders above the cards (app/pathways.py).
        self.pathway: dict[str, Any] | None = None

    def _add_resource(self, resource: dict[str, Any]) -> None:
        # VA's insturl / vet_tuition_policy_url fields are often stored
        # without a scheme ("www.wgu.edu/"), which a browser treats as a
        # path relative to this site rather than an external link.
        url = str(resource.get("url") or "").strip()
        if url and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE):
            resource = {**resource, "url": "https://" + url.lstrip("/")}
        if resource.get("url") and all(
            (item["url"], item["label"]) != (resource["url"], resource["label"])
            for item in self.resources
        ):
            self.resources.append(resource)

    def _related_programs(
        self, matched_fields: list[str], matched_facility_codes: set[str], *,
        state: str | None, latitude: float | None, longitude: float | None,
        max_miles: float | None,
    ) -> list[dict[str, Any]]:
        """Schools outside the exact results with programs in the same or a
        closely related field of study -- same field catches titles that
        never use the search words (a "data" search finding "BUSINESS
        ANALYTICS"), related fields come from careers the fields share in the
        CIP-to-SOC crosswalk (Data Science -> Computer Science, IT)."""
        fields: dict[str, dict[str, Any]] = {}
        for cip in matched_fields:
            fields[cip] = {
                "title": self.ipeds.cip_titles.get(cip, cip), "overlap": 1.0,
                "shared_careers": sorted(self.ipeds.field_careers.get(cip, set())),
            }
            for related in self.ipeds.related_fields(cip, limit=6):
                if related["overlap"] > fields.get(related["cip"], {}).get("overlap", 0):
                    fields[related["cip"]] = related
        return self._related_facilities(
            fields, matched_facility_codes, state=state, latitude=latitude,
            longitude=longitude, max_miles=max_miles,
        )

    def _military_fields(
        self, careers: list[dict[str, Any]], main_fields: set[str],
    ) -> dict[str, dict[str, Any]]:
        """Fields for the related section of a military-job search: lower-confidence
        programs in the job's own fields, plus fields for O*NET's closest
        related civilian careers -- the crosswalk links Army 68W (Combat
        Medic) only to Paramedics, while O*NET relates Paramedics to EMTs,
        Registered Nurses and LPNs. A catch-all career (25B's "Computer
        Occupations, All Other") has no related occupations of its own, so
        those of the specific jobs under it stand in (Information Security
        Engineers -> Information Security Analysts, Network Architects...)."""
        fields: dict[str, dict[str, Any]] = {
            cip: {
                "title": self.ipeds.cip_titles.get(cip, cip), "overlap": 1.0,
                "shared_careers": sorted({career["soc"][:7] for career in careers}),
            }
            for cip in main_fields
        }
        related_codes = []
        for career in careers:
            own = self.onet.related_codes(career["soc"], limit=5)
            if own:
                related_codes += list(enumerate(own))
            else:
                # One step further removed, so only same-family careers
                # (15-xxxx computer jobs for 25B) -- otherwise Management
                # Analysts brought in business and even hotel management.
                for code in career["detail_codes"]:
                    related_codes += [
                        (rank, related)
                        for rank, related in enumerate(self.onet.related_codes(code, limit=3))
                        if related[:2] == career["soc"][:2]
                    ]
        for rank, related in related_codes:
            for cip in self.ipeds.fields_for_careers([related]):
                if cip in main_fields:
                    continue
                entry = fields.setdefault(cip, {
                    "title": self.ipeds.cip_titles.get(cip, cip),
                    "overlap": 0.0, "shared_careers": [],
                })
                entry["overlap"] = max(entry["overlap"], 0.9 - 0.1 * rank)
                if related[:7] not in entry["shared_careers"]:
                    entry["shared_careers"].append(related[:7])
        return fields

    def _military_pathway(
        self, military_job: dict[str, Any], careers: list[dict[str, Any]],
        fields: dict[str, dict[str, Any]], facilities: list[dict[str, Any]],
        location_label: str | None,
    ) -> dict[str, Any] | None:
        """The "Your path forward" ladder (app/pathways.py) over the schools
        the cards show, grouped by the careers each field leads to."""
        # Each career ranks by its closest field: the job's own careers
        # (overlap 1.0) first, then O*NET's related careers in their order.
        priority = {career["soc"][:7]: 1.0 for career in careers}
        for entry in fields.values():
            for soc in entry.get("shared_careers") or []:
                priority[soc] = max(priority.get(soc, 0.0), entry.get("overlap", 0.0))
        relevant = set(priority)
        track_careers = {}
        for cip in fields:
            linked = self.ipeds.field_careers.get(cip, set()) & relevant
            if not linked:
                # Catch-all careers ("Computer Occupations, All Other") are
                # left out of field_careers but still link their fields.
                linked = {
                    soc for soc in relevant if cip in self.ipeds.career_fields.get(soc, set())
                }
            track_careers[cip] = sorted(linked, key=lambda soc: (-priority[soc], soc))
        career_titles = {
            soc: title for soc in relevant if (title := self.onet.occupation_title(soc))
        }
        job_title = (military_job.get("titles") or [""])[0]
        return build_pathway(
            self.va.facility_programs_in_fields(
                {facility["facility_code"] for facility in facilities}, set(fields),
            ),
            facilities,
            track_careers=track_careers, career_titles=career_titles,
            primary_careers={career["soc"][:7] for career in careers},
            min_score=self.va.FIELD_MIN_SCORE,
            same_school_min_score=PATHWAY_SAME_SCHOOL_MIN_SCORE,
            heading=f"Your path forward from {military_job['code']}"
            + (f" {job_title}" if job_title else ""),
            location_label=location_label,
        )

    def _related_facilities(
        self, fields: dict[str, dict[str, Any]], exclude: set[str], *,
        state: str | None, latitude: float | None, longitude: float | None,
        max_miles: float | None,
    ) -> list[dict[str, Any]]:
        """Schools with programs in fields (CIP code -> {"title", "overlap",
        "shared_careers"}) outside exclude, each with up to three careers
        that explain why it is related."""
        facilities = self.va.programs_in_fields(
            fields, exclude_facilities=exclude, state=state,
            latitude=latitude, longitude=longitude, max_miles=max_miles,
            limit=RELATED_FACILITY_LIMIT,
        )
        for facility in facilities:
            careers = []
            for title in facility["related_fields"]:
                cip = next((code for code, item in fields.items() if item["title"] == title), None)
                for soc in fields.get(cip, {}).get("shared_careers", []):
                    career = self.onet.occupation_title(soc)
                    if career and career not in careers:
                        careers.append(career)
            facility["related_careers"] = careers[:3]
        return facilities

    async def _add_provider_resource(
        self, facility: dict[str, Any], group: str | None = None, full_programs: bool = False,
    ) -> dict[str, Any]:
        # A facility that reached here via find_va_programs already has its
        # genuinely matched program(s) confirmed by that search's own
        # FTS/word-variant pipeline -- pass them through so the tile's shown
        # sample is guaranteed to include what was actually searched for,
        # rather than depending on provider_details()'s own generic
        # word-overlap scoring to independently rediscover the same result.
        required = {
            str(program.get("description") or "")
            for program in facility.get("matching_programs") or []
        }
        details = await self.va.provider_details(
            str(facility["facility_code"]), self.provider_context, required=required,
            full=full_programs,
        )
        merged = {**facility, **(details or {})}
        if "estimated_housing_allowance" in merged:
            # Once live details are fetched, estimated_housing_allowance (VA
            # API's dod_bah) is the number the frontend card shows as
            # "Housing estimate". monthly_housing_rate (the static workbook's
            # bah, or the live API's own bah field) is a second, sometimes
            # different, housing figure; leaving both in the dict the LLM
            # narrates from lets it cite a number that disagrees with the
            # card the user is looking at.
            merged.pop("monthly_housing_rate", None)
        self._add_resource({
            "label": f"View {facility['institution']} in the VA Comparison Tool",
            "url": facility["detail_url"],
            "inline_labels": [facility["institution"]],
            "kind": "provider-details",
            "group": group or str(facility["institution"]).title(),
            "action": "VA benefits",
            "provider": merged,
        })
        maps_location = merged.get("address") or ", ".join(
            part for part in (facility.get("city"), facility.get("state")) if part
        )
        if maps_location:
            self._add_resource({
                "label": f"View {facility['institution']} on Google Maps",
                "url": (
                    "https://www.google.com/maps/search/?api=1&query="
                    + quote(f"{facility['institution']}, {maps_location}")
                ),
                "kind": "map",
                "group": group or str(facility["institution"]).title(),
                "action": "Map",
            })
        if merged.get("apply_url"):
            self._add_resource({
                "label": f"Apply to {facility['institution']}: {str(merged.get('apply_label') or 'Apply')[:60].strip()}",
                "url": merged["apply_url"],
                "kind": "school-website",
                "group": group or str(facility["institution"]).title(),
                "action": "Apply",
            })
        if merged.get("veteran_tuition_policy_url"):
            self._add_resource({
                "label": f"{facility['institution']}'s page for veterans and military students",
                "url": merged["veteran_tuition_policy_url"],
                "kind": "school-website",
                "group": group or str(facility["institution"]).title(),
                "action": "Veterans Page",
            })
        return merged

    async def _search_programs(
        self, arguments: dict[str, Any], search: Any, *, tool_name: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Shared scope handling (location, state, radius, paging), card
        enrichment and reply instructions for the program-search tools.
        search(state=, latitude=, longitude=, max_miles=, limit=, offset=)
        returns (result, related_facilities); result is shaped like
        VaComparison.programs_for's."""
        extra = extra or {}
        location_text = str(arguments.get("location", "")).strip()
        if self.nationwide_requested:
            # The user's own latest wording explicitly asked for
            # nationwide/entire-country results -- never let a
            # model-invented location or radius_miles (observed:
            # defaulting to the schema's own max, 500) narrow that down
            # or wrongly demand a ZIP code the user never needed to give.
            location_text = ""
            arguments = {**arguments, "radius_miles": None, "state": None}
        if arguments.get("radius_miles") is not None and not location_text:
            if arguments.get("state"):
                # radius_miles only pairs with a specific location -- a
                # bare state search has no single point to measure
                # distance from (see the state-filter branch below), so a
                # model-invented radius here (the same tendency the
                # nationwide_requested branch above guards against) is
                # dropped silently instead of blocking a valid state-wide
                # search and wrongly demanding a ZIP the user never
                # needed to give.
                arguments = {**arguments, "radius_miles": None}
            else:
                return {
                    "error": (
                        "radius_miles was given with no location, so there is nothing to measure "
                        "the distance from. This is an internal tool-call mistake -- never mention it, "
                        "the word radius_miles, or any other tool/argument name to the user. Simply ask "
                        "them, in plain natural language, for their city and state or ZIP code, exactly "
                        "as you would if this were the first thing you'd tried, then call "
                        f"{tool_name} again with both location and radius_miles set."
                    ),
                }
        latitude: float | None = None
        longitude: float | None = None
        location_label: str | None = None
        state = arguments.get("state")
        state = str(state).strip().upper()[:2] if state else None
        if location_text:
            location = self.va.resolve_location(location_text)
            if location is None:
                return self._location_error(location_text)
            self.resolved_location = location
            location_label = location["label"]
            latitude = location["latitude"]
            longitude = location["longitude"]
            # A specific city/ZIP's coordinates make a state filter
            # redundant, so location scoping supersedes it -- but a bare
            # state name (no city) only resolves to that state's
            # centroid, not a real point, and with no radius_miles set
            # (never invented -- see the tool schema) that would
            # otherwise silently become an unbounded nationwide
            # sort-by-distance-from-centroid search, ranking a closer
            # out-of-state school above a genuine in-state one that's
            # merely far from the centroid (e.g. El Paso, TX). Keep the
            # state filter in that case.
            state = location.get("state") if not location.get("city") else None
        radius_raw = arguments.get("radius_miles")
        offset_raw = arguments.get("offset", 0)
        try:
            max_miles = (
                max(5.0, float(radius_raw)) + RADIUS_BUFFER_MILES
                if radius_raw is not None else None
            )
            offset = max(0, int(offset_raw))
        except (TypeError, ValueError):
            # A malformed radius_miles/offset from the model would
            # otherwise raise uncaught here and fail the whole chat turn
            # with an opaque error instead of letting the model correct
            # itself and retry within the same turn.
            return {"error": "radius_miles and offset must be plain numbers."}
        # Not model-controlled: the model has repeatedly chosen small
        # round-number limits (e.g. 10) on its own regardless of the
        # schema's declared default, silently truncating "show me all"
        # requests. Always return the full result set up to this ceiling
        # on a first page; a "show more" continuation (offset > 0) then
        # steps forward 50 at a time.
        page_limit = 100 if offset == 0 else 50
        result, related_facilities = search(
            state=state, latitude=latitude, longitude=longitude, max_miles=max_miles,
            limit=page_limit, offset=offset,
        )
        # Live-enrich every matched facility (housing rate, GI Bill
        # student count, contact) concurrently. This means one live VA
        # API round trip per result, so a broad nationwide search can
        # take noticeably longer to reply than a narrow one.
        result["facilities"] = await asyncio.gather(*(
            self._add_provider_resource(facility)
            for facility in result["facilities"]
        ))
        # Give every school with a known website its own resource so the
        # inline mention of its name in the reply text links to the
        # school's own site rather than only the VA Comparison Tool page
        # (appendLinkedText on the frontend prefers a "program-details"
        # kind resource over "provider-details" for the same label).
        for facility in result["facilities"]:
            if not facility.get("website"):
                continue
            self._add_resource({
                "label": f"Visit {facility['institution']}'s website",
                "url": facility["website"],
                "kind": "school-website",
                "group": str(facility["institution"]).title(),
                "action": "School website",
            })
        # For facilities with no VA-confirmed website at all, a separate
        # offline script (scripts/init-va-website-guesses.py) may have a
        # best-effort candidate found via web search. This is never
        # crawled further (compounding an already-unverified URL isn't
        # worth it) and is always labeled distinctly so it can't be
        # mistaken for a VA-confirmed link.
        for facility in result["facilities"]:
            if facility.get("website") or not facility.get("guessed_website"):
                continue
            self._add_resource({
                "label": f"Unverified: possible website for {facility['institution']}",
                "url": facility["guessed_website"],
                "kind": "unverified-website",
                "group": str(facility["institution"]).title(),
                "action": "Unverified school link",
            })
        # Enriched after the exact matches so their cards come first and
        # the related cards follow under their own "Related programs"
        # heading (see renderResources in app/static/app.js).
        related_facilities = await asyncio.gather(*(
            self._add_provider_resource(facility) for facility in related_facilities
        ))
        self.listed_facilities = {
            "main": list(result["facilities"]), "related": list(related_facilities),
        }
        has_semantic_matches = any(
            facility.get("match_type") == "semantic" for facility in result["facilities"]
        )
        return {
            **result,
            **extra,
            "related_facilities": related_facilities,
            # The model once moved exact matches that sat just past the
            # requested radius into its own "Related programs just outside
            # 20 miles" section and dropped the real related schools from
            # the text entirely -- so spell out both sections' exact line
            # counts and the related schools by name.
            "related_note": (
                f"Your reply has exactly two lists. LIST 1: exactly {len(result['facilities'])} "
                "'• ' lines, one per entry in facilities (the exact matches), all under your "
                "opening sentence -- every one of them belongs in LIST 1 whatever its "
                "distance_miles; never split any of them off into another section. LIST 2: "
                "after LIST 1, a line reading exactly 'Related programs', then one plain "
                "sentence saying these are other approved schools in closely related fields "
                "that lead to similar jobs, then exactly "
                f"{len(related_facilities)} '• ' lines, one per entry in related_facilities, "
                "in this order: "
                + "; ".join(str(item["institution"]).title() for item in related_facilities)
                + ". Each LIST 2 line gives the school, city"
                + (", distance" if location_label else "")
                + ", its related program name(s) from matching_programs, and why it is "
                "related in plain words from related_careers (for example 'also leads to jobs "
                "like Software Developer'). related_facilities are NOT matches for the search "
                "words and are NOT counted in total_facilities; never call them matches."
            ) if related_facilities else "",

            "location": location_label or ("statewide" if state else "nationwide"),
            "note": (
                (
                    "No program title in VA's catalog contains the search words themselves, "
                    "so these were found by matching the search's MEANING instead (for example "
                    "an 'EMT' search matching a program titled 'Emergency Medical Technician'). "
                    "These are still real, VA-approved programs -- present them normally -- but "
                    "if asked how confident this match is, say it was found by meaning rather "
                    "than an exact program-title match. "
                    if has_semantic_matches else ""
                ) +
                "The facilities list below already contains every matched result for this "
                "page, not a preview or a curated sample -- your reply MUST name every "
                "single one of them as its own '• ' bulleted line, in the order given, with "
                "zero omitted, even when that is dozens of lines long. If you were given, "
                "say, 68 facilities here, your reply must contain exactly 68 bulleted "
                "lines, one per institution -- never write 'here are some examples,' "
                "'notable examples,' 'a few highlights,' or any other phrasing that implies "
                "a subset: that is a direct violation of this instruction regardless of how "
                "long the full list is. The frontend auto-links each institution name "
                "mentioned in your text to its own resource, so omitting a name from the "
                "text loses that link even though its card still appears below. "
                "These are exact-name VA-approved facilities with at least one matching "
                "IHL or NCD program in VA's own catalog. total_facilities is the exact, "
                "precisely known count; open your reply with that exact number (for "
                "example 'Found 25 VA-approved diver programs') rather than a vague "
                "quantifier like several, many, or multiple. If total_facilities exceeds "
                "the number of results actually returned here, say how many more exist "
                "beyond those named -- but every result that WAS returned here still gets "
                "its own line regardless. Only when "
                "remaining_facilities is above 0, end your reply by offering to show more, and "
                "include a suggestion whose value asks to see the next batch (for example 'Show "
                "50 more marketing programs'); if the user picks it or asks for more, call "
                f"{tool_name} again with the exact same arguments "
                "plus offset set to this result's offset plus the number of facilities just "
                "shown, so the next call continues past what the user already saw instead of "
                "repeating it. When remaining_facilities is 0, never offer or suggest showing "
                "more results. " + (
                    "Results are ranked by distance from " + str(location_label) + "; each "
                    "facility's distance_miles is the actual distance, and a radius_miles cutoff "
                    "already excluded anything farther, so never claim a result is within a "
                    "range wider than what was actually requested. A few results may sit a "
                    "little past the requested radius on purpose, since a city's center point "
                    "is only approximate: list them like every other result, never drop them "
                    "for being slightly over, and never mention a buffer or extra miles. "
                    "Give each school's distance on its line, rounded to one decimal "
                    "(for example 'Stanford University, Stanford - 4.3 miles'). "
                    "Facilities with no usable "
                    "coordinates on file could not be placed and are excluded from this scope, "
                    "not confirmed absent from the area."
                    if location_label else
                    "This does not rank by distance; mention state or nationwide scope explicitly."
                ) + " A guessed_website on a "
                "facility was found by web search, not confirmed by VA -- never state it as "
                "the school's website or say VA confirms it; if you mention it at all, call "
                "it an unverified possible website the person should confirm themselves. Never "
                "invent a facility not in the results."
            ),
        }

    def _location_error(self, location: str) -> dict[str, Any]:
        self.location_candidates = self.va.location_candidates(location)
        if self.location_candidates:
            return {
                "error": "Location is ambiguous. Ask the user to choose one candidate.",
                "candidates": self.location_candidates,
            }
        return {"error": "Location could not be resolved. Ask for a city and state or ZIP."}

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "search_occupations":
            limit = max(1, min(int(arguments.get("limit", 5)), 5))
            results = self.onet.search(
                str(arguments.get("query", "")), limit,
                bright_outlook_only=bool(arguments.get("bright_outlook_only", False)),
            )
            self.matches = results
            return results

        if name == "get_occupation":
            occupation = self.onet.result_by_code(str(arguments.get("code", "")))
            if occupation is None:
                return {"error": "Unknown O*NET-SOC code."}
            self.selected = {"code": occupation["code"], "title": occupation["title"]}
            self.matches = [occupation]
            return occupation

        if name == "get_related_occupations":
            occupation = self.onet.result_by_code(str(arguments.get("code", "")))
            if occupation is None:
                return {"error": "Unknown O*NET-SOC code."}
            limit = max(1, min(int(arguments.get("limit", 5)), 8))
            return [
                {key: item[key] for key in ("code", "title", "description", "bright_outlook")}
                for item in self.onet.related_results(occupation, limit)
            ]

        if name == "resolve_location":
            location_text = str(arguments.get("location", ""))
            location = self.va.resolve_location(location_text)
            if location is None:
                return self._location_error(location_text)
            self.resolved_location = location
            return location

        if name == "find_local_training":
            occupation = self.onet.result_by_code(str(arguments.get("occupation_code", "")))
            if occupation is None:
                return {"error": "Unknown O*NET-SOC code."}
            location_text = str(arguments.get("location", "")).strip()
            scope = str(arguments.get("scope", "near"))
            state = None
            latitude: float | None = None
            longitude: float | None = None
            location_label = "nationwide"
            if scope == "nationwide" or not location_text:
                location_label = "nationwide"
            else:
                location = self.va.resolve_location(location_text)
                if location is None:
                    return self._location_error(location_text)
                self.resolved_location = location
                location_label = location["label"]
                if location.get("state") and not location.get("city"):
                    # State-level scope: no distance ranking, filter by state.
                    state = location["state"]
                else:
                    latitude = location["latitude"]
                    longitude = location["longitude"]
            result = self.ipeds.programs_for(
                occupation["code"], latitude=latitude, longitude=longitude,
                state=state, limit=8,
            )
            programs = result["programs"]
            total = result["total"]
            if programs:
                self._add_resource({
                    "label": f"Find {occupation['title']} training near {location_label}",
                    "url": (
                        f"https://www.mynextmove.org/vets/profile/localtraining/"
                        f"{occupation['code']}"
                        + (f"?zip={location['representative_zip']}" if latitude is not None else "")
                    ),
                })
                for program in programs:
                    if program.get("website"):
                        self._add_resource({
                            "label": f"Visit {program['institution']}'s website",
                            "url": program["website"],
                            "kind": "school-website",
                            "group": str(program["institution"]).title(),
                            "action": "School website",
                        })
                    va_facility = self.va.match_school(program["institution"])
                    if va_facility:
                        merged = await self._add_provider_resource(va_facility, program["institution"])
                        program["va_facility"] = merged
                        self.training_facilities.append(merged)
            return {
                "occupation": self.selected or {"code": occupation["code"], "title": occupation["title"]},
                "location": location_label,
                "programs": programs,
                "total_programs": total,
                "shown": len(programs),
                "source": "IPEDS completions + O*NET CIP-to-SOC crosswalk",
                "note": (
                    "Results are for this exact occupation only, ranked by proximity when a "
                    "city or ZIP is known. total_programs is the full count for the scope; "
                    "shown is how many are listed here, so say how many more exist when "
                    "total_programs exceeds shown. website, when present, is the institution's own "
                    "site -- link to it as the school's website, not as a page confirmed to be about "
                    "this specific program. A va_facility is an exact-name approved-school match "
                    "from the VA GI Bill Comparison Tool. Recent awards are context, not quality "
                    "rankings."
                ),
            }

        if name == "find_va_facilities":
            location_text = str(arguments.get("location", ""))
            location = self.va.resolve_location(location_text)
            if location is None:
                return self._location_error(location_text)
            self.resolved_location = location
            provider_type = str(arguments.get("provider_type", ""))
            keywords = [str(item) for item in arguments.get("keywords", []) if str(item).strip()]
            if provider_type == "school" and self.training_facilities:
                return {
                    "location": location["label"],
                    "provider_type": provider_type,
                    "keywords": keywords,
                    "facilities": self.training_facilities,
                    "source": "VA GI Bill Comparison Tool",
                    "note": "These are exact-name VA facility matches for the schools in the local training results. No unrelated nearby school was substituted.",
                }
            if provider_type == "employer" and not keywords:
                return {"error": "Employer searches require occupation-relevant keywords."}
            radius = max(5.0, float(arguments.get("radius_miles", 50)))
            search_radius = radius + RADIUS_BUFFER_MILES
            limit = max(1, min(int(arguments.get("limit", 6)), 8))
            facilities = self.va.search_nearby(
                location["latitude"], location["longitude"], keywords,
                employer=provider_type == "employer", limit=limit, max_miles=search_radius,
            )
            fallback: list[dict[str, Any]] = []
            if provider_type == "employer" and not facilities:
                # Specialized trade schools (diving academies, aviation schools)
                # are school providers, not employers. Search schools before
                # concluding nothing exists.
                fallback = self.va.search_nearby(
                    location["latitude"], location["longitude"], keywords,
                    employer=False, limit=limit, max_miles=search_radius,
                )
                for facility in fallback:
                    facility["fallback_note"] = (
                        "A school provider, not an employer OJT sponsor; its "
                        "name matched the trade semantically."
                    )
            if provider_type == "employer" and not fallback:
                fallback = self.va.nearest_ojt_providers(
                    location["latitude"], location["longitude"], limit=4, max_miles=search_radius,
                )
            self._add_resource(self.official_resources["compare"])
            merged_facilities = await asyncio.gather(*(
                self._add_provider_resource(facility) for facility in facilities[:4]
            ))
            facilities[:len(merged_facilities)] = merged_facilities
            # Fallback providers are generic-name leads: attach their cards only
            # after their approved program lists confirm trade relevance.
            for index, facility in enumerate(fallback):
                details = await self.va.provider_details(
                    str(facility["facility_code"]), " ".join(keywords),
                )
                summaries = (details or {}).get("program_summaries", [])
                relevant = any(
                    summary.get("matching", 0) > 0 for summary in summaries
                )
                if relevant:
                    facility["fallback_note"] = (
                        "Nearest approved OJT sponsor; its approved program list "
                        "mentions the trade, but the name alone did not."
                    )
                    merged = await self._add_provider_resource(facility)
                    merged["fallback_note"] = facility["fallback_note"]
                    fallback[index] = merged
            return {
                "location": location["label"],
                "provider_type": provider_type,
                "keywords": keywords,
                "radius_miles": radius,
                "facilities": facilities,
                "nearest_ojt_providers": fallback,
                "source": "VA GI Bill Comparison Tool",
                "note": (
                    "Provider names were matched semantically by trade meaning; relevance is a "
                    "lead, not confirmation of a specific approved program. Published housing "
                    "rates are not personal payment quotes. "
                    + (
                        "No provider name matched the trade, so nearest_ojt_providers lists the "
                        "closest approved providers of either type regardless of name. Specialized "
                        "trade schools (for example diving academies) are school providers, not "
                        "employers, so check their program lists too. Check program_summaries "
                        "before saying nothing exists. Never report zero training options without "
                        "checking this list and the provider program summaries."
                        if fallback else ""
                    )
                ),
            }

        if name == "find_va_programs":
            program = str(arguments.get("program", "")).strip()
            if not program:
                return {"error": "A program or trade keyword is required."}

            def search(**scope: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
                result = self.va.programs_for(program, **scope)
                matched_facility_codes = set(result.pop("_matched_facility_codes", []))
                matched_fields = result.pop("_matched_fields", [])
                # Related programs are a first-page addition only; a "show
                # more" continuation pages through the exact matches alone.
                related = (
                    self._related_programs(
                        matched_fields, matched_facility_codes, state=scope["state"],
                        latitude=scope["latitude"], longitude=scope["longitude"],
                        max_miles=scope["max_miles"],
                    )
                    if scope["offset"] == 0 else []
                )
                return result, related

            return await self._search_programs(arguments, search, tool_name="find_va_programs")

        if name == "find_va_programs_for_military_job":
            self.pathway = None
            code = str(arguments.get("military_code", "")).strip().upper()
            careers = self.onet.military_careers(code) if code else []
            if not careers:
                return {"error": (
                    f"{code or 'No code'} is not in the official military-to-civilian crosswalk. "
                    "Ask the user to double-check the code (and which branch it is from), or ask "
                    "what kind of civilian work they want and search for that instead."
                )}
            socs = [career["soc"] for career in careers]
            military_job = {
                "code": code,
                # An AFSC entered as "1D7X1" covers every skill level and
                # shred ("Cyber Defense Operations Apprentice, Networks
                # Operations") -- name the specialty itself.
                "titles": sorted({
                    AFSC_SKILL_LEVEL.sub("", career["military_title"].split(",")[0])
                    for career in careers
                }),
                "branches": sorted({career["branch"] for career in careers}),
            }
            civilian_careers = [
                {
                    "title": career["title"] or career["soc"],
                    **({"includes_jobs_like": career["example_jobs"]} if career["example_jobs"] else {}),
                }
                for career in careers
            ]
            fields = self.ipeds.fields_for_careers(socs)
            if not fields:
                return {
                    "military_job": military_job,
                    "civilian_careers": civilian_careers,
                    "error": (
                        "The official crosswalk links this military job to no civilian field of "
                        "study (it has no direct civilian counterpart -- infantry is an example). "
                        "Say so plainly and kindly, without jargon such as 'crosswalk' (say the "
                        "official military-to-civilian job list instead), then offer a next step: "
                        "ask what kind of work "
                        "they would like to do, or look at civilian careers that use the same "
                        "skills with search_occupations."
                    ),
                }
            field_map = {
                cip: {"title": self.ipeds.cip_titles.get(cip, cip), "overlap": 1.0}
                for cip in fields
            }

            def search(**scope: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
                matches = self.va.programs_in_fields(
                    field_map, exclude_facilities=set(), state=scope["state"],
                    latitude=scope["latitude"], longitude=scope["longitude"],
                    max_miles=scope["max_miles"], limit=100_000,
                    min_score=self.va.FIELD_EXACT_MIN_SCORE,
                )
                for facility in matches:
                    # The frontend files any card with related_fields under
                    # "Related programs"; these are the main results.
                    facility["fields_of_study"] = facility.pop("related_fields")
                page = matches[scope["offset"]:scope["offset"] + scope["limit"]]
                result = {
                    "total_facilities": len(matches),
                    "total_programs": sum(item["matching_program_count"] for item in matches),
                    "facilities": page,
                    "offset": scope["offset"],
                    "remaining_facilities": max(0, len(matches) - scope["offset"] - len(page)),
                    "source": (
                        "Official military-to-civilian crosswalk, CIP-to-SOC crosswalk, and VA's "
                        "approved program catalog"
                    ),
                }
                related: list[dict[str, Any]] = []
                if scope["offset"] == 0:
                    related_fields = self._military_fields(careers, set(fields))
                    related = self._related_facilities(
                        related_fields, {item["facility_code"] for item in matches},
                        state=scope["state"], latitude=scope["latitude"],
                        longitude=scope["longitude"], max_miles=scope["max_miles"],
                    )
                    self.pathway = self._military_pathway(
                        military_job, careers, related_fields, page + related,
                        self.resolved_location["label"] if scope["latitude"] is not None
                        and self.resolved_location else None,
                    )
                    if self.pathway:
                        result["pathway"] = {
                            **summary_for_model(self.pathway),
                            "note": (
                                "The page shows a 'Your path forward' section on its own, "
                                "arranging these schools' programs by level (certificate, "
                                "associate, bachelor's, graduate). After LIST 2, add one short "
                                "plain sentence pointing to it (for example 'Below, Your path "
                                "forward shows how these programs build on each other, from a "
                                "certificate up to a bachelor's degree.'). Never list its "
                                "schools or steps again in your reply."
                            ),
                        }
                return result, related

            return await self._search_programs(
                arguments, search, tool_name="find_va_programs_for_military_job",
                extra={
                    "military_job": military_job,
                    "civilian_careers": civilian_careers,
                    "fields_of_study": [item["title"] for item in field_map.values()],
                    "career_note": (
                        "These schools were found from the military job itself: its civilian "
                        "career(s) in the official crosswalk, then every VA-approved program in the "
                        "fields of study that lead there -- not a keyword search. Never use jargon "
                        "such as 'crosswalk', 'CIP' or 'SOC' in the reply. Open by naming "
                        "the military job and its civilian career(s) in plain words, then give "
                        "the count. When a civilian career is a catch-all (its title ends in 'All "
                        "Other'), describe it in plain words using its includes_jobs_like jobs."
                    ),
                },
            )

        if name == "get_va_facility":
            facility = self.va.find_facility(str(arguments.get("query", "")))
            if facility is None:
                return {"error": "No approved VA facility matched that name or code."}
            full_programs = bool(arguments.get("full_program_list"))
            merged = await self._add_provider_resource(facility, full_programs=full_programs)
            program_count = sum(
                len(summary.get("programs") or [])
                for summary in merged.get("program_summaries") or []
            )
            # A big school's complete list (USC has ~977 programs) is far too
            # long for the model to retype: it took ~10 minutes and silently
            # dropped and altered names. The school's card already renders
            # every program exactly as VA lists them (with a filter box), so
            # the model only gets a short sample plus the exact totals.
            long_full_list = full_programs and program_count > FULL_LIST_REPLY_LIMIT
            facility_for_model = merged
            if long_full_list:
                facility_for_model = {
                    **merged,
                    "program_summaries": [
                        {**summary, "programs": (summary.get("programs") or [])[:8]}
                        for summary in merged.get("program_summaries") or []
                    ],
                }
            if merged.get("website"):
                group_name = str(merged["institution"]).title()
                self._add_resource({
                    "label": f"Visit {merged['institution']}'s website",
                    "url": merged["website"],
                    "kind": "school-website",
                    "group": group_name,
                    "action": "School website",
                })
                admissions_discovery = None
                if not merged.get("apply_url"):
                    # No offline-crawled apply link on file for this school yet
                    # (scripts/init-va-admissions-guesses.py hasn't covered it) --
                    # worth a live lookup since only one school is in play here,
                    # unlike a broad search where this would slow every result.
                    try:
                        admissions_discovery = await asyncio.wait_for(
                            discover_admissions_page(merged["website"]),
                            timeout=15,
                        )
                    except asyncio.TimeoutError:
                        admissions_discovery = None
                if admissions_discovery:
                    self._add_resource({
                        "label": f"{merged['institution']}: {admissions_discovery['label'][:60].strip()}",
                        "url": admissions_discovery["url"],
                        "kind": "school-website",
                        "group": group_name,
                        "action": "How to apply",
                    })
            elif merged.get("guessed_website"):
                # No VA-confirmed website to crawl from -- only a best-effort
                # web-search candidate. Not crawled further (compounding an
                # already-unverified URL isn't worth it) and always labeled
                # distinctly from the VA-confirmed cases above.
                self._add_resource({
                    "label": f"Unverified: possible website for {merged['institution']}",
                    "url": merged["guessed_website"],
                    "kind": "unverified-website",
                    "group": str(merged["institution"]).title(),
                    "action": "Unverified school link",
                })
            return {
                "facility": facility_for_model,
                "source": "VA GI Bill Comparison Tool",
                "note": (
                    "This official detail page verifies the facility record. Contact and "
                    "current program availability may still require provider confirmation. "
                    "A guessed_website was found by web search, not confirmed by VA -- never "
                    "state it as the school's website or say VA confirms it; call it an "
                    "unverified possible website the person should confirm themselves. " + (
                        f"The user asked for this school's complete program list, and it has "
                        f"{program_count} VA-approved programs -- too many to type out. Do NOT "
                        "list the programs one by one. Instead open with the exact total, give "
                        "the exact count for each category using each program_summaries "
                        "entry's 'total', name a few examples from the programs shown, and "
                        "say the complete list of every approved program is shown right here "
                        "in this chat, on the school's card below this message, where they can "
                        "type in the card's filter box to find a specific program. Do not send "
                        "them to the VA Comparison Tool or any other website for the list. "
                        "Offer to check whether a specific program or field of study is offered."
                        if long_full_list else
                        "full_program_list was requested, so each program_summaries entry's "
                        "programs list is now the COMPLETE list for that category (selection "
                        "is 'all'), not a 6-item sample -- your reply MUST name every single "
                        "program in every category as its own '• ' bulleted line (group by "
                        "category with a heading), with zero omitted, even when that totals "
                        "dozens of lines. Never write 'some examples,' 'a few highlights,' or "
                        "any phrasing implying a subset when this flag is set."
                        if full_programs else
                        "Only a 6-item sample of this facility's programs is included per "
                        "category (selection is 'relevant'/'sample'/'all' with 'total' as the "
                        "real count) -- if the user asks to see every/all programs, call "
                        "get_va_facility again with full_program_list set to true rather than "
                        "claiming these 6 are the complete list."
                    )
                ),
            }

        if name == "get_official_resources":
            resources = []
            for topic in arguments.get("topics", []):
                if topic in self.official_resources:
                    resource = self.official_resources[topic]
                    self._add_resource(resource)
                    resources.append(resource)
            return resources

        return {"error": f"Unknown tool: {name}"}


def _display_name(value: str) -> str:
    """VA's all-caps names in title case, keeping small joining words lower
    ("College of Alameda", not "College Of Alameda")."""
    words = value.title().split(" ")
    return " ".join(
        word.lower() if position and word.lower() in {"of", "the", "and", "at", "in", "for", "on"} else word
        for position, word in enumerate(words)
    )


def _listing_line(facility: dict[str, Any]) -> str:
    parts = [_display_name(str(facility.get("institution") or ""))]
    if facility.get("city"):
        parts.append(_display_name(str(facility["city"])))
    line = "• " + ", ".join(part for part in parts if part)
    if facility.get("distance_miles") is not None:
        line += f" - {float(facility['distance_miles']):.1f} miles"
    return line


RELATED_HEADING = re.compile(r"^\W*related programs\b", re.IGNORECASE)


def arrange_listings(message: str, listed: dict[str, list[dict[str, Any]]]) -> str:
    """Make the reply's school lists match what find_va_programs returned.

    The note tells the model to write every exact match in one list and
    every related school under a 'Related programs' heading, yet in testing
    it still dropped schools (17 of 19 matches listed for a Redwood City
    "data" search, omitting College of Alameda and San Francisco State) and
    filed related schools (Academy of Art, Santa Clara University) among the
    exact matches -- while the cards below were right. The written list is
    what veterans read and count, so it's rebuilt here from the model's own
    lines: each school's line goes to its correct section in result order,
    a plain line is added for any school the model left out, and any other
    text is kept where it was.
    """
    if not message or not (listed.get("main") or listed.get("related")):
        return message

    def normalized(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.lower()))

    # Longest names first so "University of California San Francisco" isn't
    # claimed by a shorter name that happens to appear inside it.
    names = sorted(
        (
            (normalized(str(facility.get("institution") or "")), kind, position)
            for kind in ("main", "related")
            for position, facility in enumerate(listed.get(kind) or [])
        ),
        key=lambda item: -len(item[0]),
    )
    lines = message.split("\n")
    bullets = [index for index, line in enumerate(lines) if line.lstrip().startswith("•")]
    heading_at = next(
        (index for index, line in enumerate(lines) if RELATED_HEADING.match(line.strip())), None,
    )
    anchors = bullets + ([heading_at] if heading_at is not None else [])
    start = min(anchors) if anchors else len(lines)
    end = max(anchors) if anchors else len(lines) - 1
    prefix, suffix = lines[:start], lines[end + 1:]

    placed: dict[str, dict[int, str]] = {"main": {}, "related": {}}
    unmatched: dict[str, list[str]] = {"main": [], "related": []}
    extra: dict[str, list[str]] = {"main": [], "related": []}
    related_intro: list[str] = []
    heading_line = "Related programs"
    section = "main"
    related_bullet_seen = False
    for index in range(start, end + 1):
        line = lines[index]
        if index == heading_at:
            section, heading_line = "related", line.strip()
            continue
        if line.lstrip().startswith("•"):
            related_bullet_seen = related_bullet_seen or section == "related"
            text = normalized(line)
            match = next((item for item in names if item[0] and item[0] in text), None)
            if match is None:
                unmatched[section].append(line)
            else:
                placed[match[1]].setdefault(match[2], line)
        elif line.strip():
            if section == "related" and not related_bullet_seen:
                related_intro.append(line)
            else:
                extra[section].append(line)

    def section_lines(kind: str) -> list[str]:
        return [
            placed[kind].get(position) or _listing_line(facility)
            for position, facility in enumerate(listed.get(kind) or [])
        ] + unmatched[kind] + extra[kind]

    output = [*prefix, *section_lines("main")]
    related = section_lines("related")
    if related:
        output += ["", heading_line, *related_intro, *related]
    if suffix and suffix[0].strip():
        output.append("")
    return "\n".join(output + suffix)


def _result_summary(result: Any) -> str:
    """One short line describing a tool result, for the log."""
    if isinstance(result, dict):
        if result.get("error"):
            return "error: " + str(result["error"])[:120]
        parts = [
            f"{key}={result[key]}" for key in ("total_facilities", "total_programs", "total")
            if key in result
        ]
        if "related_facilities" in result:
            parts.append(f"related={len(result['related_facilities'])}")
        return ", ".join(parts) or "keys: " + ", ".join(list(result)[:8])
    if isinstance(result, list):
        return f"{len(result)} items"
    return type(result).__name__


async def run_agent(
    *, messages: list[dict[str, str]], profile: dict[str, list[str]],
    selected_occupation: dict[str, str] | None, saved_providers: list[dict[str, str]],
    onet: OnetGraph, va: VaComparison, ipeds: IpedsIndex,
    official_resources: dict[str, dict[str, str]],
    base_url: str, api_key: str, model: str,
) -> dict[str, Any]:
    provider_context = " ".join([
        messages[-1]["content"] if messages else "",
        selected_occupation.get("title", "") if selected_occupation else "",
        *(value for values in profile.values() for value in values),
    ])
    tools = JarvetTools(
        onet, va, ipeds, official_resources, selected_occupation,
        provider_context,
        nationwide_requested=wants_nationwide_scope(messages[-1]["content"] if messages else ""),
    )
    system = f"""You are Jarvet, an agentic education and career facilitator for veterans. Solve the user's actual problem by deciding which tools to call, inspecting their results, and adapting your next step. Do not follow a fixed questionnaire.

Operating principles:
- Use tools for every factual claim about occupations, programs, providers, geography, VA approval, and benefits. Never invent results.
- When a tool result includes an exact total count (total_facilities, total_programs), open with that exact number ("Found 25 VA-approved diver programs") instead of a vague quantifier like several, many, or multiple. The count is precisely known from structured data; state it precisely.
- Preserve the current selected occupation unless the user clearly changes career goals. If they do, search and then call get_occupation for the best supported match.
- When search_occupations returns several plausible matches, do not silently pick one. Present the top matches with one-line distinctions and let the user choose, unless one is an obviously exact match for the user's words. A user who said "fix cars" means automotive work; if the best match is not automotive, say why and offer the automotive match.
- Treat spelling errors and conversational wording intelligently. Search by concrete work tasks when a title is unclear.
- When a search_occupations result carries military_match, treat it as a confirmed selection, not one of several guesses to let the user pick from -- it came from an official military-to-civilian crosswalk, not a text search. Tell them which branch and military title it came from, call get_occupation on the matched code to see its education/job zone requirements, and in the same reply proactively look up matching degree or certificate programs with find_va_programs_for_military_job (nationwide if no location is known yet). Do not stop at describing the occupation and asking whether they want programs -- the user asked how to get there, not just what the job is. If more than one civilian occupation was matched, lead with the most fitting one and mention the others as alternatives.
- The first time in a conversation you mention a Bright Outlook / high-demand / growing occupation, explain in one plain-language clause what that means (O*NET projects the field will grow quickly, add many openings, or is a new/emerging field) -- never use the label "Bright Outlook" on its own without that explanation, since most users have never heard the term.
- When the user asks for high-demand/growing/Bright Outlook jobs tied to a military code or interest, call search_occupations with bright_outlook_only true. If that yields nothing (the exact military-code match is not itself Bright Outlook), call get_related_occupations on the matched code and keep only results with a non-empty bright_outlook, presenting them as related high-demand options rather than the exact match. Once you have the qualifying occupation(s), proactively look up matching degree/training programs for each (find_local_training or find_va_programs) rather than stopping at the occupation list -- the user asked for degrees, not just job titles.
- When the user gives a military job code and wants schools, programs, degrees, or training for it, call find_va_programs_for_military_job with the code as given (plus location/state/radius_miles under the same rules as find_va_programs). Never turn the code into guessed keywords for find_va_programs -- keyword guesses miss programs titled differently. If it reports no civilian field of study (for example infantry), say so plainly and offer a next step rather than inventing a match.
- "My MOS" (or similar shorthand) refers to the user's own military job code, not a literal search term. If a request mentions it but no code is on file yet and none was given in this message, ask for the code before calling search_occupations -- never search using words like "MOS," "high demand," or "my interests" themselves, since they are not real occupation or search terms and will return meaningless matches.
- Accept city/state, region, or ZIP. "Near me" means the known profile location. Never interpret pronouns as state abbreviations and never demand a ZIP when a named area is known.
- When the user names a place that is not a city or state (a region, landmark, or area such as Lake Tahoe), resolve the nearest well-known city or the containing state, say which anchor you used, and search from there. Never silently substitute a different location from the profile.
- Honor scope requests literally. If the user asks for nationwide results or clicks a nationwide suggestion, call find_local_training with scope nationwide and report results from the whole country. Never answer a nationwide request with local results.
- When a location tool returns ambiguity candidates, ask the user to choose and mention only those candidates. Do not guess a state or save a candidate to the profile before the user chooses.
- When local results are empty, broaden geography for the SAME occupation: retry find_local_training with scope state, then nationwide, or explain the exact-source gap. Never switch occupations or interests merely to produce a result. Call get_related_occupations only if the user explicitly asks for alternatives or agrees to broaden occupationally.
- When the user asks to find schools or programs for a specific named program or trade (nationwide, by state, or within a distance of a location), call find_va_programs DIRECTLY with the trade keywords. Do not call search_occupations, get_occupation, or find_local_training first, and do not resolve an occupation as a prerequisite step: find_va_programs needs no occupation code. VA's own program catalog is comprehensive and includes proprietary trade schools that IPEDS's CIP-to-SOC crosswalk may not classify under any matching occupation. Only bring O*NET/IPEDS into a named-program request if the user separately asks about the occupation itself (job outlook, bright outlook, related occupations, or degree-to-career mapping) as well as the program search. Pass location (city/state or ZIP, never "near me" literally) whenever the user gives or has a known one, and radius_miles ONLY when they explicitly name a distance in miles -- never invent one, never default it to the maximum (or any other) value just because the field exists, and never pass it when the user asked for nationwide/entire-USA/whole-country results, which means no radius and no location at all. If the user names a distance (for example "within 50 miles") but no location is given and none is already known for them, do not call find_va_programs yet -- ask for their city/state or ZIP first, since a distance is meaningless without a point to measure it from.
- For OJT/employer searches, describe the trade in plain words (for example automotive mechanic, car repair). Provider names are matched semantically by meaning, so sponsors with related names are found without exact word overlap. A semantic match is still only a lead to verify in the official VA tool.
- Treat OJT, apprenticeships, and other paid training as one family: a user asking for OJT is also asking about apprenticeships, and vice versa. One find_va_facilities employer search covers both; never tell the user you have not checked apprenticeships after an OJT search, or run a second search just for them. VA lists apprenticeships inside its OJT program data and Jarvet labels each program as an apprenticeship or on-the-job training in the provider card.
- When an employer search returns no name matches, the tool result includes nearest_ojt_providers: the closest approved providers of either type regardless of name. Many sponsors have generic names (trust funds, JATCs, joint apprenticeship councils), and specialized trade schools such as diving academies are school providers rather than employers, so a name miss does not mean no training exists. Inspect each fallback provider's program_summaries for the user's trade before concluding nothing is available. Present relevant fallback providers as leads to verify, clearly saying their names did not mention the trade but their approved programs might include it. Only say an area has no training options after checking both the fallback list and the program summaries.
- Every recommended VA facility must have its official facility-detail resource attached. For a follow-up asking for a provider's link, call get_va_facility instead of returning only a general VA page.
- When the user asks to see all/every VA-approved program at one already-named school (no trade or keyword given, e.g. "list all VA approved programs at San Jose City College"), call get_va_facility with that school's name AND full_program_list set to true -- without that flag its result only includes a short 6-item sample per category, not the complete inventory. Do not call find_va_programs for this and do not ask the user for a trade/program keyword first: a keyword is only needed to search across schools, not to list one already-identified school's own catalog.
- School links point to the institution's own website (its homepage or wherever it was already found), not a page confirmed to be specifically about the matched program -- Jarvet no longer crawls each school's site looking for a dedicated program page, since that was slow and often wrong. Never claim a school link goes directly to program-specific details.
- Local training results may also include a va_facility matched to that exact school. Present its official VA Comparison Tool resource alongside the program resource. Do not substitute an unrelated nearby VA-approved school when exact program-school VA matches are available.
- When naming specific programs or providers in the final answer, mention only results that have an attached resource. Keep the shortlist focused rather than listing unlinked results returned by a tool.
- Resources -- school website links, VA benefit links, and the result cards the frontend renders -- are only generated for facilities a tool returns THIS turn; they do not carry over from an earlier turn even though you can still see those programs in the visible conversation text. When a follow-up asks you to narrow (for example "only BS degrees," "only certificates," "only in-state"), reformat (for example "show them in tile format"), or otherwise re-present programs or providers already found earlier, call find_va_programs (or whichever tool originally found them) again with the same or refined parameters before answering. Never filter, recount, or restate results from memory of an earlier tool result alone -- besides producing a reply with no resource cards attached, the earlier result text you can see is not guaranteed complete (it may have been summarized or truncated), so re-deriving an answer from it risks stating something is missing that a fresh tool call would show does exist.
- My Next Move/IPEDS results are school programs, not employer OJT. VA employer facilities are approved providers, but their names alone do not prove a particular trade program.
- Published housing/living allowance is a facility reference, not a personal payment quote. Eligibility and payment depend on the veteran's circumstances.
- Ask at most one question, only when a missing fact blocks useful action. Otherwise use the tools and answer.
- Always return 3 or 4 concise suggestions that help the user take the next step. When asking a question, make each suggestion a plausible direct answer to that question. Otherwise offer distinct, relevant follow-up actions. Never return an empty suggestions array.
- Keep the response concise and candid about source limitations. The frontend renders content as literal text (newlines display as real line breaks, so multi-line replies are fine): do not use Markdown syntax, numbered formatting, asterisks, headings, or raw URLs. Official links called through get_official_resources appear separately as buttons.
- When naming three or more schools, programs, or other items in one reply, put each on its own line as a "• " bulleted line (a literal bullet character, never Markdown "-" or "*") instead of running them together in one paragraph sentence. A one- or two-item mention can stay inline.
- Institution links are rendered together below your message. A school website, a VA benefits page, and (when present) an Apply link are different destinations. Do not write empty link placeholders such as "Website:" or repeat raw link labels in the message. State which details are available, then let the grouped actions provide access.
- An Apply resource/apply_url on a facility points to that school's own admissions page, found automatically (either ahead of time by a background crawl, or live for a single selected school) rather than confirmed by VA. Present it as where to start an application, not a guarantee the process shown is still current -- a school can redesign its site after it was found. When it is missing, never invent one or call the plain school website an apply link.
- Do not offer actions Jarvet cannot perform, such as contacting providers. Suggest a concrete next search or verification step instead.
- Never narrate your own tool use to the user: no tool, parameter, or argument names, no admitting you "mistakenly" set something or made an internal error, no describing what you are about to call or just called. If a tool result reports a problem, silently retry or route around it, or ask the user for whatever plain-language fact you're actually missing (like a location) -- phrased as a normal first-time question, never as a confession or explanation of what went wrong internally.

Current profile:
{json.dumps(profile)}

Current selected occupation:
{json.dumps(selected_occupation)}

Saved providers:
{json.dumps(saved_providers)}

Saved providers are soft context. Use them when relevant for comparison or follow-up, but never
limit a search or answer to saved providers unless the user explicitly asks you to do so.

Return the final answer as one JSON object only:
{{"message":"plain text","suggestions":[{{"label":"short direct answer or next action","value":"complete message sent when chosen"}}],"profile":{{"interests":[],"strengths":[],"goals":[],"preferences":[],"constraints":[],"education":[],"location":[],"notes":[]}}}}
Preserve valid profile facts, update direct user corrections, and do not infer sensitive traits."""
    conversation: list[dict[str, Any]] = [{"role": "system", "content": system}, *messages[-16:]]
    headers = {"Authorization": f"Bearer {api_key}"}
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    latest_message = messages[-1]["content"] if messages else ""
    force_program_search = wants_named_program_search(latest_message, selected_occupation)
    # A program request naming a military job code ("I was a 68W, what
    # schools...") goes to the military-job search; forcing the keyword
    # search there searched for the code itself and found nothing.
    forced_program_tool = (
        "find_va_programs_for_military_job" if onet.military_code_in(latest_message)
        else "find_va_programs"
    ) if force_program_search else None

    async with httpx.AsyncClient(timeout=180) as client:
        for turn_index in range(8):
            payload: dict[str, Any] = {
                "model": model,
                "messages": conversation,
                "tools": TOOL_SCHEMAS,
                "tool_choice": (
                    {"type": "function", "function": {"name": forced_program_tool}}
                    if forced_program_tool and turn_index == 0
                    else "auto"
                ),
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }
            response = await client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            assistant = response.json()["choices"][0]["message"]
            conversation.append(assistant)
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                return {
                    "content": assistant.get("content") or "{}",
                    "matches": tools.matches,
                    "resources": tools.resources,
                    "selected_occupation": tools.selected,
                    "resolved_location": tools.resolved_location,
                    "location_candidates": tools.location_candidates,
                    "listed_facilities": tools.listed_facilities,
                    "pathway": tools.pathway,
                }
            for tool_call in tool_calls:
                function = tool_call.get("function", {})
                logger.info(
                    "tool %s %s", function.get("name", ""), str(function.get("arguments") or "")[:300],
                )
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    result = {"error": "Tool arguments were not valid JSON."}
                else:
                    result = await tools.call(function.get("name", ""), arguments)
                logger.info("tool %s -> %s", function.get("name", ""), _result_summary(result))
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": function.get("name", ""),
                    "content": json.dumps(result, default=str),
                })

        response = await client.post(endpoint, headers=headers, json={
            "model": model,
            "messages": conversation + [{
                "role": "system",
                "content": "Stop calling tools and return the required final JSON using only gathered facts.",
            }],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        })
        response.raise_for_status()
        return {
            "content": response.json()["choices"][0]["message"].get("content") or "{}",
            "matches": tools.matches,
            "resources": tools.resources,
            "selected_occupation": tools.selected,
            "resolved_location": tools.resolved_location,
            "location_candidates": tools.location_candidates,
            "listed_facilities": tools.listed_facilities,
            "pathway": tools.pathway,
        }
