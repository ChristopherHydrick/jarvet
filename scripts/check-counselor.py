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
    parser.add_argument("--library-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    checks = json.loads(Path(args.checks).read_text(encoding="utf-8"))
    if args.library_only:
        return 1 if check_library(checks) else 0
    failures = check_safety(checks)
    failures += check_library(checks)
    if not args.no_app:
        failures += check_app(checks, args.app_url, args.timeout)
    print(f"\n{'All checks passed.' if not failures else f'{failures} check(s) failed.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
