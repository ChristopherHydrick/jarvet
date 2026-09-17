from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx

from app.ipeds import IpedsIndex
from app.onet import OnetGraph
from app.programs import discover_program_pages
from app.va import VaComparison

TrainingFetcher = Callable[[str, str], Awaitable[list[dict[str, str]] | None]]
PageFetcher = Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]]

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
                    "radius_miles": {"type": "number", "minimum": 5, "maximum": 500, "default": 50},
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
            "description": "Search VA's own approved IHL (degree) and NCD (certificate/non-college) program catalog for a program or trade keyword, nationwide or within one state. Not geography-ranked. Complements find_local_training: use this when IPEDS results are sparse or empty for a specific named program, since VA approves many proprietary trade schools (for example commercial diving academies) that IPEDS's CIP crosswalk can miss.",
            "parameters": {
                "type": "object",
                "properties": {
                    "program": {"type": "string", "description": "Plain program or trade keywords, e.g. 'commercial diver', 'HVAC', 'dental assisting'."},
                    "state": {"type": "string", "description": "Two-letter state code to narrow results. Omit for nationwide."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 15, "default": 8},
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
        provider_context: str, fetch_page: PageFetcher | None = None,
    ) -> None:
        self.onet = onet
        self.va = va
        self.ipeds = ipeds
        self.fetch_page = fetch_page
        self.official_resources = official_resources
        self.selected = selected
        self.matches: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.resolved_location: dict[str, Any] | None = None
        self.location_candidates: list[str] = []
        self.training_facilities: list[dict[str, Any]] = []
        self.provider_context = provider_context

    def _add_resource(self, resource: dict[str, Any]) -> None:
        if resource.get("url") and all(
            (item["url"], item["label"]) != (resource["url"], resource["label"])
            for item in self.resources
        ):
            self.resources.append(resource)

    async def _add_provider_resource(
        self, facility: dict[str, Any], group: str | None = None,
    ) -> None:
        details = await self.va.provider_details(
            str(facility["facility_code"]), self.provider_context,
        )
        self._add_resource({
            "label": f"View {facility['institution']} in the VA Comparison Tool",
            "url": facility["detail_url"],
            "inline_labels": [facility["institution"]],
            "kind": "provider-details",
            "group": group or str(facility["institution"]).title(),
            "action": "VA benefits",
            "provider": {**facility, **(details or {})},
        })

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
                # Verify official program pages for the closest few, in parallel.
                verified = await discover_program_pages(
                    [
                        {
                            "school": program["institution"],
                            "program": program["cip_title"],
                            "url": program["website"] or "",
                        }
                        for program in programs[:4]
                    ],
                    fetch=self.fetch_page,
                )
                for program, discovery in zip(programs[:4], verified):
                    program_url = (discovery or {}).get("url")
                    if program_url:
                        program["program_url"] = program_url
                        program["link_status"] = "verified_official_program_page"
                    else:
                        program["link_status"] = "source_listing_only"
                    va_facility = self.va.match_school(program["institution"])
                    if va_facility:
                        program["va_facility"] = va_facility
                        self.training_facilities.append(va_facility)
                        await self._add_provider_resource(va_facility, program["institution"])
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
                    "total_programs exceeds shown. A verified_official_program_page links to "
                    "institution program details; source_listing_only means no official page "
                    "was verified and the My Next Move source listing is the reference. A "
                    "va_facility is an exact-name approved-school match from the VA GI Bill "
                    "Comparison Tool. Recent awards are context, not quality rankings."
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
            radius = max(5.0, min(float(arguments.get("radius_miles", 50)), 500.0))
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
            await asyncio.gather(*(
                self._add_provider_resource(facility) for facility in facilities[:4]
            ))
            # Fallback providers are generic-name leads: attach their cards only
            # after their approved program lists confirm trade relevance.
            for facility in fallback:
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
                    await self._add_provider_resource(facility)
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
            state = arguments.get("state")
            state = str(state).strip().upper()[:2] if state else None
            limit = max(1, min(int(arguments.get("limit", 8)), 15))
            result = self.va.programs_for(program, state=state, limit=limit)
            for facility in result["facilities"]:
                self._add_resource({
                    "label": f"View {facility['institution']} in the VA Comparison Tool",
                    "url": facility["detail_url"],
                    "inline_labels": [facility["institution"]],
                    "kind": "provider-details",
                    "group": str(facility["institution"]).title(),
                    "action": "VA benefits",
                    "provider": facility,
                })
            return {
                **result,
                "note": (
                    "These are exact-name VA-approved facilities with at least one matching "
                    "IHL or NCD program in VA's own catalog. total_facilities/total_programs "
                    "are the full counts; say how many more exist when they exceed what is "
                    "shown. This does not rank by distance; mention state or nationwide scope "
                    "explicitly. Not geography-ranked. Never invent a facility not in the results."
                ),
            }

        if name == "get_va_facility":
            facility = self.va.find_facility(str(arguments.get("query", "")))
            if facility is None:
                return {"error": "No approved VA facility matched that name or code."}
            await self._add_provider_resource(facility)
            return {
                "facility": facility,
                "source": "VA GI Bill Comparison Tool",
                "note": "This official detail page verifies the facility record. Contact and current program availability may still require provider confirmation.",
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
    fetch_page: PageFetcher | None = None,
) -> dict[str, Any]:
    provider_context = " ".join([
        messages[-1]["content"] if messages else "",
        selected_occupation.get("title", "") if selected_occupation else "",
        *(value for values in profile.values() for value in values),
    ])
    tools = JarvetTools(
        onet, va, ipeds, official_resources, selected_occupation,
        provider_context, fetch_page,
    )
    system = f"""You are Jarvet, an agentic education and career facilitator for veterans. Solve the user's actual problem by deciding which tools to call, inspecting their results, and adapting your next step. Do not follow a fixed questionnaire.

Operating principles:
- Use tools for every factual claim about occupations, programs, providers, geography, VA approval, and benefits. Never invent results.
- Preserve the current selected occupation unless the user clearly changes career goals. If they do, search and then call get_occupation for the best supported match.
- When search_occupations returns several plausible matches, do not silently pick one. Present the top matches with one-line distinctions and let the user choose, unless one is an obviously exact match for the user's words. A user who said "fix cars" means automotive work; if the best match is not automotive, say why and offer the automotive match.
- Treat spelling errors and conversational wording intelligently. Search by concrete work tasks when a title is unclear.
- Accept city/state, region, or ZIP. "Near me" means the known profile location. Never interpret pronouns as state abbreviations and never demand a ZIP when a named area is known.
- When the user names a place that is not a city or state (a region, landmark, or area such as Lake Tahoe), resolve the nearest well-known city or the containing state, say which anchor you used, and search from there. Never silently substitute a different location from the profile.
- Honor scope requests literally. If the user asks for nationwide results or clicks a nationwide suggestion, call find_local_training with scope nationwide and report results from the whole country. Never answer a nationwide request with local results.
- When a location tool returns ambiguity candidates, ask the user to choose and mention only those candidates. Do not guess a state or save a candidate to the profile before the user chooses.
- When local results are empty, broaden geography for the SAME occupation: retry find_local_training with scope state, then nationwide, or explain the exact-source gap. Never switch occupations or interests merely to produce a result. Call get_related_occupations only if the user explicitly asks for alternatives or agrees to broaden occupationally.
- When find_local_training returns few or no IPEDS results for a specific named program, also try find_va_programs with the same trade keywords before concluding options are scarce. It searches VA's own approved program catalog nationwide or by state, which includes proprietary trade schools IPEDS may not classify under the occupation's CIP code.
- For OJT/employer searches, describe the trade in plain words (for example automotive mechanic, car repair). Provider names are matched semantically by meaning, so sponsors with related names are found without exact word overlap. A semantic match is still only a lead to verify in the official VA tool.
- Treat OJT, apprenticeships, and other paid training as one family: a user asking for OJT is also asking about apprenticeships, and vice versa. One find_va_facilities employer search covers both; never tell the user you have not checked apprenticeships after an OJT search, or run a second search just for them. VA lists apprenticeships inside its OJT program data and Jarvet labels each program as an apprenticeship or on-the-job training in the provider card.
- When an employer search returns no name matches, the tool result includes nearest_ojt_providers: the closest approved providers of either type regardless of name. Many sponsors have generic names (trust funds, JATCs, joint apprenticeship councils), and specialized trade schools such as diving academies are school providers rather than employers, so a name miss does not mean no training exists. Inspect each fallback provider's program_summaries for the user's trade before concluding nothing is available. Present relevant fallback providers as leads to verify, clearly saying their names did not mention the trade but their approved programs might include it. Only say an area has no training options after checking both the fallback list and the program summaries.
- Every recommended VA facility must have its official facility-detail resource attached. For a follow-up asking for a provider's link, call get_va_facility instead of returning only a general VA page.
- Local training results may include a verified program_url from the institution's official website. Distinguish it from school_url and source_url. Recommend program details using program_url when present; never describe an institution homepage as program details.
- Local training results may also include a va_facility matched to that exact school. Present its official VA Comparison Tool resource alongside the program resource. Do not substitute an unrelated nearby VA-approved school when exact program-school VA matches are available.
- Respect each training result's link_status. Say a result has direct program details only for verified_official_program_page. Describe source_listing_only as the My Next Move source listing, never as a direct program page.
- When naming specific programs or providers in the final answer, mention only results that have an attached resource. Keep the shortlist focused rather than listing unlinked results returned by a tool.
- My Next Move/IPEDS results are school programs, not employer OJT. VA employer facilities are approved providers, but their names alone do not prove a particular trade program.
- Published housing/living allowance is a facility reference, not a personal payment quote. Eligibility and payment depend on the veteran's circumstances.
- Ask at most one question, only when a missing fact blocks useful action. Otherwise use the tools and answer.
- Always return 3 or 4 concise suggestions that help the user take the next step. When asking a question, make each suggestion a plausible direct answer to that question. Otherwise offer distinct, relevant follow-up actions. Never return an empty suggestions array.
- Keep the response concise and candid about source limitations. The frontend renders content as literal text: do not use Markdown syntax, numbered formatting, asterisks, headings, or raw URLs. Official links called through get_official_resources appear separately as buttons.
- Institution links are rendered together below your message. A school website, verified program page, and VA benefits page are different destinations. Do not write empty link placeholders such as "Direct program page:" or repeat raw link labels in the message. State which details are available, then let the grouped actions provide access.
- Do not offer actions Jarvet cannot perform, such as contacting providers. Suggest a concrete next search or verification step instead.

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

    async with httpx.AsyncClient(timeout=180) as client:
        for _ in range(8):
            payload: dict[str, Any] = {
                "model": model,
                "messages": conversation,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
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
