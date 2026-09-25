"""Education pathways: the programs a search found, arranged as a ladder of
credential levels (certificate -> associate -> bachelor's -> graduate) so a
veteran can see how one credential leads to the next -- for example Army 68W
(Combat Medic): EMT or LVN certificate -> associate degree in nursing (RN) ->
BSN -> MSN.

The level comes from the program title's own words (CERT, AAS, BSN, MASTER
OF...), not from its field of study: the field codes say what a program is
about, not its level (Palo Alto College's "AAS NURSING" is filed under
Nursing Science, a bachelor's-style field). Titles with no level word (about
16% of college programs, e.g. "NURSING - GENERIC") are left off the ladder;
their schools' cards still show them."""
from __future__ import annotations

import re
from typing import Any

LEVELS = ["certificate", "associate", "bachelor", "graduate"]
LEVEL_TEXT = {
    "certificate": {
        "title": "Certificate",
        "blurb": "A few months to a year. The fastest way to start working.",
    },
    "associate": {
        "title": "Associate degree",
        "blurb": "About 2 years full time, often at a community college.",
    },
    "bachelor": {
        "title": "Bachelor's degree",
        "blurb": "About 4 years, or about 2 more after an associate degree.",
    },
    "graduate": {
        "title": "Graduate degree (optional)",
        "blurb": "A master's degree, graduate certificate or doctorate, after a bachelor's.",
    },
}

# Level words that are safe anywhere in a title.
_GRADUATE_WORDS = {
    "MASTER", "MASTERS", "DOCTOR", "DOCTORATE", "DOCTORAL", "PHD", "DNP", "MSN", "MBA",
    "MPH", "MSW", "EDD", "DPT", "JD", "GRADUATE", "POSTBACCALAUREATE", "POSTMASTERS",
}
_BACHELOR_WORDS = {
    "BACHELOR", "BACHELORS", "BACCALAUREATE", "BSN", "ABSN", "BBA", "BFA", "BSW",
}
_ASSOCIATE_WORDS = {"ASSOCIATE", "ASSOCIATES", "AAS", "ADN", "AAT", "AOS"}
_CERTIFICATE_WORDS = {"CERT", "CERTIFICATE", "DIPLOMA"}
# Short degree abbreviations that are also ordinary words or parts of other
# words ("ENGLISH AS A SECOND LANGUAGE", "MED ASSIST") -- only trusted as a
# title's first or last word, where VA puts the degree ("BS NURSING").
_EDGE_WORDS = {
    "MS": "graduate", "MA": "graduate", "MED": "graduate", "BS": "bachelor",
    "BA": "bachelor", "AS": "associate", "AA": "associate",
}
# Programs for people who already hold an earlier credential ("LVN TO RN",
# "RN TO BSN", "LPN LVN TO ADN BRIDGE OPT", "NURSING CAREER MOB LVN-RN").
# Matched against _words() output, where hyphens are already spaces.
_BRIDGE = re.compile(
    r"\b(LVN|LPN|RN|ADN|BSN|MSN)\s+(TO\s+)?(RN|ADN|BSN|MSN|DNP)\b"
    r"|\bBRIDGE\b|\bCAREER MOB|\bCOMPLETION\b"
)
_MILITARY = re.compile(r"\b(MIL|MILITARY|VETERANS?)\b")
_ACCELERATED = re.compile(r"\b(ABSN|ACCELERATED)\b")
# A bridge into nursing licensure belongs on the Registered Nurse track even
# when VA's catalog filed it under the starting credential (St. Philip's
# "AAS NURSING CAREER MOBILITY LVN TO RN" sits in vocational nursing).
_RN_TARGET = re.compile(r"\b(LVN|LPN)\s+(TO\s+)?(RN|ADN|BSN)\b|\bTO\s+(RN|ADN|BSN)\b")
REGISTERED_NURSE = "29-1141"


def _words(description: str) -> list[str]:
    return re.sub(r"[^A-Z0-9]+", " ", description.upper().replace("'", "")).split()


def credential_level(description: str, program_type: str) -> str | None:
    """certificate / associate / bachelor / graduate, or None when the
    title says nothing about it. VA's non-college-degree programs (NCD,
    flight, correspondence, OJT) are certificates whatever the title says."""
    if program_type != "IHL":
        return "certificate"
    words = _words(description)
    if not words:
        return None
    text = " ".join(words)
    found = set(words)
    if found & _GRADUATE_WORDS or "GRAD CERT" in text or "POST MASTER" in text:
        return "graduate"
    if found & _BACHELOR_WORDS:
        return "bachelor"
    if found & _ASSOCIATE_WORDS:
        return "associate"
    for word in (words[0], words[-1]):
        if word in _EDGE_WORDS:
            return _EDGE_WORDS[word]
    if found & _CERTIFICATE_WORDS:
        return "certificate"
    return None


def program_tags(description: str) -> list[str]:
    text = " ".join(_words(description))
    tags = []
    if _BRIDGE.search(text):
        tags.append("bridge")
    if _MILITARY.search(text):
        tags.append("military")
    if _ACCELERATED.search(text):
        tags.append("accelerated")
    return tags


