from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import Any

from pyoxigraph import Store

SCHEMA = "https://www.onetcenter.org/rdf/schema/onet/"
ROOT = Path(__file__).resolve().parent.parent
BRIGHT_OUTLOOK = ROOT / "data" / "db_31_0_nt" / "BrightOutlook.csv"
MILITARY_CROSSWALK = ROOT / "data" / "military-crosswalk" / "military_crosswalk.csv"
PREFIXES = f"PREFIX onet: <{SCHEMA}>\nPREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>"
MILITARY_BRANCH_LABELS = {
    "A": "Army", "C": "Coast Guard", "F": "Air Force", "H": "Space Force",
    "M": "Marine Corps", "N": "Navy", "P": "Navy (Officer Designator)",
    "G": "Federal Civilian (OPM)",
}
STOP_WORDS = {
    "about", "already", "and", "are", "area", "available", "career", "certificate",
    "become", "consider", "could", "degree", "education", "enjoy", "find", "fits", "have", "help", "idea",
    "interested", "into", "jose", "like", "live", "matching", "near", "nearby", "path",
    "paths", "running", "san", "should", "that", "the", "want", "what", "where", "with",
    "would",
}
def _search_terms(query: str) -> list[str]:
    terms = {
        term for term in re.findall(r"[a-z0-9]+", query.lower())
        if len(term) > 2 and term not in STOP_WORDS and not term.isdigit()
    }
    terms.update(term[:-5] for term in tuple(terms) if term.endswith("shops") and len(term) > 7)
    return sorted(terms)


def _military_code_candidates(query: str) -> list[str]:
    return list(dict.fromkeys(
        token.upper() for token in re.findall(r"[A-Za-z0-9]+", query)
        if 2 <= len(token) <= 7 and any(char.isdigit() for char in token)
    ))


def _text(term: Any) -> str:
    return term.value if term is not None else ""


