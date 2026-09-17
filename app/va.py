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
}
PROGRAM_STOP_WORDS = {
    "about", "and", "career", "certificate", "degree", "find", "for", "from",
    "help", "near", "program", "programs", "school", "study", "the", "training",
    "want", "with",
}
PROGRAM_CONTEXT_EXPANSIONS = {
    "healthcare": {
        "ambulatory", "case", "clinical", "coder", "health", "hospital", "medical",
        "nursing", "patient", "radiologic", "sterile", "surgical",
    },
    "technology": {"computer", "cyber", "data", "information", "network", "software"},
    "trades": {"carpenter", "construction", "electrical", "electrician", "hvac", "plumbing", "welding"},
}
# VA program titles are short and sometimes abbreviate "diverse"/"diversity"
# down to just "diver"/"divers" (e.g. "MA EDU LINGUISTICS DIVER EDU EQUITY"
# for "...Diverse Edu Equity"), which collides with genuine diver/diving
# trade programs under a whole-word match. These titles reliably co-occur
# with DEI/education jargon that never appears in an actual diving program,
# so a match containing any of these markers is dropped as a false positive.
DIVERSITY_FALSE_POSITIVE_MARKERS = ("equity", "inclus", "cultu", "lingui", "cltrly", "learn")


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
            chosen = sorted(matches, key=lambda item: (-item[0], item[1]))[:6]
            selection = "relevant"
            if not terms:
                chosen = ranked[:6]
                selection = "all"
            elif not chosen:
                chosen = ranked[:6]
                selection = "sample"
            summaries.append({
                "type": code,
                "label": PROGRAM_LABELS.get(code, f"{code} programs"),
                "total": len(ranked),
                "matching": len(matches) if terms else len(ranked),
                "selection": selection,
                "category_counts": category_counts,
                "programs": [
                    {"name": item[2], "category": item[3]}
                    for item in chosen
                ],
            })
        return {
            "facility_code": attributes.get("facility_code") or facility_code,
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
            return None
        return {
            "label": zip_code,
            "city": None,
            "state": None,
            "latitude": center["latitude"],
            "longitude": center["longitude"],
            "representative_zip": zip_code,
        }

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
        self, keyword: str, *, state: str | None = None, limit: int = 8,
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
            if len(term) >= 3 and term not in PROGRAM_STOP_WORDS
        ]
        if not terms:
            return {
                "total_facilities": 0, "total_programs": 0, "facilities": [],
                "source": "VA GI Bill Comparison Tool approved program catalog",
            }
        term_variants = [_word_variants(term) for term in terms]
        match_query = " AND ".join(
            "(" + " OR ".join(f'"{variant}"*' for variant in variants) + ")"
            for variants in term_variants
        )
        rows = database.execute(
            "SELECT facility_code, program_type, description, "
            "bm25(va_program_search) AS rank FROM va_program_search "
            "WHERE va_program_search MATCH ? ORDER BY rank",
            (match_query,),
        ).fetchall()

        # The FTS5 prefix wildcard above is a raw character-prefix match, so a
        # query for "diver" also matches indexed terms like "diversity" and
        # "diversified" that merely start with the same letters. Require each
        # query term (or one of its word-form variants, such as "diving" for
        # "diver") to appear as a whole word (allowing a short suffix such as
        # a plural "s") in the actual description before counting it as a
        # genuine match.
        word_pattern_groups = [
            [re.compile(rf"\b{re.escape(variant)}\w?\b", re.I) for variant in variants]
            for variants in term_variants
        ]
        rows = [
            row for row in rows
            if all(
                any(pattern.search(row[2]) for pattern in group)
                for group in word_pattern_groups
            )
        ]
        if any(term.startswith("div") for term in terms):
            rows = [
                row for row in rows
                if not any(marker in row[2].lower() for marker in DIVERSITY_FALSE_POSITIVE_MARKERS)
            ]

        grouped: dict[str, dict[str, Any]] = {}
        facility_cache: dict[str, sqlite3.Row | None] = {}
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
            entry = grouped.setdefault(facility_code, {
                "facility": self._record(facility_row),
                "matching_programs": [],
                "best_rank": rank,
            })
            entry["matching_programs"].append({"type": program_type, "description": description})
            entry["best_rank"] = min(entry["best_rank"], rank)

        ordered = sorted(grouped.values(), key=lambda item: item["best_rank"])
        results = [
            {
                **item["facility"],
                "matching_programs": item["matching_programs"][:6],
                "matching_program_count": len(item["matching_programs"]),
            }
            for item in ordered[:limit]
        ]
        return {
            "total_facilities": len(ordered),
            "total_programs": sum(len(item["matching_programs"]) for item in ordered),
            "facilities": results,
            "source": "VA GI Bill Comparison Tool approved program catalog",
        }

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

    @staticmethod
    def _record(row: sqlite3.Row, distance: float | None = None) -> dict[str, Any]:
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
            "veteran_tuition_policy_url": row["vet_tuition_policy_url"],
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
