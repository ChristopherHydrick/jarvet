from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from app.ipeds import IpedsIndex
from app.onet import OnetGraph
from app.programs import discover_admissions_page
from app.va import VaComparison

TrainingFetcher = Callable[[str, str], Awaitable[list[dict[str, str]] | None]]

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
            "description": "Search O*NET occupations by a user's work goal, tasks, interests, or job title. Use this before choosing an occupation unless a current selected occupation still matches the user's goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Concrete work goal or tasks, preserving the user's words."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
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
            "description": "Get O*NET-related occupations. Use only when the user explicitly asks for alternatives or agrees to broaden the occupation; never use merely because local results are empty.",
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
            "name": "get_va_facility",
            "description": "Find one previously named VA-approved provider by exact facility code or institution name and attach its official VA detail-page link. Use for follow-ups asking for a provider link or details.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Facility code or full provider name."}},
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

    def _add_resource(self, resource: dict[str, Any]) -> None:
        if resource.get("url") and all(
            (item["url"], item["label"]) != (resource["url"], resource["label"])
            for item in self.resources
        ):
            self.resources.append(resource)

    async def _add_provider_resource(
        self, facility: dict[str, Any], group: str | None = None,
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
        return merged

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
            results = self.onet.search(str(arguments.get("query", "")), limit)
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
                {key: item[key] for key in ("code", "title", "description")}
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
            limit = max(1, min(int(arguments.get("limit", 6)), 8))
            facilities = self.va.search_nearby(
                location["latitude"], location["longitude"], keywords,
                employer=provider_type == "employer", limit=limit, max_miles=radius,
            )
            fallback: list[dict[str, Any]] = []
            if provider_type == "employer" and not facilities:
                # Specialized trade schools (diving academies, aviation schools)
                # are school providers, not employers. Search schools before
                # concluding nothing exists.
                fallback = self.va.search_nearby(
                    location["latitude"], location["longitude"], keywords,
                    employer=False, limit=limit, max_miles=radius,
                )
                for facility in fallback:
                    facility["fallback_note"] = (
                        "A school provider, not an employer OJT sponsor; its "
                        "name matched the trade semantically."
                    )
            if provider_type == "employer" and not fallback:
                fallback = self.va.nearest_ojt_providers(
                    location["latitude"], location["longitude"], limit=4, max_miles=radius,
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
                            "find_va_programs again with both location and radius_miles set."
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
                max_miles = max(5.0, float(radius_raw)) if radius_raw is not None else None
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
            result = self.va.programs_for(
                program, state=state, limit=page_limit, offset=offset,
                latitude=latitude, longitude=longitude, max_miles=max_miles,
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
            has_semantic_matches = any(
                facility.get("match_type") == "semantic" for facility in result["facilities"]
            )
            return {
                **result,
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
                    "These are exact-name VA-approved facilities with at least one matching "
                    "IHL or NCD program in VA's own catalog. total_facilities is the exact, "
                    "precisely known count; open your reply with that exact number (for "
                    "example 'Found 25 VA-approved diver programs') rather than a vague "
                    "quantifier like several, many, or multiple. Name every single result in "
                    "your reply by institution name -- do not summarize as 'some' or 'notable "
                    "examples' and truncate the list; the frontend auto-links each institution "
                    "name mentioned in your text to its own resource, so omitting a name from "
                    "the text loses that link even though its card still appears below. List three "
                    "or more institutions as separate '• ' bulleted lines, one per line, never as one "
                    "run-on paragraph. If total_facilities exceeds the number of results actually "
                    "returned here, say how many more exist beyond those named. Only when "
                    "remaining_facilities is above 0, end your reply by offering to show more, and "
                    "include a suggestion whose value asks to see the next batch (for example 'Show "
                    "50 more marketing programs'); if the user picks it or asks for more, call "
                    "find_va_programs again with the exact same program/state/location/radius_miles "
                    "plus offset set to this result's offset plus the number of facilities just "
                    "shown, so the next call continues past what the user already saw instead of "
                    "repeating it. When remaining_facilities is 0, never offer or suggest showing "
                    "more results. " + (
                        "Results are ranked by distance from " + str(location_label) + "; each "
                        "facility's distance_miles is the actual distance, and a radius_miles cutoff "
                        "already excluded anything farther, so never claim a result is within a "
                        "range wider than what was actually requested. Facilities with no usable "
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

        if name == "get_va_facility":
            facility = self.va.find_facility(str(arguments.get("query", "")))
            if facility is None:
                return {"error": "No approved VA facility matched that name or code."}
            merged = await self._add_provider_resource(facility)
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
                "facility": merged,
                "source": "VA GI Bill Comparison Tool",
                "note": (
                    "This official detail page verifies the facility record. Contact and "
                    "current program availability may still require provider confirmation. "
                    "A guessed_website was found by web search, not confirmed by VA -- never "
                    "state it as the school's website or say VA confirms it; call it an "
                    "unverified possible website the person should confirm themselves."
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
    force_program_search = wants_named_program_search(
        messages[-1]["content"] if messages else "", selected_occupation,
    )

    async with httpx.AsyncClient(timeout=180) as client:
        for turn_index in range(8):
            payload: dict[str, Any] = {
                "model": model,
                "messages": conversation,
                "tools": TOOL_SCHEMAS,
                "tool_choice": (
                    {"type": "function", "function": {"name": "find_va_programs"}}
                    if force_program_search and turn_index == 0
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
                }
            for tool_call in tool_calls:
                function = tool_call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    result = {"error": "Tool arguments were not valid JSON."}
                else:
                    result = await tools.call(function.get("name", ""), arguments)
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
        }
