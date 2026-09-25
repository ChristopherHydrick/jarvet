# Jarvet counselor plan (agreed 2026-09-25)

Goal: Jarvet acts as a **VA education-benefits counselor, career counselor,
education counselor and social worker** for veterans.

**Scope for now:** education, careers, housing and support services -- including
routes for veterans the VA does not cover (state vocational rehabilitation,
federal and state aid).
**Out of scope for now:** VA disability claims/compensation and VA health care
(Jarvet may mention they exist and point to an accredited representative, but
does not advise on them).

Work happens across many sessions (the user's daily credits are limited), so
each session should finish at a clean point: committed, notes updated, app
running. Progress is tracked in the checklist at the bottom of this file.

## Core idea: a library, not a memory

Benefit rules and rates change, so Jarvet must **look things up and cite
them**, never answer benefits questions from the model's memory (the system
prompt already forbids uncited benefit claims). Three kinds of knowledge:

1. **Benefits library (prose).** Download relevant VA.gov pages (and later
   studentaid.gov and state veterans-agency pages), split into short
   passages, index by keyword (FTS5) and meaning (fastembed, same model as
   program search), stored in its own database file or tables. New chat tool
   `search_benefits_info` returns passages with source URL and date; Jarvet
   answers only from them and shows the source links. Refreshed by
   `scripts/monthly-refresh.py` (add a step).
2. **Exact tables (numbers).** GI Bill payment rates (tuition cap for private
   schools, books stipend), Post-9/11 percentage by months of service, VR&E
   subsistence rates -- from VA's published rate tables, stored as data. Housing
   allowance per school we already have (facilities.bah). Enables a
   **"what would I get" calculator** next to the school cards.
3. **Directories (who can help near me)**, using Jarvet's existing location
   resolver:
   - **SSVF** (Supportive Services for Veteran Families): VA's *SSVF Provider
     Intake List* spreadsheet --
     https://department.va.gov/homeless/wp-content/uploads/sites/72/2026/03/SSVF_Provider_Intake_List_FY26.xlsx
     (found via https://department.va.gov/homeless/supportive-services-for-veteran-families/ ;
     the old va.gov/homeless/ssvf URL redirects there). Checked 2026-09-25: 235
     providers; columns Grant Fiscal Year, Organization Name, FAIN, States
     Served, Counties Served ("Santa Clara County (CA);..."), VISN, two intake
     emails, two intake phones, Intake Information Comments. A new FY file
     appears yearly (FY2027 grantee awards list already posted) -- the refresh
     should find the newest link on that page. Needs a **ZIP-to-county**
     file (free Census ZCTA-to-county relationship file) to match a veteran's
     ZIP/city to the counties served.
   - **VA facilities** (medical centers, Vet Centers, regional benefit offices)
     via the VA Facilities API (below).
   - **Accredited representatives** (VSO reps, agents, attorneys) -- VA OGC
     accreditation search; check for a downloadable list or API.
   - **State education benefits** -- a table we build (phase 6).
   - **Scholarships** -- CareerOneStop (Dept. of Labor) Scholarship Finder web
     API (free key; verify terms) plus a short hand-checked list of trusted
     veteran scholarships.

## Official guides and documents (PDFs, regulations)

The benefits library should also hold official guides, handbooks and rules,
not just web pages. PDFs are split into passages with the **page number**
kept, so Jarvet can cite "SSVF Annual GHSA Update 2026, p. 4". Many are written
for staff/providers, not veterans, so Jarvet must translate them into plain
language; where a rule is in the regulation, the regulation wins. Text
extraction with a PDF library in the jarvet-dev container (e.g. pypdf or
pdfplumber); check tables come out readable.

Where rules exist as regulations, prefer the free, clean **eCFR API**
(ecfr.gov, current text of the Code of Federal Regulations) and **Federal
Register API** (federalregister.gov, notices) over PDFs.

SSVF (checked 2026-09-25 on the department.va.gov SSVF pages -- Compliance and
Program Library sub-pages):
- Current rules: 38 CFR Part 62 (via eCFR API) -- the Federal Register final
  rules listed on the Compliance page (2010 through 2021) are its history.
- FY2027 Notice of Funding Opportunity (Federal Register 2026-00009) -- the
  current year's program priorities.
