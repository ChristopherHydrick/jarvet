from __future__ import annotations

import collections
import math
import sqlite3
from pathlib import Path
from typing import Any

AWARD_LEVELS = {
    "1": "certificate", "2": "certificate", "3": "associate", "4": "certificate",
    "5": "bachelor's", "6": "post-bachelor's certificate", "7": "master's",
    "8": "post-master's certificate", "17": "doctoral", "18": "doctoral",
    "19": "doctoral", "20": "doctoral", "21": "doctoral",
}
CONTROL_LABELS = {1: "public", 2: "private nonprofit", 3: "private for-profit"}
# Careers the CIP-to-SOC crosswalk attaches to so many fields of study that
# sharing one says nothing about two fields being related: every academic
# subject maps to its own postsecondary-teacher code (25-1xxx), and catch-alls
# like "Natural Sciences Managers" (11-9121) or "Managers, All Other" (11-9199)
# link 100+ fields -- which made Creative Writing "related" to Data Science.
# Most careers link about 4 fields; anything linked to more than this is
# ignored when relating fields, as is every "All Other" catch-all code (SOC
# codes ending in 9, e.g. 19-4099, 27-1019) -- "Life, Physical, and Social
# Science Technicians, All Other" made a nanomaterials certificate "related"
# to criminal justice.
GENERIC_CAREER_FIELD_LIMIT = 30
# Below this share of careers in common (shared / combined), two fields are
# not shown as related. Calibrated by hand across data science, nursing
# assistant, welding, criminal justice, graphic design, accounting and
# electrician fields.
RELATED_FIELD_MIN_OVERLAP = 0.2


