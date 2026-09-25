from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

PROGRAM_LABELS = {
    "IHL": "Degree programs",
    "NCD": "Certificate and non-college programs",
    "OJT": "On-the-job training and apprenticeships",
    "FLGT": "Flight training programs",
}
PROGRAM_STOP_WORDS = {
    "about", "and", "career", "certificate", "degree", "find", "for", "from",
    "help", "near", "program", "programs", "school", "study", "the", "training",
    "want", "with",
}
# VA program titles lead with a short degree-level abbreviation (e.g. "BS
# BUSINESS MARKETING", "AA MARKETING MANAGEMENT", "MS MARKETING") or embed a
# short credential abbreviation (e.g. "BSN NURSING-RN TO BSN"). These are two
# letters, so without this allowlist they'd be silently dropped by the >= 3
# character floor below, which exists to filter out noise words -- but that
# also meant a request like "only BS programs" or "RN programs" could never
# actually be scoped server-side: "bs"/"rn" was thrown away, the search fell
# back to matching every level/credential, and the model's own text-only
# filtering left every non-matching facility's tile attached anyway (tiles
# come from the raw tool result, not the model's prose).
DEGREE_LEVEL_TERMS = {"aa", "as", "ba", "bs", "ma", "ms", "gc", "jd", "do", "rn"}
PROGRAM_CONTEXT_EXPANSIONS = {
    "healthcare": {
        "ambulatory", "case", "clinical", "coder", "health", "hospital", "medical",
        "nursing", "patient", "radiologic", "sterile", "surgical",
    },
    "technology": {"computer", "cyber", "data", "information", "network", "software"},
    "trades": {"carpenter", "construction", "electrical", "electrician", "hvac", "plumbing", "welding"},
    # A veteran asking about becoming a pilot rarely says "aviation" and
    # vice versa, but VA program titles are inconsistent about which word
    # they use ("PROFESSIONAL PILOT BS" vs. "AVIATION MANAGEMENT BS" at the
    # same school) -- without this, the plain word-overlap scoring below
    # ranks a same-school program that happens to share a literal word with
    # the request (or even an unrelated one that merely shares the searched
    # city/state name) ahead of the actual best-fit program, silently
    # dropping it from the tile's shown sample.
    "aviation": {"pilot", "flight", "aircraft", "aeronautics", "flying"},
    "pilot": {"aviation", "flight", "aircraft", "flying"},
    "flight": {"pilot", "aviation", "aircraft", "flying"},
}
# VA program titles are short and sometimes abbreviate "diverse"/"diversity"
# down to just "diver"/"divers" (e.g. "MA EDU LINGUISTICS DIVER EDU EQUITY"
# for "...Diverse Edu Equity"), which collides with genuine diver/diving
# trade programs under a whole-word match. These titles reliably co-occur
# with DEI/education jargon that never appears in an actual diving program,
# so a match containing any of these markers is dropped as a false positive.
DIVERSITY_FALSE_POSITIVE_MARKERS = ("equity", "inclus", "cultu", "lingui", "cltrly", "learn")
# Specific (facility_code, description) pairs confirmed to be unrelated to
# what their text superficially matches, due to a likely typo or unusual
# abbreviation in VA's own source data. These are indistinguishable from a
# genuine match by any general text pattern (unlike the diversity/equity
# case above), so each entry here is a manually verified, one-off exclusion,
# not a rule -- add to this only after confirming the specific case, never as
# a general heuristic.
KNOWN_FALSE_POSITIVE_PROGRAMS = {
    # "DIVER OPERATOR CT" at Ivy Tech Community College-South Bend is VA's
    # own listing for "Driver/Operator Certificate (CT)", a heavy-equipment
    # credential; the source data appears to be missing the "R" in "DRIVER".
    ("14903414", "DIVER OPERATOR CT"),
}
# VA's own program catalog rarely uses the literal acronym "EMT" for basic
# EMT training -- it's overwhelmingly spelled out as "EMERGENCY MEDICAL
# TECHNICIAN" or "EMERGENCY MEDICAL SERVICES" (e.g. Chabot College, Merritt
# College, City College of San Francisco), so a plain "emt" search would
# otherwise only surface the handful of schools that happen to use the
# acronym in their own title (e.g. "FIRE FIGHTER EMT ACADEMY"), silently
# dropping every school that spells the credential out in full. Maps a
# search term to an equivalent phrase that counts as a genuine match even
# without the literal acronym.
TERM_PHRASE_SYNONYMS: dict[str, str] = {
    "emt": "emergency medical",
}
# A single search term whose most common wording in VA's own program titles
# is a DIFFERENT single word for the same specific credential, not merely a
# plural/verb-form of the searched term (which _word_variants already
# handles) and not a fused compound (handled separately below). Narrower
# than PROGRAM_CONTEXT_EXPANSIONS on purpose: that dict maps a broad field
# ("aviation") to every related word, including generic ones ("aircraft")
# that dominate a DIFFERENT specific credential within the same field
# (aircraft maintenance/mechanic programs, not piloting) -- reusing it here
# for retrieval was tried and made a "pilot" search return every aircraft
# maintenance program in the state. This dict instead pairs only terms
# confirmed to mean the same specific credential:
# "pilot" <-> "flight", since a school's own title is inconsistent about
# which one it uses for the same learn-to-fly program ("AS PILOT TRAINING"
# at Glendale CC vs. "AS COMMERCIAL FLIGHT" at Mt. San Antonio College,
# "BS AVIATION FLIGHT" at California Baptist University -- none of which a
# literal "pilot" search alone would find).
RETRIEVAL_TERM_SYNONYMS: dict[str, set[str]] = {
    "pilot": {"flight"},
    "flight": {"pilot"},
    # A veteran searching "trucking" is looking for the same programs VA's
    # catalog almost always titles by the credential's own name, "CDL"
    # (Commercial Driver's License) -- e.g. "CDL A CERT", "TRACTOR AND
    # TRAILER OPERATIONS-CDL", "PROFESSIONAL DRIVER TRAINING CDL". A plain
    # "trucking" search finds only 6 schools nationwide despite 200+
    # approved CDL programs; adding the synonym here brings all of them in.
    "trucking": {"cdl"},
}
# A retrieval synonym above can itself collide with a different, unrelated
# credential: "flight" also appears in "FLIGHT ATTENDANT" programs (cabin
# crew, not piloting), which would otherwise get pulled into a "pilot"
# search purely because it shares the synonym word. Maps a search term to
# the marker(s) that disqualify a match pulled in only via
# RETRIEVAL_TERM_SYNONYMS for that term -- same shape as
# DIVERSITY_FALSE_POSITIVE_MARKERS above, but keyed per term since each
# synonym pair can have its own unrelated-credential collision.
# A credential that VA program titles name in several unrelated wordings,
# none of which a word-by-word search can bridge: "CNA" only finds the
# handful of titles that literally include the acronym, and "certified
# nursing assistant" requires "certified", which most titles ("NURSE
# ASSISTANT", "NURSE AIDE-HOME HEALTH AIDE", "NURSING ASSISTING TRAINING
# PROGRAM") leave out -- a California CNA search returned 4 or 13 of the 28
# approved schools depending on phrasing. When the query itself names one of
# these credentials, retrieval switches to the credential's own FTS query and
# title pattern instead of the per-word match. \bcna\b never matches Cisco's
# "CCNA", which only contains the letters.
CREDENTIAL_CONCEPTS: list[dict[str, Any]] = [
    {
        "query": re.compile(
            r"\bcnas?\b|\bnurs\w*\s+(assist\w*|aides?)\b|\bnurse\s*aides?\b", re.I,
        ),
        "fts": '("nurs"* AND ("assist"* OR "aid"*)) OR "cna"*',
        "title": re.compile(r"\bcnas?\b|\bnurs\w*\s+(assist\w*|aides?)\b", re.I),
    },
]
FIELD_EXPANSION_FALSE_POSITIVE_MARKERS: dict[str, set[str]] = {
    "pilot": {"attendant", "engineering"},
    "flight": {"attendant", "engineering"},
}


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_address(attributes: dict[str, Any]) -> str | None:
    """One-line street address from the VA API's institution attributes,
    preferring the physical address over the mailing address."""
    street = str(
        attributes.get("physical_address_1") or attributes.get("address_1") or ""
    ).strip()
    city = str(attributes.get("physical_city") or attributes.get("city") or "").strip()
    state = str(attributes.get("physical_state") or attributes.get("state") or "").strip()
    zip_code = str(attributes.get("physical_zip") or attributes.get("zip") or "").strip()
    state_zip = " ".join(part for part in (state.upper(), zip_code) if part)
    city_state_zip = ", ".join(part for part in (city.title(), state_zip) if part)
    if not street and not city_state_zip:
        return None
    return ", ".join(part for part in (street.title(), city_state_zip) if part)