class OnetGraph:
    def __init__(self, store_path: Path):
        self.store_path = store_path
        self.store: Store | None = None
        self.search_db: sqlite3.Connection | None = None
        self.occupation_count = 0

    def load(self) -> None:
        if not (self.store_path / "READY").exists():
            raise RuntimeError("O*NET graph store is missing. Run scripts/init-onet-store.py.")
        self.store = Store(str(self.store_path / "oxigraph"))
        search_path = self.store_path / "search.sqlite"
        if not search_path.exists():
            self._build_search_index(search_path)
        self.search_db = sqlite3.connect(search_path, check_same_thread=False)
        row = next(iter(self._query(
            "SELECT (COUNT(?occupation) AS ?count) WHERE "
            "{ ?occupation rdf:type onet:Occupation . }"
        )))
        self.occupation_count = int(_text(row["count"]))

    def _query(self, sparql: str):
        if self.store is None:
            raise RuntimeError("O*NET graph store has not been loaded.")
        return self.store.query(f"{PREFIXES}\n{sparql}")

    def search(self, query: str, limit: int = 5, bright_outlook_only: bool = False) -> list[dict]:
        if self.search_db is None:
            raise RuntimeError("O*NET search index has not been loaded.")
        fetch_limit = limit * 6 if bright_outlook_only else limit
        military_results = []
        military_seen = set()
        for candidate in _military_code_candidates(query):
            for occupation in self.military_lookup(candidate):
                if occupation["code"] not in military_seen:
                    military_seen.add(occupation["code"])
                    military_results.append(occupation)
        text_results = []
        terms = _search_terms(query)
        if terms:
            term_query = " OR ".join(f'"{term}"*' for term in terms)
            all_terms_query = " AND ".join(f'"{term}"*' for term in terms)
            sql = """SELECT occupation_uri, code, title, description,
                            bm25(occupation_search, 0, 0, 12, 9, 3, 5, 2, 2) AS rank
                     FROM occupation_search WHERE occupation_search MATCH ?
                     ORDER BY rank LIMIT ?"""
            rows = list(self.search_db.execute(
                sql, (f"{{title alternate_titles}} : ({all_terms_query})", fetch_limit)
            ))
            if len(rows) < fetch_limit:
                seen = {row[1] for row in rows}
                rows.extend(
                    row for row in self.search_db.execute(
                        sql, (f"{{title alternate_titles}} : ({term_query})", fetch_limit * 2)
                    )
                    if row[1] not in seen
                )
                rows = rows[:fetch_limit]
            if len(rows) < fetch_limit:
                seen = {row[1] for row in rows}
                rows.extend(
                    row for row in self.search_db.execute(sql, (term_query, fetch_limit * 2))
                    if row[1] not in seen
                )
                rows = rows[:fetch_limit]
            for row in rows:
                if row[1] in military_seen:
                    continue
                bright = self.search_db.execute(
                    "SELECT categories FROM bright_outlook WHERE code = ?", (row[1],)
                ).fetchone()
                text_results.append({
                    "uri": row[0], "code": row[1], "title": row[2], "description": row[3],
                    "bright_outlook": bright[0].split("; ") if bright else [],
                })
        combined = military_results + text_results
        if bright_outlook_only:
            combined = [item for item in combined if item["bright_outlook"]]
        return combined[:limit]

    def results(self, query: str, limit: int = 5) -> list[dict]:
        terms = _search_terms(query)
        return [self._features(item, terms) for item in self.search(query, limit)]

    def _occupation_row(self, code: str) -> dict | None:
        row = self.search_db.execute(
            "SELECT occupation_uri, code, title, description FROM occupation_search WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        bright = self.search_db.execute(
            "SELECT categories FROM bright_outlook WHERE code = ?", (code,)
        ).fetchone()
        return {
            "uri": row[0], "code": row[1], "title": row[2], "description": row[3],
            "bright_outlook": bright[0].split("; ") if bright else [],
        }

    def occupation_title(self, soc: str) -> str | None:
        """Plain occupation title for a 7-character SOC code (e.g. 15-2051),
        as used by the CIP-to-SOC crosswalk, via its base O*NET-SOC code."""
        if self.search_db is None:
            return None
        row = self.search_db.execute(
            "SELECT title FROM occupation_search WHERE code = ? OR code LIKE ? ORDER BY code LIMIT 1",
            (f"{soc}.00", f"{soc}.%"),
        ).fetchone()
        return row[0] if row else None

    def result_by_code(self, code: str) -> dict | None:
        if self.search_db is None:
            raise RuntimeError("O*NET search index has not been loaded.")
        occupation = self._occupation_row(code)
        return self._features(occupation, []) if occupation is not None else None

    def military_lookup(self, code: str) -> list[dict]:
        if self.search_db is None:
            raise RuntimeError("O*NET search index has not been loaded.")
        normalized = code.strip().upper()
        if not normalized:
            return []
        rows = self._military_rows(normalized)
        # A code's retired ("O") meanings can be a different job entirely --
        # Army 91B is Wheeled Vehicle Mechanic today but was once Medical
        # Specialist -- so they only count when no current meaning has an
        # O*NET occupation.
        occupations = {row[0]: self._occupation_row(row[0]) for row in rows}
        if any(occupations[row[0]] and row[3] == "A" for row in rows):
            rows = [row for row in rows if row[3] == "A"]
        results = []
        seen = set()
        for onet_code, svc, moc_title, status in rows:
            occupation = occupations[onet_code]
            if occupation is None or occupation["code"] in seen:
                continue
            seen.add(occupation["code"])
            occupation["military_match"] = {
                "code": normalized, "title": moc_title,
                "branch": MILITARY_BRANCH_LABELS.get(svc, svc), "active": status == "A",
            }
            results.append(occupation)
        return results

    def _military_rows(self, code: str) -> list[tuple[str, str, str, str]]:
        rows = [
            tuple(row) for row in self.search_db.execute(
                """SELECT DISTINCT onet_code, svc, moc_title, status FROM military_crosswalk
                   WHERE moc = ? ORDER BY status != 'A'""",
                (code,),
            )
        ]
        if rows or "X" not in code[1:]:
            return rows
        # Airmen write an AFSC with X for the skill level ("1D7X1"), while
        # the crosswalk lists each level (1D711 Helper ... 1D771 Craftsman),
        # sometimes with a shred letter (1D731A).
        pattern = code[0] + code[1:].replace("X", "_")
        return [
            tuple(row) for row in self.search_db.execute(
                """SELECT DISTINCT onet_code, svc, moc_title, status FROM military_crosswalk
                   WHERE svc = 'F' AND (moc LIKE ? OR moc LIKE ?) ORDER BY status != 'A'""",
                (pattern, pattern + "_"),
            )
        ]

    def military_code_in(self, text: str) -> str | None:
        """The first military job code in free text that the crosswalk
        knows, e.g. "68W" in "I was a 68W, what schools...". An all-digit
        code counts only as a four-digit Marine Corps MOS or Navy NEC (0311)
        and not a year ("got out in 2019"), since "within 25 miles" or a ZIP
        code would otherwise match some code in the crosswalk."""
        for candidate in _military_code_candidates(text):
            if candidate.isdigit() and (len(candidate) != 4 or candidate[:2] in ("19", "20")):
                continue
            if self._military_rows(candidate):
                return candidate
        return None

    def military_careers(self, code: str) -> list[dict]:
        """A military job code's civilian careers from the official crosswalk
        -- current meanings only when it has any, since a retired meaning can
        be an unrelated job (see military_lookup). Kept even without an O*NET
        occupation, since the CIP-to-SOC crosswalk can still link the career
        to fields of study."""
        if self.search_db is None:
            raise RuntimeError("O*NET search index has not been loaded.")
        rows = self._military_rows(code.strip().upper())
        if any(row[3] == "A" for row in rows):
            rows = [row for row in rows if row[3] == "A"]
        careers = []
        seen = set()
        for onet_code, svc, moc_title, status in rows:
            # An AFSC's skill levels and shreds (1D711 ... 1D771A) repeat the
            # same civilian career.
            if onet_code in seen:
                continue
            seen.add(onet_code)
            occupation = self._occupation_row(onet_code)
            details = [
                (row[0], row[1]) for row in self.search_db.execute(
                    "SELECT code, title FROM occupation_search WHERE code LIKE ? AND code != ? ORDER BY code",
                    (f"{onet_code[:7]}.%", onet_code),
                )
            ] if onet_code.endswith(".00") else []
            careers.append({
                "soc": onet_code, "military_title": moc_title,
                "branch": MILITARY_BRANCH_LABELS.get(svc, svc), "active": status == "A",
                "title": occupation["title"] if occupation else None,
                # An "All Other" catch-all such as Army 25B's "Computer
                # Occupations, All Other" (15-1299.00) has O*NET's specific
                # jobs under it (15-1299.01 Web Administrators, .05
                # Information Security Engineers, ...) and no related
                # occupations of its own; its specific jobs' stand in.
                "detail_codes": [code for code, _ in details],
                "example_jobs": [title for _, title in details[:6]],
            })
        return careers

    def related_results(self, occupation: dict, limit: int = 8) -> list[dict]:
        return [
            result for code in self._related_codes(occupation["uri"], limit)
            if (result := self.result_by_code(code))
        ]

    def related_codes(self, code: str, limit: int = 5) -> list[str]:
        """O*NET's most closely related occupations for one occupation code,
        closest first (the first five are O*NET's "Primary-Short" tier --
        Paramedics -> EMTs, Registered Nurses, LPNs). Empty for a code with
        no O*NET occupation of its own."""
        occupation = self._occupation_row(code)
        return self._related_codes(occupation["uri"], limit) if occupation else []

    def _related_codes(self, uri: str, limit: int) -> list[str]:
        rows = self._query(f"""
            SELECT ?code WHERE {{
              <{uri}> onet:hasRelatedOccupation ?link .
              ?link onet:refersTo ?related ; onet:relatedIndex ?index .
              ?related onet:onetSOCCode ?code .
            }} ORDER BY ?index LIMIT {limit}
        """)
        return [_text(row["code"]) for row in rows]

    def _build_search_index(self, path: Path) -> None:
        documents: dict[str, dict[str, Any]] = {}
        for row in self._query("""
            SELECT ?occupation ?code ?title ?description WHERE {
              ?occupation rdf:type onet:Occupation ; onet:onetSOCCode ?code ;
                onet:title ?title ; onet:description ?description .
            }
        """):
            uri = _text(row["occupation"])
            documents[uri] = {
                "uri": uri, "code": _text(row["code"]), "title": _text(row["title"]),
                "description": _text(row["description"]), "alternate_titles": [],
                "tasks": [], "features": [], "software": [],
            }

        text_queries = {
            "alternate_titles": """
                SELECT ?occupation ?text WHERE {
                  { ?occupation onet:hasJobTitle ?resource . ?resource onet:jobTitle ?text . }
                  UNION
                  { ?occupation onet:hasReportedTitle ?resource .
                    ?resource onet:reportedJobTitle ?text . }
                }
            """,
            "tasks": """
                SELECT ?occupation ?text WHERE {
                  ?occupation onet:hasTask ?resource . ?resource onet:task ?text .
                }
            """,
            "features": """
                SELECT DISTINCT ?occupation ?text WHERE {
                  ?occupation onet:hasRating ?rating . ?rating onet:refersTo ?resource .
                  ?resource onet:elementName|onet:categoryDescription|onet:name ?text .
                }
            """,
            "software": """
                SELECT DISTINCT ?occupation ?text WHERE {
                  ?occupation onet:hasSoftware ?link . ?link onet:refersTo ?resource .
                  ?resource onet:workplaceExample ?text .
                }
            """,
        }
        for field, query in text_queries.items():
            for row in self._query(query):
                document = documents.get(_text(row["occupation"]))
                if document is not None:
                    document[field].append(_text(row["text"]))

        connection = sqlite3.connect(path)
        connection.execute("""
            CREATE VIRTUAL TABLE occupation_search USING fts5(
              occupation_uri UNINDEXED, code UNINDEXED, title, alternate_titles,
              description, tasks, features, software, tokenize='porter unicode61'
            )
        """)
        connection.executemany(
            "INSERT INTO occupation_search VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((
                    document["uri"], document["code"], document["title"],
                    " | ".join(document["alternate_titles"]), document["description"],
                    " | ".join(document["tasks"]), " | ".join(document["features"]),
                    " | ".join(document["software"]),
                )
                for document in documents.values()
            )
        )
        connection.execute(
            "CREATE TABLE bright_outlook (code TEXT PRIMARY KEY, categories TEXT NOT NULL)"
        )
        with BRIGHT_OUTLOOK.open(newline="", encoding="utf-8-sig") as source:
            connection.executemany(
                "INSERT INTO bright_outlook VALUES (?, ?)",
                ((row["Code"], row["Categories"]) for row in csv.DictReader(source)),
            )
        connection.execute("""
            CREATE TABLE military_crosswalk (
              moc TEXT NOT NULL, onet_code TEXT NOT NULL, svc TEXT NOT NULL,
              moc_title TEXT NOT NULL, status TEXT NOT NULL
            )
        """)
        connection.execute("CREATE INDEX military_crosswalk_moc ON military_crosswalk (moc)")
        if MILITARY_CROSSWALK.exists():
            with MILITARY_CROSSWALK.open(newline="", encoding="utf-8-sig") as source:
                entries = [
                    (moc, onet_code, row["SVC"], row["MOC_TITLE"], row["STATUS"])
                    for row in csv.DictReader(source)
                    if (moc := row["MOC"].strip().upper())
                    for onet_code in (row.get(f"ONET{i}", "").strip() for i in "1234")
                    if onet_code
                ]
                connection.executemany(
                    "INSERT INTO military_crosswalk VALUES (?, ?, ?, ?, ?)", entries
                )
        connection.commit()
        connection.close()

    def _features(self, occupation: dict, search_terms: list[str]) -> dict:
        uri = occupation["uri"]
        education = [
            {"level": _text(row["level"]), "share": float(_text(row["share"]))}
            for row in self._query(f"""
                SELECT ?level ?share WHERE {{
                  <{uri}> onet:hasRating ?rating .
                  ?rating rdf:type onet:EducationRating ; onet:refersTo ?category ;
                    onet:dataValue ?share .
                  ?category onet:categoryDescription ?level .
                }} ORDER BY DESC(?share)
            """)
        ]
        all_tasks = [
            _text(row["task"]) for row in self._query(f"""
                SELECT ?task WHERE {{ <{uri}> onet:hasTask ?resource .
                  ?resource onet:task ?task . }}
            """)
        ]
        tasks = sorted(
            all_tasks,
            key=lambda task: sum(
                any(token.startswith(term[:4]) for token in re.findall(r"[a-z0-9]+", task.lower()))
                for term in search_terms
            ),
            reverse=True,
        )[:6]
        software = [
            _text(row["name"]) for row in self._query(f"""
                SELECT DISTINCT ?name WHERE {{ <{uri}> onet:hasSoftware ?link .
                  ?link onet:refersTo ?resource .
                  ?resource onet:workplaceExample ?name . }} LIMIT 8
            """)
        ]
        job_zone_rows = list(self._query(f"""
            SELECT ?name ?education ?experience ?training WHERE {{
              <{uri}> onet:hasRating ?rating .
              ?rating rdf:type onet:JobZoneRating ; onet:refersTo ?zone .
              ?zone onet:name ?name ; onet:education ?education ;
                onet:experience ?experience ; onet:jobTraining ?training .
            }} LIMIT 1
        """))
        job_zone = None
        if job_zone_rows:
            row = job_zone_rows[0]
            job_zone = {key: _text(row[key]) for key in (
                "name", "education", "experience", "training"
            )}
        elements = [
            {
                "name": _text(row["name"]),
                "type": _text(row["type"]).rsplit("/", 1)[-1],
                "score": float(_text(row["score"])),
            }
            for row in self._query(f"""
                SELECT ?name ?type (MAX(?value) AS ?score) WHERE {{
                  <{uri}> onet:hasRating ?rating .
                  ?rating rdf:type ?type ; onet:refersTo ?element ; onet:dataValue ?value .
                  ?element onet:elementName ?name .
                  FILTER(?type IN (onet:EssentialSkillsRating, onet:KnowledgeRating,
                    onet:AbilitiesRating, onet:WorkActivitiesRating, onet:WorkStylesRating))
                }} GROUP BY ?name ?type ORDER BY DESC(?score) LIMIT 12
            """)
        ]
        return {
            **occupation,
            "education": education,
            "tasks": tasks,
            "software": software,
            "job_zone": job_zone,
            "elements": elements,
        }