- SSVF Annual GHSA Update 2026 (PDF):
  https://department.va.gov/homeless/wp-content/uploads/sites/72/2026/09/SSVF_Annual_GHSA_Update_2026.pdf
- SSVF Annual Report FY2024 (PDF):
  https://department.va.gov/homeless/wp-content/uploads/sites/72/2026/04/SSVF_Annual_Report_FY2024.pdf
- ~50 weekly "SSVF_Program_Update_[date].pdf" provider updates -- mostly
  provider news; index only the latest few, if any.
- A standalone **SSVF Program Guide** was not linked on those pages -- search
  for it in phase 3 (it has historically been VA's main how-it-works guide:
  eligibility, temporary financial assistance, rapid rehousing, prevention).

Education / VR&E / financial aid (candidates to find and verify in phases 1-5):
- 38 CFR Part 21 (VA education and VR&E rules) via eCFR API.
- VA School Certifying Official Handbook (PDF) -- how schools certify GI Bill
  enrollment; good for "why hasn't my payment come" questions.
- VA GI Bill and VR&E fact sheets/pamphlets on va.gov and benefits.va.gov.
- VR&E procedures manual (M28C) on KnowVA -- staff-facing; use carefully.
- Federal Student Aid Handbook (fsapartners.ed.gov) and studentaid.gov guides
  for FAFSA/Pell, including the military/veteran sections.
- HUD-VASH and Grant and Per Diem program guides (housing phase).

The monthly refresh should re-check these (new fiscal-year files, updated
PDFs) and record each document's date, so answers can say how current they are.

## VA APIs (developer.va.gov catalog, checked 2026-09-25)

Catalog JSON: https://developer.va.gov/platform-backend/v0/providers/transformations/legacy.json

Useful now (API key, no veteran login):
- **VA Facilities API** -- locations, services, hours of VA medical centers,
  Vet Centers, benefits offices. Free sandbox key; production key needs an
  application to VA. Use for "nearest Vet Center / VA benefits office".
- **VA Forms API** -- current VA form names, versions and PDF links (e.g. the
  GI Bill application, VR&E application), so Jarvet links the current form.
- **GI Bill Comparison Tool API** (api.va.gov/v0/gi, public, undocumented) --
  already used by Jarvet for schools and programs.

Later / maybe (need the veteran to sign in with Login.gov/ID.me through
Jarvet, VA production approval, and careful privacy handling):
- **Education Benefits API** -- the veteran's own Post-9/11 GI Bill
  eligibility and remaining months. Would make the calculator personal.
- **Veteran Service History and Eligibility API** -- service history.

Not for this scope: claims, appeals, decision reviews, disability, health
(FHIR), loan guaranty, direct deposit, letters. **Veteran Confirmation API**
needs SSN-level personal data -- avoid.

## The counselor interview

Ask only what is needed, gradually, and keep answers in the conversation
profile Jarvet already has: service dates/branch and months of active duty
since 9/10/2001, discharge type, whether they have a VA disability rating (yes
/ no / percent -- only to screen for VR&E, not to advise on claims), state of
residence, dependents, school/career goal, rough household income (Pell).
Rule-based screening says "you likely qualify for ..." with "confirm with VA"
and who to contact.

## Topics

- **GI Bill** (first): Post-9/11 (Ch. 33), Montgomery GI Bill Active Duty
  (Ch. 30) and Selected Reserve (Ch. 1606), DEA (Ch. 35) and Fry Scholarship
  for dependents, Yellow Ribbon, housing allowance, tutorial assistance,
  licensing/certification test reimbursement, using more than one GI Bill,
  how to apply. Calculator next to the school cards.
- **VR&E** (Veteran Readiness and Employment, Ch. 31): who qualifies, the five
  tracks, how it differs from the GI Bill, how to apply, how to prepare for
  the counselor meeting. Jarvet is honest that a VR&E counselor decides.
- **Paying for school:** FAFSA and how federal aid stacks with the GI Bill,
  Pell (point to the official Federal Student Aid Estimator, never promise
  amounts), school net price and Pell share from IPEDS (we have the IPEDS
  database; add those columns), scholarships (above), scam warning.
  College Scorecard (step 3 of the old plan, skipped) may return here.
