"""Monthly refresh of the VA school list and program catalog.

Jarvet's program search, related programs, military-job search and pathways
all read the VA school list (from the VA's Comparison Tool workbook) and every
approved school's program list (from the VA programs API), plus what is built
from those: the keyword index, the meaning-based index and each program's
field of study. This rebuilds all of them in three steps, run from the project
root with Windows Python:

  python scripts/monthly-refresh.py prepare   # ~45 min, app keeps running
  python scripts/monthly-refresh.py report    # what changed + checks on the copy
  python scripts/monthly-refresh.py apply     # few minutes of downtime

prepare works on a snapshot of the database in <CACHE_BACKUP_DIR>/refresh/
(never the live file): downloads the latest VA workbook, rebuilds the school
list, re-fetches every approved school's programs (twice, so the second pass
retries failures), drops programs of schools that lost approval, rebuilds the
keyword index, embeds new program titles and re-sorts every program into its
field (scripts/init-va-program-fields.py). The slow steps run in throwaway
jarvet-dev containers. It is resumable: re-running continues from the first
step not yet done; `prepare --restart` starts a new refresh from a new snapshot.

report compares the copy with the live database (read-only) and runs the
database checks (scripts/check-search-counts.py --db-only) against the copy.
Look at it before applying: a check whose count moved because schools really
added or dropped programs gets its expected number updated in
scripts/search-checks.json; anything else is a bug to look into first.

apply stops the app, backs up (scripts/backup-cache.sh), copies only the
refreshed tables from the copy into the live database -- everything else
(hand corrections, website/apply-link guesses, saved school details) stays as
it is -- checks integrity, installs the new workbook, restarts the app and
re-runs the database checks on the live file.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = ROOT / ".cache" / "va-comparison.sqlite"
VA_DATA_DIR = ROOT / "data" / "va-comparison"
WORKBOOK_URL = "https://www.benefits.va.gov/GIBILL/docs/job_aids/ComparisonToolData.xlsx"
CONTAINER = "jarvet"
IMAGE = "jarvet-dev"

# Replaced wholesale on apply; the embedding tables are merged instead (new
# titles added, vanished ones dropped) since they're mostly unchanged and big.
REPLACED_TABLES = (
    "facilities", "zcta", "va_programs", "va_programs_crawl_state", "va_program_fields",
)
PREPARE_STEPS = ("snapshot", "download", "schools", "programs", "prune", "embeddings", "fields", "finish")


def backup_dir() -> Path:
    value = os.environ.get("CACHE_BACKUP_DIR")
    env_file = ROOT / ".env"
    if not value and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("CACHE_BACKUP_DIR="):
                value = line.split("=", 1)[1].strip().strip("\"'")
    if not value:
        sys.exit("CACHE_BACKUP_DIR is not set (see scripts/backup-cache.sh)")
    if len(value) > 2 and value[0] == "/" and value[2] == "/":  # Git Bash form /c/Users/...
        value = f"{value[1].upper()}:{value[2:]}"
    return Path(value)


WORK_DIR = backup_dir() / "refresh"
WORK_DB = WORK_DIR / "va-comparison.work.sqlite"
WORK_WORKBOOK = WORK_DIR / "ComparisonToolData.xlsx"
STATE_FILE = WORK_DIR / "state.json"
REPORT_FILE = WORK_DIR / "report.txt"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def app_running() -> bool:
    container = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{CONTAINER}$"], capture_output=True, text=True,
    ).stdout.strip()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        listening = sock.connect_ex(("127.0.0.1", 8000)) == 0
    return bool(container) or listening


def in_container(command: str) -> None:
    """Run a shell command in a throwaway jarvet-dev container, with the
    project at /workspace and the refresh folder at /refresh."""
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{ROOT.as_posix()}:/workspace",
            "-v", f"{WORK_DIR.as_posix()}:/refresh",
            "-w", "/workspace",
            "-e", "JARVET_VA_DB=/refresh/va-comparison.work.sqlite",
            IMAGE, "bash", "-c",
            # The embedding model, unpacked where fastembed looks for it, so
            # the container needn't download it.
            f"set -e; tar -C /tmp -xf .cache/_related_fields/fastembed_cache.tar; {command}",
        ],
        check=True,
    )


def integrity(connection: sqlite3.Connection) -> str:
    return connection.execute("PRAGMA integrity_check").fetchone()[0]


# --- prepare ---------------------------------------------------------------

def step_snapshot(state: dict) -> None:
    # SQLite's backup API reads a consistent copy from a read-only connection,
    # so the running app is never disturbed.
    temporary = WORK_DB.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    source = read_only(LIVE_DB)
    target = sqlite3.connect(temporary)
    source.backup(target)
    source.close()
    # One plain file (no -wal/-shm): the copy moves between Windows and the
    # containers' mounted folder.
    target.execute("PRAGMA journal_mode=DELETE")
    target.close()
    temporary.replace(WORK_DB)
    state["since"] = int(time.time())
    print(f"Snapshot of the live database -> {WORK_DB}")


def step_download(state: dict) -> None:
    temporary = WORK_WORKBOOK.with_suffix(".download")
    request = urllib.request.Request(WORKBOOK_URL, headers={"User-Agent": "Mozilla/5.0 (jarvet refresh)"})
    with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as out:
        shutil.copyfileobj(response, out)
    if not zipfile.is_zipfile(temporary):
        sys.exit(f"Downloaded workbook is not a valid .xlsx: {temporary}")
    temporary.replace(WORK_WORKBOOK)
    size = WORK_WORKBOOK.stat().st_size / 1e6
    print(f"Downloaded the VA workbook ({size:.1f} MB)")


def step_schools(state: dict) -> None:
    # A fresh marker path makes init-va-data.py rebuild facilities from the
    # downloaded workbook; it also embeds any newly approved school names.
    (WORK_DIR / "va-comparison.ready").unlink(missing_ok=True)
    in_container(
        "JARVET_VA_WORKBOOK=/refresh/ComparisonToolData.xlsx "
        "JARVET_VA_MARKER=/refresh/va-comparison.ready "
        ".venv/bin/python scripts/init-va-data.py"
    )


def step_programs(state: dict) -> None:
    since = state["since"]
    for _ in range(2):  # the second pass retries schools whose fetch failed
        in_container(f".venv/bin/python scripts/init-va-programs-data.py --since {since}")
    connection = read_only(WORK_DB)
    missed = connection.execute(
        "SELECT COUNT(*) FROM facilities WHERE approved = 1 AND school_provider = 1 "
        "AND facility_code NOT IN (SELECT facility_code FROM va_programs_crawl_state WHERE fetched_at >= ?)",
        (since,),
    ).fetchone()[0]
    connection.close()
    state["not_refetched"] = missed
    print(f"Schools whose programs could not be re-fetched (kept last month's list): {missed:,}")


def step_prune(state: dict) -> None:
    connection = sqlite3.connect(WORK_DB)
    approved = "SELECT facility_code FROM facilities WHERE approved = 1 AND school_provider = 1"
    with connection:
        removed = connection.execute(
            f"DELETE FROM va_programs WHERE facility_code NOT IN ({approved})"
        ).rowcount
        connection.execute(f"DELETE FROM va_programs_crawl_state WHERE facility_code NOT IN ({approved})")
        connection.execute(
            "DELETE FROM provider_embeddings WHERE facility_code NOT IN "
            "(SELECT facility_code FROM facilities WHERE approved = 1)"
        )
        connection.execute("DELETE FROM va_program_search")
        connection.execute(
            "INSERT INTO va_program_search (facility_code, program_type, description) "
            "SELECT facility_code, program_type, description FROM va_programs"
        )
    connection.close()
    print(f"Dropped {removed:,} programs of schools no longer approved; rebuilt the keyword index")


def step_embeddings(state: dict) -> None:
    in_container(".venv/bin/python scripts/init-va-program-embeddings.py")
    connection = sqlite3.connect(WORK_DB)
    with connection:
        dropped = connection.execute(
            "DELETE FROM program_embeddings WHERE description NOT IN (SELECT description FROM va_programs)"
        ).rowcount
    connection.close()
    print(f"Dropped meaning data for {dropped:,} program titles no longer offered")


def step_fields(state: dict) -> None:
    in_container(
        ".venv/bin/python scripts/init-va-program-fields.py compute "
        "--source /refresh/va-comparison.work.sqlite --ipeds /workspace/.cache/ipeds.sqlite "
        "--out /refresh/va_program_fields.jsonl && "
        ".venv/bin/python scripts/init-va-program-fields.py write "
        "--input /refresh/va_program_fields.jsonl --db /refresh/va-comparison.work.sqlite"
    )


def step_finish(state: dict) -> None:
    connection = sqlite3.connect(WORK_DB)
    connection.execute("PRAGMA journal_mode=DELETE")
    result = integrity(connection)
    connection.close()
    if result != "ok":
        sys.exit(f"Working copy failed its integrity check: {result}")
    print("Working copy integrity: ok")


def prepare(restart: bool) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    state = {"done": []} if restart else load_state()
    if state.get("applied"):
        sys.exit("The last refresh was already applied. Use `prepare --restart` to start a new one.")
    steps = {name: globals()[f"step_{name}"] for name in PREPARE_STEPS}
    for name in PREPARE_STEPS:
        if name in state["done"]:
            continue
        print(f"\n== {name} ==", flush=True)
        started = time.time()
        steps[name](state)
        state["done"].append(name)
        state.pop("reported", None)
        save_state(state)
        print(f"({name} took {(time.time() - started) / 60:.1f} min)", flush=True)
    print("\nWorking copy ready. Next: python scripts/monthly-refresh.py report")


# --- report ----------------------------------------------------------------

def report() -> None:
    state = load_state()
    if state["done"] != list(PREPARE_STEPS):
        sys.exit("The working copy isn't finished yet -- run `prepare` first.")
    lines: list[str] = []
    say = lines.append
    live, work = read_only(LIVE_DB), read_only(WORK_DB)

    def schools(connection):
        return dict(connection.execute(
            "SELECT facility_code, institution || ' (' || COALESCE(city, '') || ', ' || COALESCE(state, '') || ')' "
            "FROM facilities WHERE approved = 1 AND school_provider = 1"
        ))

    def programs(connection):
        result: dict[str, set] = {}
        for code, kind, description in connection.execute(
            "SELECT facility_code, program_type, description FROM va_programs"
        ):
            result.setdefault(code, set()).add((kind, description))
        return result

    old_schools, new_schools = schools(live), schools(work)
    added = sorted(set(new_schools) - set(old_schools), key=new_schools.get)
    removed = sorted(set(old_schools) - set(new_schools), key=old_schools.get)
    say(f"Monthly refresh report ({time.strftime('%Y-%m-%d %H:%M')}), refresh started "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(state['since']))}")
    say("")
    say(f"VA-approved schools: {len(old_schools):,} -> {len(new_schools):,} "
        f"({len(added):,} newly approved, {len(removed):,} no longer approved)")
    say(f"Schools whose programs could not be re-fetched (kept last month's list): {state.get('not_refetched', 0):,}")

    old_programs, new_programs = programs(live), programs(work)
    old_total = sum(len(v) for v in old_programs.values())
    new_total = sum(len(v) for v in new_programs.values())
    gained = lost = 0
    changes = []
    for code in set(old_programs) | set(new_programs):
        before, after = old_programs.get(code, set()), new_programs.get(code, set())
        plus, minus = len(after - before), len(before - after)
        gained += plus
        lost += minus
        if plus or minus:
            changes.append((plus + minus, plus, minus, code))
    say(f"Programs: {old_total:,} -> {new_total:,} ({gained:,} added, {lost:,} removed, "
        f"{sum(1 for c in changes):,} schools changed)")

    fields_old = dict(((f, d), c) for f, d, c in live.execute(
        "SELECT facility_code, description, cip FROM va_program_fields"))
    same = moved = 0
    for (f, d, c) in work.execute("SELECT facility_code, description, cip FROM va_program_fields"):
        if (f, d) in fields_old:
            if fields_old[(f, d)] == c:
                same += 1
            else:
                moved += 1
    say(f"Field of study, for programs in both: {same:,} unchanged, {moved:,} re-sorted "
        f"(should be near zero unless the sorting rules changed)")
    confident = work.execute(
        # Same cutoffs as the app's VaComparison.FIELD_MIN_SCORE.
        "SELECT COUNT(*) FROM va_program_fields WHERE cip IS NOT NULL AND (method = 'rule' "
        "OR (method = 'school' AND score >= 0.75) OR (method = 'global' AND score >= 0.78))"
    ).fetchone()[0]
    confident_before = read_only(LIVE_DB).execute(
        "SELECT COUNT(*) FROM va_program_fields WHERE cip IS NOT NULL AND (method = 'rule' "
        "OR (method = 'school' AND score >= 0.75) OR (method = 'global' AND score >= 0.78))"
    ).fetchone()[0]
    say(f"Programs with a confident field: {confident_before:,} -> {confident:,}")

    def names(codes, lookup, limit=25):
        shown = [f"  {lookup[c]}" for c in codes[:limit]]
        if len(codes) > limit:
            shown.append(f"  ... and {len(codes) - limit:,} more")
        return shown

    if added:
        say("")
        say("Newly approved schools:")
        lines.extend(names(added, new_schools))
    if removed:
        say("")
        say("No longer approved (their programs are dropped):")
        lines.extend(names(removed, old_schools))
    if changes:
        say("")
        say("Schools with the most program changes:")
        for _, plus, minus, code in sorted(changes, reverse=True)[:20]:
            name = new_schools.get(code) or old_schools.get(code) or code
            say(f"  {name}: +{plus} / -{minus}")
    live.close()
    work.close()

    say("")
    say("== Database checks on the working copy ==")
    checks = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-search-counts.py"), "--db-only"],
        capture_output=True, text=True, env={**os.environ, "JARVET_CHECK_DB": str(WORK_DB)},
    )
    lines.extend(checks.stdout.rstrip().splitlines())
    if checks.returncode != 0 and checks.stderr.strip():
        lines.extend(checks.stderr.rstrip().splitlines()[-15:])

    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n(saved to {REPORT_FILE})")
    state["reported"] = True
    state["checks_passed"] = checks.returncode == 0
    save_state(state)


# --- apply -----------------------------------------------------------------

def git_bash() -> str:
    # Not plain "bash": on Windows that can resolve to WSL's.
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(candidate).exists():
            return candidate
    sys.exit("Git Bash not found (needed for scripts/backup-cache.sh)")


def copy_tables(live: sqlite3.Connection) -> None:
    # A plain path: "file:...?mode=ro" only works on a connection opened with
    # uri=True. The copy is only read here.
    live.execute("ATTACH DATABASE ? AS work", (str(WORK_DB),))
    work_schema = {
        name: (kind, table, sql) for name, kind, table, sql in live.execute(
            "SELECT name, type, tbl_name, sql FROM work.sqlite_master WHERE sql IS NOT NULL"
        )
    }
    # One explicit transaction: Python's sqlite3 would otherwise commit each
    # DROP/CREATE on its own, so a failure midway could leave tables missing.
    live.isolation_level = None
    live.execute("BEGIN")
    try:
        for table in REPLACED_TABLES:
            live.execute(f"DROP TABLE IF EXISTS main.{table}")
            live.execute(work_schema[table][2])
            live.execute(f"INSERT INTO main.{table} SELECT * FROM work.{table}")
            for name, (kind, owner, sql) in work_schema.items():
                if kind == "index" and owner == table:
                    live.execute(sql)
            print(f"  {table}: {live.execute(f'SELECT COUNT(*) FROM main.{table}').fetchone()[0]:,} rows")
        live.execute(
            "UPDATE main.sqlite_sequence SET seq = (SELECT MAX(id) FROM main.va_programs) "
            "WHERE name = 'va_programs'"
        )
        for table, key in (("provider_embeddings", "facility_code"), ("program_embeddings", "description")):
            live.execute(f"DELETE FROM main.{table} WHERE {key} NOT IN (SELECT {key} FROM work.{table})")
            live.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM work.{table}")
            print(f"  {table}: {live.execute(f'SELECT COUNT(*) FROM main.{table}').fetchone()[0]:,} rows")
        live.execute("DROP TABLE IF EXISTS main.va_program_search")
        live.execute(work_schema["va_program_search"][2])
        live.execute(
            "INSERT INTO main.va_program_search (facility_code, program_type, description) "
            "SELECT facility_code, program_type, description FROM main.va_programs"
        )
        print("  keyword index rebuilt")
        live.execute("COMMIT")
    except BaseException:
        live.execute("ROLLBACK")
        raise
    live.execute("DETACH DATABASE work")


def wait_for_app(seconds: int = 120) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:8000/", timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        if not subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=^{CONTAINER}$"], capture_output=True, text=True,
        ).stdout.strip():
            return False  # the container exited (startup failed)
        time.sleep(2)
    return False


def apply(keep_running_check: bool) -> None:
    state = load_state()
    if state["done"] != list(PREPARE_STEPS) or not state.get("reported"):
        sys.exit("Run `prepare` and then `report` (and look at it) before applying.")
    if state.get("applied"):
        sys.exit("This refresh was already applied.")

    print("Stopping the app...")
    subprocess.run(["docker", "stop", CONTAINER], check=False)
    if app_running():
        sys.exit("The app still appears to be running (container up or port 8000 in use) -- stop it first.")

    print("Backing up...")
    subprocess.run([git_bash(), "scripts/backup-cache.sh"], cwd=ROOT, check=True)

    print("Copying the refreshed tables into the live database...")
    live = sqlite3.connect(LIVE_DB, timeout=30)
    try:
        copy_tables(live)
        result = integrity(live)
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        live.close()
    # Closing the last connection removes -wal/-shm; left behind by Windows,
    # they made the app's first open in the container fail ("disk I/O error").
    leftovers = [p.name for p in LIVE_DB.parent.glob(LIVE_DB.name + "-*")]
    if leftovers:
        sys.exit(f"Database side files still present after closing: {leftovers} -- app left stopped.")
    print(f"Integrity: {result}")
    if result != "ok":
        sys.exit(
            "INTEGRITY CHECK FAILED -- the app was left stopped. Restore the backup just taken "
            f"({backup_dir()}\\va-comparison.sqlite -> .cache\\va-comparison.sqlite) before restarting."
        )

    # Install the workbook the school list now comes from, and record it as
    # indexed so the next container start doesn't rebuild facilities again.
    shutil.copy2(WORK_WORKBOOK, VA_DATA_DIR / "ComparisonToolData.xlsx")
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT.as_posix()}:/workspace", "-w", "/workspace",
         IMAGE, ".venv/bin/python", "scripts/init-va-data.py", "--write-marker"],
        check=True,
    )

    state["applied"] = int(time.time())
    save_state(state)
    print("Restarting the app...")
    subprocess.run(["docker", "start", CONTAINER], check=True)
    # Wait for the app to open the database itself before anything else
    # (the checks) opens it from Windows.
    if not wait_for_app():
        sys.exit(f"The app did not come up -- see `docker logs --tail 30 {CONTAINER}`.")
    print("App is up.")
    if keep_running_check:
        print("\n== Database checks on the live database ==")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "check-search-counts.py"), "--db-only"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="build the refreshed working copy (app keeps running)")
    prepare_parser.add_argument("--restart", action="store_true", help="start over from a new snapshot")
    commands.add_parser("report", help="compare the working copy with the live database and run the checks")
    apply_parser = commands.add_parser("apply", help="stop, back up, copy the refreshed tables in, restart")
    apply_parser.add_argument("--no-checks", action="store_true", help="skip the checks after restarting")
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        prepare(arguments.restart)
    elif arguments.command == "report":
        report()
    else:
        apply(not arguments.no_checks)


if __name__ == "__main__":
    main()