class IpedsIndex:
    """Local school-program index built from IPEDS completions and the official
    O*NET CIP-to-SOC crosswalk. Replaces scraping My Next Move, which derives
    its local-training table from the same sources."""

    def __init__(self, path: Path):
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.institution_count = 0
        self.program_count = 0
        self.cip_titles: dict[str, str] = {}
        self.field_careers: dict[str, set[str]] = {}
        self.career_fields: dict[str, set[str]] = {}

    def load(self) -> None:
        if not self.path.exists():
            raise RuntimeError("IPEDS index is missing. Run scripts/init-ipeds-data.py.")
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.institution_count = self.connection.execute(
            "SELECT COUNT(*) FROM institutions"
        ).fetchone()[0]
        self.program_count = self.connection.execute(
            "SELECT COUNT(*) FROM programs"
        ).fetchone()[0]
        self.cip_titles = dict(self.connection.execute("SELECT cip, title FROM cip_titles"))
        linked: dict[str, set[str]] = collections.defaultdict(set)
        for cip, soc_codes in self.connection.execute(
            "SELECT DISTINCT cip, soc_codes FROM programs"
        ):
            for soc in soc_codes.replace(";", ",").split(","):
                if soc.strip():
                    linked[cip].add(soc.strip()[:7])
        fields_per_career = collections.Counter(soc for socs in linked.values() for soc in socs)
        self.field_careers = {
            cip: {
                soc for soc in socs
                if not soc.startswith("25-1") and not soc.endswith("9")
                and fields_per_career[soc] <= GENERIC_CAREER_FIELD_LIMIT
            }
            for cip, socs in linked.items()
        }
        # Career -> fields for going from a job (e.g. a military job's civilian
        # match) to what to study. Unlike field_careers this keeps "All Other"
        # catch-alls -- a job's own code is all there is to go on, and the
        # Army 25B's current civilian match is "Computer Occupations, All
        # Other" -- but still drops careers linked to so many fields that
        # they point nowhere in particular.
        career_fields: dict[str, set[str]] = collections.defaultdict(set)
        for cip, socs in linked.items():
            for soc in socs:
                if fields_per_career[soc] <= GENERIC_CAREER_FIELD_LIMIT:
                    career_fields[soc].add(cip)
        self.career_fields = dict(career_fields)

    def related_fields(self, cip: str, limit: int = 8) -> list[dict[str, Any]]:
        """Fields of study that prepare for the same careers as cip, most
        overlapping first, via the official CIP-to-SOC crosswalk. Each entry
        carries the shared career codes so a caller can explain the link."""
        careers = self.field_careers.get(cip) or set()
        if not careers:
            return []
        scored = []
        for other, other_careers in self.field_careers.items():
            shared = careers & other_careers
            if other == cip or not shared:
                continue
            overlap = len(shared) / len(careers | other_careers)
            if overlap >= RELATED_FIELD_MIN_OVERLAP:
                scored.append((overlap, other, shared))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {
                "cip": other, "title": self.cip_titles.get(other, other),
                "overlap": round(overlap, 2), "shared_careers": sorted(shared),
            }
            for overlap, other, shared in scored[:limit]
        ]

    def fields_for_careers(self, soc_codes: list[str]) -> list[str]:
        """Fields of study the CIP-to-SOC crosswalk links to any of these
        careers (SOC codes; an O*NET code's .00 suffix is ignored)."""
        return sorted({
            cip for soc in soc_codes for cip in self.career_fields.get(soc[:7], set())
        })

    def _database(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("IPEDS index has not been loaded.")
        return self.connection

    def program_count_for(self, soc_code: str) -> int:
        row = self._database().execute(
            "SELECT program_count FROM soc_index WHERE soc = ?", (soc_code,)
        ).fetchone()
        return row[0] if row else 0

    def programs_for(
        self, soc_code: str, *, latitude: float | None = None,
        longitude: float | None = None, state: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Programs for one O*NET-SOC code, ranked by proximity when
        coordinates are given, otherwise alphabetically. Returns the chosen
        rows plus the total count so the agent can say how many more exist."""
        database = self._database()
        base_sql = (
            "SELECT p.unitid, p.cip, p.awlevel, p.awards, p.soc_codes, "
            "t.title AS cip_title, "
            "i.name, i.city, i.state, i.zip, i.latitude, i.longitude, i.website, "
            "i.control FROM programs p JOIN institutions i ON i.unitid = p.unitid "
            "LEFT JOIN cip_titles t ON t.cip = p.cip "
            "WHERE p.soc_codes LIKE ?"
        )
        pattern = f"%{soc_code}%"
        parameters: list[Any] = [pattern]
        if state:
            base_sql += " AND i.state = ?"
            parameters.append(state.upper())
        rows = database.execute(base_sql, parameters).fetchall()

        scored: list[tuple[float, int, float | None, sqlite3.Row]] = []
        for row in rows:
            socs = row["soc_codes"].split(",")
            if soc_code not in socs:
                continue
            if (
                latitude is not None and longitude is not None
                and row["latitude"] is not None and row["longitude"] is not None
            ):
                distance = _distance_miles(
                    latitude, longitude, row["latitude"], row["longitude"],
                )
                score = -distance
            else:
                distance = None
                score = 0.0
            scored.append((score, -row["awards"], distance, row))
        scored.sort(key=lambda item: (-item[0], item[1]))

        results = []
        for _, _, distance, row in scored[:limit]:
            results.append({
                "unitid": row["unitid"],
                "institution": row["name"],
                "city": row["city"],
                "state": row["state"],
                "zip": row["zip"],
                "cip": row["cip"],
                "cip_title": row["cip_title"] or row["cip"],
                "award_level": AWARD_LEVELS.get(str(row["awlevel"]), "award"),
                "recent_awards": row["awards"],
                "website": row["website"],
                "control": CONTROL_LABELS.get(row["control"]),
                "distance_miles": round(distance, 1) if distance is not None else None,
            })
        return {
            "total": len(scored),
            "programs": results,
            "source": "IPEDS completions + O*NET CIP-to-SOC crosswalk",
        }

    def match_institution(self, name: str) -> dict[str, Any] | None:
        normalized = " ".join(name.lower().split())
        row = self._database().execute(
            "SELECT * FROM institutions WHERE LOWER(name) = ?", (normalized,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)


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
