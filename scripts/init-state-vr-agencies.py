"""Build data/state-vr-agencies.json: every state vocational rehabilitation
(VR) agency, from the Rehabilitation Services Administration's official list
(https://rsa.ed.gov/about/states -- 78 agencies in the 50 states, DC, Puerto
Rico and 4 territories; 22 states have a separate agency for people who are
blind or have low vision).

State VR is the route to training for veterans the VA does not cover: its
disability test is the state's own (34 CFR 361.42 -- any physical or mental
impairment that is a substantial impediment to employment), not a VA rating
(docs/counselor-plan.md, phase 1a). Jarvet's find_state_help tool reads the
JSON file. No database is touched; re-run yearly (or from the monthly refresh):

  python scripts/init-state-vr-agencies.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "https://rsa.ed.gov/about/states"
OUT = ROOT / "data" / "state-vr-agencies.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36 Jarvet"

STATES = {
    "Alabama": "AL", "Alaska": "AK", "American Samoa": "AS", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Guam": "GU", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Northern Marianas": "MP", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virgin Islands": "VI", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
FIELD = re.compile(r"^(Phone|Toll[- ]?Free|TTY|Video Phone|VP|Website|Email|Fax):\s*(.*)$", re.S | re.I)


def main() -> int:
    request = urllib.request.Request(SOURCE, headers={"User-Agent": USER_AGENT})
    raw = urllib.request.urlopen(request, timeout=60).read().decode("utf-8", "replace")
    start = raw.find("Contact information for the")
    if start < 0:
        sys.exit("RSA page layout changed: agency list not found")
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw[start:], flags=re.S)
    # Every tag except links becomes a separator; links keep their href.
    body = re.sub(r"<(?!a[\s>]|/a>)[^>]+>", "|", body)
    tokens = [html.unescape(re.sub(r"\s+", " ", t)).strip() for t in body.split("|")]
    tokens = [t for t in tokens if t]

    agencies: list[dict] = []
    state = None
    for token in tokens:
        text = re.sub(r"<[^>]+>", "", token).strip()
        if text in STATES:
            state = text
            continue
        if state is None:
            continue
        field = FIELD.match(text)
        if field and agencies and agencies[-1]["state"] == state:
            name, value = field.group(1), field.group(2).strip()
            if name.lower() == "website":
                link = re.search(r'href="([^"]+)"', token)
                agencies[-1]["website"] = link.group(1) if link else value
            else:
                key = {"phone": "phone", "tty": "tty", "video phone": "video_phone", "vp": "video_phone",
                       "email": "email", "fax": "fax"}.get(name.lower(), "toll_free")
                agencies[-1][key] = value
            continue
        if text.startswith(("Contact information", "Connect with")) or len(text) > 140:
            continue
        kind = "combined"
        match = re.match(rf"{re.escape(state)}\s*-\s*(General|Blind)\s*:\s*(.+)$", text)
        if match:
            kind, text = match.group(1).lower(), match.group(2).strip()
        if FIELD.match(text):
            continue
        agencies.append({"state": state, "code": STATES[state], "type": kind, "name": text})

    # Stop at the end of the list (the page footer has no more states).
    agencies = [a for a in agencies if a.get("website") or a.get("phone")]
    result = {
        "source": SOURCE,
        "fetched": dt.date.today().isoformat(),
        "about": ("State vocational rehabilitation agencies. 'combined' serves all disabilities; "
                  "where a state has two, 'general' serves all but blindness/low vision and "
                  "'blind' serves blindness/low vision."),
        "agencies": agencies,
    }
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    states = {a["state"] for a in agencies}
    print(f"{len(agencies)} agencies in {len(states)} states/territories -> {OUT}")
    missing = sorted(set(STATES) - states)
    if missing:
        print("No agency found for:", ", ".join(missing))
    return 0 if len(agencies) >= 70 else 1


if __name__ == "__main__":
    sys.exit(main())
