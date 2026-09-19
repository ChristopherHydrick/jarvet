"""Authoritative apply-page override for California Community Colleges.

scripts/init-va-admissions-guesses.py and init-va-apply-path-guesses.py
crawl each school's own homepage for something that looks like an apply
link, which is inherently a guess. That guess can be wrong in a way that's
hard to catch generically: on sdmiramar.edu, the crawler picked up
"Important Transfer Deadline... the deadline to apply for graduation" --
a real homepage link containing "apply", but about graduating, not
enrolling.

Every California Community College actually shares ONE real application
system, OpenCCCApply, keyed by a fixed per-college "MIS code" the CCC
Chancellor's Office assigns -- e.g. San Diego Miramar College is
https://www.opencccapply.net/gateway/apply?cccMisCode=073. That's an
authoritative source for these 115 schools, not a guess, so it overrides
whatever init-va-admissions-guesses.py / init-va-apply-path-guesses.py
found (or didn't) for the same facility_code.

CCC_APPLY_URLS below maps a VA facility_code straight to its OpenCCCApply
URL (matched by hand against VA's own facility names -- see conversation
notes for the handful of colleges whose short/common name is genuinely
ambiguous, like "Mission College" in Santa Clara vs. "Los Angeles Mission
College", or "West Valley College" vs. "West Los Angeles College").
8 CCC colleges are deliberately left out: continuing-education/adult-school
listings (e.g. "San Francisco Ctrs", "Rancho Santiago CED") and Calbright
College, none of which have their own distinct degree-granting VA facility
record to attach an apply URL to.

Since this is a small, fixed, publicly stable dataset (California's
72 community college districts don't reorganize often), it's embedded
directly here rather than crawled -- re-run this after scripts/init-va-data.py
rebuilds the database from scratch, since that wipes va_admissions_guesses.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / ".cache" / "va-comparison.sqlite"

CCC_APPLY_URLS: dict[str, str] = {
    "14971405": "https://www.opencccapply.net/gateway/apply?cccMisCode=021",  # CUYAMACA COLLEGE
    "14970405": "https://www.opencccapply.net/gateway/apply?cccMisCode=022",  # GROSSMONT COLLEGE
    "14983405": "https://www.opencccapply.net/gateway/apply?cccMisCode=031",  # IMPERIAL VALLEY COLLEGE
    "11116405": "https://www.opencccapply.net/gateway/apply?cccMisCode=051",  # MIRACOSTA COLLEGE
    "14981405": "https://www.opencccapply.net/gateway/apply?cccMisCode=061",  # PALOMAR COLLEGE
    "11000505": "https://www.opencccapply.net/gateway/apply?cccMisCode=071",  # SAN DIEGO CITY COLLEGE
    "11000005": "https://www.opencccapply.net/gateway/apply?cccMisCode=072",  # SAN DIEGO MESA COLLEGE
    "14985405": "https://www.opencccapply.net/gateway/apply?cccMisCode=073",  # SAN DIEGO MIRAMAR COLLEGE
    "14945405": "https://www.opencccapply.net/gateway/apply?cccMisCode=091",  # SOUTHWESTERN COMMUNITY COLLEGE
    "14916405": "https://www.opencccapply.net/gateway/apply?cccMisCode=111",  # BUTTE COLLEGE
    "11118005": "https://www.opencccapply.net/gateway/apply?cccMisCode=121",  # FEATHER RIVER COLLEGE
    "149A2405": "https://www.opencccapply.net/gateway/apply?cccMisCode=131",  # LASSEN COMMUNITY COLLEGE
    "14984405": "https://www.opencccapply.net/gateway/apply?cccMisCode=141",  # MENDOCINO COLLEGE
    "14908105": "https://www.opencccapply.net/gateway/apply?cccMisCode=161",  # COLLEGE OF THE REDWOODS
    "11117805": "https://www.opencccapply.net/gateway/apply?cccMisCode=171",  # SHASTA COLLEGE
    "14941105": "https://www.opencccapply.net/gateway/apply?cccMisCode=181",  # COLLEGE OF THE SISKIYOUS
    "14909405": "https://www.opencccapply.net/gateway/apply?cccMisCode=221",  # LAKE TAHOE COMMUNITY COLLEGE
    "14994405": "https://www.opencccapply.net/gateway/apply?cccMisCode=231",  # AMERICAN RIVER COLLEGE
    "14969405": "https://www.opencccapply.net/gateway/apply?cccMisCode=232",  # COSUMNES RIVER COLLEGE
    "14993405": "https://www.opencccapply.net/gateway/apply?cccMisCode=233",  # SACRAMENTO CITY COLLEGE
    "149A5405": "https://www.opencccapply.net/gateway/apply?cccMisCode=234",  # FOLSOM LAKE COLLEGE
    "14917405": "https://www.opencccapply.net/gateway/apply?cccMisCode=241",  # NAPA VALLEY COLLEGE
    "14925405": "https://www.opencccapply.net/gateway/apply?cccMisCode=261",  # SANTA ROSA JUNIOR COLLEGE
    "14955105": "https://www.opencccapply.net/gateway/apply?cccMisCode=271",  # SIERRA COLLEGE
    "11116505": "https://www.opencccapply.net/gateway/apply?cccMisCode=281",  # SOLANO COMMUNITY COLLEGE
    "14935405": "https://www.opencccapply.net/gateway/apply?cccMisCode=291",  # YUBA COLLEGE
    "149A6405": "https://www.opencccapply.net/gateway/apply?cccMisCode=292",  # WOODLAND COMMUNITY COLLEGE
    "14978405": "https://www.opencccapply.net/gateway/apply?cccMisCode=311",  # CONTRA COSTA COLLEGE
    "14944105": "https://www.opencccapply.net/gateway/apply?cccMisCode=312",  # DIABLO VALLEY COLLEGE
    "14912405": "https://www.opencccapply.net/gateway/apply?cccMisCode=313",  # LOS MEDANOS COLLEGE
    "14926105": "https://www.opencccapply.net/gateway/apply?cccMisCode=334",  # COLLEGE OF MARIN
    "14930105": "https://www.opencccapply.net/gateway/apply?cccMisCode=341",  # COLLEGE OF ALAMEDA
    "14933105": "https://www.opencccapply.net/gateway/apply?cccMisCode=343",  # LANEY COLLEGE
    "14932405": "https://www.opencccapply.net/gateway/apply?cccMisCode=344",  # MERRITT COLLEGE
    "14904405": "https://www.opencccapply.net/gateway/apply?cccMisCode=345",  # BERKELEY CITY COLLEGE
    "14907105": "https://www.opencccapply.net/gateway/apply?cccMisCode=361",  # CITY COLLEGE OF SAN FRANCISCO
    "14979405": "https://www.opencccapply.net/gateway/apply?cccMisCode=371",  # CANADA COLLEGE
    "14915105": "https://www.opencccapply.net/gateway/apply?cccMisCode=372",  # COLLEGE OF SAN MATEO
    "11117405": "https://www.opencccapply.net/gateway/apply?cccMisCode=373",  # SKYLINE COLLEGE
    "14974405": "https://www.opencccapply.net/gateway/apply?cccMisCode=411",  # CABRILLO COLLEGE
    "11001405": "https://www.opencccapply.net/gateway/apply?cccMisCode=421",  # DE ANZA COLLEGE
    "11118205": "https://www.opencccapply.net/gateway/apply?cccMisCode=422",  # FOOTHILL COLLEGE
    "14980405": "https://www.opencccapply.net/gateway/apply?cccMisCode=431",  # OHLONE COLLEGE
    "14905405": "https://www.opencccapply.net/gateway/apply?cccMisCode=441",  # GAVILAN COLLEGE
    "14966405": "https://www.opencccapply.net/gateway/apply?cccMisCode=451",  # HARTNELL COLLEGE
    "14923105": "https://www.opencccapply.net/gateway/apply?cccMisCode=461",  # MONTEREY PENINSULA COLLEGE
    "14940105": "https://www.opencccapply.net/gateway/apply?cccMisCode=471",  # EVERGREEN VALLEY COLLEGE
    "14951405": "https://www.opencccapply.net/gateway/apply?cccMisCode=472",  # SAN JOSE CITY COLLEGE
    "14954105": "https://www.opencccapply.net/gateway/apply?cccMisCode=481",  # LAS POSITAS COLLEGE
    "14919105": "https://www.opencccapply.net/gateway/apply?cccMisCode=482",  # CHABOT COLLEGE (in Hayward, CA)
    "14990405": "https://www.opencccapply.net/gateway/apply?cccMisCode=492",  # MISSION COLLEGE (Santa Clara -- not LA Mission College)
    "149A4405": "https://www.opencccapply.net/gateway/apply?cccMisCode=493",  # WEST VALLEY COLLEGE (Saratoga -- not West LA College)
    "11117605": "https://www.opencccapply.net/gateway/apply?cccMisCode=521",  # BAKERSFIELD COLLEGE
    "14988405": "https://www.opencccapply.net/gateway/apply?cccMisCode=522",  # CERRO COSO COMMUNITY COLLEGE
    "14927405": "https://www.opencccapply.net/gateway/apply?cccMisCode=523",  # PORTERVILLE COLLEGE
    "14910405": "https://www.opencccapply.net/gateway/apply?cccMisCode=531",  # MERCED COLLEGE
    "14903105": "https://www.opencccapply.net/gateway/apply?cccMisCode=551",  # SAN JOAQUIN DELTA COLLEGE
    "14942105": "https://www.opencccapply.net/gateway/apply?cccMisCode=561",  # COLLEGE OF THE SEQUOIAS
    "11001005": "https://www.opencccapply.net/gateway/apply?cccMisCode=571",  # FRESNO CITY COLLEGE
    "14937405": "https://www.opencccapply.net/gateway/apply?cccMisCode=572",  # REEDLEY COLLEGE
    "14131805": "https://www.opencccapply.net/gateway/apply?cccMisCode=574",  # MADERA COMMUNITY COLLEGE
    "14131505": "https://www.opencccapply.net/gateway/apply?cccMisCode=576",  # CLOVIS COMMUNITY COLLEGE-FRESNO
    "14131605": "https://www.opencccapply.net/gateway/apply?cccMisCode=581",  # WEST HILLS COLLEGE COALINGA
    "14991405": "https://www.opencccapply.net/gateway/apply?cccMisCode=582",  # LEMOORE COLLEGE
    "14911405": "https://www.opencccapply.net/gateway/apply?cccMisCode=591",  # COLUMBIA COLLEGE
    "11000405": "https://www.opencccapply.net/gateway/apply?cccMisCode=592",  # MODESTO JUNIOR COLLEGE
    "14964405": "https://www.opencccapply.net/gateway/apply?cccMisCode=611",  # ALLAN HANCOCK COLLEGE
    "11116605": "https://www.opencccapply.net/gateway/apply?cccMisCode=621",  # ANTELOPE VALLEY COLLEGE
    "14948105": "https://www.opencccapply.net/gateway/apply?cccMisCode=641",  # CUESTA COLLEGE
    "14959405": "https://www.opencccapply.net/gateway/apply?cccMisCode=651",  # SANTA BARBARA CITY COLLEGE
    "14998405": "https://www.opencccapply.net/gateway/apply?cccMisCode=661",  # COLLEGE OF THE CANYONS
    "11000805": "https://www.opencccapply.net/gateway/apply?cccMisCode=681",  # MOORPARK COLLEGE
    "11001205": "https://www.opencccapply.net/gateway/apply?cccMisCode=682",  # OXNARD COLLEGE
    "14996405": "https://www.opencccapply.net/gateway/apply?cccMisCode=683",  # VENTURA COLLEGE
    "14975405": "https://www.opencccapply.net/gateway/apply?cccMisCode=691",  # TAFT COLLEGE
    "14131705": "https://www.opencccapply.net/gateway/apply?cccMisCode=711",  # COMPTON COLLEGE
    "11000605": "https://www.opencccapply.net/gateway/apply?cccMisCode=721",  # EL CAMINO COLLEGE
    "14999405": "https://www.opencccapply.net/gateway/apply?cccMisCode=731",  # GLENDALE COMMUNITY COLLEGE
    "149A1405": "https://www.opencccapply.net/gateway/apply?cccMisCode=741",  # LOS ANGELES CITY COLLEGE
    "14900405": "https://www.opencccapply.net/gateway/apply?cccMisCode=742",  # LOS ANGELES HARBOR COLLEGE
    "14938405": "https://www.opencccapply.net/gateway/apply?cccMisCode=743",  # LOS ANGELES MISSION COLLEGE
    "14976405": "https://www.opencccapply.net/gateway/apply?cccMisCode=744",  # LOS ANGELES PIERCE COLLEGE
    "14901405": "https://www.opencccapply.net/gateway/apply?cccMisCode=745",  # LOS ANGELES SOUTHWEST COLLEGE
    "14931405": "https://www.opencccapply.net/gateway/apply?cccMisCode=746",  # LOS ANGELES TRADE TECHNICAL COLLEGE
    "11001105": "https://www.opencccapply.net/gateway/apply?cccMisCode=747",  # LOS ANGELES VALLEY COLLEGE
    "14948405": "https://www.opencccapply.net/gateway/apply?cccMisCode=748",  # EAST LOS ANGELES COLLEGE
    "11000305": "https://www.opencccapply.net/gateway/apply?cccMisCode=749",  # WEST LOS ANGELES COLLEGE
    "14961405": "https://www.opencccapply.net/gateway/apply?cccMisCode=771",  # PASADENA CITY COLLEGE
    "11117105": "https://www.opencccapply.net/gateway/apply?cccMisCode=781",  # SANTA MONICA COLLEGE
    "11001305": "https://www.opencccapply.net/gateway/apply?cccMisCode=811",  # CERRITOS COLLEGE
    "14902405": "https://www.opencccapply.net/gateway/apply?cccMisCode=821",  # CITRUS COLLEGE
    "14949405": "https://www.opencccapply.net/gateway/apply?cccMisCode=831",  # COASTLINE COLLEGE
    "14952405": "https://www.opencccapply.net/gateway/apply?cccMisCode=832",  # GOLDEN WEST COLLEGE
    "14928405": "https://www.opencccapply.net/gateway/apply?cccMisCode=833",  # ORANGE COAST COLLEGE
    "14965405": "https://www.opencccapply.net/gateway/apply?cccMisCode=841",  # LONG BEACH CITY COLLEGE
    "14977405": "https://www.opencccapply.net/gateway/apply?cccMisCode=851",  # MT SAN ANTONIO COLLEGE
    "11950405": "https://www.opencccapply.net/gateway/apply?cccMisCode=861",  # CYPRESS COLLEGE
    "14982405": "https://www.opencccapply.net/gateway/apply?cccMisCode=862",  # FULLERTON COLLEGE
    "11117305": "https://www.opencccapply.net/gateway/apply?cccMisCode=871",  # SANTA ANA COLLEGE
    "14131205": "https://www.opencccapply.net/gateway/apply?cccMisCode=873",  # SANTIAGO CANYON COLLEGE
    "11116805": "https://www.opencccapply.net/gateway/apply?cccMisCode=881",  # RIO HONDO COLLEGE
    "149A0405": "https://www.opencccapply.net/gateway/apply?cccMisCode=891",  # SADDLEBACK COLLEGE
    "14936405": "https://www.opencccapply.net/gateway/apply?cccMisCode=892",  # IRVINE VALLEY COLLEGE
    "14939405": "https://www.opencccapply.net/gateway/apply?cccMisCode=911",  # BARSTOW COMMUNITY COLLEGE
    "14956405": "https://www.opencccapply.net/gateway/apply?cccMisCode=921",  # CHAFFEY COLLEGE
    "14800005": "https://www.opencccapply.net/gateway/apply?cccMisCode=931",  # COLLEGE OF THE DESERT
    "14972405": "https://www.opencccapply.net/gateway/apply?cccMisCode=941",  # MT SAN JACINTO COMMUNITY COLLEGE DISTRICT
    "14957405": "https://www.opencccapply.net/gateway/apply?cccMisCode=951",  # PALO VERDE COLLEGE
    "14995405": "https://www.opencccapply.net/gateway/apply?cccMisCode=961",  # RIVERSIDE CITY COLLEGE
    "14130905": "https://www.opencccapply.net/gateway/apply?cccMisCode=962",  # MORENO VALLEY COLLEGE
    "14130705": "https://www.opencccapply.net/gateway/apply?cccMisCode=963",  # NORCO COLLEGE
    "149A3405": "https://www.opencccapply.net/gateway/apply?cccMisCode=971",  # COPPER MOUNTAIN COLLEGE
    "14960405": "https://www.opencccapply.net/gateway/apply?cccMisCode=981",  # CRAFTON HILLS COLLEGE
    "14958405": "https://www.opencccapply.net/gateway/apply?cccMisCode=982",  # SAN BERNARDINO VALLEY COLLEGE
    "14973405": "https://www.opencccapply.net/gateway/apply?cccMisCode=991",  # VICTOR VALLEY COLLEGE
}


def main() -> None:
    if not DATABASE.exists():
        raise SystemExit("VA Comparison Tool index is missing. Run scripts/init-va-data.py first.")
    connection = sqlite3.connect(DATABASE)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS va_admissions_guesses ("
            "facility_code TEXT PRIMARY KEY, url TEXT NOT NULL, label TEXT NOT NULL, "
            "fetched_at INTEGER NOT NULL)"
        )
        now = int(time.time())
        connection.executemany(
            "INSERT OR REPLACE INTO va_admissions_guesses VALUES (?, ?, ?, ?)",
            [(code, url, "Apply (CCCApply)", now) for code, url in CCC_APPLY_URLS.items()],
        )
        connection.commit()
        print(f"Set {len(CCC_APPLY_URLS):,} California Community College apply URLs via CCCApply.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
