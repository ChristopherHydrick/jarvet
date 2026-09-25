"""Best-effort, offline discovery of each school's apply/admissions page.

app/agent.py used to crawl each school's own site for its admissions page
live, during a search -- which meant a broad find_va_programs search paid the
cost of crawling several schools' sites on every single query, and a slow or
unresponsive school site could stall the whole reply. This script does the
same crawl once, offline, for every approved school that has a known website
(VA-confirmed via facilities.insturl, or a guess from
scripts/init-va-website-guesses.py), and stores the result in a SEPARATE,
clearly-marked table -- never written into facilities.insturl itself. See
"Found offline by scripts/init-va-admissions-guesses.py" in app/va.py for how
the app surfaces this.

Unlike init-va-website-guesses.py, this makes no external search API calls:
it just fetches each school's own homepage directly and looks for an apply/
admissions link, the same discover_admissions_page() get_va_facility already
uses for a single live lookup in app/agent.py. Re-run periodically (schools
redesign their sites); it resumes from wherever it left off and only retries
facilities whose known website has changed since the last attempt.
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

from app.programs import discover_admissions_page  # noqa: E402

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
        "CREATE TABLE IF NOT EXISTS va_admissions_guess_attempts ("
        "facility_code TEXT PRIMARY KEY, website TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    # The query below LEFT JOINs va_website_guesses (owned by
    # init-va-website-guesses.py) to also cover schools with a Serper-guessed
    # website, not just a VA-confirmed one -- but that script may never have
    # been run (for example, no SERPER_API_KEY available), in which case the
    # table wouldn't exist yet and the join would fail outright rather than
    # just finding no matches. Ensure it exists, empty, so this script can
    # still cover every school with a VA-confirmed website on its own.
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
                discover_admissions_page(website), timeout=REQUEST_TIMEOUT,
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
        "INSERT OR REPLACE INTO va_admissions_guess_attempts VALUES (?, ?, ?)",
        [(code, website, now) for code, website, _ in pending],
    )
    connection.commit()


async def run(connection: sqlite3.Connection, row_limit: int | None) -> None:
    ensure_schema(connection)
    already_attempted = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT facility_code, website FROM va_admissions_guess_attempts"
        )
    }
    all_rows = connection.execute(
        "SELECT f.facility_code, "
        "COALESCE(NULLIF(f.insturl, ''), g.url) AS website "
        "FROM facilities f LEFT JOIN va_website_guesses g ON g.facility_code = f.facility_code "
        "WHERE f.approved = 1 AND f.school_provider = 1 "
        "AND COALESCE(NULLIF(f.insturl, ''), g.url) IS NOT NULL"
    ).fetchall()
    # Skip a facility only if its known website hasn't changed since the last
    # attempt -- a school that gained a real (or newly guessed) website since
    # then is worth retrying even though its facility_code was seen before.
    targets = [
        (code, website) for code, website in all_rows
        if already_attempted.get(code) != website
    ]
    if row_limit is not None:
        targets = targets[:row_limit]
    if not targets:
        print(f"All {len(all_rows):,} schools with a known website already attempted.")
        return
    print(
        f"Crawling {len(targets):,} of {len(all_rows):,} schools with a known website "
        f"for their apply/admissions page ({len(all_rows) - len(targets):,} unchanged since "
        "last attempt)..."
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
