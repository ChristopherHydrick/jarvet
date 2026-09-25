"""Bulk-crawl every VA-approved school's IHL/NCD/FLGT program catalog.

app/va.py's provider_details() only fetches and caches one facility's
programs on demand, when the agent looks that facility up. This script
instead walks every approved school_provider facility already indexed by
init-va-data.py and pulls its full program list from the same public VA API,
so the app can search program names (e.g. "commercial diver") across every
VA-approved school nationwide or within one state, not just schools already
looked up. It is resumable: facility codes already recorded in
va_programs_crawl_state are skipped on a re-run, so an interrupted crawl can
just be restarted. It is not wired into postCreateCommand.sh because a full
run makes roughly three API calls per approved school (~53,000 requests) and
can take a long time; run it manually and re-run it periodically to refresh.

Standalone flight academies (VA facility type "FLIGHT") file their approved
courses under a third program type, "FLGT", that IHL/NCD does not cover --
without it, schools like flight academies are entirely absent from program
search even though they hold real VA-approved pilot training programs.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / ".cache" / "va-comparison.sqlite"
PROGRAM_TYPES = ("IHL", "NCD", "FLGT")
CONCURRENCY = 20
BATCH_COMMIT = 200
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_programs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, facility_code TEXT NOT NULL, "
        "program_type TEXT NOT NULL, description TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS va_programs_facility ON va_programs(facility_code)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_programs_crawl_state ("
        "facility_code TEXT PRIMARY KEY, program_count INTEGER NOT NULL, "
        "fetched_at INTEGER NOT NULL)"
    )
    connection.commit()


async def fetch_programs(
    client: httpx.AsyncClient, facility_code: str, program_type: str,
) -> list[str]:
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.get(
                "https://api.va.gov/v0/gi/institution_programs/search",
                params={
                    "type": program_type,
                    "facility_code": facility_code,
                    "disable_pagination": "true",
                },
            )
            if response.status_code == 429:
                await asyncio.sleep(2 ** attempt * 2)
                continue
            response.raise_for_status()
            data = response.json().get("data", [])
            return [
                description
                for item in data
                if (description := str(item.get("attributes", {}).get("description") or "").strip())
            ]
        except (httpx.HTTPError, ValueError):
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    return []


async def crawl_facility(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, facility_code: str,
    program_types: tuple[str, ...],
) -> tuple[str, list[tuple[str, str]], bool]:
    async with semaphore:
        rows: list[tuple[str, str]] = []
        ok = True
        for program_type in program_types:
            try:
                descriptions = await fetch_programs(client, facility_code, program_type)
            except (httpx.HTTPError, ValueError):
                ok = False
                continue
            rows.extend((program_type, description) for description in descriptions)
        return facility_code, rows, ok


def flush(
    connection: sqlite3.Connection,
    pending: list[tuple[str, list[tuple[str, str]], bool]],
    program_types: tuple[str, ...],
    track_state: bool,
) -> None:
    now = int(time.time())
    for facility_code, rows, ok in pending:
        if not ok:
            continue
        for program_type in program_types:
            connection.execute(
                "DELETE FROM va_programs WHERE facility_code = ? AND program_type = ?",
                (facility_code, program_type),
            )
        if rows:
            connection.executemany(
                "INSERT INTO va_programs (facility_code, program_type, description) "
                "VALUES (?, ?, ?)",
                [(facility_code, program_type, description) for program_type, description in rows],
            )
        if track_state:
            connection.execute(
                "INSERT OR REPLACE INTO va_programs_crawl_state VALUES (?, ?, ?)",
                (facility_code, len(rows), now),
            )
    connection.commit()


async def crawl(
    connection: sqlite3.Connection, test_limit: int | None,
    facility_codes: list[str], program_types: tuple[str, ...], track_state: bool,
) -> None:
    total = len(facility_codes)
    if test_limit is not None:
        facility_codes = facility_codes[:test_limit]
    if not facility_codes:
        print(f"All {total:,} facilities already crawled.")
        return
    print(f"Crawling {len(facility_codes):,} of {total:,} facilities for {', '.join(program_types)}...")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    done = 0
    failed = 0
    program_total = 0
    pending: list[tuple[str, list[tuple[str, str]], bool]] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        tasks = [
            asyncio.create_task(crawl_facility(client, semaphore, code, program_types))
            for code in facility_codes
        ]
        for task in asyncio.as_completed(tasks):
            facility_code, rows, ok = await task
            pending.append((facility_code, rows, ok))
            done += 1
            if ok:
                program_total += len(rows)
            else:
                failed += 1
            if len(pending) >= BATCH_COMMIT:
                flush(connection, pending, program_types, track_state)
                pending.clear()
                print(
                    f"  {done:,}/{len(facility_codes):,} attempted, "
                    f"{program_total:,} programs found, {failed:,} failed (will retry on re-run)",
                    flush=True,
                )
        if pending:
            flush(connection, pending, program_types, track_state)
            print(
                f"  {done:,}/{len(facility_codes):,} attempted, "
                f"{program_total:,} programs found, {failed:,} failed (will retry on re-run)",
                flush=True,
            )


def build_search_index(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS va_program_search")
    connection.execute("""
        CREATE VIRTUAL TABLE va_program_search USING fts5(
          facility_code UNINDEXED, program_type UNINDEXED, description,
          tokenize='porter unicode61'
        )
    """)
    connection.execute(
        "INSERT INTO va_program_search (facility_code, program_type, description) "
        "SELECT facility_code, program_type, description FROM va_programs"
    )
    connection.commit()
    count = connection.execute("SELECT COUNT(*) FROM va_program_search").fetchone()[0]
    print(f"Built search index over {count:,} VA-approved IHL/NCD/FLGT program rows.")


def main() -> None:
    if not DATABASE.exists():
        raise SystemExit("VA Comparison Tool index is missing. Run scripts/init-va-data.py first.")
    args = sys.argv[1:]
    flight_only = "--flight-only" in args
    args = [a for a in args if a != "--flight-only"]
    test_limit = int(args[0]) if args else None

    connection = sqlite3.connect(DATABASE)
    ensure_schema(connection)
    if flight_only:
        # Backfill just the FLGT type for known flight schools (facilities.flight
        # = 1), skipping crawl_state entirely -- that table only tracks whether a
        # facility's IHL/NCD pass is done and would wrongly skip every flight
        # school here, since they were already marked done under the old
        # IHL/NCD-only crawl. Cheap (~500 requests nationwide) and safe to
        # re-run any time to refresh, unlike the full crawl below.
        facility_codes = [
            row[0] for row in connection.execute(
                "SELECT facility_code FROM facilities WHERE approved = 1 "
                "AND school_provider = 1 AND flight = 1"
            )
        ]
        asyncio.run(crawl(connection, test_limit, facility_codes, ("FLGT",), False))
    else:
        already_done = {
            row[0] for row in connection.execute("SELECT facility_code FROM va_programs_crawl_state")
        }
        facility_codes = [
            row[0] for row in connection.execute(
                "SELECT facility_code FROM facilities WHERE approved = 1 AND school_provider = 1"
            )
            if row[0] not in already_done
        ]
        asyncio.run(crawl(connection, test_limit, facility_codes, PROGRAM_TYPES, True))
    build_search_index(connection)
    connection.close()


if __name__ == "__main__":
    main()