def build_pathway(
    rows: list[tuple[str, str, str, float, str, str]],
    facilities: list[dict[str, Any]],
    *,
    track_careers: dict[str, list[str]],
    career_titles: dict[str, str],
    primary_careers: set[str],
    min_score: dict[str, float],
    same_school_min_score: float,
    heading: str,
    location_label: str | None,
) -> dict[str, Any] | None:
    """The ladder for the schools in facilities (the cards the search is
    showing), from rows = (facility_code, description, cip, score, method,
    program_type) of their programs in the search's fields.

    track_careers maps each field (CIP) to the careers it leads to among the
    ones the search is about, most relevant first; each program goes on the
    track of its field's first career, so "AAS NURSING" (Nursing Science)
    and "AAS NURSING GENERIC" (Registered Nursing) share the Registered
    Nurses track. Tracks leading to primary_careers (the military job's own
    civilian careers) come first in each step.

    A program is included when its field score clears min_score, or clears
    the lower same_school_min_score at a school that already has a confident
    program on the same track -- titles like "AAS NURSING CAREER MOB LVN-RN
    MIL- RN" score low against any field name but are plainly nursing at a
    school that teaches nursing. None when fewer than two levels are found."""
    by_code = {facility["facility_code"]: facility for facility in facilities}

    def track_of(cip: str, description: str) -> tuple[str, ...]:
        careers = tuple((track_careers.get(cip) or [f"cip:{cip}"])[:1])
        if (
            REGISTERED_NURSE in career_titles and REGISTERED_NURSE not in careers
            and _RN_TARGET.search(" ".join(_words(description)))
        ):
            return (REGISTERED_NURSE,)
        return careers

    candidates = []
    confident_tracks: set[tuple[str, tuple[str, ...]]] = set()
    for facility_code, description, cip, score, method, program_type in rows:
        if facility_code not in by_code:
            continue
        track = track_of(cip, description)
        confident = score >= min_score.get(method, 1)
        if confident:
            confident_tracks.add((facility_code, track))
        candidates.append((facility_code, description, cip, score, program_type, track, confident))

    steps: dict[str, dict[tuple[str, ...], dict[str, dict[str, Any]]]] = {}
    school_levels: dict[str, dict[str, list[str]]] = {}
    seen: set[tuple[str, str]] = set()
    for facility_code, description, cip, score, program_type, track, confident in candidates:
        if not confident and (
            score < same_school_min_score or (facility_code, track) not in confident_tracks
        ):
            continue
        if (facility_code, description) in seen:
            continue
        seen.add((facility_code, description))
        level = credential_level(description, program_type)
        if level is None:
            continue
        facility = by_code[facility_code]
        school = steps.setdefault(level, {}).setdefault(track, {}).setdefault(facility_code, {
            "facility_code": facility_code,
            # Title case like the cards' headings, so the page can find a
            # school's card by name.
            "institution": str(facility["institution"]).title(),
            "distance_miles": facility.get("distance_miles"),
            "programs": [],
        })
        school["programs"].append({"description": description, "tags": program_tags(description)})
        school_levels.setdefault(facility_code, {}).setdefault(level, []).append(description)

    if len(steps) < 2:
        return None

    def track_title(track: tuple[str, ...]) -> str:
        if track[0].startswith("cip:"):
            return ""
        title = career_titles.get(track[0], track[0])
        # "Computer Occupations, All Other" -> "Other computer occupations"
        if title.endswith(", All Other"):
            title = "Other " + title[: -len(", All Other")].lower()
        return title

    def school_order(school: dict[str, Any]) -> tuple[float, str]:
        distance = school.get("distance_miles")
        return (distance if distance is not None else 1e9, school["institution"])

    result_steps = []
    for level in LEVELS:
        if level not in steps:
            continue
        tracks = []
        for track, schools in steps[level].items():
            ordered = sorted(schools.values(), key=school_order)
            for school in ordered:
                school["programs"].sort(key=lambda program: program["description"])
            tracks.append({
                "title": track_title(track), "schools": ordered,
                "primary": bool(primary_careers & set(track)),
            })
        # The job's own career first, then the biggest tracks.
        tracks.sort(key=lambda item: (not item["primary"], -len(item["schools"]), item["title"]))
        result_steps.append({"level": level, **LEVEL_TEXT[level], "tracks": tracks})

    same_school = []
    for facility_code, levels in school_levels.items():
        if len(levels) < 2:
            continue
        facility = by_code[facility_code]
        same_school.append({
            "facility_code": facility_code,
            "institution": str(facility["institution"]).title(),
            "distance_miles": facility.get("distance_miles"),
            "levels": [LEVEL_TEXT[level]["title"] for level in LEVELS if level in levels],
        })
    same_school.sort(key=lambda item: (-len(item["levels"]), *school_order(item)))

    return {
        "heading": heading,
        "intro": (
            "Each step below is a real VA-approved credential"
            + (f" near {location_label}" if location_label else "")
            + ". Many people earn one, work for a while, then come back for the next."
        ),
        "steps": result_steps,
        "same_school": same_school[:5],
        "advice": (
            "Ask each school how much credit it gives for your military training and "
            "whether credits from an earlier step transfer. GI Bill months are limited, so a "
            "shorter bridge program can save them."
        ),
    }


def summary_for_model(pathway: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the chat model needs to know about the pathway section (which the
    page shows on its own): which levels exist, not every school again."""
    if not pathway:
        return None
    return {
        "levels_shown": [step["title"] for step in pathway["steps"]],
        "schools_offering_several_levels": [
            item["institution"] for item in pathway["same_school"]
        ],
    }