- **State free-college / tuition programs:** no national dataset. Build a
  50-state + DC + territories table: program name, who qualifies (residency,
  disability percent, service era, dependents), what it covers, official link,
  date last verified. Research, then hand-check; re-check yearly. Start with a
  few states (Texas, California -- they come up most), then the rest.
- **Housing and support services (social worker):** SSVF directory, HUD-VASH,
  Grant and Per Diem, Community Resource and Referral Centers, the National
  Call Center for Homeless Veterans (877-424-3838, 24/7), Vet Centers.
  Caseworker style: next steps, who to call, what to have ready.
- **Careers:** already strong (O*NET, military crosswalk, pathways); connect it
  to the above (e.g. VR&E for a career change, apprenticeship/OJT GI Bill).

## Safety first (phase 1, fixed rules, not left to the model)

- Mentions of suicide or self-harm: **Veterans Crisis Line first** -- dial
  988 then press 1, or text 838255 -- before anything else in the reply.
- Homeless or about to lose housing: the homeless-veterans call center and
  their local SSVF provider first.
- A server-side keyword check as a backup, so it works even if the model
  forgets.

## Guardrails

- Jarvet cannot see the veteran's VA record, cannot file anything, and is not
  an accredited representative -- it says so and points to the school
  certifying official, a VR&E counselor, or an accredited representative.
