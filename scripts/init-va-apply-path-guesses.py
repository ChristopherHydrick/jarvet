"""Best-effort, offline fallback for schools scripts/init-va-admissions-guesses.py
already crawled but found no admissions link on the homepage itself.

Many school sites answer a plain "/apply" path directly (for example
moorparkcollege.edu/apply) even without linking to it anywhere on their
homepage, so this tries that one specific guess -- see
app.programs.guess_apply_path() for how a candidate is verified as a real,
distinct application page rather than a soft-404/catch-all template or a
silent redirect back to the homepage.

Writes into the SAME va_admissions_guesses table init-va-admissions-guesses.py
uses, since the app already surfaces any row there identically as an Apply
resource (see "Found offline by scripts/init-va-admissions-guesses.py" in
app/va.py). Tracks its own attempts in a separate table so it never re-hits a
site it already tried, and only ever targets facilities that still have no
apply URL on file -- a facility that discover_admissions_page succeeded for
is out of scope here.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.programs import guess_apply_path  # noqa: E402

DATABASE = ROOT / ".cache" / "va-comparison.sqlite"
CONCURRENCY = 8
BATCH_COMMIT = 50
REQUEST_TIMEOUT = 20


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_admissions_guesses ("
        "facility_code TEXT PRIMARY KEY, url TEXT NOT NULL, label TEXT NOT NULL, "
        "fetched_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_apply_path_guess_attempts ("
        "facility_code TEXT PRIMARY KEY, website TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    # See the matching comment in init-va-admissions-guesses.py: this script's
    # query also LEFT JOINs va_website_guesses, which may not exist if
    # init-va-website-guesses.py was never run (no SERPER_API_KEY available).
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_website_guesses ("
        "facility_code TEXT PRIMARY KEY, url TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    connection.commit()


async def lookup_facility(
    semaphore: asyncio.Semaphore, facility_code: str, website: str,
) -> tuple[str, dict[str, str] | None]:
    async with semaphore:
        try:
            discovery = await asyncio.wait_for(
                guess_apply_path(website), timeout=REQUEST_TIMEOUT,
            )
        except (asyncio.TimeoutError, httpx.HTTPError):
            discovery = None
        return facility_code, discovery


def flush(
    connection: sqlite3.Connection,
    pending: list[tuple[str, str, dict[str, str] | None]],
) -> None:
    now = int(time.time())
    found = [
        (code, discovery["url"], discovery["label"][:120], now)
        for code, website, discovery in pending if discovery
    ]
    if found:
        connection.executemany(
            "INSERT OR REPLACE INTO va_admissions_guesses VALUES (?, ?, ?, ?)", found,
        )
    connection.executemany(
        "INSERT OR REPLACE INTO va_apply_path_guess_attempts VALUES (?, ?, ?)",
        [(code, website, now) for code, website, _ in pending],
    )
    connection.commit()


async def run(connection: sqlite3.Connection, row_limit: int | None) -> None:
    ensure_schema(connection)
    already_attempted = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT facility_code, website FROM va_apply_path_guess_attempts"
        )
    }
    all_rows = connection.execute(
        "SELECT f.facility_code, "
        "COALESCE(NULLIF(f.insturl, ''), g.url) AS website "
        "FROM facilities f "
        "LEFT JOIN va_website_guesses g ON g.facility_code = f.facility_code "
        "LEFT JOIN va_admissions_guesses a ON a.facility_code = f.facility_code "
        "WHERE f.approved = 1 AND f.school_provider = 1 "
        "AND COALESCE(NULLIF(f.insturl, ''), g.url) IS NOT NULL "
        "AND a.facility_code IS NULL"
    ).fetchall()
    # Skip a facility only if its known website hasn't changed since the last
    # attempt here -- same resume behavior as init-va-admissions-guesses.py.
    targets = [
        (code, website) for code, website in all_rows
        if already_attempted.get(code) != website
    ]
    if row_limit is not None:
        targets = targets[:row_limit]
    if not targets:
        print(f"All {len(all_rows):,} schools without an apply URL already attempted.")
        return
    print(
        f"Guessing a /apply path for {len(targets):,} of {len(all_rows):,} schools with a "
        f"known website but no apply URL yet ({len(all_rows) - len(targets):,} unchanged "
        "since last attempt)..."
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    done = 0
    found_count = 0
    pending: list[tuple[str, str, dict[str, str] | None]] = []

    tasks = [
        asyncio.create_task(lookup_facility(semaphore, code, website))
        for code, website in targets
    ]
    website_by_code = dict(targets)
    for task in asyncio.as_completed(tasks):
        facility_code, discovery = await task
        pending.append((facility_code, website_by_code[facility_code], discovery))
        done += 1
        if discovery:
            found_count += 1
        if len(pending) >= BATCH_COMMIT:
            flush(connection, pending)
            pending.clear()
            print(f"  {done:,}/{len(targets):,} crawled, {found_count:,} found", flush=True)
    if pending:
        flush(connection, pending)
        print(f"  {done:,}/{len(targets):,} crawled, {found_count:,} found", flush=True)


def main() -> None:
    if not DATABASE.exists():
        raise SystemExit("VA Comparison Tool index is missing. Run scripts/init-va-data.py first.")
    row_limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    connection = sqlite3.connect(DATABASE)
    try:
        asyncio.run(run(connection, row_limit))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
