# Jarvet critical user journeys (draft 2026-09-25, for the user's review)

Seven journeys that show what Jarvet can do. Each one starts from a card on
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

## 7. "I'm about to lose my housing" (new card: Housing help -- social worker)
**Who:** veteran who is homeless, facing eviction, or behind on rent.
**Questions (after the 24/7 call center is shown first):**
1. Which is closest to your situation? (Homeless now / Eviction notice / Behind
   on rent or utilities / Couch surfing / Worried about next month)
2. Where are you staying or living? (city/ZIP) -- *to find the local SSVF provider.*
3. Who is in your household? (Just me / Me and children / Me and partner /
   Other) -- *SSVF serves veteran families.*
4. Would you also like help with work or training? (Yes / Not now)
**Ends with:** the local SSVF provider(s) with intake phone/email (phase 3
directory), what they can pay for (rent/utility arrears, deposits -- cited from
38 CFR 62), HUD-VASH and Grant and Per Diem explained, "what to have ready when
you call" (DD-214, ID, lease or eviction notice, income), nearest Vet Center.
**Shows off:** safety rules, SSVF directory, benefits library, caseworker style.

---

## Candidate 8 (later): "I used up my GI Bill / it expired"
Disability rating -> VR&E screening; months left; FAFSA/Pell (phase 5);
state programs (phase 6); scholarships.

## How each journey is checked
For every journey, a saved script of answers runs through the app: the free
check confirms each question appears in order with its buttons and nothing
already known is asked again; one chat run per journey (costs credits, run
sparingly) confirms the final answer contains the key facts (e.g. journey 2:
housing allowance for the school's ZIP; journey 7: the SSVF provider's phone).
