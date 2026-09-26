"""Checks for Jarvet's counselor features (docs/counselor-plan.md).

  safety -- the server-side crisis/housing check (app/safety.py) fires for
            the saved messages that need it and stays quiet for look-alikes
            ("killing it at work"), and the fixed help text names every
            required contact (988 then 1, chat and text at
            veteranscrisisline.net, 838255; 877-424-3838). Free, no model.
  library-- the benefits library (app/benefits.py) returns the expected
            source (link) or fact (text) in its top results for saved
            questions, and nothing for an off-topic one. Needs numpy and
            fastembed, so on Windows it runs itself inside the jarvet
            container (docker exec, library file opened read-only). Free.
  state  -- the state vocational rehabilitation directory (app/state_help.py,
            data/state-vr-agencies.json) has all 78 agencies and finds the
            right ones for saved places. Free.
  scholarships -- the hand-checked scholarship list (data/scholarships.json)
            is complete and well-formed, and saved searches find (and leave out)
            the right scholarships. Free. With --links, also opens every
            sponsor link and the Scholarship Finder (network, still free).
  journeys- the guided journeys (app/journeys.py): saved answers are sent
            to the running app's /api/journey one by one; each question must
            come in order (with its buttons), nothing known is asked again, and
            the finished request must name the right first tool and facts.
            Free (no model); skipped if the app is not running.
  app    -- (skipped with --no-app) sends the safety_app messages to the
            running app and checks the reply starts with the help text and
            carries the call/chat/text buttons. Costs model credits.

Saved cases live in scripts/counselor-checks.json. Nothing here writes to any
database. Run from the project root with Windows Python:

  python scripts/check-counselor.py --no-app
  python scripts/check-counselor.py

Exits 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import safety  # noqa: E402


def check_safety(checks: dict) -> int:
    failures = 0
    for case in checks["safety"]:
        got = safety.check(case["message"])
        ok = got == case["expect"]
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  safety {case['message']!r}: {got or 'none'}"
              + ("" if ok else f" (expected {case['expect'] or 'none'})"))
    for kind, needed in checks["safety_text"].items():
        block = safety.notices([kind])[0]
        text = block["text"] + " " + " ".join(a["label"] + " " + a["url"] for a in block["actions"])
        missing = [word for word in needed if word.lower() not in text.lower()]
        failures += bool(missing)
        print(f"{'ok  ' if not missing else 'FAIL'}  {kind} help text names {', '.join(needed)}"
              + (f" -- missing {missing}" if missing else ""))
    return failures


def check_library(checks: dict) -> int:
    try:
        from fastembed import TextEmbedding

        from app.benefits import BenefitsLibrary, fastembed_reranker
    except ImportError:
        # Windows Python has neither; the app container has both.
        result = subprocess.run(
            ["docker", "exec", "jarvet", "/workspace/.venv/bin/python",
             "scripts/check-counselor.py", "--library-only"],
            capture_output=True, text=True, encoding="utf-8",
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith(("ok  ", "FAIL"))]
        print("\n".join(lines) or result.stderr.strip()[-500:])
        return sum(line.startswith("FAIL") for line in lines) or (result.returncode != 0 and not lines)
    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    library = BenefitsLibrary(ROOT / ".cache" / "benefits-library.sqlite",
                              embed=lambda text: next(model.embed([text])),
                              rerank=fastembed_reranker())
    library.load()
    if not library.available:
        print("FAIL  library: .cache/benefits-library.sqlite missing or empty")
        return 1
    failures = 0
    for case in checks["library"]:
        results = library.search(case["question"], limit=case.get("top", 5))
        if case.get("expect_none"):
            ok = not results
            detail = "" if ok else f" -- got {len(results)} passages, expected none"
        else:
            ok = any(
                case.get("url", "") in row["url"] and case.get("text", "") in row["text"]
                for row in results
            )
            detail = "" if ok else " -- top: " + "; ".join(row["url"].split("/")[-2] for row in results[:3])
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  library {case['question']!r}{detail}")
    return failures


def check_state_help(checks: dict) -> int:
    from app.state_help import StateHelp
    helper = StateHelp()
    helper.load()
    failures = 0
    total_ok = len(helper.agencies) == checks.get("state_help_total", 78)
    failures += not total_ok
    print(f"{'ok  ' if total_ok else 'FAIL'}  state VR directory has {len(helper.agencies)} agencies "
          f"(expected {checks.get('state_help_total', 78)})")
    for case in checks.get("state_help", []):
        result = helper.routes(case["state"])
        agencies = result.get("state_vr_agencies", [])
        problems = []
        if len(agencies) != case["agencies"]:
            problems.append(f"{len(agencies)} agencies, expected {case['agencies']}")
        if case.get("name") and not any(case["name"] in a["name"] for a in agencies):
            problems.append(f"no agency named {case['name']!r}")
        if case.get("code") and helper.state_code(case["state"]) != case["code"]:
            problems.append(f"state {helper.state_code(case['state'])}, expected {case['code']}")
        if case.get("notice") and "state_vr_current_notice" not in result:
            problems.append("current notice missing")
        failures += bool(problems)
        print(f"{'ok  ' if not problems else 'FAIL'}  state help {case['state']!r}"
              + (f" -- {'; '.join(problems)}" if problems else ""))
    return failures


def check_scholarships(checks: dict, links: bool) -> int:
    from app.scholarships import LEVELS, SITUATIONS, STUDENTS, Scholarships
    data = Scholarships()
    data.load()
    failures = 0
    problems = []
    if len(data.entries) != checks.get("scholarships_total", 18):
        problems.append(f"{len(data.entries)} scholarships, expected {checks.get('scholarships_total')}")
    for entry in data.entries:
        if not entry["url"].startswith("https://") or not entry.get("checked"):
            problems.append(f"{entry['id']}: needs an https link and a checked date")
        bad = [v for v in entry["students"] if v not in STUDENTS] + [
            v for v in entry["situations"] if v not in SITUATIONS] + [v for v in entry["levels"] if v not in LEVELS]
        if bad:
            problems.append(f"{entry['id']}: unknown values {bad}")
    failures += bool(problems)
    print(f"{'ok  ' if not problems else 'FAIL'}  scholarship list has {len(data.entries)} well-formed entries"
          + (f" -- {'; '.join(problems)}" if problems else ""))
    for case in checks.get("scholarships", []):
        names = [entry["name"] for entry in data.find(**case["find"])["scholarships"]]
        problems = [f"missing {name!r}" for name in case.get("include", []) if name not in names]
        problems += [f"should not list {name!r}" for name in case.get("exclude", []) if name in names]
        failures += bool(problems)
        print(f"{'ok  ' if not problems else 'FAIL'}  scholarships {case['find']}"
              + (f" -- {'; '.join(problems)}" if problems else ""))
    if links:
        urls = sorted({entry["url"] for entry in data.entries} | {
            data.finder_link("veteran")["url"], data.data["scam_warning"]["url"]})
        for url in urls:
            request = urllib.request.Request(url, headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128 Safari/537.36")})
            try:
                with urllib.request.urlopen(request, timeout=40) as response:
                    ok, status = response.status == 200, response.status
            except OSError as error:
                ok, status = False, error
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'}  link {url} ({status})")
    return failures


def post_json(url: str, payload: dict, timeout: float = 30) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def check_journeys(checks: dict, base_url: str) -> int:
    try:
        urllib.request.urlopen(f"{base_url.rstrip('/')}/api/health", timeout=10)
    except OSError:
        print("skip  journeys -- the app is not running")
        return 0
    failures = 0
    for case in checks.get("journeys", []):
        url = f"{base_url.rstrip('/')}/api/journey"
        state = {"journey": case["journey"], "answers": dict(case.get("prefill", {})), "asked": [],
                 "pending": None, "known_location": case.get("known_location")}
        body = post_json(url, state)
        problems = []
        for number, step in enumerate(case["steps"], 1):
            if body.get("kind") != "question":
                problems.append(f"step {number}: got {body.get('kind')} instead of a question")
                break
            if body["pending"] != step["slot"]:
                problems.append(f"step {number}: asked {body['pending']!r}, expected {step['slot']!r}")
                break
            if step.get("journey") and body["journey"] != step["journey"]:
                problems.append(f"step {number}: journey {body['journey']}, expected {step['journey']}")
            labels = [option["label"] for option in body.get("options", [])]
            missing = [label for label in step.get("options", []) if label not in labels]
            if missing:
                problems.append(f"step {number}: buttons missing {missing}")
            if step.get("note") and not any(step["note"] in note for note in body.get("notes", [])):
                problems.append(f"step {number}: note {step['note']!r} missing")
            state = {"journey": body["journey"], "answers": body["answers"], "asked": body["asked"],
                     "pending": body["pending"], "text": step.get("text"), "value": step.get("value")}
            body = post_json(url, state)
        expect = case["expect"]
        if not problems:
            if body.get("kind") != expect["kind"]:
                problems.append(f"ended with {body.get('kind')}, expected {expect['kind']}")
            if "first_tool" in expect and body.get("first_tool") != expect["first_tool"]:
                problems.append(f"first tool {body.get('first_tool')}, expected {expect['first_tool']}")
            if expect.get("journey") and body.get("journey") != expect["journey"]:
                problems.append(f"journey {body.get('journey')}, expected {expect['journey']}")
            if expect.get("slot") and body.get("pending") != expect["slot"]:
                problems.append(f"next question {body.get('pending')}, expected {expect['slot']}")
            if expect.get("note") and not any(expect["note"] in note for note in body.get("notes", [])):
                problems.append(f"note {expect['note']!r} missing")
            if "safety" in expect and [n["kind"] for n in body.get("safety", [])] != expect["safety"]:
                problems.append(f"safety {[n['kind'] for n in body.get('safety', [])]}, expected {expect['safety']}")
            missing = [fact for fact in expect.get("request", []) if fact not in body.get("request", "")]
            if missing:
                problems.append(f"request missing {missing}")
        failures += bool(problems)
        print(f"{'ok  ' if not problems else 'FAIL'}  journey: {case['name']}"
              + (f" -- {'; '.join(problems)}" if problems else ""))
    return failures


def check_app(checks: dict, base_url: str, timeout: float) -> int:
    failures = 0
    for case in checks["safety_app"]:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/chat",
            data=json.dumps({"messages": [{"role": "user", "content": case["message"]}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        kinds = [notice["kind"] for notice in body.get("safety") or []]
        problems = []
        if kinds != case["expect"]:
            problems.append(f"safety {kinds} != {case['expect']}")
        if not body.get("message", "").startswith(safety.prefix(case["expect"])):
            problems.append("reply does not start with the help text")
        urls = {a["url"] for notice in body.get("safety") or [] for a in notice["actions"]}
        if "crisis" in case["expect"] and not {"tel:988", "sms:838255"} <= urls:
            problems.append("call/text buttons missing")
        failures += bool(problems)
        print(f"{'ok  ' if not problems else 'FAIL'}  app {case['message']!r}"
              + (f" -- {'; '.join(problems)}" if problems else ""))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checks", default=str(ROOT / "scripts" / "counselor-checks.json"))
    parser.add_argument("--no-app", action="store_true", help="skip the chat (model) checks")
    parser.add_argument("--app-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--links", action="store_true", help="also open every scholarship link")
    parser.add_argument("--library-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    checks = json.loads(Path(args.checks).read_text(encoding="utf-8"))
    if args.library_only:
        return 1 if check_library(checks) else 0
    failures = check_safety(checks)
    failures += check_state_help(checks)
    failures += check_scholarships(checks, args.links)
    failures += check_library(checks)
    failures += check_journeys(checks, args.app_url)
    if not args.no_app:
        failures += check_app(checks, args.app_url, args.timeout)
    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
