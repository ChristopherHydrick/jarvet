"""Routes to training for veterans the VA does not cover (docs/counselor-plan.md,
phase 1a): no GI Bill (never qualified, used up, expired, other-than-honorable
discharge) and no VA disability rating for VR&E.

For a state, lists its vocational rehabilitation (VR) agencies from
data/state-vr-agencies.json (scripts/init-state-vr-agencies.py, from the
Rehabilitation Services Administration's official list) plus the federal
routes open to anyone. State VR uses the state's own disability test, not a VA
rating (34 CFR 361.42). Every fact here is dated and sourced; details beyond
it come from the benefits library (search_benefits_info).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
AGENCIES_FILE = ROOT / "data" / "state-vr-agencies.json"

STATE_VR_RULE = {
    "summary": (
        "State vocational rehabilitation can pay for job training, school, books, tools and job "
        "placement for people with a physical or mental impairment that is a substantial "
        "impediment to getting or keeping a job. The state's own staff decide -- a VA disability "
        "rating is not needed, and the rule has no military-discharge requirement. Anyone "
        "receiving Social Security disability (SSI or SSDI) is presumed eligible. The federal "
        "definition covers any mental or psychological disorder (for example mental illness or a "
        "specific learning disability) and any physical condition affecting a body system (for "
        "example breathing, skin, heart, nerves or joints -- which can include severe allergies "
        "or chronic pain), if it makes getting or keeping a job substantially harder. Substance "
        "use disorders are not named in that definition; ask the state agency how it treats them."
    ),
    "order_of_selection": (
        "When a state lacks money to serve everyone, it runs an 'order of selection': people with "
        "the most significant disabilities are served first and others may wait on a list. "
        "Applying still starts the process, and plans already signed continue."
    ),
    "source": "34 CFR 361.5(c)(40), 361.42 and 361.36 (eCFR, current as of 2026-09)",
    "source_url": "https://www.ecfr.gov/current/title-34/section-361.42",
}

# Current notices from a state's own VR agency, checked by hand (dated).
STATE_NOTES: dict[str, dict[str, str]] = {
    "CA": {
        "note": (
            "The California Department of Rehabilitation (DOR) is in an order of selection: all new "
            "applicants found eligible are placed on a waiting list. DOR still accepts applications, "
            "but not by email or online right now -- apply by phone or in person at a DOR office "
            "(office locator on its Contact Us page). Plans already signed continue."
        ),
        "source": "California DOR, Get Started page (checked 2026-09-25)",
        "source_url": "https://www.dor.ca.gov/Home/GettingStarted",
        "apply_url": "https://www.dor.ca.gov/Home/ContactUs",
    },
}

# State tuition and fee programs, checked by hand against the official source
# (dated). Income limits are the program's own table; compare them with the
# veteran's household size and income before recommending.
STATE_AID: dict[str, list[dict[str, Any]]] = {
    "CA": [
        {
            "name": "California College Promise Grant (CCPG, formerly the BOG Fee Waiver)",
            "what": (
                "Waives enrollment fees at any California community college for eligible students. "
                "It does not cover books or living costs -- the FAFSA or California Dream Act "
                "Application can help with those. Apply through the college's financial aid office; "
                "most waivers are processed within a week, and it must be renewed every school year."
            ),
            "who_can_apply": (
                "California residents (lived in California at least one year), students eligible for "
                "in-state tuition under AB 540 or AB 1899 or with a T or U visa, and California resident "
                "homeless youth."
            ),
            "ways_to_qualify": [
                "Income: household income in 2024 at or under the limit below for the household size "
                "(150% of the federal poverty guideline).",
                "Benefits: currently receiving monthly cash aid from TANF/CalWORKs, SSI/SSP or General "
                "Assistance. (SSI counts here; SSDI does not -- SSDI is counted as income instead.)",
                "Financial need shown on the FAFSA or California Dream Act Application, reviewed by the college.",
                "Special cases, including dependents certified by CalVet or the National Guard Adjutant "
                "General for a dependent's fee waiver, and Medal of Honor recipients and their children.",
            ],
            "veterans_note": (
                "A veteran of the U.S. Armed Forces counts as an independent student on the application, "
                "so the veteran's own household income is used, not their parents'."
            ),
            "income_limits_2026_27": {
                "1": 23475, "2": 31725, "3": 39975, "4": 48225,
                "5": 56475, "6": 64725, "7": 72975, "8": 81225, "each_additional": 8250,
            },
            "source": (
                "California Community Colleges Chancellor's Office, 2026-27 CCPG application and Type B "
                "income standards (5 CCR 58620) (checked 2026-09-25)"
            ),
            "url": "https://www.cccco.edu/-/media/CCCCO-Website/docs/application/2026-27-english-ccpg-application-a11y.pdf",
            "income_limits_url": "https://www.cccco.edu/-/media/CCCCO-Website/docs/general/2026-27-ccpg-income-maximums-a11y.pdf",
        },
    ],
}

FEDERAL_ROUTES = [
    {
        "name": "FAFSA and Pell Grant",
        "what": (
            "The FAFSA is the free federal application for student aid. Pell Grants go to "
            "undergraduate students with exceptional financial need who have not yet earned a "
            "bachelor's degree, and grants do not have to be repaid (Federal Grant Programs guide)."
        ),
        "source": "Federal Student Aid, U.S. Department of Education",
        "url": "https://studentaid.gov/h/apply-for-aid/fafsa",
    },
    {
        "name": "American Job Center (WIOA training funds)",
        "what": (
            "Local American Job Centers help job seekers find training and may pay for approved "
            "programs through WIOA (the federal Workforce Innovation and Opportunity Act) for those "
            "who qualify; the center decides eligibility."
        ),
        "source": "CareerOneStop, U.S. Department of Labor",
        "url": "https://www.careeronestop.org/LocalHelp/AmericanJobCenters/find-american-job-centers.aspx",
    },
    {
        "name": "Apprenticeships (paid training)",
        "what": "Registered apprenticeships pay wages while you learn a trade; no VA benefits are needed.",
        "source": "Apprenticeship.gov, U.S. Department of Labor",
        "url": "https://www.apprenticeship.gov/apprenticeship-job-finder",
    },
    {
        "name": "VA career counseling (Chapter 36)",
        "what": (
            "Free VA educational and career counseling for veterans who separated (other than "
            "dishonorably) within the past year or will within 6 months, or who have a VA education benefit."
        ),
        "source": "VA.gov",
        "url": "https://www.va.gov/careers-employment/education-and-career-counseling/",
    },
]


class StateHelp:
    def __init__(self, path: Path = AGENCIES_FILE) -> None:
        self.path = path
        self.agencies: list[dict[str, Any]] = []
        self.fetched = ""
        self._names: dict[str, str] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.agencies = data["agencies"]
        self.fetched = data.get("fetched", "")
        for agency in self.agencies:
            self._names[agency["state"].lower()] = agency["code"]
            self._names[agency["code"].lower()] = agency["code"]

    def state_code(self, text: str) -> str | None:
        value = re.sub(r"[^a-z ]", "", (text or "").lower()).strip()
        if value in self._names:
            return self._names[value]
        # "San Jose, CA" / "Austin Texas": the last recognizable state wins.
        for piece in reversed(re.split(r"[,\s]+", (text or "").strip())):
            if piece.lower() in self._names:
                return self._names[piece.lower()]
        for name, code in sorted(self._names.items(), key=lambda item: -len(item[0])):
            if len(name) > 2 and name in value:
                return code
        return None

    def routes(self, state: str) -> dict[str, Any]:
        code = self.state_code(state)
        if code is None:
            return {"error": "Unknown state; ask the veteran which state they live in.",
                    "federal_routes": FEDERAL_ROUTES}
        agencies = [
            {key: value for key, value in agency.items() if key not in ("code",)}
            for agency in self.agencies if agency["code"] == code
        ]
        result: dict[str, Any] = {
            "state": agencies[0]["state"] if agencies else code,
            "state_vr_agencies": agencies,
            "state_vr_agencies_source": f"Rehabilitation Services Administration list (fetched {self.fetched})",
            "state_vr_rule": STATE_VR_RULE,
            "federal_routes": FEDERAL_ROUTES,
        }
        if code in STATE_AID:
            result["state_aid_programs"] = STATE_AID[code]
            result["state_aid_note"] = (
                "Other state aid (for example state veteran programs) is not in Jarvet's data yet; "
                "suggest the college's financial aid office and the state veterans agency."
            )
        else:
            result["state_aid"] = (
                "State tuition and fee programs (for example community college fee waivers and state "
                "veteran programs) are not in Jarvet's data yet; suggest the college's financial aid "
                "office and the state veterans agency."
            )
        if code in STATE_NOTES:
            result["state_vr_current_notice"] = STATE_NOTES[code]
        return result
