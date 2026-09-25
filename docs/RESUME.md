# Resuming Jarvet (written 2026-09-25, end of the guided-journeys session)

Works for any AI coding assistant (Claude Code, Cursor, ...) or by hand.

## Where things stand
- **Branch:** `related-programs-2026-09-25` (many commits ahead of `main`; not
  pushed to origin, not merged). Push/merge only when the user says so.
- **App:** Docker container `jarvet` -- LEFT RUNNING at the end of this session
  (no database writes this session; last backup in `C:\Users\chris\jarvet-backups`
  from the phase 1a session). If it is stopped: `docker start jarvet`; open
  http://localhost:8000.
- **Plan and progress:** `docs/counselor-plan.md` (checklist at the bottom) and
  `docs/user-journeys.md` (8 critical user journeys -- APPROVED; journey 7 is
  "Housing Assistance"). Detailed session log: `.cache/session-notes-2026-09-25.md`.

## Done so far (counselor work)
1. Safety: crisis line (988 press 1 / chat / text 838255) and homeless call
   center (877-424-3838) shown above any reply that mentions them (`app/safety.py`).
2. Benefits library: official VA, eCFR, SSVF, state VR and federal student aid
   sources, searched by the `search_benefits_info` tool with cited sources and
   dates (`app/benefits.py`, built by `scripts/init-benefits-library.py`, own file
   `.cache/benefits-library.sqlite`, refreshed by `scripts/monthly-refresh.py`).
3. Routes without VA benefits (phase 1a): `find_help_without_va_benefits`
   (`app/state_help.py`, `data/state-vr-agencies.json`) -- state vocational
   rehabilitation (no VA rating needed), FAFSA/Pell, American Job Centers,
   apprenticeships, VA career counseling. California DOR waiting list noted.
4. Guided journeys 1, 3, 5 and 8 (`app/journeys.py`, `POST /api/journey`):
   one question at a time with buttons, "why I'm asking" and progress, asked by
   the app itself (no model, free); a finished journey sends one composed
   request to the chat with the tool to run first. Journey 1 hands off to 8
   when the GI Bill likely doesn't cover the veteran. Cards ALPHA, DELTA, ECHO,
   GOLF start journeys; new card HOTEL "Other ways to pay for training".

## Next steps (in order)
1. SCHOLARSHIPS (user request 2026-09-25) -- plan shown to the user, WAITING for
   their go: (a) hand-checked veteran scholarship list (data/scholarships.json,
   each entry verified on its official page) + tool `find_scholarships`;
   (b) every scholarship answer also links CareerOneStop's Scholarship Finder
   pre-filtered (www.careeronestop.org/Toolkit/Training/find-scholarships.aspx?keyword=veteran
   works: ~9,500 awards, 100 for "veteran"); (c) scam warning from official
   sources; (d) a "Find scholarships" card/journey. CareerOneStop's Web API has
   NO scholarship endpoint (checked the API explorer) -- ask them via "Data
   Requests" once the user registers for the API key.
2. Guided journeys: paid chats for journeys 1, 3, 5, 8 run 2026-09-25 (all ran the
   right tools). Fix journey 5 from the A+ card (keyword "CompTIA A+" finds 0,
   fallback pulls unrelated schools; 53 s). Then journeys 4 and 6, a "change my
   answer" button, the paid journey check in check-counselor.py.
3. Phase 1a leftovers (see the checklist): state tuition/fee aid (California
   College Promise Grant needs an official source that downloads), other states'
   VR notices, CareerOneStop API key (the user signs up) for a job-center directory.
4. Phase 2: GI Bill rate tables and the "what would I get" calculator.

## Rules that must be followed (they have prevented real damage)
- **Database writes** (`.cache/va-comparison.sqlite`): stop the app first
  (`docker stop jarvet`), back up (`bash scripts/backup-cache.sh`), write, check
  `PRAGMA integrity_check` = ok, make sure no `-wal`/`-shm` files remain, then
  `docker start jarvet`. Never open that database read-write while the app runs;
  read-only checks from Windows are fine
  (`sqlite3.connect('file:.cache/va-comparison.sqlite?mode=ro', uri=True)`).
- **Benefits library** is a separate file: build to
  `.cache/benefits-library.new.sqlite`, then swap it in with the app stopped.
- After editing `app/*.py`: `docker restart jarvet`. After editing
  `app/static/app.js` or `styles.css`: bump `?v=N` in `app/static/index.html`.
- Benefit facts must come from the library (cited), never from the model's memory.
  Explain jargon in plain language. Never leave a veteran at "you don't qualify".
- Status updates to the user in plain English, not code terms.
- Show the plan for a phase in plain English before building it.

## Checks (run after starting the app)
    python scripts/check-search-counts.py --db-only   # 17 search checks, free
    python scripts/check-counselor.py --no-app        # safety, state help, library, journeys; free
    python scripts/check-counselor.py                 # + 1 chat call (costs model credits)
Windows Python; the library checks run themselves inside the `jarvet` container.

## Environment notes
- Windows + Docker Desktop; project bind-mounted at `/workspace` in the container.
- The app's model is set in the container environment (`LLM_MODEL`, currently
  gpt-4.1-mini via `LLM_BASE_URL`); chat calls cost credits there.
- Slow data jobs run in throwaway `jarvet-dev` containers, e.g.
  `docker run --rm -v C:/Users/chris/jarvet:/workspace -w /workspace jarvet-dev bash -c "tar -C /tmp -xf .cache/_related_fields/fastembed_cache.tar && .venv/bin/python scripts/init-benefits-library.py build"`
  (in Git Bash prefix `MSYS_NO_PATHCONV=1`).
- Git Bash heredocs mangle `\b` / `\n` inside Python edit scripts -- edit files
  directly instead.

## What to say to resume
> Resuming jarvet. Read docs/RESUME.md, docs/counselor-plan.md and the latest
> .cache/session-notes-*.md. Start the app (docker start jarvet) and make sure
> `python scripts/check-search-counts.py --db-only` (17) and
> `python scripts/check-counselor.py --no-app` pass. Then continue with the next
> step in docs/RESUME.md -- show me the plan in plain English before building,
> follow the stop-app/backup rules for database writes, and stop at a clean point
> with everything committed and the checklist, RESUME.md and notes updated.
