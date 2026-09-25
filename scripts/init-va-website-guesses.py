"""Best-effort discovery of school websites VA's own workbook is missing.

VA's GI Bill Comparison Tool workbook leaves many approved schools'
`insturl` field blank, especially small proprietary trade schools -- roughly
two-thirds of the schools in an average find_va_programs search have no
website on file at all, so app/agent.py has nothing to crawl from for a
program/apply page.

This script fills that gap with a SEPARATE, clearly-marked table rather than
writing into `facilities.insturl` itself: for each approved school missing a
website, it searches for "<institution> <city> <state>" via the Serper.dev
API, filters out directory/review/social sites, and prefers a .edu match
when one is present (a much harder domain to fake than a generic TLD). The
result is a *guess*, not a VA-confirmed fact, and the app must present it as
such (see the "Unverified school link" labeling in app/agent.py).

An earlier version of this script scraped DuckDuckGo's HTML results page
directly, which is not a documented, sanctioned API -- after roughly 100
requests during testing, it started hanging indefinitely with no error at
all, confirming that risk concretely. Serper.dev is an actual API with a
published contract (2,500 free queries on signup, no card required), so this
now uses that instead. It requires SERPER_API_KEY in .env; get a key at
https://serper.dev.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / ".cache" / "va-comparison.sqlite"
CONCURRENCY = 5
BATCH_COMMIT = 50
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2


def _load_api_key() -> str:
    key = os.getenv("SERPER_API_KEY")
    if key:
        return key
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("SERPER_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(
        "SERPER_API_KEY is not set. Add it to .env -- get a free key at https://serper.dev."
    )
NAME_STOP_WORDS = {
    "a", "academy", "and", "at", "campus", "center", "college", "communications",
    "community", "for", "in", "inc", "institute", "international", "llc", "main",
    "of", "school", "technical", "technology", "the", "training", "university",
}

# Hostname substrings that flag local news outlets, tourism boards, and
# similar community sites. These aren't specific enough for an exact-host
# blocklist entry, but a facility name that includes a location suffix (e.g.
# "-MINOT AFB", "-EDINBORO") can make that location's name look like a name
# match against a local newspaper or "visit our town" site, which is never
# the institution's own official site.
BLOCKED_HOST_PATTERNS = re.compile(
    r"news|visit[a-z]*\.|tourism|dailynews|gazette|tribune|herald|patch\.com|"
    # State-government "manual"/almanac reference pages (e.g. Maryland's
    # msa.maryland.gov directory) describe an institution but aren't its own
    # site.
    r"manual|"
    # Multi-school directory/aggregator sites, named as a trade or category
    # word (school(s), college, beauty, massage, cdl, trade...) glued to an
    # aggregator-ish suffix (directory, finder, near(me), now, usa, guide,
    # authority...). These routinely share one category word with a real
    # institution in that trade without being that institution's own site --
    # this is what let beautyschoolsdirectory.com and massagetherapylicense.org
    # outrank real schools' sites earlier in this same batch run.
    r"schools?directory|schools?finder|schools?near|school(s)?now|schoolsusa|"
    r"schoolguide|schoolauthority|collegehelpguide|therapylicense",
    re.I,
)

# Directories, review sites, social platforms, and school-ranking aggregators
# that reliably outrank a small school's own site but are never that site.
BLOCKED_HOSTS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "linkedin.com", "youtube.com", "yelp.com", "bbb.org", "yellowpages.com",
    "zoominfo.com", "nextdoor.com", "chamberofcommerce.com", "mapquest.com",
    "google.com", "business.google.com", "indeed.com", "glassdoor.com",
    "wikipedia.org", "findglocal.com", "tradecolleges.org", "tradeschoolsusa.net",
    "educationguider.org", "niche.com", "collegesimply.com", "usnews.com",
    "collegeboard.org", "cappex.com", "greatschools.org", "publicschoolreview.com",
    "privateschoolreview.com", "bing.com", "duckduckgo.com", "yahoo.com",
    "socialsecurityhop.com", "beautyschoolsdirectory.com", "careerschoolnow.org",
    "beautyschools.com", "beautyschoolfinder.com", "massagebook.com",
    "massagesup.com", "cosmetologyschoolsnearme.org",
}


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_website_guesses ("
        "facility_code TEXT PRIMARY KEY, url TEXT NOT NULL, fetched_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS va_website_guess_attempts ("
        "facility_code TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL)"
    )
    connection.commit()


def _name_tokens(institution: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", institution.lower())
        if len(token) > 2 and token not in NAME_STOP_WORDS
    }


def pick_best(urls: list[str], institution: str) -> str | None:
    """Prefer a .edu match, but only ever accept a candidate whose registrable
    domain label (the part right before the public suffix, e.g. "msu" in
    msu.edu or "massagebook" in massagebook.com -- never a subdomain prefix,
    since a real school's own site is essentially always at its own
    registrable domain, not hosted as a subdomain of someone else's) contains
    a significant word from the institution's own name. This rejects
    legitimate foreign institutions whose domain uses a native-language name
    (e.g. "University of Vienna" -> univie.ac.at), which is why this script
    is scoped to US facilities only.

    A .edu domain is inherently harder to fake (restricted registration), so
    one matching word is enough there. Any other TLD requires at least two
    matching words -- a single generic word (e.g. "massage") is not enough
    evidence on its own; it is exactly what let a third-party directory like
    massagebook.com outrank the real massageschoolofmontana.com, which
    matches on two words (massage + montana).

    Also accepts a domain whose registrable label is a prefix of the acronym
    formed from the institution's own words (e.g. "Tulsa Welding School" ->
    "tws", "San Joaquin Valley College" -> "sjvc") -- many small schools' real
    domains are exactly this kind of abbreviation rather than a spelled-out
    word, which the word-matching checks above can't see on their own. Unlike
    those checks' word list, category words like "college"/"institute"/
    "school"/"university"/"academy" are KEPT here since their initials are
    typically part of the real abbreviation (dropping them would turn
    "Northeast Maritime Institute" into "nm" instead of "nmi", missing the
    real nmi.edu). This acronym match counts on its own regardless of TLD.

    A .edu candidate only wins the final tie-break when its match strength
    (token count, or an acronym match counted as strength 2) STRICTLY beats
    the strongest raw strength among every other candidate with any match at
    all -- even one that doesn't itself clear the 2-word bar above. A weak,
    single-word .edu match tied by an equally weak non-.edu candidate is
    treated as ambiguous evidence and the whole lookup returns no guess,
    rather than either one winning just for being .edu. That tie is exactly
    what let an unrelated school like natradeschools.edu beat the real site
    for "Delta School of Trades" on an accidental "trades" substring split
    across "na-trade-schools" while a same-strength non-.edu competitor
    (deltatechnicalcollege.com, matching only "delta") sat right next to it."""
    tokens = _name_tokens(institution)
    if not tokens:
        return None
    acronym_stop_words = NAME_STOP_WORDS - {"college", "institute", "school", "university", "academy"}
    acronym = "".join(
        word[0] for word in re.findall(r"[a-z0-9]+", institution.lower())
        if word not in acronym_stop_words
    )
    candidates = []
    for url in urls:
        host = (urlparse(url).hostname or "").removeprefix("www.")
        if not host or any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS):
            continue
        if BLOCKED_HOST_PATTERNS.search(host):
            continue
        is_edu = host.lower().endswith(".edu")
        labels = host.lower().split(".")
        registrable_label = re.sub(r"[^a-z0-9]", "", labels[-2] if len(labels) >= 2 else host.lower())
        token_matches = sum(1 for token in tokens if token in registrable_label)
        acronym_match = len(registrable_label) >= 2 and acronym.startswith(registrable_label)
        strength = max(token_matches, 2) if acronym_match else token_matches
        if strength == 0:
            continue
        qualifies = acronym_match or token_matches >= (1 if is_edu else 2)
        candidates.append((url, is_edu, strength, qualifies))
    if not candidates:
        return None
    best_non_edu_strength = max((s for _, edu, s, _ in candidates if not edu), default=0)
    selectable = [
        (url, is_edu, strength) for url, is_edu, strength, qualifies in candidates
        if qualifies and (not is_edu or strength > best_non_edu_strength)
    ]
    if not selectable:
        return None
    # Among several qualifying candidates in the same edu/non-edu tier, prefer
    # the one whose registrable label matches the MOST words from the
    # institution's name, not just whichever search result happened to rank
    # first -- search-result order reflects the query engine's relevance
    # scoring, not name-match confidence, and picking list order first let a
    # weaker two-word match (e.g. signaturedesignstyles.com, matching
    # "signature"+"design") win over the correct three-word match
    # (signaturedesignbeauty.com, matching "signature"+"design"+"beauty")
    # just because it appeared earlier in the results. max() is stable, so
    # true ties still resolve to the first-listed candidate as before.
    edu_selectable = [(url, strength) for url, is_edu, strength in selectable if is_edu]
    if edu_selectable:
        return max(edu_selectable, key=lambda pair: pair[1])[0]
    non_edu_selectable = [(url, strength) for url, is_edu, strength in selectable if not is_edu]
    return max(non_edu_selectable, key=lambda pair: pair[1])[0]


class QuotaExhausted(Exception):
    """Serper API returned 402/403 -- the free query allowance is used up."""


async def search(client: httpx.AsyncClient, api_key: str, query: str) -> list[str]:
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query},
            )
            if response.status_code in (400, 402, 403):
                # Serper returns plain 400 with {"message": "Not enough
                # credits"} when the allowance runs out (not just 402/403),
                # so check the body rather than trusting the status code
                # alone -- a real 400 (bad request) should still retry/fail
                # normally instead of being mistaken for quota exhaustion.
                try:
                    message = response.json().get("message", "")
                except ValueError:
                    message = ""
                if response.status_code in (402, 403) or "credit" in message.lower():
                    raise QuotaExhausted(
                        f"Serper API returned {response.status_code} ({message or 'no message'}) "
                        "-- free query allowance is likely used up. Check your usage at "
                        "https://serper.dev/dashboard."
                    )
            if response.status_code == 429:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            return [item["link"] for item in data.get("organic", []) if item.get("link")]
        except (httpx.HTTPError, ValueError):
            if attempt == MAX_RETRIES - 1:
                return []
            await asyncio.sleep(2)
    return []


async def lookup_facility(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, api_key: str,
    facility_code: str, institution: str, city: str, state: str,
) -> tuple[str, str | None]:
    async with semaphore:
        query = " ".join(part for part in (institution, city, state) if part)
        urls = await search(client, api_key, query)
        return facility_code, pick_best(urls, institution)


def flush(
    connection: sqlite3.Connection, pending: list[tuple[str, str | None]],
) -> None:
    now = int(time.time())
    found = [(code, url, now) for code, url in pending if url]
    if found:
        connection.executemany(
            "INSERT OR REPLACE INTO va_website_guesses VALUES (?, ?, ?)", found,
        )
    connection.executemany(
        "INSERT OR REPLACE INTO va_website_guess_attempts VALUES (?, ?)",
        [(code, now) for code, _ in pending],
    )
    connection.commit()


async def run(connection: sqlite3.Connection, row_limit: int | None, api_key: str) -> None:
    already_attempted = {
        row[0] for row in connection.execute("SELECT facility_code FROM va_website_guess_attempts")
    }
    all_rows = connection.execute(
        "SELECT facility_code, institution, city, state FROM facilities "
        "WHERE approved = 1 AND school_provider = 1 AND (insturl IS NULL OR insturl = '') "
        # Scoped to US facilities: a foreign institution's domain typically
        # uses its native-language name (e.g. "University of Vienna" ->
        # univie.ac.at), which the English-name-token overlap check in
        # pick_best() can't validate, so it would either reject every real
        # match or need a much more permissive check that risks accepting
        # wrong ones instead.
        "AND state IS NOT NULL AND length(state) = 2 "
        # High schools are out of scope for this tool (it's for VA
        # post-secondary/GI Bill benefits comparison), so don't spend Serper
        # queries looking up their websites.
        "AND institution NOT LIKE '%HIGH SCHOOL%'"
    ).fetchall()
    targets = [row for row in all_rows if row[0] not in already_attempted]
    if row_limit is not None:
        targets = targets[:row_limit]
    if not targets:
        print(f"All {len(all_rows):,} website-less approved schools already attempted.")
        return
    print(
        f"Looking up {len(targets):,} of {len(all_rows):,} approved schools missing a "
        f"website ({len(already_attempted):,} already attempted, resuming)..."
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    done = 0
    found_count = 0
    pending: list[tuple[str, str | None]] = []

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        tasks = [
            asyncio.create_task(
                lookup_facility(client, semaphore, api_key, code, institution, city, state)
            )
            for code, institution, city, state in targets
        ]
        try:
            for task in asyncio.as_completed(tasks):
                facility_code, url = await task
                pending.append((facility_code, url))
                done += 1
                if url:
                    found_count += 1
                if len(pending) >= BATCH_COMMIT:
                    flush(connection, pending)
                    pending.clear()
                    print(f"  {done:,}/{len(targets):,} attempted, {found_count:,} found", flush=True)
        except QuotaExhausted as error:
            for task in tasks:
                task.cancel()
            flush(connection, pending)
            print(f"Stopped early: {error}")
            print(f"  {done:,}/{len(targets):,} attempted before stopping, {found_count:,} found")
            return
        if pending:
            flush(connection, pending)
            print(f"  {done:,}/{len(targets):,} attempted, {found_count:,} found", flush=True)


def main() -> None:
    if not DATABASE.exists():
        raise SystemExit("VA Comparison Tool index is missing. Run scripts/init-va-data.py first.")
    api_key = _load_api_key()
    row_limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    connection = sqlite3.connect(DATABASE)
    ensure_schema(connection)
    asyncio.run(run(connection, row_limit, api_key))
    connection.close()


if __name__ == "__main__":
    main()
