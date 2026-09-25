"""Assign every VA-approved program title an official field of study (CIP code).

VA's program catalog (va_programs, built by init-va-programs-data.py) is free
text -- "AS-T COMPUTER SCIENCE", "CERT-CNC PROGRAMMING" -- with no field code,
so nothing links a program to the careers it leads to or to other programs
that lead to the same careers. The official CIP-to-SOC crosswalk already in
ipeds.sqlite (programs.soc_codes) provides that link once each title has a
CIP code; this script supplies the code.

Each title (degree-level prefixes like "AS-T"/"CERT" stripped) is embedded
with the same model app/va.py uses and compared against all 1,948 CIP titles.
When the VA school also appears in IPEDS (normalized name + state, ZIP as a
tie-break -- about 74% of program rows), the best match among the fields
that school itself reports to IPEDS wins if it scores within SCHOOL_MARGIN of
the nationwide best; this fixed most outright errors in a 200-title hand
review (e.g. "NURSING ASSISTANT" -> Nursing Administration nationwide, but
Nursing Assistant/Aide from the school's own list). Accuracy on that review
was roughly 90%, so callers should treat low scores as unassigned. High
school diplomas/GED have no career field and are stored with a NULL cip.

Two steps, so the slow part never touches the live database:
  compute --source <db copy> --ipeds <ipeds copy> --out <jsonl>
      Reads only (opened immutable/read-only) and writes a JSONL file.
      Run it against a backup copy while the app keeps serving.
  write --input <jsonl>
      Replaces the va_program_fields table in .cache/va-comparison.sqlite.
      Refuses to run while the jarvet container is up; run integrity checks
      afterwards (it prints PRAGMA integrity_check).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / ".cache" / "va-comparison.sqlite"
SCHOOL_MARGIN = 0.08
# For a school matched to IPEDS only by a looser name (a branch campus to its
# main campus, a chain's sibling campus), its field list is a weaker guide:
# at 0.08 it pulled "BA ART" to Architecture and "AA GENERAL BUSINESS" to
# Agribusiness, so it has to be nearly as good as the nationwide best.
LOOSE_SCHOOL_MARGIN = 0.04
BATCH_SIZE = 512

DEGREE_PREFIX = re.compile(
    r"^(AAS|AA-T|AS-T|AAT|AST|AA|AS|BA|BS|BFA|BSN|BBA|MA|MS|MBA|MED|MFA|PHD|EDD|DNP|GC|"
    r"CERT|COA|COC|CA|CP|AOS|AGS|BAS|BPS|MPA|MPH|MSW|DPT|OTD|PHARMD|JD|MD|DDS|"
    r"BSBA|BSED|MSED|BSE|MSE|BSC|MSC|AAB|AFA|DIPL|DIP|TC|TD|GRAD CERT)\b[\s\-]*",
    re.I,
)
# VA's shorthand, spelled out before embedding -- the model reads "mgmt" or
# "hlth" as noise. Only unambiguous ones: ENG (English or engineering?), SEC
# (security or secondary?) and COMP (computer or composition?) stay as is.
ABBREVIATIONS = {
    "admin": "administration", "adm": "administration", "mgmt": "management",
    "mgt": "management", "edu": "education", "educ": "education", "sci": "science",
    "info": "information", "sys": "systems", "spec": "specialist", "adv": "advanced",
    "tech": "technology", "techn": "technician", "tec": "technology", "hlth": "health",
    "maint": "maintenance", "psych": "psychology", "asst": "assistant",
    "ldrshp": "leadership", "dev": "development", "prof": "professional",
    "elem": "elementary", "lic": "licensed", "lvl": "level", "org": "organizational",
    "lang": "language", "stud": "studies", "conc": "concentration", "engr": "engineering",
    "comm": "communication", "bus": "business", "acct": "accounting", "mfg": "manufacturing",
    "const": "construction", "mech": "mechanical", "elec": "electrical",
}
NO_FIELD = re.compile(r"\bhigh\s*school\b|\bG\.?E\.?D\b|\bHiSET\b|\bHSE\b", re.I)
# CIP's homeland-security fields (43.04xx) have titles like "Cybersecurity
# Defense Strategy/Policy" that out-score the IT security field on a plain
# "CYBERSECURITY" title, but the crosswalk links them to police and military
# careers (33-1012, 55-1017) -- so an IT cybersecurity certificate came out
# "related" to criminal justice. VA's cyber/infosec programs are IT programs:
# move them to Computer and Information Systems Security (11.1003) unless the
# title is about crime/forensics/policy, which the 43.04xx fields do fit.
IT_SECURITY_CIP = "11.1003"
IT_SECURITY_TITLE = re.compile(
    r"cyber|information\s+assurance|info\w*\s+security|network\w*\s+security|infosec", re.I,
)
NOT_IT_SECURITY_TITLE = re.compile(r"crim|forensic|policy|law\b|homeland|terror|intel", re.I)
SECURITY_POLICY_CIPS = {"43.0401", "43.0403", "43.0404", "43.0499"}


# Titles whose field is plain from one keyword, where the embedding match
# (or the school's own IPEDS list) often chose a neighbor: "PARALEGAL STUDIES"
# -> Legal Studies, Palo Verde's "AS NURSING - RN" -> Nursing Assistant, CDL
# courses -> Transportation Law or Animal Training. First match wins, so the
# nursing bridges ("LPN TO ASN", "PARAMEDIC TO ADN") land on registered
# nursing before the LPN or EMT rules see them. Each pattern was checked
# against samples of the rows it moves; exclusions keep teaching, management
# and advanced-practice programs (nurse practitioner, CRNA...) where they were.
NOT_CLINICAL = (
    r"ADMIN|MANAGEMENT|MGMT|MGT|EDUCAT|TEACH|LEADERSHIP|LDRSHP|INFORMATICS|INSTRUCTOR|BUSINESS"
)
ADVANCED_NURSING = r"|PRACTITIONER|\bFNP|\bNP\b|DNP|MSN|MASTER|DOCTOR|ANESTH|MIDWI|\bCRNA"
KEYWORD_RULES = [
    (cip, re.compile(pattern), re.compile(exclude))
    for cip, pattern, exclude in [
        ("51.3801",
         r"\b(LPN|LVN|PARAMEDIC)\W+(TO\W+)?(RN|ADN|ASN|AAS|BSN)\b|NURSING\W+(CAREER\W+)?MOB"
         r"|NURSING.*TRANSITION",
         NOT_CLINICAL + ADVANCED_NURSING),
        ("51.3801",
         r"\bRN\b|\bADN\b|\bASN\b|\bBSN\b|REGISTERED NURS|PROFESSIONAL NURSING"
         r"|ASSOCIATE DEGREE NURSING|\bNURSING GENERIC|^(AAS|AS|AAS-T|AS-T)\W+NURSING\b",
         NOT_CLINICAL + ADVANCED_NURSING
         + r"|SPECIALIST|CLINICAL NURSE|VOCATIONAL|PRACTICAL|\bLVN\b|\bLPN\b|ASSISTANT|AIDE"),
        ("51.3901", r"VOCATIONAL NURS|PRACTICAL NURS|\bLVN\b|\bLPN\b", NOT_CLINICAL),
        ("51.3902", r"NURS\w* ASSIST|NURSE AIDE|NURSING AIDE|\bCNA\b", NOT_CLINICAL + r"|RESTORATIVE"),
        ("51.0904", r"\bEMT\b|EMERGENCY MEDICAL TECH|PARAMEDIC",
         NOT_CLINICAL + r"|FIRE|DISPATCH|NURS"),
        ("22.0302", r"PARALEGAL|LEGAL ASSIST", r"NURS"),
        ("51.0801", r"MEDICAL ASSIST", NOT_CLINICAL + r"|RADIO|X.?RAY|CODING|BILLING|OFFICE"),
        ("51.0805", r"PHARMACY TECH", NOT_CLINICAL),
        ("51.0909", r"SURGICAL TECH", NOT_CLINICAL),
        ("51.1009", r"PHLEBOTOM", NOT_CLINICAL + r"|EKG|ECG|MEDICAL ASSIST"),
        ("51.0601", r"DENTAL ASSIST", NOT_CLINICAL + r"|HYGIEN"),
        ("51.0602", r"DENTAL HYGIEN", NOT_CLINICAL),
        ("51.0908", r"RESPIRATORY (THERAP|CARE)", NOT_CLINICAL),
        ("48.0508", r"WELD", r"ENGINEER|INSPECT"),
        ("47.0201", r"\bHVAC|HEATING.{0,20}AIR COND|REFRIGERATION", r"ENGINEER"),
        ("47.0605", r"DIESEL", r"ENGINEER"),
        ("46.0503", r"PLUMB", r"ENGINEER"),
        ("49.0205", r"\bCDL\b|TRUCK DRIV|TRACTOR.?TRAILER|COMMERCIAL DRIV", r"INSTRUCTOR"),
        ("12.0402", r"BARBER", r"INSTRUCTOR|TEACH|EDUCATOR"),
        ("12.0401", r"COSMETOLOG", r"INSTRUCTOR|TEACH|EDUCATOR|MGMT|MANAGEMENT|NAIL|ESTHET"),
        ("51.3501", r"MASSAGE", r"INSTRUCTOR"),
    ]
]


def adjusted(record: dict) -> dict:
    """Rule-based corrections applied on top of the embedding match."""
    if record.get("cip") is None:
        return record
    title = record["description"].upper()
    for cip, pattern, exclude in KEYWORD_RULES:
        if pattern.search(title) and not exclude.search(title):
            return {**record, "cip": cip, "method": "rule", "score": 1.0}
    if (
        record.get("cip") in SECURITY_POLICY_CIPS
        and IT_SECURITY_TITLE.search(record["description"])
        and not NOT_IT_SECURITY_TITLE.search(record["description"])
    ):
        return {**record, "cip": IT_SECURITY_CIP, "method": "rule", "score": 1.0}
    return record


def clean_title(title: str) -> str:
    text = re.sub(r"\s+", " ", title).strip()
    previous = None
    while previous != text:
        previous, text = text, DEGREE_PREFIX.sub("", text)
    return re.sub(
        r"[a-z]+", lambda word: ABBREVIATIONS.get(word.group(), word.group()),
        (text or previous).lower(),
    ).replace("comp science", "computer science")


def normalized_name(name: str) -> str:
    words = " ".join(re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ")))
    return words.replace("saint ", "st ")


def name_variants(name: str, city: str = "") -> set[str]:
    """Other ways the same school is written: without "the", without
    "main campus", the part before a dash, "X-Y" as "X at Y", and without a
    trailing city. Generic leftovers ("campus", short words) are dropped."""
    base = normalized_name(name)
    variants = {base, re.sub(r"^the ", "", base), base.replace(" at ", " ")}
    parts = re.split(r"\s*-\s*", name.strip())
    if len(parts) > 1:
        head = normalized_name(parts[0])
        variants |= {head, re.sub(r"^the ", "", head), normalized_name(" at ".join(parts))}
    variants.add(re.sub(r"\s+", " ", re.sub(r"\b(main campus|main|campus)\b", "", base)).strip())
    city_name = normalized_name(city)
    if city_name and base.endswith(" " + city_name):
        variants.add(base[: -len(city_name) - 1])
    return {variant for variant in variants if len(variant) > 6 and variant not in {"the", "campus"}}


def read_only(path: Path) -> sqlite3.Connection:
    # immutable=1: the source is a stopped snapshot, so SQLite needn't create
    # -wal/-shm files (it can't on a read-only mount) or take any locks.
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)


def compute(source: Path, ipeds_path: Path, out: Path) -> None:
    import numpy as np
    from fastembed import TextEmbedding

    va = read_only(source)
    ipeds = read_only(ipeds_path)

    ipeds_by_name: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    ipeds_by_variant: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    ipeds_names_in_state: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    ipeds_place: dict[str, tuple[str, str]] = {}
    for unitid, name, state, zip_code, city in ipeds.execute(
        "SELECT unitid, name, state, zip, city FROM institutions"
    ):
        ipeds_by_name[(normalized_name(name), state)].append(unitid)
        for variant in name_variants(name, city or ""):
            ipeds_by_variant[(variant, state)].add(unitid)
        ipeds_names_in_state[state].append((normalized_name(name), unitid))
        ipeds_place[unitid] = ((zip_code or "")[:5], (city or "").upper())
    school_cips: dict[str, list[str]] = collections.defaultdict(list)
    for unitid, cip in ipeds.execute("SELECT DISTINCT unitid, cip FROM programs"):
        school_cips[unitid].append(cip)

    def one_school(candidates, zip_code: str | None, city: str | None) -> str | None:
        """The candidate at the same ZIP, else the same city, else the only one."""
        candidates = sorted(set(candidates))
        if len(candidates) == 1:
            return candidates[0]
        for position, value in ((0, (zip_code or "")[:5]), (1, (city or "").upper())):
            same = [unitid for unitid in candidates if ipeds_place[unitid][position] == value]
            if len(same) == 1:
                return same[0]
            if same:
                candidates = same
        return None

    def ipeds_unitid(
        name: str, state: str, zip_code: str | None, city: str | None,
    ) -> tuple[str | None, bool]:
        """Exact normalized name first; then name variants ("X-MAIN CAMPUS",
        "THE X", "X-CITY" -> "X at City"); then one name starting with the
        other ("ARIZONA STATE UNIVERSITY" -> "Arizona State University Campus
        Immersion"). A branch matched to its main campus or a sibling campus
        is fine here: the school's field list only steers the match toward
        fields the institution teaches. Raised IPEDS-matched program rows
        from about 74% to about 88%. Returns (unitid, matched exactly)."""
        exact = ipeds_by_name.get((normalized_name(name), state))
        if exact:
            return one_school(exact, zip_code, city) or exact[0], True
        variants = name_variants(name, city or "")
        by_variant = set().union(*(ipeds_by_variant.get((v, state), set()) for v in variants))
        if by_variant:
            return one_school(by_variant, zip_code, city), False
        by_prefix = {
            unitid for other, unitid in ipeds_names_in_state[state] for v in variants
            if other.startswith(v + " ") or v.startswith(other + " ")
        }
        return (one_school(by_prefix, zip_code, city) if by_prefix else None), False

    facility_match = {
        code: ipeds_unitid(name, state, zip_code, city)
        for code, name, state, zip_code, city in va.execute(
            "SELECT facility_code, institution, state, zip, city FROM facilities WHERE approved = 1"
        )
    }
    facility_unitid = {code: unitid for code, (unitid, _) in facility_match.items()}
    school_margin = {
        code: SCHOOL_MARGIN if exact else LOOSE_SCHOOL_MARGIN
        for code, (_, exact) in facility_match.items()
    }
    rows = va.execute(
        "SELECT DISTINCT p.facility_code, p.description FROM va_programs p "
        "JOIN facilities f USING (facility_code) WHERE f.approved = 1"
    ).fetchall()
    print(f"{len(rows):,} program rows; "
          f"{sum(1 for code, _ in rows if facility_unitid.get(code)):,} at IPEDS-matched schools",
          flush=True)

    cips = ipeds.execute("SELECT cip, title FROM cip_titles ORDER BY cip").fetchall()
    cip_codes = [cip for cip, _ in cips]
    cip_index = {cip: position for position, cip in enumerate(cip_codes)}
    model = TextEmbedding("BAAI/bge-small-en-v1.5")

    def embed(texts: list[str]) -> "np.ndarray":
        matrix = np.array(list(model.embed(texts, batch_size=BATCH_SIZE)), dtype=np.float32)
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    cip_matrix = embed([re.sub(r",\s*(General|Other)$", "", title) for _, title in cips])

    def _pick(facility_code: str, description: str, similarities: "np.ndarray") -> dict:
        if NO_FIELD.search(description):
            return {"cip": None, "score": None, "method": "no-field"}
        best = int(np.argmax(similarities))
        pick, method = best, "global"
        unitid = facility_unitid.get(facility_code)
        allowed = [cip_index[c] for c in school_cips.get(unitid, []) if c in cip_index]
        if allowed:
            school_best = max(allowed, key=lambda position: similarities[position])
            if similarities[school_best] >= similarities[best] - school_margin[facility_code]:
                pick, method = school_best, "school"
        return {
            "cip": cip_codes[pick],
            "score": round(float(similarities[pick]), 4),
            "method": method,
        }

    rows_by_title: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for facility_code, description in rows:
        rows_by_title[clean_title(description)].append((facility_code, description))
    cleaned = sorted(rows_by_title)
    print(f"embedding {len(cleaned):,} distinct cleaned titles", flush=True)
    started = time.time()

    counts = collections.Counter()
    with out.open("w", encoding="utf-8") as handle:
        # Chunked so only one chunk's title-by-field similarity matrix is in
        # memory at a time (all titles at once would be ~200k x 1,948 floats).
        for start in range(0, len(cleaned), 10_000):
            chunk = cleaned[start:start + 10_000]
            for text, similarities in zip(chunk, embed(chunk) @ cip_matrix.T):
                for facility_code, description in rows_by_title[text]:
                    record = _pick(facility_code, description, similarities)
                    counts[record["method"]] += 1
                    handle.write(json.dumps(
                        {"facility_code": facility_code, "description": description, **record}
                    ) + "\n")
            print(f"  {min(start + 10_000, len(cleaned)):,}/{len(cleaned):,} "
                  f"({time.time() - started:.0f}s)", flush=True)
    print("done:", dict(counts), flush=True)


def write(input_path: Path) -> None:
    if subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=^jarvet$"], capture_output=True, text=True,
    ).stdout.strip():
        sys.exit("jarvet container is running -- stop it before writing to the database")
    records = [
        adjusted(json.loads(line))
        for line in input_path.read_text(encoding="utf-8").splitlines() if line
    ]
    connection = sqlite3.connect(DATABASE, timeout=30)
    try:
        with connection:
            connection.execute("DROP TABLE IF EXISTS va_program_fields")
            connection.execute(
                "CREATE TABLE va_program_fields ("
                "facility_code TEXT NOT NULL, description TEXT NOT NULL, cip TEXT, "
                "score REAL, method TEXT NOT NULL, program_type TEXT, "
                "PRIMARY KEY (facility_code, description))"
            )
            connection.execute("CREATE INDEX va_program_fields_cip ON va_program_fields(cip)")
            # Stored here so a related-programs lookup by cip needn't join
            # back to va_programs (no index on description) on every search.
            program_types = {
                (facility_code, description): program_type
                for facility_code, description, program_type in connection.execute(
                    "SELECT facility_code, description, program_type FROM va_programs"
                )
            }
            connection.executemany(
                "INSERT OR REPLACE INTO va_program_fields VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (r["facility_code"], r["description"], r["cip"], r["score"], r["method"],
                     program_types.get((r["facility_code"], r["description"])))
                    for r in records
                ],
            )
        stored = connection.execute("SELECT COUNT(*) FROM va_program_fields").fetchone()[0]
        print(f"stored {stored:,} rows")
        print("integrity:", connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    compute_parser = commands.add_parser("compute")
    compute_parser.add_argument("--source", type=Path, required=True)
    compute_parser.add_argument("--ipeds", type=Path, required=True)
    compute_parser.add_argument("--out", type=Path, required=True)
    write_parser = commands.add_parser("write")
    write_parser.add_argument("--input", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "compute":
        compute(arguments.source, arguments.ipeds, arguments.out)
    else:
        write(arguments.input)


if __name__ == "__main__":
    main()
