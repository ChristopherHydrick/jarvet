# Jarvet critical user journeys (approved 2026-09-25; journey 7 renamed "Housing Assistance")

**Build status (2026-09-25):** journeys **1, 3, 5, 8 and 9 (scholarships) are built** as guided
questions (app/journeys.py, /api/journey, cards ALPHA, GOLF, DELTA/ECHO and the
new HOTEL card "Other ways to pay for training"); journey 1 hands off to 8.
Still to build: 2 (with the phase 2 calculator), 7 (with the phase 3 SSVF
directory), 6, 4. Small changes made while building, versus the draft below:
- Journey 1 service buttons follow VA's Post-9/11 tiers (90 days-6 months,
  6-18, 18-24, 24-30, 30-36, 36+ months), and a follow-up "Have you used any of
  your GI Bill before?" is asked when the service time qualifies (answers
  "used it all" / "expired" also hand off to journey 8).
- Journey 3 checks the job code against the military crosswalk; a description
  ("combat medic") is accepted instead and the branch is asked.
- Journey 8 skips the state question when the location already gave it, and the
  Social Security question when the veteran said they have no health condition.
- Every question after the first offers "Skip the rest of the questions".

Eight journeys that show what Jarvet can do. Each one starts from a card on
the landing page (or from the veteran's own words), asks only the questions it
needs -- one at a time, in plain language, with tap-to-answer buttons and a
short "why I'm asking" -- and ends with something the veteran can act on.

Common rules for every journey:
- **Safety first.** A crisis or housing emergency in any answer interrupts the
  journey with the Veterans Crisis Line / homeless call center (app/safety.py).
- **One question at a time**, each with 2-5 answer buttons plus "Not sure" /
  "Skip". Typed answers are accepted too. Progress shows ("Question 2 of 4").
- **Never ask twice.** Anything already known (profile, earlier answer) is
  skipped; the veteran can change an answer ("Change my location").
- **Off-script is fine.** A question typed mid-journey ("wait, what is MHA?")
  gets answered, then the journey resumes.
- **Benefit facts are cited** from the benefits library; Jarvet says "you may
  qualify" and "VA makes the final decision".
- **Sensitive answers** (disability rating, housing situation, income) are used
  for this conversation only unless the veteran ticks "Remember me".

---

## 1. "I just got out -- where do I start?" (card ALPHA: Help me get started)
**Who:** recently separated veteran, unsure what they have or want.
**Questions:**
1. When did you serve on active duty after September 10, 2001? (buttons: less
   than 90 days / 90 days-6 months / 6-18 / 18-30 / 30-36 / 36+ months / never)
   -- *decides which GI Bill and what percentage.*
2. What type of discharge did you receive? (Honorable / General / Other / Not
   yet discharged) -- *most education benefits need honorable.*
3. Do you have a VA disability rating? (No / Yes: 10-20% / Yes: 20%+ / Applied,
   waiting) -- *only to check whether VR&E could fit; Jarvet doesn't handle claims.*
4. What kind of work interests you? (Healthcare / Skilled trades / Technology /
   Business / Not sure -- help me explore)
5. Where do you live? (city, state or ZIP)
**Ends with:** a short "what you likely have" list (cited: Post-9/11 at N%,
VR&E worth a look, etc.), 3-5 matching programs nearby, and 3 next steps
(apply for benefits, compare schools, talk to a school certifying official).
If the answers show no GI Bill (no qualifying service, other-than-honorable
discharge, used up or expired) and no VA rating, it continues straight into
journey 8 instead of stopping at "you don't qualify".
**Shows off:** benefits library, screening rules, program search, location.

## 2. "What would the GI Bill pay at this school?" (card BRAVO: Get paid to go to school)
**Who:** veteran choosing between schools, wants real numbers.
**Questions:**
1. Which benefit will you use? (Post-9/11 / Montgomery Active Duty / Montgomery
   Selected Reserve / VR&E / Not sure)
2. How long did you serve on active duty after 9/10/2001? (tiers as above; skipped
   if known)
3. Which school or program? (typed name, or "help me find one")
4. In person or online? (In person / Online only / Mix)
5. Full-time or part-time? (Full-time / 3/4 / Half / Less)
**Ends with:** an estimate card: tuition covered (public in-state vs private cap),
monthly housing allowance for that school's ZIP (online = half the national
average), book stipend, Yellow Ribbon if the school takes part -- every number
cited to VA's rate tables with the date, marked "estimate, VA decides".
**Shows off:** rate tables + calculator (phase 2), school data (facilities.bah,
Yellow Ribbon), benefits library.

