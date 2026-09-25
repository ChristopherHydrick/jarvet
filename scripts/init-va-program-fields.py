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
BATCH_SIZE = 512

DEGREE_PREFIX = re.compile(
    r"^(AAS|AA-T|AS-T|AAT|AST|AA|AS|BA|BS|BFA|BSN|BBA|MA|MS|MBA|MED|MFA|PHD|EDD|DNP|GC|"
    r"CERT|COA|COC|CA|CP|AOS|AGS|BAS|BPS|MPA|MPH|MSW|DPT|OTD|PHARMD|JD|MD|DDS)\b[\s\-]*",
    re.I,
)
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


def adjusted(record: dict) -> dict:
    """Rule-based corrections applied on top of the embedding match."""
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
    return (text or previous).lower()


def normalized_name(name: str) -> str:
    words = " ".join(re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ")))
    return words.replace("saint ", "st ")


def read_only(path: Path) -> sqlite3.Connection:
    # immutable=1: the source is a stopped snapshot, so SQLite needn't create
    # -wal/-shm files (it can't on a read-only mount) or take any locks.
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)


def compute(source: Path, ipeds_path: Path, out: Path) -> None:
    import numpy as np
    from fastembed import TextEmbedding

    va = read_only(source)
    ipeds = read_only(ipeds_path)

    ipeds_by_name: dict[tuple[str, str], list[tuple[str, str]]] = collections.defaultdict(list)
    for unitid, name, state, zip_code in ipeds.execute(
        "SELECT unitid, name, state, zip FROM institutions"
    ):
        ipeds_by_name[(normalized_name(name), state)].append((unitid, (zip_code or "")[:5]))
    school_cips: dict[str, list[str]] = collections.defaultdict(list)
    for unitid, cip in ipeds.execute("SELECT DISTINCT unitid, cip FROM programs"):
        school_cips[unitid].append(cip)

    def ipeds_unitid(name: str, state: str, zip_code: str | None) -> str | None:
        candidates = ipeds_by_name.get((normalized_name(name), state))
        if not candidates:
            return None
        same_zip = [unitid for unitid, zz in candidates if zz == (zip_code or "")[:5]]
        return same_zip[0] if same_zip else candidates[0][0]

    facility_unitid = {
        code: ipeds_unitid(name, state, zip_code)
        for code, name, state, zip_code in va.execute(
            "SELECT facility_code, institution, state, zip FROM facilities WHERE approved = 1"
        )
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
            if similarities[school_best] >= similarities[best] - SCHOOL_MARGIN:
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