# A closed compound like "divemaster" (diver + master, a real scuba
# certification title) is a genuine match for "diver"/"dive"/"diving" that no
# suffix rule below can produce, since the whole-word check in programs_for
# requires a boundary right after the matched root (see DIVEMASTER missing
# from a "diver" search until this was added). Unlike that boundary check,
# blanket-allowing any trailing text after the root would also re-admit
# "diversity"/"biodiversity"/"diversified", which are common in unrelated
# program titles -- so this is a small curated list of confirmed compounds,
# the same spirit as KNOWN_FALSE_POSITIVE_PROGRAMS below but for inclusions.
KNOWN_COMPOUND_VARIANTS: dict[str, set[str]] = {
    "dive": {"divemaster"},
    "diver": {"divemaster"},
    "diving": {"divemaster"},
    "dived": {"divemaster"},
    # "BSC" (e.g. Santa Clara University's "BSC MARKETING") is a genuine BS
    # variant, unlike "BSN" (Bachelor of Science in Nursing) -- an explicit
    # variant here, rather than a general one-trailing-character allowance,
    # catches BSC without also matching BSN under an exact-word "bs" search.
    "bs": {"bsc"},
}


def _word_variants(term: str) -> list[str]:
    """Common English word-form variants of a search term (diver/diving/dive),
    so one query catches an agent noun, its -ing form, and its base verb
    without falling back to a stemmer, which classically conflates unrelated
    words such as "diver" and "diversity"."""
    variants = {term}
    if term.endswith("er") and len(term) > 4:
        root = term[:-2]
        variants.update({root + "ing", root + "e", root + "ed"})
    if term.endswith("ing") and len(term) > 5:
        root = term[:-3]
        variants.update({root + "er", root + "e", root + "ed"})
    if term.endswith("e") and len(term) > 3:
        variants.update({term + "r", term[:-1] + "ing", term + "d"})
    if term.endswith("ed") and len(term) > 4 and not term.endswith("eed"):
        root = term[:-2]
        variants.update({root, root + "er", root + "ing"})
    variants.update(KNOWN_COMPOUND_VARIANTS.get(term, set()))
    return sorted(variants)


def _distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    latitude_delta = math.radians(lat2 - lat1)
    longitude_delta = math.radians(lon2 - lon1)
    start_latitude = math.radians(lat1)
    end_latitude = math.radians(lat2)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(start_latitude) * math.cos(end_latitude) * math.sin(longitude_delta / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(haversine))