- Cite sources and dates; plain language; explain jargon (as with "Bright
  Outlook").
- **Accuracy checks**, like scripts/check-search-counts.py: ~30 standard
  questions with facts the answer must contain (e.g. months of GI Bill, Pell
  plus GI Bill, who helps with rent in a given county, crisis-line reply).
  Directory/library checks run free; chat checks cost credits, run sparingly.
- Usual database rules: stop app -> backup -> write -> integrity -> restart.

## Critical user journeys

At least 6 journeys that demonstrate Jarvet end to end, each walking the
veteran through the right questions with guided prompts (one question at a
time, answer buttons, "why I'm asking"): see **docs/user-journeys.md**
(draft of 7, requested 2026-09-25). Every phase below should move at least one
journey forward; each journey gets its own automated check.

## Phases (checklist -- update as work is done)

1. [~] **Foundation + safety.** Check which VA.gov pages and official PDFs/regulations (eCFR API) download cleanly (try
   sitemap https://www.va.gov/sitemap.xml; education, VR&E, housing/homeless
   sections), build the benefits library + `search_benefits_info` tool, crisis
   rules + server-side check, first accuracy checks. Show the user a sample
   cited answer before building out fully.
   - [x] Safety (2026-09-25): app/safety.py server check + Veterans Crisis Line
     (988 then 1, chat and text on veteranscrisisline.net, 838255) / homeless
     call center (877-424-3838) block and call/chat/text buttons above the reply,
     shown even if the model fails; prompt rule too.
   - [x] Library (2026-09-25): 115 sources -> 2,844 passages in
     .cache/benefits-library.sqlite (scripts/init-benefits-library.py,
     scripts/benefits-sources.json; VA.gov sitemap is sitemap_index.xml ->
     sitemap-nb/-cb.xml). Tool `search_benefits_info` (app/benefits.py: keyword
     + meaning search, off-topic floor 0.6), forced for benefits questions,
     source links with dates under the reply.
   - [x] Checks: scripts/check-counselor.py (18 safety, 11 library, 1 chat).
   - [x] Sample cited answers shown to the user (GI Bill %/online MHA, SSVF,
     transfer to children).
   - [x] Library added to scripts/monthly-refresh.py (prepare step "library",
     report section, swap on apply) -- 2026-09-25.
   - [ ] User feedback on the sample answers.
1a. [~] **Routes without VA benefits (critical, user 2026-09-25).** DONE 2026-09-25:
   state VR directory (data/state-vr-agencies.json, 78 agencies, from
   scripts/init-state-vr-agencies.py), tool `find_help_without_va_benefits`
   (app/state_help.py: agencies, 34 CFR 361 rule in plain words, California DOR
   waiting-list notice, FAFSA/Pell, American Job Center/WIOA, apprenticeships,
   Chapter 36), prompt rule "never stop at you don't qualify", library +8 sources
   (34 CFR 361 eligibility/services sections, DOR Get Started, CareerOneStop
   veterans + job-center finder, 2 Federal Student Aid PDFs), checks. STILL TO DO:
   state aid (California College Promise Grant page blocks scripts -- find another
   official source), other states' current VR notices (only CA hand-checked),
   CareerOneStop API key for a real job-center directory (user to sign up), journey 8
   screens (with the journey engine). Veterans with
   no GI Bill (never qualified, used up, expired, other-than-honorable
   discharge) or no VA rating for VR&E must still get a path to training:
   - State vocational rehabilitation (VR): 78 agencies (RSA list,
     rsa.ed.gov/about/states) -- directory by state (general + blind agencies),
     plus eligibility rules from 34 CFR 361.42 (any physical or mental
     impairment that is a substantial impediment to employment, decided by the
     state; SSI/SSDI recipients presumed eligible; no VA rating or discharge
     requirement) and each agency's own pages, starting with California DOR.
     Plain-language examples: mental health conditions, substance use disorders
     in recovery, chronic illnesses -- always "the state decides".
   - Federal aid: FAFSA / Pell Grant (studentaid.gov is a JavaScript site --
     use its API/handbook or fsapartners.ed.gov), WIOA training money through
     American Job Centers (veterans get priority of service; DVOP specialists
     for veterans with significant barriers), DOL HVRP for homeless veterans.
   - State aid: community college fee waivers (California College Promise
     Grant first), state veteran programs (phase 6 table).
   - Free VA help that needs no GI Bill: Chapter 36 career counseling; SSVF job
     costs (certifications, tools) for SSVF-eligible families.
   - "Other ways to pay for training" screening + journey 8, and journey 1
     branches to it; prompt rule: never stop at "you don't qualify".
1b. [~] **Guided journeys:** journey definitions + guided-question engine
   (docs/user-journeys.md), starting with journeys 1, 3, 5 (data exists today),
   then 2 (with phase 2), 7 (with phase 3), 6.
   - [x] Journeys approved by the user 2026-09-25 (journey 7 renamed "Housing
     Assistance").
   - [x] Engine (2026-09-25): app/journeys.py + POST /api/journey -- questions
     asked by the app itself (no model, instant, free), typed answers matched
     to buttons, places checked with the location resolver, typed questions go
     to the chat and the journey resumes, safety check on every answer, answers
     kept in the page only (never saved). A finished journey sends one composed
     request to /api/chat with `first_tool` (e.g. find_help_without_va_benefits).
     Journeys 1, 3, 5, 8 built; 1 hands off to 8; new card HOTEL.
   - [x] Free checks: 8 journey cases in check-counselor.py --no-app.
   - [ ] Paid check: one chat per finished journey (user's go needed); watch
     that the model runs both searches for journey 1 (benefits + programs).
   - [ ] Journeys 4, 6 (library data exists), 2 (phase 2), 7 (phase 3).
   - [ ] Nice to have: "change my answer" button; state names spelled out.
   - [ ] Build out: more sources as later phases need them (studentaid.gov in
     phase 5, SSVF Program Guide in phase 3, VA Forms API).
2. [ ] **GI Bill:** rate tables, "what would I get" calculator, estimate on
   school cards.
3. [ ] **Housing and support services:** SSVF directory + ZIP-to-county,
   VA Facilities API (Vet Centers etc.), homeless programs, accredited reps.
4. [ ] **VR&E** + the counselor interview (profile questions, screening).
5. [ ] **Paying for school:** FAFSA/Pell, IPEDS net price, scholarships.
6. [ ] **State free-college table** (Texas, California first, then all).
7. [ ] Later/maybe: veteran sign-in for the Education Benefits API.

## Restarting after a chat clear

**See docs/RESUME.md first** (current state, rules, next steps, resume prompt).

Tell Claude:

> Resuming jarvet. Read docs/counselor-plan.md and the latest
> .cache/session-notes-*.md. Make sure the app is running and the 17 database
> checks pass (--db-only). Then start the next unchecked phase of the
> counselor plan -- show me the plan for that phase in plain English before
> building, follow the stop-app/backup rules for database writes, and stop at
> a clean point with everything committed, the checklist and notes updated.
