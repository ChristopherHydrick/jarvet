from __future__ import annotations

import os
import re
import socket
import sqlite3
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "data" / "va-comparison"
# The JARVET_VA_* overrides let scripts/monthly-refresh.py rebuild a working
# copy of the database from a freshly downloaded workbook while the live file
# (and the app using it) stay untouched.
WORKBOOK = Path(os.environ.get("JARVET_VA_WORKBOOK") or SOURCE_DIR / "ComparisonToolData.xlsx")
ZCTA_ARCHIVE = SOURCE_DIR / "2025_Gaz_zcta_national.zip"
DATABASE = Path(os.environ.get("JARVET_VA_DB") or ROOT / ".cache" / "va-comparison.sqlite")
MARKER = Path(os.environ.get("JARVET_VA_MARKER") or ROOT / ".cache" / "va-comparison.ready")
INDEX_VERSION = 3
XML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def refuse_if_server_running() -> None:
    """app/va.py's VaIndex keeps one long-lived connection to DATABASE open
    for the whole lifetime of the running app. Rebuilding this file's schema
    while that connection is live once corrupted the file outright (not just
    left it with stale data), because the devcontainer runs uvicorn directly
    as its container command rather than through start-web.sh, so there's no
    pidfile to check -- a live TCP probe of the app's own port is the one
    signal that works regardless of how it was started. Set
    JARVET_ALLOW_LIVE_REBUILD=1 to override (for example if you've confirmed
    whatever's listening on that port isn't actually Jarvet)."""
    if os.environ.get("JARVET_ALLOW_LIVE_REBUILD") == "1":
        return
    port = int(os.environ.get("JARVET_PORT", "8000"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return  # Nothing listening -- safe to proceed.
    raise SystemExit(
        f"Something is listening on port {port} -- Jarvet's server appears to "
        "be running. Stop it first (stop the container, or kill the uvicorn "
        "process) before rebuilding this shared database: rebuilding it live "
        "once corrupted the file. Set JARVET_ALLOW_LIVE_REBUILD=1 to skip "
        "this check."
    )
FIELDS = (
    "facility code", "institution", "city", "state", "zip", "country", "type",
    "approved", "flight", "bah", "insturl", "vet tuition policy url", "pred degree awarded",
    "gibill", "undergrad enrollment", "student veteran", "credit for mil training",
    "p911 tuition fees", "p911 recipients", "p911 yellow ribbon", "p911 yr recipients",
    "accredited", "accreditation type", "accreditation status", "caution flag",
    "caution flag reason", "school closing", "latitude", "longitude",
    "employer provider", "school provider", "ownership name",
)
REAL_FIELDS = {"bah", "p911 tuition fees", "p911 yellow ribbon", "latitude", "longitude"}
INTEGER_FIELDS = {
    "approved", "flight", "gibill", "undergrad enrollment", "student veteran",
    "credit for mil training", "p911 recipients", "p911 yr recipients", "accredited",
    "caution flag", "school closing", "employer provider", "school provider",
}


def column_index(reference: str) -> int:
    index = 0
    for character in re.match(r"[A-Z]+", reference).group():
        index = index * 26 + ord(character) - 64
    return index - 1


def shared_strings(archive: ZipFile) -> list[str]:
    values: list[str] = []
    for _, element in ET.iterparse(archive.open("xl/sharedStrings.xml"), events=("end",)):
        if element.tag == XML_NS + "si":
            values.append("".join(node.text or "" for node in element.iter(XML_NS + "t")))
            element.clear()
    return values


def cells(element: ET.Element, strings: list[str]) -> dict[int, str]:
    values: dict[int, str] = {}
    for cell in element.findall(XML_NS + "c"):
        value = cell.find(XML_NS + "v")
        text = "" if value is None else value.text or ""
        if cell.attrib.get("t") == "s" and text:
            text = strings[int(text)]
        values[column_index(cell.attrib["r"])] = text.strip()
    return values


def as_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def marker_text() -> str:
    return (
        f"{INDEX_VERSION}:{WORKBOOK.stat().st_size}:{WORKBOOK.stat().st_mtime_ns}:"
        f"{ZCTA_ARCHIVE.stat().st_size}:{ZCTA_ARCHIVE.stat().st_mtime_ns}"
    )


def build_database() -> None:
    if not WORKBOOK.exists() or not ZCTA_ARCHIVE.exists():
        raise SystemExit("VA Comparison Tool or Census ZCTA data is missing. Run init-onet-data.sh.")
    marker = marker_text()
    if DATABASE.exists() and MARKER.exists() and MARKER.read_text() == marker:
        embed_provider_names()
        print("VA Comparison Tool index is ready.")
        return

    refuse_if_server_running()
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    # Rebuild only the tables this script owns (facilities, zcta), never the
    # whole file. va-comparison.sqlite is shared storage for several other
    # scripts' tables (va_programs, va_website_guesses, va_admissions_guesses,
    # provider_details) that each take real time/API calls to rebuild --
    # deleting the file wiped all of them out from under this script's own,
    # unrelated rebuild once already. Dropping only these two tables via a
    # normal SQL transaction also avoids the file-level delete-and-recreate
    # race that corrupted this same file while the live app server held it
    # open in WAL mode: a DROP/CREATE TABLE is just an ordinary write SQLite
    # already knows how to coordinate with concurrent readers.
    connection.execute("DROP TABLE IF EXISTS facilities")
    connection.execute("DROP TABLE IF EXISTS zcta")
    connection.execute("""
        CREATE TABLE facilities (
          facility_code TEXT PRIMARY KEY, institution TEXT NOT NULL, city TEXT, state TEXT,
          zip TEXT, country TEXT, type TEXT, approved INTEGER, flight INTEGER, bah REAL, insturl TEXT,
          vet_tuition_policy_url TEXT, pred_degree_awarded TEXT, gibill INTEGER,
          undergrad_enrollment INTEGER, student_veteran INTEGER, credit_for_mil_training INTEGER,
          p911_tuition_fees REAL, p911_recipients INTEGER, p911_yellow_ribbon REAL,
          p911_yr_recipients INTEGER, accredited INTEGER, accreditation_type TEXT,
          accreditation_status TEXT, caution_flag INTEGER, caution_flag_reason TEXT,
          school_closing INTEGER, latitude REAL, longitude REAL, employer_provider INTEGER,
          school_provider INTEGER, ownership_name TEXT
        )
    """)
    connection.execute("CREATE INDEX facilities_location ON facilities(latitude, longitude)")
    connection.execute("CREATE INDEX facilities_name ON facilities(institution COLLATE NOCASE)")

    with ZipFile(WORKBOOK) as archive:
        strings = shared_strings(archive)
        header: dict[str, int] = {}
        records = []
        for _, row in ET.iterparse(
            archive.open("xl/worksheets/sheet3.xml"), events=("end",)
        ):
            if row.tag != XML_NS + "row":
                continue
            values = cells(row, strings)
            if not header:
                header = {value.lower(): index for index, value in values.items()}
                missing = set(FIELDS) - set(header)
                if missing:
                    raise RuntimeError(f"VA workbook fields changed: missing {sorted(missing)}")
                row.clear()
                continue
            record = []
            for field in FIELDS:
                value = values.get(header[field], "")
                if field in REAL_FIELDS:
                    value = as_float(value)
                elif field in INTEGER_FIELDS:
                    number = as_float(value)
                    value = round(number) if number is not None else None
                record.append(value if value != "" else None)
            if record[0] and record[1]:
                records.append(record)
            if len(records) >= 1000:
                connection.executemany(
                    "INSERT OR REPLACE INTO facilities VALUES (" + ",".join("?" * len(FIELDS)) + ")",
                    records,
                )
                records.clear()
            row.clear()
        if records:
            connection.executemany(
                "INSERT OR REPLACE INTO facilities VALUES (" + ",".join("?" * len(FIELDS)) + ")",
                records,
            )

    connection.execute("CREATE TABLE zcta (zip TEXT PRIMARY KEY, latitude REAL, longitude REAL)")
    with ZipFile(ZCTA_ARCHIVE) as archive:
        filename = next(name for name in archive.namelist() if name.endswith(".txt"))
        lines = (line.decode("utf-8").strip() for line in archive.open(filename))
        headers = next(lines).split("|")
        zip_column = headers.index("GEOID")
        latitude_column = headers.index("INTPTLAT")
        longitude_column = headers.index("INTPTLONG")
        connection.executemany(
            "INSERT INTO zcta VALUES (?, ?, ?)",
            (
                (fields[zip_column], as_float(fields[latitude_column]), as_float(fields[longitude_column]))
                for line in lines if line and (fields := line.split("|"))
            ),
        )
    connection.commit()
    count = connection.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
    zip_count = connection.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
    connection.close()
    embed_provider_names()
    MARKER.write_text(marker)
    print(f"Indexed {count:,} VA facilities and {zip_count:,} Census ZIP-area centroids.")


def embed_provider_names() -> None:
    """Precompute semantic embeddings for all approved provider names.

    OJT sponsors often have generic names (trust funds, JATCs, joint
    apprenticeship councils), and trade schools like diving academies are
    school providers rather than employers. Embeddings let the agent search by
    trade meaning across both provider types. Vectors are stored as float32
    blobs keyed by facility code.
    """
    from fastembed import TextEmbedding
    import numpy as np

    connection = sqlite3.connect(DATABASE)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS provider_embeddings ("
        "facility_code TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
    )
    # Only providers without a vector yet, so a refreshed workbook's newly
    # approved schools get one without re-embedding the other ~20,000.
    rows = connection.execute(
        "SELECT facility_code, institution FROM facilities WHERE approved = 1 "
        "AND facility_code NOT IN (SELECT facility_code FROM provider_embeddings)"
    ).fetchall()
    if not rows:
        print("Provider name embeddings already present.")
        connection.close()
        return

    print(f"Embedding {len(rows):,} provider names (first run downloads a ~67 MB model)...")
    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    batch: list[tuple[str, list[float]]] = []
    total = 0
    for code, name in rows:
        vector = next(model.embed([name]))
        batch.append((code, np.asarray(vector, dtype=np.float32).tobytes()))
        if len(batch) >= 512:
            connection.executemany(
                "INSERT OR REPLACE INTO provider_embeddings VALUES (?, ?)", batch,
            )
            connection.commit()
            total += len(batch)
            print(f"  {total:,}/{len(rows):,}", flush=True)
            batch.clear()
    if batch:
        connection.executemany(
            "INSERT OR REPLACE INTO provider_embeddings VALUES (?, ?)", batch,
        )
        total += len(batch)
    connection.commit()
    connection.close()
    print(f"Embedded {total:,} provider names.")


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--write-marker"]:
        # After scripts/monthly-refresh.py copies a new workbook into place:
        # record it as already indexed so the next container start doesn't
        # rebuild the facilities table from it a second time.
        MARKER.write_text(marker_text())
    else:
        build_database()
