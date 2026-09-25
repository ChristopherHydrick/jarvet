"""Regression checks for program search counts.

Runs the saved searches in scripts/search-checks.json two ways:

  db   -- through the app's own search code (VaComparison.programs_for plus
          JarvetTools._related_programs, with the same 5-mile radius buffer),
          against the live database opened READ-ONLY, and compares the exact
          and related school counts with the saved expected numbers.
  app  -- as chat messages to the running app (POST /api/chat), and checks
          that the cards and the reply's two bullet lists match the db counts.

Nothing here writes to va-comparison.sqlite, so it is safe while the app runs.
Run from the project root with Windows Python:

  python scripts/check-search-counts.py            # db checks, then app checks
  python scripts/check-search-counts.py --db-only  # no model calls
  python scripts/check-search-counts.py --only cyber

Exits 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Windows Python has neither; the search paths used here never touch them
# (numpy is only needed by the meaning-based fallback, which runs only when
# the exact search finds nothing -- a check that hits it fails loudly).
sys.modules.setdefault("numpy", types.ModuleType("numpy"))
_pyox = types.ModuleType("pyoxigraph")
_pyox.Store = object
sys.modules.setdefault("pyoxigraph", _pyox)

from app.agent import RADIUS_BUFFER_MILES, RELATED_HEADING, JarvetTools  # noqa: E402
from app.ipeds import IpedsIndex  # noqa: E402
from app.onet import OnetGraph  # noqa: E402
from app.va import VaComparison, _normalized  # noqa: E402

CACHE = ROOT / ".cache"


_connect = sqlite3.connect


def read_only(path: Path) -> sqlite3.Connection:
    connection = _connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def build_tools() -> tuple[VaComparison, JarvetTools]:
    # Mirrors each index's load() but with read-only connections: load()
    # itself runs a CREATE TABLE IF NOT EXISTS on the VA database.
    va = VaComparison(CACHE / "va-comparison.sqlite")
    va.connection = read_only(va.path)
    va.cities = [
        (row[0], row[1], _normalized(row[0]))
        for row in va.connection.execute(
            "SELECT DISTINCT city, state FROM facilities WHERE city IS NOT NULL AND state IS NOT NULL"
        )
    ]
    ipeds = IpedsIndex(CACHE / "ipeds.sqlite")
    sqlite3.connect = lambda *args, **kwargs: read_only(ipeds.path)
    try:
        ipeds.load()
    finally:
        sqlite3.connect = _connect
    onet = OnetGraph(CACHE / "onet-store")
    onet.search_db = read_only(CACHE / "onet-store" / "search.sqlite")
    return va, JarvetTools(onet, va, ipeds, {}, None, "")


def db_counts(va: VaComparison, tools: JarvetTools, check: dict) -> dict:
    """The same steps find_va_programs takes for a first page."""
    latitude = longitude = None
    state = check.get("state")
    if check.get("location"):
        location = va.resolve_location(check["location"])
        if location is None:
            raise ValueError(f"location not found: {check['location']}")
        latitude, longitude = location["latitude"], location["longitude"]
        state = location.get("state") if not location.get("city") else None
    radius = check.get("radius")
    max_miles = max(5.0, float(radius)) + RADIUS_BUFFER_MILES if radius is not None else None
    result = va.programs_for(
        check["program"], state=state, limit=100, latitude=latitude, longitude=longitude,
        max_miles=max_miles,
    )
    codes = set(result.pop("_matched_facility_codes", []))
    fields = result.pop("_matched_fields", [])
    related = tools._related_programs(
        fields, codes, state=state, latitude=latitude, longitude=longitude, max_miles=max_miles,
    )
    return {
        "exact": len(result["facilities"]),
        "related": len(related),
        "exact_names": sorted(str(f["institution"]) for f in result["facilities"]),
        "related_names": sorted(str(f["institution"]) for f in related),
    }


def reply_counts(message: str) -> dict:
    """Bullet lines before and after the 'Related programs' heading, found
    the same way arrange_listings() finds it."""
    exact = related = 0
    in_related = False
    for line in message.splitlines():
        if RELATED_HEADING.match(line.strip()):
            in_related = True
        elif line.lstrip().startswith("•"):
            if in_related:
                related += 1
            else:
                exact += 1
    return {"exact": exact, "related": related}


def app_counts(base_url: str, message: str, timeout: float) -> dict:
    body = json.dumps({"messages": [{"role": "user", "content": message}]}).encode()
    request = urllib.request.Request(
        f"{base_url}/api/chat", data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    exact, related = [], []
    for resource in payload.get("resources", []):
        if resource.get("kind") != "provider-details":
            continue
        provider = resource.get("provider") or {}
        (related if provider.get("related_fields") else exact).append(str(provider.get("institution")))
    return {
        "cards": {"exact": len(exact), "related": len(related)},
        "reply": reply_counts(str(payload.get("message", ""))),
        "exact_names": sorted(exact),
        "related_names": sorted(related),
    }


def name_diff(expected: list[str], actual: list[str]) -> str:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    parts = []
    if missing:
        parts.append("missing: " + "; ".join(missing))
    if extra:
        parts.append("extra: " + "; ".join(extra))
    return " | ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checks", default=str(ROOT / "scripts" / "search-checks.json"))
    parser.add_argument("--only", help="run only checks whose name contains this text")
    parser.add_argument("--db-only", action="store_true", help="skip the chat (model) checks")
    parser.add_argument("--app-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()

    checks = json.loads(Path(args.checks).read_text(encoding="utf-8"))
    if args.only:
        checks = [c for c in checks if args.only.lower() in c["name"].lower()]
    failures = 0

    print("== Database checks (app search code, read-only) ==")
    va, tools = build_tools()
    db_results: dict[str, dict] = {}
    for check in checks:
        try:
            counts = db_counts(va, tools, check)
        except Exception as error:  # noqa: BLE001 -- report and keep going
            print(f"FAIL  {check['name']}: {type(error).__name__}: {error}")
            failures += 1
            continue
        db_results[check["name"]] = counts
        problems = [
            f"{kind} {counts[kind]} (expected {want})"
            for kind, want in check.get("expect", {}).items() if counts[kind] != want
        ]
        status = "FAIL" if problems else "ok  "
        failures += bool(problems)
        print(f"{status}  {check['name']}: {counts['exact']} exact + {counts['related']} related"
              + (" -- " + ", ".join(problems) if problems else ""))

    if not args.db_only:
        print(f"\n== App checks (chat at {args.app_url}, compared with database counts) ==")
        for check in checks:
            if "message" not in check or check["name"] not in db_results:
                continue
            want = db_results[check["name"]]
            started = time.monotonic()
            try:
                got = app_counts(args.app_url, check["message"], args.timeout)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                print(f"FAIL  {check['name']}: app request failed: {error}")
                failures += 1
                continue
            problems = []
            for source in ("cards", "reply"):
                for kind in ("exact", "related"):
                    if got[source][kind] != want[kind]:
                        problems.append(f"{source} {kind} {got[source][kind]} (database {want[kind]})")
            for kind in ("exact", "related"):
                diff = name_diff(want[f"{kind}_names"], got[f"{kind}_names"])
                if diff:
                    problems.append(f"{kind} cards {diff}")
            status = "FAIL" if problems else "ok  "
            failures += bool(problems)
            print(f"{status}  {check['name']} ({time.monotonic() - started:.0f}s): "
                  f"cards {got['cards']['exact']}+{got['cards']['related']}, "
                  f"reply lists {got['reply']['exact']}+{got['reply']['related']}"
                  + ("\n      " + "\n      ".join(problems) if problems else ""))

    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
