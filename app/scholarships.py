"""Scholarships for veterans, service members and their families
(data/scholarships.json, hand-checked on each sponsor's page), plus a link to
the Department of Labor's CareerOneStop Scholarship Finder for everything
else (~9,500 awards; it has no public API, so Jarvet links to a search
already filtered by keyword) and the FTC's scam warning.

Matching is by rules, not the model: who the student is (veteran, service
member, spouse, surviving spouse, child, grandchild), the military member's
situation for family members (serving, retired, veteran, disabled, fallen),
branch, and level of study. Unknown answers keep a scholarship in the list;
it is only dropped when an answer rules it out.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
SCHOLARSHIPS_FILE = ROOT / "data" / "scholarships.json"

STUDENTS = ("veteran", "service_member", "spouse", "surviving_spouse", "child", "grandchild")
SITUATIONS = ("serving", "retired", "veteran", "disabled", "fallen")
LEVELS = ("certificate", "undergraduate", "graduate")
BRANCHES = ("Army", "Navy", "Air Force", "Marine Corps", "Coast Guard", "Space Force")

# Keyword for the CareerOneStop Scholarship Finder link, by student.
FINDER_KEYWORDS = {
    "veteran": "veteran",
    "service_member": "military",
    "spouse": "military spouse",
    "surviving_spouse": "military spouse",
    "child": "military dependent",
    "grandchild": "military dependent",
}


def _normalized_student(value: str) -> str | None:
    text = (value or "").lower().replace("-", " ").replace("_", " ")
    for pattern, student in (
        (r"surviv|widow", "surviving_spouse"), (r"grand", "grandchild"),
        (r"spouse|wife|husband", "spouse"), (r"child|son|daughter|dependent|kid", "child"),
        (r"service ?member|active|serving|guard|reserv", "service_member"), (r"veteran|vet\b", "veteran"),
    ):
        if re.search(pattern, text):
            return student
    return None


def _normalized_branch(value: str) -> str | None:
    text = (value or "").lower()
    for branch in BRANCHES:
        if branch.lower() in text:
            return branch
    if "marine" in text or "usmc" in text:
        return "Marine Corps"
    return None


class Scholarships:
    def __init__(self, path: Path = SCHOLARSHIPS_FILE) -> None:
        self.path = path
        self.data: dict[str, Any] = {}

    def load(self) -> None:
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def entries(self) -> list[dict[str, Any]]:
        return self.data.get("scholarships", [])

    def finder_link(self, student: str | None, keyword: str | None = None) -> dict[str, str]:
        words = keyword or FINDER_KEYWORDS.get(student or "", "veteran")
        finder = self.data["finder"]
        return {
            "label": f"Search all scholarships for \"{words}\" (CareerOneStop Scholarship Finder)",
            "url": f"{finder['url']}?keyword={quote_plus(words)}",
            "note": finder["note"],
        }

    def find(
        self, *, student: str = "", situation: str = "", branch: str = "", level: str = "",
        field: str = "", has_va_benefit: bool | None = None,
    ) -> dict[str, Any]:
        who = _normalized_student(student)
        branch_name = _normalized_branch(branch)
        situation = situation if situation in SITUATIONS else ""
        level = level if level in LEVELS else ""
        family = who in ("spouse", "surviving_spouse", "child", "grandchild")
        if who == "surviving_spouse" and not situation:
            situation = "fallen"
        matches = []
        for entry in self.entries:
            if who and who not in entry["students"]:
                continue
            if family and situation and entry["situations"] and situation not in entry["situations"]:
                continue
            if branch_name and entry["branches"] and branch_name not in entry["branches"]:
                continue
            if level and level not in entry["levels"]:
                continue
            if has_va_benefit is False and entry.get("requires_va_benefit"):
                continue
            score = 0
            if family and situation and situation in entry["situations"]:
                score += 2  # made for this family's situation
            if branch_name and branch_name in entry["branches"]:
                score += 2  # branch-specific help
            if field and any(word in field.lower() for f in entry["fields"] for word in f.split()):
                score += 1
            if entry["fields"] and field and score == 0:
                score -= 1
            matches.append((score, entry))
        matches.sort(key=lambda item: -item[0])
        found = [
            {key: entry[key] for key in (
                "name", "sponsor", "url", "amount", "timing", "rules", "need_based", "checked",
            )}
            | ({"only_if": "the family member's situation is " + " or ".join(entry["situations"])}
               if family and not situation and entry["situations"] else {})
            | ({"only_for_branches": entry["branches"]} if not branch_name and entry["branches"] else {})
            for _, entry in matches[:12]
        ]
        return {
            "scholarships": found,
            "total_matching": len(matches),
            "unknown": [name for name, value in (
                ("who the student is", who), ("branch", branch_name), ("level of study", level),
            ) + ((("the military member's situation", situation),) if family else ()) if not value],
            "all_scholarships": self.finder_link(who),
            "scam_warning": self.data["scam_warning"],
            "also": (
                "The school's financial aid office knows its own scholarships and state grants, and "
                "its veterans or military resource office often knows local veteran scholarships."
            ),
            "list_checked": self.data.get("checked"),
            "say": (
                "Amounts and dates change every year -- confirm on each sponsor's site before applying. "
                "Scholarships are free money that doesn't have to be paid back."
            ),
        }