class VaComparison:
    def __init__(self, path: Path):
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.facility_count = 0
        self.cities: list[tuple[str, str, str]] = []
        self._embedder: Any = None
        self._query_cache: dict[str, Any] = {}
        self._program_embedding_descriptions: list[str] | None = None
        self._program_embedding_matrix: Any = None

    def load(self) -> None:
        if not self.path.exists():
            raise RuntimeError("VA Comparison Tool index is missing. Run scripts/init-va-data.py.")
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS provider_details ("
            "facility_code TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
        )
        self.connection.commit()
        self.facility_count = self.connection.execute(
            "SELECT COUNT(*) FROM facilities"
        ).fetchone()[0]
        self.cities = [
            (row[0], row[1], _normalized(row[0]))
            for row in self.connection.execute(
                "SELECT DISTINCT city, state FROM facilities "
                "WHERE city IS NOT NULL AND state IS NOT NULL"
            )
        ]

    async def provider_details(
        self, facility_code: str, context: str = "", *, ttl_seconds: int = 7 * 24 * 60 * 60,
        required: set[str] | None = None, full: bool = False,
    ) -> dict[str, Any] | None:
        payload = await self._provider_payload(facility_code, ttl_seconds)
        if payload is None:
            return None
        attributes = payload.get("institution", {})
        programs = payload.get("programs", {})
        officials = attributes.get("versioned_school_certifying_officials") or []
        primary = next(
            (official for official in officials if official.get("priority") == "Primary"),
            officials[0] if officials else None,
        )
        contact = None
        if primary:
            contact = {
                "name": " ".join(
                    part.title() for part in (primary.get("first_name"), primary.get("last_name"))
                    if part
                ),
                "title": str(primary.get("title") or "School certifying official").title(),
            }
        terms = {
            term for term in _normalized(context).split()
            if len(term) >= 3 and term not in PROGRAM_STOP_WORDS
        }
        for term in list(terms):
            terms.update(PROGRAM_CONTEXT_EXPANSIONS.get(term, set()))
        # A facility reached via find_va_programs already has its genuinely
        # matched program(s) confirmed by that search's own FTS/word-variant
        # pipeline -- a strictly more precise relevance signal than this
        # function's plain word-overlap scoring below, which can rank an
        # unrelated program ahead of it (for instance one that merely shares
        # a literal word with the searched city or state name) and bump it
        # out of the shown sample. Those confirmed matches are guaranteed a
        # slot rather than just another scoring input.
        required_normalized = {_normalized(item) for item in (required or set())}
        summaries = []
        for program_type in attributes.get("program_types") or programs:
            code = str(program_type).upper()
            items = programs.get(code, [])
            ranked = []
            category_counts: dict[str, int] = {}
            for position, item in enumerate(items):
                description = str(item.get("description") or "").strip()
                words = set(_normalized(description).split())
                score = len(words & terms)
                if description:
                    subtype = str(item.get("ojt_app_type") or "").upper()
                    category = (
                        "Apprenticeship" if subtype == "APP"
                        else "On-the-job training" if code == "OJT" and subtype == "OJT"
                        else "Approved training" if code == "OJT"
                        else ""
                    )
                    if category:
                        category_counts[category] = category_counts.get(category, 0) + 1
                    ranked.append((score, position, description, category))
            matches = [item for item in ranked if item[0] > 0]
            required_items = [
                item for item in ranked if _normalized(item[2]) in required_normalized
            ]
            required_descriptions = {item[2] for item in required_items}
            remaining = [item for item in matches if item[2] not in required_descriptions]
            cap = None if full else 6  # list[:None] is the whole list -- "show all" asked for it.
            chosen = (
                required_items + sorted(remaining, key=lambda item: (-item[0], item[1]))
            )[:cap]
            selection = "relevant"
            if not terms and not required_items:
                chosen = ranked[:cap]
                selection = "all"
            elif not chosen:
                chosen = ranked[:cap]
                selection = "sample"
            if full:
                # cap is None above, so chosen already IS every program in
                # this category regardless of which branch set it -- "sample"
                # would wrongly tell the frontend to show its "no close title
                # match" caveat on what's actually the complete VA list.
                chosen = ranked
                selection = "all"
            summaries.append({
                "type": code,
                "label": PROGRAM_LABELS.get(code, f"{code} programs"),
                "total": len(ranked),
                "matching": len(matches) if terms else len(ranked),
                "selection": selection,
                "category_counts": category_counts,
                "programs": [
                    {
                        "name": item[2], "category": item[3],
                        # True when this program genuinely matched the
                        # search context -- the same scoring used to choose
                        # and rank the programs above (word-overlap against
                        # terms/PROGRAM_CONTEXT_EXPANSIONS, or a required
                        # item confirmed by find_va_programs' own FTS
                        # pipeline) -- not just "was shown", since a
                        # "sample"/"all" selection shows programs with no
                        # real match at all. Lets the frontend highlight
                        # only the programs that actually matched what was
                        # searched for.
                        "matched": item[0] > 0 or item[2] in required_descriptions,
                    }
                    for item in chosen
                ],
            })
        result = {
            "facility_code": attributes.get("facility_code") or facility_code,
            "address": _format_address(attributes),
            "contact": contact,
            "monthly_housing_rate": _number(attributes.get("bah")),
            "estimated_housing_allowance": _number(attributes.get("dod_bah")),
            "tuition_in_state": _number(attributes.get("tuition_in_state")),
            "books": _number(attributes.get("books")),
            "gi_bill_students": attributes.get("student_count"),
            "yellow_ribbon": bool(attributes.get("yr")),
            "accredited": bool(attributes.get("accredited")),
            "credit_for_military_training": bool(attributes.get("credit_for_mil_training")),
            "program_summaries": summaries,
            "source_updated_at": attributes.get("updated_at"),
        }
        # VA's static comparison-tool workbook (facilities.city/state, the
        # source for a bare facility record) can be stale for a school that's
        # since relocated, while this live institution API call reflects its
        # current physical address -- the same "address" string used just
        # above. Overriding city/state here with that live location, when
        # available, keeps the LLM's prose (which narrates from these fields)
        # in agreement with the address actually shown on the provider's
        # card, instead of naming two different cities for one school.
        live_city = str(attributes.get("physical_city") or attributes.get("city") or "").strip()
        live_state = str(attributes.get("physical_state") or attributes.get("state") or "").strip()
        if live_city:
            result["city"] = live_city.upper()
        if live_state:
            result["state"] = live_state.upper()
        return result

    async def _provider_payload(
        self, facility_code: str, ttl_seconds: int,
    ) -> dict[str, Any] | None:
        database = self._database()
        cached = database.execute(
            "SELECT payload, fetched_at FROM provider_details WHERE facility_code = ?",
            (facility_code,),
        ).fetchone()
        now = int(time.time())
        if cached is not None and now - cached["fetched_at"] < ttl_seconds:
            return json.loads(cached["payload"])
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(
                    f"https://api.va.gov/v0/gi/institutions/{facility_code}"
                )
                response.raise_for_status()
                attributes = response.json()["data"]["attributes"]
                program_types = [
                    str(item).upper() for item in attributes.get("program_types") or []
                ]
                programs: dict[str, list[dict[str, Any]]] = {}
                for program_type in program_types:
                    response = await client.get(
                        "https://api.va.gov/v0/gi/institution_programs/search",
                        params={
                            "type": program_type,
                            "facility_code": facility_code,
                            "disable_pagination": "true",
                        },
                    )
                    response.raise_for_status()
                    programs[program_type] = [
                        item.get("attributes", {}) for item in response.json().get("data", [])
                    ]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return json.loads(cached["payload"]) if cached is not None else None
        payload = {"institution": attributes, "programs": programs}
        database.execute(
            "INSERT OR REPLACE INTO provider_details (facility_code, payload, fetched_at) "
            "VALUES (?, ?, ?)",
            (facility_code, json.dumps(payload, separators=(",", ":")), now),
        )
        database.commit()
        return payload

    def _database(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("VA Comparison Tool index has not been loaded.")
        return self.connection

    def nearby(
        self, zip_code: str, *, employer: bool | None = None, limit: int = 8,
        max_miles: float = 100,
    ) -> list[dict[str, Any]]:
        database = self._database()
        center = database.execute(
            "SELECT latitude, longitude FROM zcta WHERE zip = ?", (zip_code,)
        ).fetchone()
        if center is None:
            return []
        return self.nearby_coordinates(
            center["latitude"], center["longitude"], employer=employer,
            limit=limit, max_miles=max_miles,
        )

    def nearby_coordinates(
        self, latitude: float, longitude: float, *, employer: bool | None = None,
        limit: int = 8, max_miles: float = 100,
    ) -> list[dict[str, Any]]:
        database = self._database()
        clauses = ["approved = 1", "latitude IS NOT NULL", "longitude IS NOT NULL"]
        if employer is True:
            clauses.append("employer_provider = 1")
        elif employer is False:
            clauses.append("school_provider = 1")
        rows = database.execute(
            "SELECT * FROM facilities WHERE " + " AND ".join(clauses)
        )
        facilities = []
        for row in rows:
            distance = _distance_miles(
                latitude, longitude, row["latitude"], row["longitude"]
            )
            if distance <= max_miles:
                facilities.append(self._record(row, distance))
        return sorted(facilities, key=lambda item: item["distance_miles"])[:limit]

    def resolve_area(self, text: str) -> dict[str, Any] | None:
        normalized = f" {_normalized(text)} "
        state = None
        for name, abbreviation in STATE_NAMES.items():
            if f" {name} " in normalized:
                state = abbreviation
                break
        if state is None:
            abbreviation_match = re.search(r",\s*([A-Za-z]{2})\b", text)
            abbreviation = abbreviation_match.group(1) if abbreviation_match else ""
            if (
                abbreviation.upper() in STATE_NAMES.values()
                and not (abbreviation.islower() and abbreviation.lower() == "me")
            ):
                state = abbreviation.upper()
            else:
                trailing = re.search(r"\b([A-Za-z]{2})\s*$", text)
                candidate = trailing.group(1).upper() if trailing else ""
                if candidate in STATE_NAMES.values() and (
                    text.strip().upper() == candidate
                    or any(
                        city_state == candidate and f" {normalized_city} " in normalized
                        for _, city_state, normalized_city in self.cities
                    )
                ):
                    state = candidate

        city_matches = [
            (city, city_state, normalized_city)
            for city, city_state, normalized_city in self.cities
            if (state is None or city_state == state)
            and f" {normalized_city} " in normalized
        ]
        if city_matches:
            states = {item[1] for item in city_matches}
            if state is None and len(states) != 1:
                return None
            city, state, _ = max(city_matches, key=lambda item: len(item[2]))
            where = "city = ? COLLATE NOCASE AND state = ?"
            parameters = (city, state)
            label = f"{city.title()}, {state}"
        elif state:
            where = "state = ?"
            parameters = (state,)
            label = state
        else:
            return None

        database = self._database()
        representative = database.execute(
            f"SELECT substr(zip, 1, 5), COUNT(*) AS uses FROM facilities WHERE {where} "
            "AND zip GLOB '[0-9][0-9][0-9][0-9][0-9]*' "
            "GROUP BY substr(zip, 1, 5) ORDER BY uses DESC LIMIT 1",
            parameters,
        ).fetchone()
        center = None
        if city_matches and representative:
            center = database.execute(
                "SELECT latitude, longitude FROM zcta WHERE zip = ?", (representative[0],)
            ).fetchone()
        if center is None:
            center = database.execute(
                f"SELECT AVG(latitude), AVG(longitude) FROM facilities WHERE {where} "
                "AND latitude IS NOT NULL AND longitude IS NOT NULL",
                parameters,
            ).fetchone()
        if center is None or center[0] is None or center[1] is None:
            return None
        return {
            "label": label,
            "city": city if city_matches else None,
            "state": state,
            "latitude": center[0],
            "longitude": center[1],
            "representative_zip": representative[0] if representative else "",
        }

    def resolve_location(self, text: str) -> dict[str, Any] | None:
        zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
        if not zip_match:
            return self.resolve_area(text)
        zip_code = zip_match.group(1)
        center = self._database().execute(
            "SELECT latitude, longitude FROM zcta WHERE zip = ?", (zip_code,)
        ).fetchone()
        if center is None:
            center = self._nearest_zip_by_prefix(zip_code)
        if center is None:
            return None
        return {
            "label": zip_code,
            "city": None,
            "state": None,
            "latitude": center["latitude"],
            "longitude": center["longitude"],
            "representative_zip": zip_code,
        }

    def _nearest_zip_by_prefix(self, zip_code: str) -> sqlite3.Row | None:
        """Fallback for a ZIP code missing from zcta (the Census ZCTA
        Gazetteer file it's built from) -- usually a PO-Box-only or other
        zero-population ZIP Census doesn't assign its own ZCTA centroid to.
        Confirmed case: 06813 (a Brookfield, CT PO-Box ZIP) is absent even
        though 06804, Brookfield's regular delivery ZIP, is present. ZIP
        prefixes are assigned by regional USPS district, so the numerically
        closest ZIP sharing the same 3-digit prefix is reliably a few miles
        away at most -- close enough for this tool's proximity search,
        without needing a full ZIP-to-ZCTA crosswalk data source.
        """
        prefix = zip_code[:3]
        candidates = self._database().execute(
            "SELECT zip, latitude, longitude FROM zcta WHERE zip LIKE ?", (prefix + "%",)
        ).fetchall()
        if not candidates:
            return None
        return min(candidates, key=lambda row: abs(int(row["zip"]) - int(zip_code)))

    def location_candidates(self, text: str, limit: int = 6) -> list[str]:
        if self.resolve_location(text) is not None:
            return []
        normalized = f" {_normalized(text)} "
        matching_cities = {
            city.lower(): city
            for city, _, normalized_city in self.cities
            if f" {normalized_city} " in normalized
        }
        if not matching_cities:
            return []
        city = max(matching_cities.values(), key=len)
        rows = self._database().execute(
            "SELECT city, state, COUNT(*) AS uses FROM facilities "
            "WHERE city = ? COLLATE NOCASE GROUP BY city, state "
            "ORDER BY uses DESC, state LIMIT ?",
            (city, limit),
        )
        return [f"{row['city'].title()}, {row['state']}" for row in rows]

    def search_nearby(
        self, latitude: float, longitude: float, keywords: list[str], *,
        employer: bool | None = None, limit: int = 8, max_miles: float = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["approved = 1", "latitude IS NOT NULL", "longitude IS NOT NULL"]
        if employer is True:
            clauses.append("employer_provider = 1")
        elif employer is False:
            clauses.append("school_provider = 1")
        rows = self._database().execute(
            "SELECT * FROM facilities WHERE " + " AND ".join(clauses)
        )
        query_text = " ".join(keywords)
        facilities = []
        for row in rows:
            distance = _distance_miles(latitude, longitude, row["latitude"], row["longitude"])
            if distance > max_miles:
                continue
            relevance = self._name_relevance(row["facility_code"], row["institution"], query_text)
            if relevance is None:
                continue
            facilities.append((relevance, distance, self._record(row, distance)))
        facilities.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in facilities[:limit]]

    RELEVANCE_THRESHOLD = 0.62

    def _name_relevance(
        self, facility_code: str, institution: str, query_text: str,
    ) -> float | None:
        """Semantic relevance of a provider name to the query, or None when the
        name is clearly unrelated. Uses precomputed name embeddings plus a
        small exact-word bonus so 'auto mechanic' prefers 'Automotive
        Apprenticeship Group' over 'Automation Specialists'. Generic sponsor
        names (JATCs, trust funds) score below the threshold for any specific
        trade and are filtered out."""
        if not query_text.strip():
            return None
        similarity = self._embedding_similarity(facility_code, institution, query_text)
        if similarity is None:
            return None
        words = set(_normalized(institution).split())
        query_words = set(_normalized(query_text).split())
        overlap = len(words & query_words) / max(len(query_words), 1)
        relevance = similarity + 0.05 * overlap
        if relevance < self.RELEVANCE_THRESHOLD:
            return None
        return relevance

    def _embedding_similarity(
        self, facility_code: str, institution: str, query_text: str,
    ) -> float | None:
        database = self._database()
        row = database.execute(
            "SELECT embedding FROM provider_embeddings WHERE facility_code = ?",
            (facility_code,),
        ).fetchone()
        if row is None:
            return None
        query = self._query_embedding(query_text)
        if query is None:
            return None
        vector = np.frombuffer(row[0], dtype=np.float32)
        norm = float(np.linalg.norm(vector)) * float(np.linalg.norm(query))
        if norm == 0:
            return None
        return float(np.dot(vector, query) / norm)

    def _query_embedding(self, query_text: str) -> Any:
        cache = self._query_cache
        if cache is not None and query_text in cache:
            return cache[query_text]
        model = self._embedding_model()
        if model is None:
            return None
        vector = np.asarray(next(model.embed([query_text])), dtype=np.float32)
        if cache is not None:
            cache[query_text] = vector
        return vector

    def _embedding_model(self):
        if self._embedder is None:
            try:
                from fastembed import TextEmbedding
                self._embedder = TextEmbedding("BAAI/bge-small-en-v1.5")
            except Exception:
                return None
        return self._embedder

    # Below this similarity, a program description is treated as unrelated
    # rather than a genuine synonym/abbreviation match. Calibrated against
    # real VA program titles: true synonyms (EMT vs. "EMERGENCY MEDICAL
    # TECHNICIAN", CNA vs. "CERTIFIED NURSING ASSISTANT", HVAC vs. "HEATING
    # VENTILATION AND AIR CONDITIONING") scored 0.72-0.87, while the closest
    # observed false positive (HVAC vs. "AVIATION MAINTENANCE", both
    # mechanical/technical trades) scored 0.66 -- there is no single
    # threshold that cleanly separates every case (a weaker true synonym,
    # CNA vs. "NURSE AIDE TRAINING", scored only 0.65), so this leans toward
    # precision. That's acceptable because programs_for() only calls this as
    # a last-resort fallback when its precise word-match search (including
    # word-form variants, known compounds, and hand-curated phrase synonyms)
    # already found nothing -- a plausible but uncertain semantic lead beats
    # telling a veteran no program exists at all.
    PROGRAM_SEMANTIC_THRESHOLD = 0.68

    def _semantic_program_matches(
        self, query_text: str, *, limit: int = 200,
    ) -> list[tuple[str, float]]:
        """Program descriptions whose meaning resembles query_text, most
        similar first, using scripts/init-va-program-embeddings.py's
        precomputed embeddings. Empty if that table hasn't been built yet or
        the embedding model isn't available."""
        if not query_text.strip():
            return []
        descriptions, matrix = self._program_embedding_index()
        if matrix is None or not len(descriptions):
            return []
        query = self._query_embedding(query_text)
        if query is None:
            return []
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0:
            return []
        similarities = matrix.dot(query) / query_norm
        candidate_positions = np.where(similarities >= self.PROGRAM_SEMANTIC_THRESHOLD)[0]
        ranked = sorted(
            ((float(similarities[i]), descriptions[i]) for i in candidate_positions),
            key=lambda item: -item[0],
        )
        return [(description, score) for score, description in ranked[:limit]]

    def _program_embedding_index(self) -> tuple[list[str], Any]:
        """Lazily loads and caches every (description, embedding) row as one
        normalized matrix, so a semantic query is a single matrix-vector
        product rather than a per-row SQLite round trip. Built once per
        process; ~237k rows nationwide costs roughly 350MB resident."""
        if self._program_embedding_matrix is not None:
            return self._program_embedding_descriptions or [], self._program_embedding_matrix
        database = self._database()
        if database.execute(
            "SELECT name FROM sqlite_master WHERE name = 'program_embeddings'"
        ).fetchone() is None:
            self._program_embedding_descriptions = []
            self._program_embedding_matrix = np.zeros((0, 0), dtype=np.float32)
            return [], self._program_embedding_matrix
        rows = database.execute("SELECT description, embedding FROM program_embeddings").fetchall()
        descriptions = [row[0] for row in rows]
        vectors = np.stack([
            np.frombuffer(row[1], dtype=np.float32) for row in rows
        ]) if rows else np.zeros((0, 384), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self._program_embedding_descriptions = descriptions
        self._program_embedding_matrix = vectors / norms
        return descriptions, self._program_embedding_matrix

    def nearest_ojt_providers(
        self, latitude: float, longitude: float, *, limit: int = 4,
        max_miles: float = 500,
    ) -> list[dict[str, Any]]:
        """Closest approved providers of either type regardless of name
        keywords. Many OJT sponsors have generic names (trust funds, JATCs,
        joint apprenticeship councils), and specialized trade schools such as
        diving academies are school providers rather than employers, so an
        empty employer search does not mean no training exists nearby."""
        rows = self._database().execute(
            "SELECT * FROM facilities WHERE approved = 1 "
            "AND (employer_provider = 1 OR school_provider = 1) "
            "AND latitude IS NOT NULL AND longitude IS NOT NULL"
        )
        facilities = []
        for row in rows:
            distance = _distance_miles(latitude, longitude, row["latitude"], row["longitude"])
            if distance <= max_miles:
                facilities.append(self._record(row, distance))
        return sorted(facilities, key=lambda item: item["distance_miles"])[:limit]

    def programs_for(
        self, keyword: str, *, state: str | None = None, limit: int = 8, offset: int = 0,
        latitude: float | None = None, longitude: float | None = None,
        max_miles: float | None = None,
    ) -> dict[str, Any]:
        """Search VA's own approved IHL/NCD program catalog for a keyword,
        nationwide or within one state. Built by
        scripts/init-va-programs-data.py, which bulk-crawls every approved
        school's program list from the same VA API provider_details() uses
        for a single facility. Complements the IPEDS/O*NET pipeline, which
        can miss proprietary trade schools such as commercial diving
        academies that VA approves directly."""
        database = self._database()
        if database.execute(
            "SELECT name FROM sqlite_master WHERE name = 'va_program_search'"
        ).fetchone() is None:
            return {
                "total_facilities": 0, "total_programs": 0, "facilities": [],
                "source": "VA GI Bill Comparison Tool approved program catalog",
                "note": "VA program index has not been built yet. Run scripts/init-va-programs-data.py.",
            }
        terms = [
            term for term in _normalized(keyword).split()
            if (len(term) >= 3 or term in DEGREE_LEVEL_TERMS) and term not in PROGRAM_STOP_WORDS
        ]
        if not terms:
            return {
                "total_facilities": 0, "total_programs": 0, "facilities": [],
                "source": "VA GI Bill Comparison Tool approved program catalog",
            }
        term_variants = [_word_variants(term) for term in terms]
        # See RETRIEVAL_TERM_SYNONYMS -- a same-credential word swap (e.g.
        # "flight" for a "pilot" search) so a school reached ONLY via the
        # synonym word actually surfaces in the search in the first place.
        term_expansions = [RETRIEVAL_TERM_SYNONYMS.get(term, set()) for term in terms]
        # The FTS5 prefix wildcard below is a raw character-prefix match, so a
        # query for "diver" also matches indexed terms like "diversity" and
        # "diversified" that merely start with the same letters. Require each
        # query term (or one of its word-form variants, such as "diving" for
        # "diver") to appear as a whole word (allowing a short suffix such as
        # a plural "s") in the actual description before counting it as a
        # genuine match.
        word_pattern_groups = [
            [
                # A short degree/credential abbreviation (e.g. "bs") must
                # match exactly -- the trailing \w? tolerance below exists
                # for an ordinary word's plural ("diver" -> "divers"), but
                # for a 2-3 letter code it would also accept an unrelated,
                # longer credential that merely starts with the same
                # letters (e.g. "bs" matching "BSN", Bachelor of Science in
                # Nursing, defeating the point of an explicit "bs" search).
                re.compile(rf"\b{re.escape(variant)}\b", re.I)
                if term in DEGREE_LEVEL_TERMS
                else re.compile(rf"\b{re.escape(variant)}\w?\b", re.I)
                for variant in variants
            ] + (
                [re.compile(rf"\b{re.escape(TERM_PHRASE_SYNONYMS[term])}\b", re.I)]
                if term in TERM_PHRASE_SYNONYMS else []
            ) + [
                re.compile(rf"\b{re.escape(expansion)}\w?\b", re.I)
                for expansion in expansions
            ]
            for term, variants, expansions in zip(terms, term_variants, term_expansions)
        ]
        # A phrase synonym (e.g. "emt" -> "emergency medical") or a field
        # expansion word (e.g. "flight" for "pilot") only helps once the
        # FTS5 stage above has actually retrieved the row -- a query for
        # "emt"*/"pilot"* alone never matches an indexed "emergency"/"flight"
        # token, so each has to be added to that term's FTS clause too, kept
        # separate from term_variants (used for the whole-word regex above)
        # so the acronym-only variants there stay exact.
        fts_term_variants = [
            (
                variants + [TERM_PHRASE_SYNONYMS[term].split()[0]] if term in TERM_PHRASE_SYNONYMS
                else variants
            ) + list(expansions)
            for term, variants, expansions in zip(terms, term_variants, term_expansions)
        ]
        # A multi-word query like "fire fighter" is sometimes written as one
        # fused compound word in VA's own data ("FIREFIGHTER"), with no space
        # or other separator between the two terms. Neither term then matches
        # as an isolated whole word: "fire" is immediately followed by more
        # word characters ("fighter"), so \bfire\w?\b never finds a boundary,
        # and likewise "fighter" is never preceded by one. FTS5 prefix search
        # only compounds the problem for a strict all-terms-required query --
        # "fighter"* has no token in "FIREFIGHTER ACADEMY" that starts with
        # "fighter" (the row's only tokens are "firefighter" and "academy"),
        # so that row is never even retrieved, regardless of the regex step
        # below. Both the retrieval query and the whole-word check need a
        # concatenated-compound escape hatch, or a plainly-relevant result
        # like Chabot College's "FIRE FIGHTER ACADEMY"... sorry, "FIREFIGHTER
        # ACADEMY" silently vanishes from a "fire fighter" search.
        concatenated_term = "".join(terms) if len(terms) > 1 else None
        concatenated_pattern = (
            re.compile(rf"\b{re.escape(concatenated_term)}\w?\b", re.I)
            if concatenated_term else None
        )

        by_distance = latitude is not None and longitude is not None
        facility_cache: dict[str, sqlite3.Row | None] = {}
        distance_cache: dict[str, float | None] = {}

        def _pipeline(
            *, require_all: bool, concept: dict[str, Any] | None = None,
        ) -> list[dict[str, Any]]:
            if concept is not None:
                return _group(
                    [
                        row for row in database.execute(
                            "SELECT facility_code, program_type, description, "
                            "bm25(va_program_search) AS rank FROM va_program_search "
                            "WHERE va_program_search MATCH ? ORDER BY rank",
                            (concept["fts"],),
                        ).fetchall()
                        if concept["title"].search(row[2])
                        and (row[0], row[2]) not in KNOWN_FALSE_POSITIVE_PROGRAMS
                    ],
                    require_all=True,
                )
            match_query = (
                " AND ".join(
                    "(" + " OR ".join(f'"{variant}"*' for variant in variants) + ")"
                    for variants in fts_term_variants
                )
                if require_all
                else " OR ".join(
                    f'"{variant}"*' for variants in fts_term_variants for variant in variants
                )
            )
            if concatenated_term:
                match_query = f'({match_query}) OR "{concatenated_term}"*'
            found = database.execute(
                "SELECT facility_code, program_type, description, "
                "bm25(va_program_search) AS rank FROM va_program_search "
                "WHERE va_program_search MATCH ? ORDER BY rank",
                (match_query,),
            ).fetchall()
            combine = all if require_all else any
            rows = [
                row for row in found
                if (
                    combine(
                        any(pattern.search(row[2]) for pattern in group)
                        for group in word_pattern_groups
                    )
                    or (concatenated_pattern is not None and concatenated_pattern.search(row[2]))
                )
                and (row[0], row[2]) not in KNOWN_FALSE_POSITIVE_PROGRAMS
            ]
            if any(term.startswith("div") for term in terms):
                rows = [
                    row for row in rows
                    if not any(marker in row[2].lower() for marker in DIVERSITY_FALSE_POSITIVE_MARKERS)
                ]
            expansion_markers = {
                marker
                for term in terms
                for marker in FIELD_EXPANSION_FALSE_POSITIVE_MARKERS.get(term, set())
            }
            if expansion_markers:
                rows = [
                    row for row in rows
                    if not any(marker in row[2].lower() for marker in expansion_markers)
                ]
            return _group(rows, require_all=require_all)

        def _group(rows: list[Any], *, require_all: bool) -> list[dict[str, Any]]:
            grouped: dict[str, dict[str, Any]] = {}
            for facility_code, program_type, description, rank in rows:
                if facility_code not in facility_cache:
                    facility_cache[facility_code] = database.execute(
                        "SELECT * FROM facilities WHERE facility_code = ? AND approved = 1",
                        (facility_code,),
                    ).fetchone()
                facility_row = facility_cache[facility_code]
                if facility_row is None:
                    continue
                if state and facility_row["state"] != state.upper():
                    continue
                if by_distance:
                    if facility_code not in distance_cache:
                        if facility_row["latitude"] is None or facility_row["longitude"] is None:
                            distance_cache[facility_code] = None
                        else:
                            distance_cache[facility_code] = _distance_miles(
                                latitude, longitude, facility_row["latitude"], facility_row["longitude"],
                            )
                    distance = distance_cache[facility_code]
                    # A facility with no usable coordinates (missing/unrecognized
                    # ZIP -- mostly overseas schools, see app/va.py facilities
                    # table) can't be placed relative to the search location, so
                    # it's excluded rather than kept with an unknown distance.
                    if distance is None:
                        continue
                    if max_miles is not None and distance > max_miles:
                        continue
                else:
                    distance = None
                if facility_code not in grouped:
                    # self._record() issues its own DB lookups (website/apply
                    # guesses) -- call it once per facility, not once per
                    # matching program row, which dict.setdefault() would
                    # otherwise do since it evaluates its default argument
                    # unconditionally on every call.
                    grouped[facility_code] = {
                        "facility": self._record(facility_row, distance),
                        "matching_programs": [],
                        "best_rank": rank,
                        "match_count": 0,
                        "distance": distance,
                    }
                entry = grouped[facility_code]
                entry["matching_programs"].append({"type": program_type, "description": description})
                entry["best_rank"] = min(entry["best_rank"], rank)
                if not require_all:
                    # bm25 has no notion of "satisfies more of the original
                    # query terms", so an OR fallback match (e.g. "auto
                    # mechanic" relaxed to auto-or-mechanic) is ranked on how
                    # many of the original terms this specific row actually
                    # satisfies before falling back to bm25 as a tie-break --
                    # otherwise a single-term match could outrank a result
                    # satisfying every term just because of its raw bm25 rank.
                    match_count = sum(
                        any(pattern.search(description) for pattern in group)
                        for group in word_pattern_groups
                    )
                    entry["match_count"] = max(entry["match_count"], match_count)

            if not require_all:
                if by_distance:
                    return sorted(
                        grouped.values(),
                        key=lambda item: (item["distance"], -item["match_count"], item["best_rank"]),
                    )
                return sorted(
                    grouped.values(), key=lambda item: (-item["match_count"], item["best_rank"]),
                )
            if by_distance:
                return sorted(grouped.values(), key=lambda item: (item["distance"], item["best_rank"]))
            return sorted(grouped.values(), key=lambda item: item["best_rank"])

        def _semantic_pipeline() -> list[dict[str, Any]]:
            """Last-resort fallback when the precise word-match search above
            (variants, known compounds, hand-curated phrase synonyms) found
            nothing at all: match by MEANING against every distinct program
            description nationwide (see _semantic_program_matches), so a
            genuine synonym/abbreviation the precise search has no rule for
            yet -- CNA, HVAC, CDL, or whatever's reported next -- still
            surfaces without needing its own hand-written exception."""
            matches = self._semantic_program_matches(keyword)
            if not matches:
                return []
            description_scores = dict(matches)
            placeholders = ",".join("?" for _ in description_scores)
            found = database.execute(
                "SELECT facility_code, program_type, description FROM va_program_search "
                f"WHERE description IN ({placeholders})",
                tuple(description_scores),
            ).fetchall()
            grouped: dict[str, dict[str, Any]] = {}
            for facility_code, program_type, description in found:
                if (facility_code, description) in KNOWN_FALSE_POSITIVE_PROGRAMS:
                    continue
                if facility_code not in facility_cache:
                    facility_cache[facility_code] = database.execute(
                        "SELECT * FROM facilities WHERE facility_code = ? AND approved = 1",
                        (facility_code,),
                    ).fetchone()
                facility_row = facility_cache[facility_code]
                if facility_row is None:
                    continue
                if state and facility_row["state"] != state.upper():
                    continue
                if by_distance:
                    if facility_code not in distance_cache:
                        if facility_row["latitude"] is None or facility_row["longitude"] is None:
                            distance_cache[facility_code] = None
                        else:
                            distance_cache[facility_code] = _distance_miles(
                                latitude, longitude, facility_row["latitude"], facility_row["longitude"],
                            )
                    distance = distance_cache[facility_code]
                    if distance is None:
                        continue
                    if max_miles is not None and distance > max_miles:
                        continue
                else:
                    distance = None
                score = description_scores[description]
                if facility_code not in grouped:
                    record = self._record(facility_row, distance)
                    record["match_type"] = "semantic"
                    grouped[facility_code] = {
                        "facility": record, "matching_programs": [], "best_score": score,
                        "distance": distance,
                    }
                entry = grouped[facility_code]
                entry["matching_programs"].append({"type": program_type, "description": description})
                entry["best_score"] = max(entry["best_score"], score)
            if by_distance:
                return sorted(grouped.values(), key=lambda item: (item["distance"], -item["best_score"]))
            return sorted(grouped.values(), key=lambda item: -item["best_score"])

        concept = next(
            (item for item in CREDENTIAL_CONCEPTS if item["query"].search(keyword)), None,
        )
        ordered = _pipeline(require_all=True, concept=concept)
        # A plain-English multi-word trade query (e.g. "auto mechanic") can
        # legitimately return nothing near the searched location under a
        # strict all-terms-required search, because VA program titles often
        # use different wording for the same trade (e.g. "AUTOMOTIVE
        # TECHNOLOGY", never "mechanic") -- there is no dedicated fallback
        # source like find_local_training has for these results, so an empty
        # strict search here can silently tell a veteran no program exists
        # for a trade that plainly has several approved schools nearby. When
        # that happens with more than one search term, retry once requiring
        # only ANY term to genuinely match.
        if not ordered and len(term_variants) > 1:
            ordered = _pipeline(require_all=False)
        if not ordered:
            ordered = _semantic_pipeline()
        results = [
            {
                **item["facility"],
                "matching_programs": item["matching_programs"][:6],
                "matching_program_count": len(item["matching_programs"]),
            }
            for item in ordered[offset:offset + limit]
        ]
        return {
            "total_facilities": len(ordered),
            "total_programs": sum(len(item["matching_programs"]) for item in ordered),
            "facilities": results,
            "offset": offset,
            "remaining_facilities": max(0, len(ordered) - offset - len(results)),
            "source": "VA GI Bill Comparison Tool approved program catalog",
            # Internal, for the related-programs step in app/agent.py (which
            # pops both before the result reaches the model): every matched
            # school across all pages, and the fields of study the matches
            # themselves belong to.
            "_matched_facility_codes": [item["facility"]["facility_code"] for item in ordered],
            "_matched_fields": self._dominant_fields([
                (item["facility"]["facility_code"], program["description"])
                for item in ordered for program in item["matching_programs"]
            ]),
        }

    # A program title's field of study (scripts/init-va-program-fields.py)
    # is an AI match, right roughly 90% of the time in a hand review; below
    # these scores the wrong matches dominated, so the program is treated as
    # having no known field. A pick from the school's own federal (IPEDS)
    # program list is more trustworthy than a nationwide one at the same score.
    # Set for precision (a wrong "related" suggestion costs a veteran's trust;
    # a missing one costs little): 0.74 still let Canada College's "BUSINESS
    # ASSISTANT" through as Medical/Clinical Assistant. About 70% of programs
    # clear these.
    FIELD_MIN_SCORE = {"school": 0.75, "global": 0.78, "rule": 0.0}

    def _has_program_fields(self) -> bool:
        return self._database().execute(
            "SELECT name FROM sqlite_master WHERE name = 'va_program_fields'"
        ).fetchone() is not None

    def _dominant_fields(self, programs: list[tuple[str, str]], limit: int = 3) -> list[str]:
        """The field(s) of study most of the matched programs belong to --
        e.g. Data Science and Data Analytics for a "data" search -- ignoring
        fields only a stray match or two landed in."""
        if not programs or not self._has_program_fields():
            return []
        database = self._database()
        counts: dict[str, int] = {}
        for facility_code, description in programs:
            row = database.execute(
                "SELECT cip, score, method FROM va_program_fields "
                "WHERE facility_code = ? AND description = ?",
                (facility_code, description),
            ).fetchone()
            if row and row[0] and row[1] >= self.FIELD_MIN_SCORE.get(row[2], 1):
                counts[row[0]] = counts.get(row[0], 0) + 1
        total = sum(counts.values())
        ranked = sorted(counts.items(), key=lambda item: -item[1])
        # 25%: a couple of mis-sorted matches (roughly 1 in 10 titles) must not
        # pull in a field of their own -- two HVAC titles filed under Computer
        # Installation and Repair made up 17% of a Phoenix HVAC search.
        return [cip for cip, count in ranked[:limit] if total and count / total >= 0.25]

    def programs_in_fields(
        self, fields: dict[str, dict[str, Any]], *, exclude_facilities: set[str],
        state: str | None = None, latitude: float | None = None,
        longitude: float | None = None, max_miles: float | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Approved programs whose field of study is one of fields (a CIP code
        -> {"title", "overlap", ...} map from IpedsIndex.related_fields), at
        schools not in exclude_facilities, within the same state/radius as
        the main search. Closest (or most related) schools first."""
        if not fields or not self._has_program_fields():
            return []
        database = self._database()
        placeholders = ",".join("?" for _ in fields)
        rows = database.execute(
            "SELECT facility_code, description, cip, score, method, program_type "
            f"FROM va_program_fields WHERE cip IN ({placeholders})",
            tuple(fields),
        ).fetchall()
        by_distance = latitude is not None and longitude is not None
        grouped: dict[str, dict[str, Any]] = {}
        for facility_code, description, cip, score, method, program_type in rows:
            if facility_code in exclude_facilities or score < self.FIELD_MIN_SCORE.get(method, 1):
                continue
            if facility_code not in grouped:
                facility_row = database.execute(
                    "SELECT * FROM facilities WHERE facility_code = ? AND approved = 1",
                    (facility_code,),
                ).fetchone()
                if facility_row is None or (state and facility_row["state"] != state.upper()):
                    grouped[facility_code] = None
                    continue
                distance = None
                if by_distance:
                    if facility_row["latitude"] is None or facility_row["longitude"] is None:
                        grouped[facility_code] = None
                        continue
                    distance = _distance_miles(
                        latitude, longitude, facility_row["latitude"], facility_row["longitude"],
                    )
                    if max_miles is not None and distance > max_miles:
                        grouped[facility_code] = None
                        continue
                grouped[facility_code] = {
                    "facility_row": facility_row, "distance": distance, "programs": [],
                    "best_overlap": 0.0,
                }
            entry = grouped[facility_code]
            if entry is None:
                continue
            entry["programs"].append({
                "type": program_type, "description": description,
                "related_field": fields[cip]["title"], "score": score,
            })
            entry["best_overlap"] = max(entry["best_overlap"], fields[cip].get("overlap", 0))
        entries = [entry for entry in grouped.values() if entry]
        if by_distance:
            entries.sort(key=lambda entry: (entry["distance"], -entry["best_overlap"]))
        else:
            entries.sort(key=lambda entry: (-entry["best_overlap"], -len(entry["programs"])))
        return [
            {
                **self._record(entry["facility_row"], entry["distance"]),
                # Most confident field matches first, so the card's sample
                # leads with the programs least likely to be mis-sorted.
                "matching_programs": [
                    {key: value for key, value in program.items() if key != "score"}
                    for program in sorted(entry["programs"], key=lambda item: -item["score"])[:6]
                ],
                "matching_program_count": len(entry["programs"]),
                "related_fields": sorted({program["related_field"] for program in entry["programs"]}),
            }
            for entry in entries[:limit]
        ]

    def match_school(self, name: str) -> dict[str, Any] | None:
        normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        rows = self._database().execute(
            "SELECT * FROM facilities WHERE approved = 1 AND school_provider = 1"
        )
        best = None
        for row in rows:
            candidate = re.sub(r"[^a-z0-9]+", " ", row["institution"].lower()).strip()
            if candidate == normalized:
                return self._record(row)
            if normalized in candidate or candidate in normalized:
                best = self._record(row)
        return best

    def _record(self, row: sqlite3.Row, distance: float | None = None) -> dict[str, Any]:
        database = self._database()
        guessed_website = None
        if not row["insturl"]:
            if database.execute(
                "SELECT name FROM sqlite_master WHERE name = 'va_website_guesses'"
            ).fetchone() is not None:
                guess = database.execute(
                    "SELECT url FROM va_website_guesses WHERE facility_code = ?",
                    (row["facility_code"],),
                ).fetchone()
                guessed_website = guess[0] if guess else None
        apply_url = apply_label = None
        if database.execute(
            "SELECT name FROM sqlite_master WHERE name = 'va_admissions_guesses'"
        ).fetchone() is not None:
            apply = database.execute(
                "SELECT url, label FROM va_admissions_guesses WHERE facility_code = ?",
                (row["facility_code"],),
            ).fetchone()
            if apply:
                apply_url, apply_label = apply
        # VA's own veterans page link wins; otherwise fall back to one from the
        # federal IPEDS college directory (VETURL), which is kept in its own
        # table so scripts/init-va-data.py rebuilding facilities can't wipe it.
        veteran_page_url = row["vet_tuition_policy_url"]
        if not veteran_page_url and database.execute(
            "SELECT name FROM sqlite_master WHERE name = 'va_veterans_page_guesses'"
        ).fetchone() is not None:
            veteran_page = database.execute(
                "SELECT url FROM va_veterans_page_guesses WHERE facility_code = ?",
                (row["facility_code"],),
            ).fetchone()
            veteran_page_url = veteran_page[0] if veteran_page else None
        return {
            "facility_code": row["facility_code"],
            "detail_url": (
                "https://www.va.gov/education/gi-bill-comparison-tool/"
                "schools-and-employers/institution/"
                + row["facility_code"]
            ),
            "institution": row["institution"],
            "city": row["city"],
            "state": row["state"],
            "zip": row["zip"],
            "type": row["type"],
            "distance_miles": round(distance, 1) if distance is not None else None,
            "monthly_housing_rate": _number(row["bah"]),
            "website": row["insturl"],
            # Not VA-confirmed -- found via a separate offline search script
            # (scripts/init-va-website-guesses.py) only when VA's own
            # website field is empty. Must be labeled as unverified wherever
            # it's surfaced; see the "Unverified school link" handling in
            # app/agent.py.
            "guessed_website": guessed_website,
            # Found offline by scripts/init-va-admissions-guesses.py (crawls
            # the school's own known website, VA-confirmed or guessed, once
            # for its apply/admissions page) or, when that found nothing,
            # scripts/init-va-apply-path-guesses.py (tries a plain "/apply"
            # path directly). Not VA-confirmed and may go stale if the school
            # redesigns its site -- present it as an "Apply" link to the
            # school's own admissions page, not a guarantee the process still
            # works exactly as found.
            "apply_url": apply_url,
            "apply_label": apply_label,
            "veteran_tuition_policy_url": veteran_page_url,
            "p911_recipients": row["p911_recipients"],
            "p911_tuition_fees": _number(row["p911_tuition_fees"]),
            "yellow_ribbon_recipients": row["p911_yr_recipients"],
            "yellow_ribbon_payments": _number(row["p911_yellow_ribbon"]),
            "accredited": bool(row["accredited"]),
            "accreditation_status": row["accreditation_status"],
            "caution_flag": bool(row["caution_flag"]),
            "caution_flag_reason": row["caution_flag_reason"],
            "school_closing": bool(row["school_closing"]),
            "credit_for_military_training": bool(row["credit_for_mil_training"]),
        }

    def find_facility(self, query: str) -> dict[str, Any] | None:
        normalized = _normalized(query)
        if not normalized:
            return None
        exact_code = self._database().execute(
            "SELECT * FROM facilities WHERE facility_code = ? COLLATE NOCASE AND approved = 1",
            (query.strip(),),
        ).fetchone()
        if exact_code:
            return self._record(exact_code)
        rows = self._database().execute("SELECT * FROM facilities WHERE approved = 1")
        best = None
        best_score = 0.0
        query_terms = set(normalized.split())
        for row in rows:
            name = _normalized(row["institution"])
            if name == normalized:
                return self._record(row)
            name_terms = set(name.split())
            score = len(query_terms & name_terms) / max(len(query_terms), 1)
            if score > best_score and (normalized in name or score >= 0.75):
                best = row
                best_score = score
        return self._record(best) if best is not None else None