## 3. "What civilian jobs and degrees fit my military job?" (card GOLF)
**Who:** transitioning service member or veteran with an MOS / AFSC / rating.
**Questions:**
1. What was your military job code? (typed; "I don't know my code" -> branch +
   job description instead)
2. Where do you want to study? (city/ZIP / Online / Anywhere)
3. How far can you travel? (10 / 25 / 50 miles / Online is fine) -- skipped for
   online or anywhere.
**Ends with:** matching civilian careers (with plain-language "growing field"
explanation), "Your path forward" ladder (certificate -> associate -> bachelor's),
and school cards with distances. (Works today; the journey adds the guided questions.)
**Shows off:** military crosswalk, O*NET, pathways, related programs.

## 4. "I want to earn a paycheck while I train" (card CHARLIE)
**Who:** veteran who can't stop working to go to school.
**Questions:**
1. What kind of work? (Electrician / Plumbing / HVAC / Welding / Truck driving /
   IT support / Other)
2. Where? (city/ZIP)
3. Do you have GI Bill benefits to use? (Yes / No / Not sure)
**Ends with:** VA-approved apprenticeship/OJT employers and schools nearby, plus
how the GI Bill OJT payment steps down as wages rise (cited, with current rates).
**Shows off:** OJT/apprenticeship search, benefits library (OJT rates).

## 5. "Help me choose what to study" (cards DELTA / ECHO)
**Who:** veteran with an interest but no school in mind (A+, nursing, welding...).
**Questions:**
1. What would you like to do or study? (typed, or Healthcare / Trades / Tech /
   Business / Explore careers first)
2. How long do you want to be in school? (Short certificate / 2-year associate /
   4-year bachelor's / Not sure)
3. Where, and how far? (city/ZIP + distance, or Online)
**Ends with:** programs grouped by level with the related-programs section and
the ladder to the next credential; career outlook in plain words; certification
test reimbursement mentioned when relevant (cited).
**Shows off:** program search, related programs, pathways, O*NET.

## 6. "Can my spouse or kids use education benefits?" (card FOXTROT)
**Who:** veteran, spouse, or adult child asking about family benefits.
**Questions:**
1. Who is the student? (My spouse / My child / I'm the spouse / I'm the child)
2. What is the veteran's situation? (Still serving / Retired or separated /
   Permanently and totally disabled from service / Died in the line of duty or
   from a service-connected cause)
3. Has Post-9/11 GI Bill been transferred already? (Yes / No / Not sure) --
   only if still serving or retired.
4. Which state do you live in? -- *for state tuition waivers (phase 6).*
**Ends with:** which programs likely apply (Transfer of Entitlement, Fry
Scholarship, DEA Chapter 35, state waiver) with who decides (DoD for transfer,
VA for Fry/DEA), how to apply (current form), all cited.
**Shows off:** benefits library, screening rules, forms, state table (later).

## 7. Housing Assistance -- SSVF and HUD-VASH (new card: Housing Assistance)
**Who:** veteran (and family) who is homeless, facing eviction, behind on rent or
bills, or housed but struggling to stay that way. SSVF is **more than housing**:
the journey covers the whole range of help, not only rent.
**Questions (the 24/7 homeless call center, 877-424-3838, is shown first if the
veteran is homeless or about to lose housing):**
1. Which is closest to your situation? (Homeless now / Eviction notice / Behind
   on rent or utilities / Staying with others / Housed but need support)
2. What do you need help with? (pick any: Rent or deposit / Utilities / A job,
   training or certification / Using my VA education benefits / Legal problem /
   Child care / Transportation or car repair / Budgeting or credit) -- *so the
   answer covers everything SSVF and HUD-VASH can do for you.*
3. Where are you living or staying? (city/ZIP) -- *to find your local SSVF
   provider and VA medical center.*
4. Who is in your household? (Just me / Me and children / Me and partner /
   Other) -- *SSVF serves veteran families; household size affects limits.*
5. Roughly what is your household income? (optional; buttons by range) -- *SSVF
   is for very low-income families (about half the local median or less).*
**Ends with:**
- **SSVF** (short-term, through a local nonprofit): what it can pay for, cited
  from 38 CFR 62.33-62.34 -- rent and back rent (up to 6 months a year, 10 in
  2 years; more for extremely low-income), utilities, security/utility deposits,
  moving costs, emergency housing; job costs such as uniforms, tools,
  certifications and licenses (general housing stability assistance, $2,137 per
  person in 2026 per VA's GHSA update); legal help incl. court fees; child care;
  transportation incl. up to $1,200 of car repairs; credit counseling; a case
  manager; and help getting VA benefits incl. **education, vocational and
  employment services** (62.32). SSVF does not pay tuition -- it helps the
  veteran get the GI Bill / VR&E that does.
- **HUD-VASH** (long-term): a HUD housing voucher plus an ongoing VA case
  manager, for homeless veterans who need intensive support; reached through
  the VA medical center's homeless program or the call center, not SSVF.
- Grant and Per Diem (transitional housing) and Community Resource and Referral
  Centers when relevant.
- The local SSVF provider(s) with intake phone/email (phase 3 directory),
  nearest VA medical center homeless program and Vet Center, and "what to have
  ready when you call" (DD-214, ID, lease or eviction notice, proof of income).
- If they picked job/training/education: hand-off to journey 4 or 5.
**Shows off:** safety rules, SSVF directory, benefits library (regulation + GHSA
update), caseworker style, links to the education journeys.

---

## 8. "The VA won't cover me -- how else can I pay for training?" (new card: Other ways to pay)
**Who:** veteran with no GI Bill (never qualified, used it up, it expired, or
an other-than-honorable discharge) and no VA disability rating for VR&E --
or anyone who wants to add non-VA money on top of VA benefits.
**Questions:**
1. Which state do you live in? -- *each state runs its own vocational
   rehabilitation agency and aid programs.*
2. Do you have any health condition that makes working or keeping a job harder?
   It does not need a VA rating. (Yes / No / Not sure / Prefer not to say) --
   *examples in plain words: a mental health condition, a substance use disorder
   or recovery, chronic pain or illness, a learning disability, severe
   allergies or chemical sensitivities; the state decides, not VA.*
3. Do you get Social Security disability (SSI or SSDI)? (Yes / No / Not sure)
   -- *if yes, the state program must presume you're eligible.*
4. Roughly what is your household income? (optional, ranges) -- *for Pell
   Grant and WIOA training money.*
5. What do you want to train for? (typed, or Healthcare / Trades / Tech /
   Business / Not sure)
**Ends with:**
- **State vocational rehabilitation** (if yes/not sure to 2 or yes to 3): the
  state agency's name, how to apply and phone (e.g. California Department of
  Rehabilitation), what it can pay for (training, tuition, books, tools,
  job placement), and that eligibility is the state's decision -- cited from
  34 CFR 361.42 and the agency's own pages.
- **Federal aid:** FAFSA / Pell Grant (free money that doesn't need to be paid
  back, for lower-income students), WIOA training funds through the local
  American Job Center, where veterans get priority of service.
- **State aid:** e.g. California College Promise Grant (community college fees
  waived for eligible low-income residents); state veteran programs (phase 6).
- **Still free from VA:** Chapter 36 career counseling; for SSVF-eligible
  families, SSVF can pay job certifications and tools.
- **Earn while you learn:** apprenticeships (paid, no benefits needed) -- hands
  off to journey 4; matching schools -- hands off to journey 5.
**Shows off:** state VR directory, federal/state aid in the library, screening
without VA benefits, links to the school and training journeys.

## 9. "Find scholarships" (card INDIA, added 2026-09-25 at the user's request) -- BUILT
**Who:** a veteran, service member, spouse, surviving spouse, child or grandchild.
**Questions:** who is the student; the service member's or veteran's situation
(only for family members: serving / retired / separated / service-connected
disability / died in service); branch; kind of school (certificate or trade /
college / graduate); what they want to study.
**Ends with:** matching scholarships from Jarvet's hand-checked list (who can
apply, amount, timing, "confirm on the sponsor's site"), a link to search all
~9,500 scholarships in the CareerOneStop Scholarship Finder, the FTC scam
warning (never pay to apply), the school's financial aid and veterans offices,
and -- for families of the fallen or disabled -- the Fry Scholarship and
Chapter 35 (cited).

## How each journey is checked
Built (free, no model): `python scripts/check-counselor.py --no-app` runs the
saved answer scripts in scripts/counselor-checks.json ("journeys") through the
running app's /api/journey. The paid one-chat-per-journey check is not built
yet (run only when the user says so).

For every journey, a saved script of answers runs through the app: the free
check confirms each question appears in order with its buttons and nothing
already known is asked again; one chat run per journey (costs credits, run
sparingly) confirms the final answer contains the key facts (e.g. journey 2:
housing allowance for the school's ZIP; journey 7: the SSVF provider's phone).
