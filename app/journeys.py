"""Guided journeys (docs/user-journeys.md): one question at a time, asked by
the app itself -- no model call, so each step is instant and free.

A journey is an ordered list of questions, each filling one "slot" of the
veteran's answers. Slots are shared across journeys (the page keeps the
answers for the conversation), so nothing is asked twice: journey 1 hands a
veteran the GI Bill may not cover straight into journey 8 without asking the
location or training goal again. When every question that applies has an
answer, the journey composes one request for the agent (/api/chat) that
carries all the answers and says which tool to run first.

Typed answers are accepted: a choice question matches the text against its
options (and a few number formats, e.g. "4 years", "30%"); a place is checked
with the program search's own location resolver. Text that reads as a
question ("wait, what is MHA?") is handed to the agent off-script and the
journey resumes afterwards. Sensitive answers (discharge, rating, health,
income) live only in the page's memory for this conversation; they are not
written to the profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

SKIP = "skip"
UNSURE = "unsure"
FINISH = "__finish__"


@dataclass
class Option:
    value: str
    label: str
    match: str = ""  # extra regex that also selects this option


@dataclass
class Question:
    slot: str
    text: str
    why: str
    kind: str = "choice"  # choice | text | location | state
    options: list[Option] = field(default_factory=list)
    ask_if: Callable[[dict[str, str]], bool] = lambda answers: True
    # For choice questions: a typed answer that matches no option is kept as
    # typed (e.g. "nursing" for the field question) instead of re-asked.
    free_text: bool = False


@dataclass
class Journey:
    id: str
    title: str
    intro: str
    questions: list[Question]
    compose: Callable[[dict[str, str]], tuple[str, str | None]]
    hand_off: Callable[[dict[str, str]], tuple[str, str] | None] = lambda answers: None


NOT_SURE = Option(UNSURE, "Not sure", r"\b(?:not sure|no idea|don'?t know|dunno|unsure)\b")
SKIP_OPTION = Option(SKIP, "Skip this question", r"^(?:skip|pass|rather not|prefer not)")

QUESTION_PATTERN = re.compile(
    r"\?\s*$|^(?:wait|what|what's|whats|how|why|who|when|which|can|could|does|do|is|are|will|"
    r"would|should|explain|tell me)\b",
    re.I,
)

# Labels for the "my situation" lines of a composed request.
SLOT_LABELS = {
    "service": "Active-duty time after September 10, 2001",
    "discharge": "Discharge",
    "used": "GI Bill used before",
    "rating": "VA disability rating",
    "code": "Military job code",
    "branch": "Branch",
    "duties": "What I did in the military",
    "state": "State I live in",
    "health": "Health condition that makes working harder (no VA rating needed)",
    "ssdi": "Gets Social Security disability (SSI or SSDI)",
    "income": "Household income per year",
    "field": "What I want to do or study",
    "length": "How long I want to be in school",
    "location": "Where I live or want to study",
    "distance": "How far I can travel",
}

FIELD_QUESTION = Question(
    "field",
    "What kind of work or study interests you? Tap one, or type something specific "
    "(like nursing, welding, IT support or an A+ certification).",
    "So I can find programs and careers that fit you.",
    options=[
        Option("Healthcare", "Healthcare", r"\bhealth|medical|nurs"),
        Option("Skilled trades", "Skilled trades", r"\btrades?\b"),
        Option("Technology", "Technology", r"^(?:tech|technology|it|computers?)$"),
        Option("Business", "Business", r"^business$"),
        Option("explore", "Not sure yet -- help me explore",
               r"\b(?:not sure|no idea|don'?t know|explore|unsure)\b"),
    ],
    free_text=True,
)


def location_question(text: str) -> Question:
    return Question(
        "location", text,
        "To find schools and training you can actually get to.",
        kind="location",
        options=[
            Option("online", "Online programs only", r"^online\b|\bonline (?:only|programs?)\b"),
            Option("anywhere", "Anywhere in the US", r"\b(?:anywhere|nationwide|whole country|any state)\b"),
        ],
    )


def _in_person(answers: dict[str, str]) -> bool:
    return answers.get("location") not in ("online", "anywhere", SKIP, None)


DISTANCE_QUESTION = Question(
    "distance", "How far can you travel to class?",
    "So I only show schools within reach.",
    options=[
        Option("10", "10 miles"), Option("25", "25 miles"),
        Option("50", "50 miles"), Option("100", "100 miles"),
    ],
    ask_if=_in_person,
)

# --- Journey 1: "I just got out -- where do I start?" -----------------------

SERVICE_OPTIONS = [
    Option("none", "None after 9/10/2001", r"\b(?:none|never|no active)\b"),
    Option("lt90", "Less than 90 days"),
    Option("3to6", "90 days to 6 months"),
    Option("6to18", "6 to 18 months"),
    Option("18to24", "18 to 24 months"),
    Option("24to30", "24 to 30 months"),
    Option("30to36", "30 to 36 months"),
    Option("36plus", "36 months or more", r"\b(?:3[6-9]|[4-9]\d)\s*\+?\s*months?\b|\bmore than 3 years\b"),
    NOT_SURE,
]


def service_from_text(text: str) -> str | None:
    """'4 years' -> 36plus, '20 months' -> 18to24, '60 days' -> lt90."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mos?|days?)\b", text.lower())
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2)
    months = amount * 12 if unit.startswith("y") else amount / 30 if unit.startswith("d") else amount
    for limit, value in ((3, "lt90"), (6, "3to6"), (18, "6to18"), (24, "18to24"),
                         (30, "24to30"), (36, "30to36")):
        if months < limit:
            return value
    return "36plus"


def rating_from_text(text: str) -> str | None:
    match = re.search(r"\b(\d{1,3})\s*(?:%|percent)", text.lower())
    if not match:
        return None
    percent = int(match.group(1))
    return "no" if percent == 0 else "r10" if percent < 20 else "r20"


GI_BILL_LIKELY = ("3to6", "6to18", "18to24", "24to30", "30to36", "36plus")


def _no_gi_bill_likely(answers: dict[str, str]) -> bool:
    return (
        answers.get("service") in ("none", "lt90")
        or answers.get("discharge") in ("oth", "bad")
        or answers.get("used") in ("all", "expired")
    )


def compose_start(answers: dict[str, str]) -> tuple[str, str | None]:
    place = _place_phrase(answers)
    field_text = _field_phrase(answers)
    request = (
        "Help me get started with education or training.\n"
        f"{situation(answers)}\n"
        "Please:\n"
        "1. Tell me which VA education benefits I likely have and roughly what they cover, using the "
        "official sources (search the benefits library) -- say VA makes the final decision. If my "
        "rating means Veteran Readiness and Employment (VR&E) could fit, say so.\n"
        f"2. Find 3 to 5 VA-approved programs {field_text} {place}.\n"
        "3. Give me 3 concrete next steps."
    )
    return request, "search_benefits_info"


def hand_off_start(answers: dict[str, str]) -> tuple[str, str] | None:
    if _no_gi_bill_likely(answers) and answers.get("rating") != "r20":
        return (
            "other-ways",
            "Thanks. From your answers, the GI Bill may not cover you -- VA makes the final "
            "decision, and I'll show you how to check. But that does not mean you're out of "
            "options: there are other ways to pay for training that don't depend on the VA. "
            "A few more questions.",
        )
    return None


START = Journey(
    "start", "Help me get started",
    "Let's find your best starting point. I'll ask a few quick questions -- tap an answer "
    "or type your own, and skip anything you'd rather not answer.",
    [
        Question(
            "service",
            "How much active-duty time did you serve after September 10, 2001? Add up all your "
            "periods of service.",
            "This decides which GI Bill you may have and what percentage of it.",
            options=SERVICE_OPTIONS,
        ),
        Question(
            "discharge", "What type of discharge did you get (or will you get)?",
            "Most VA education benefits need an honorable discharge. If yours isn't, there are "
            "still other ways to pay for training -- I'll show you.",
            options=[
                Option("honorable", "Honorable", r"^honou?rable\b"),
                Option("general", "General (under honorable conditions)", r"\bgeneral\b"),
                Option("oth", "Other than honorable", r"\bother than\b|\both\b"),
                Option("bad", "Bad conduct or dishonorable", r"\bbad conduct\b|\bdishonou?rable\b|\bbcd\b"),
                Option("serving", "Still serving", r"\bstill (?:serving|in)\b|\bactive duty\b"),
                NOT_SURE,
            ],
        ),
        Question(
            "used", "Have you used any of your GI Bill before?",
            "So I can tell you what may be left -- or other ways to pay if it's used up.",
            options=[
                Option("no", "No, never used it", r"^(?:no|nope|never)\b"),
                Option("some", "Used some of it", r"\bsome\b|\bpart\b"),
                Option("all", "Used all of it", r"\ball\b|\bused (?:it )?up\b|\bran out\b"),
                Option("expired", "I think it expired", r"\bexpired?\b|\bran out of time\b"),
                NOT_SURE,
            ],
            ask_if=lambda answers: answers.get("service") in GI_BILL_LIKELY + (UNSURE, SKIP)
            and answers.get("discharge") not in ("oth", "bad"),
        ),
        Question(
            "rating", "Do you have a VA disability rating?",
            "Only to see whether Veteran Readiness and Employment (VR&E) -- VA's training "
            "program for veterans with a service-connected disability -- might fit. Jarvet "
            "doesn't help with disability claims.",
            options=[
                Option("no", "No", r"^(?:no|nope|none)\b"),
                Option("r10", "Yes, 10%"),
                Option("r20", "Yes, 20% or more"),
                Option("waiting", "Applied, waiting", r"\b(?:waiting|pending|applied)\b"),
                NOT_SURE,
            ],
        ),
        FIELD_QUESTION,
        location_question(
            "Where do you live, or where do you want to study? A city and state or a ZIP code."
        ),
    ],
    compose_start,
    hand_off_start,
)

# --- Journey 3: military job -> civilian careers and programs ----------------


def compose_mos(answers: dict[str, str]) -> tuple[str, str | None]:
    if answers.get("code") not in ("unknown", SKIP, None):
        what = f"related to my military job ({answers['code']})"
        tool = "find_va_programs_for_military_job"
    else:
        what = "related to what I did in the military"
        tool = None
    request = (
        f"Find degrees and certificates for high-demand civilian jobs {what}.\n"
        f"{situation(answers)}\n"
        f"Find VA-approved programs {_place_phrase(answers)}."
    )
    return request, tool


MOS = Journey(
    "mos", "Find high-demand jobs for my military job",
    "Let's match your military job to civilian careers that are growing, then find "
    "programs that train for them. Two or three quick questions.",
    [
        Question(
            "code",
            "What was your military job code? For example 68W (Army), 0311 (Marine Corps), "
            "3D0X2 (Air Force) or HM (Navy).",
            "Your job code connects to the civilian careers that use the same skills.",
            kind="text",
            options=[Option("unknown", "I don't know my code", r"\bdon'?t know\b|\bnot sure\b|\bforgot\b")],
        ),
        Question(
            "branch", "Which branch did you serve in?",
            "Job codes and training differ by branch.",
            options=[Option(name, name) for name in (
                "Army", "Navy", "Air Force", "Marine Corps", "Coast Guard", "Space Force",
            )],
            ask_if=lambda answers: answers.get("code") == "unknown",
            free_text=True,
        ),
        Question(
            "duties",
            "In a few words, what did you do? (For example: \"fixed helicopter engines\" or "
            "\"combat medic\".)",
            "So I can match your skills without the code.",
            kind="text",
            ask_if=lambda answers: answers.get("code") == "unknown",
        ),
        location_question("Where do you want to study? A city and state or a ZIP code."),
        DISTANCE_QUESTION,
    ],
    compose_mos,
)

# --- Journey 5: help me choose what to study ---------------------------------


def compose_study(answers: dict[str, str]) -> tuple[str, str | None]:
    if answers.get("field") in ("explore", SKIP, UNSURE, None):
        request = (
            "I'm not sure what to study yet -- help me explore careers first.\n"
            f"{situation(answers)}\n"
            "Suggest a few careers that fit, explain the job outlook in plain words, and then "
            f"find VA-approved programs {_place_phrase(answers)}."
        )
    else:
        request = (
            f"Help me choose what to study. I'm interested in {_field_label(answers)}.\n"
            f"{situation(answers)}\n"
            f"Find VA-approved programs {_place_phrase(answers)}, show the next credential they "
            "lead to, and tell me the career outlook in plain words."
        )
    return request, None


STUDY = Journey(
    "study", "Figure out what to study",
    "Let's connect what you want to do with real programs near you. Three or four quick "
    "questions.",
    [
        Question(
            "field",
            "What would you like to do or study? Tap one, or type something specific "
            "(like nursing, welding or an A+ certification).",
            FIELD_QUESTION.why, options=FIELD_QUESTION.options, free_text=True,
        ),
        Question(
            "length", "How long do you want to be in school?",
            "Short certificates can get you working sooner; degrees open more doors later.",
            options=[
                Option("short", "A short certificate (under a year)", r"\bcert|\bshort\b|\bmonths?\b"),
                Option("associate", "2-year associate degree", r"\bassociate|\b2[\s-]?years?\b|\btwo years?\b"),
                Option("bachelor", "4-year bachelor's degree", r"\bbachelor|\b4[\s-]?years?\b|\bfour years?\b"),
                NOT_SURE,
            ],
        ),
        location_question("Where do you want to study? A city and state or a ZIP code."),
        DISTANCE_QUESTION,
    ],
    compose_study,
)

# --- Journey 8: the VA won't cover me -- other ways to pay --------------------


def compose_other_ways(answers: dict[str, str]) -> tuple[str, str | None]:
    state = answers.get("state")
    has_state = state not in (SKIP, UNSURE, None)
    request = (
        "Help me find ways to pay for training that don't depend on the GI Bill or VR&E.\n"
        f"{situation(answers)}\n"
        "Look up the routes for my state and walk me through each one that fits my answers: "
        "my state's vocational rehabilitation agency (what it can pay for, how to apply, and that "
        "the state decides, not VA), FAFSA and Pell Grants, my American Job Center (WIOA "
        "training money), apprenticeships, and free VA career counseling if it applies. Cite "
        "the official sources."
    )
    if _in_person(answers) and answers.get("field") not in ("explore", SKIP, UNSURE, None):
        request += (
            f"\nAlso show me 3 to 5 schools {_field_phrase(answers)} {_place_phrase(answers)}. I may not have "
            "VA benefits, so don't present them as VA-approved programs -- present them as places to train, and "
            "tell me to contact their financial aid office and veterans office to ask about funding "
            "(FAFSA/Pell, state aid, school scholarships)."
        )
    return request, "find_help_without_va_benefits" if has_state else None


OTHER_WAYS = Journey(
    "other-ways", "Other ways to pay for training",
    "Even without the GI Bill or a VA disability rating, there are ways to pay for training. "
    "A few quick questions so I can find the ones that fit you.",
    [
        Question(
            "state", "Which state do you live in?",
            "Each state runs its own vocational rehabilitation agency and aid programs.",
            kind="state",
        ),
        Question(
            "health",
            "Do you have a health condition that makes it harder to work or keep a job? "
            "It does NOT need a VA rating.",
            "Your state's vocational rehabilitation program can pay for training for people "
            "with conditions like PTSD, depression, a substance use disorder in recovery, chronic "
            "pain or illness, or a learning disability. The state decides, not the VA.",
            options=[
                Option("yes", "Yes", r"^(?:yes|yeah|yep)\b"),
                Option("no", "No", r"^(?:no|nope)\b"),
                NOT_SURE,
                Option("private", "Prefer not to say", r"\bprefer not\b|\brather not\b"),
            ],
        ),
        Question(
            "ssdi", "Do you get Social Security disability payments (SSI or SSDI)?",
            "If you do, the state program must treat you as eligible.",
            options=[
                Option("yes", "Yes", r"^(?:yes|yeah|yep)\b"),
                Option("no", "No", r"^(?:no|nope)\b"),
                NOT_SURE,
            ],
            ask_if=lambda answers: answers.get("health") != "no",
        ),
        Question(
            "income", "Roughly what is your household's income per year? (Optional.)",
            "Pell Grants and job-center (WIOA) training money are mainly for lower-income "
            "households. This answer stays in this conversation only.",
            options=[
                Option("Under $30,000", "Under $30,000"),
                Option("$30,000 to $60,000", "$30,000 to $60,000"),
                Option("$60,000 to $100,000", "$60,000 to $100,000"),
                Option("Over $100,000", "Over $100,000"),
                SKIP_OPTION,
            ],
            free_text=True,
        ),
        FIELD_QUESTION,
    ],
    compose_other_ways,
)

JOURNEYS = {journey.id: journey for journey in (START, MOS, STUDY, OTHER_WAYS)}
FIRST_TOOLS = {
    "search_benefits_info", "find_help_without_va_benefits",
    "find_va_programs_for_military_job", "find_va_programs",
}

# --- Helpers for composed requests --------------------------------------------


def _all_questions() -> dict[str, Question]:
    found: dict[str, Question] = {}
    for journey in JOURNEYS.values():
        for question in journey.questions:
            found.setdefault(question.slot, question)
    return found


def answer_label(slot: str, value: str) -> str:
    if value == SKIP:
        return "skipped"
    if value == UNSURE:
        return "not sure"
    question = _all_questions().get(slot)
    for option in question.options if question else []:
        if option.value == value:
            return option.label
    if slot == "distance":
        return f"{value} miles"
    return value


def situation(answers: dict[str, str]) -> str:
    lines = [
        f"- {label}: {answer_label(slot, answers[slot])}"
        for slot, label in SLOT_LABELS.items()
        if answers.get(slot) not in (None, SKIP) and not (slot == "code" and answers[slot] == "unknown")
    ]
    return "My situation:\n" + "\n".join(lines) if lines else ""


def _field_label(answers: dict[str, str]) -> str:
    return answer_label("field", answers["field"]).lower()


def _field_phrase(answers: dict[str, str]) -> str:
    if answers.get("field") in ("explore", SKIP, UNSURE, None):
        return "that fit my answers"
    return f"for {_field_label(answers)}"


def _place_phrase(answers: dict[str, str]) -> str:
    location = answers.get("location")
    if location == "online":
        return "that I can take online"
    if location == "anywhere":
        return "anywhere in the US"
    if location in (SKIP, None):
        return "(ask me where if you need it)"
    distance = answers.get("distance")
    if distance in (None, SKIP, UNSURE):
        distance = "25"
    return f"within {distance} miles of {location}"


# --- The engine -----------------------------------------------------------------


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%$+' ]", " ", text.lower())).strip()


def match_option(question: Question, text: str) -> str | None:
    value = _normalized(text)
    for option in question.options + [SKIP_OPTION]:
        if value in (_normalized(option.label), _normalized(option.value)):
            return option.value
    if question.slot == "service":
        parsed = service_from_text(text)
        if parsed:
            return parsed
    if question.slot == "rating":
        parsed = rating_from_text(text)
        if parsed:
            return parsed
    if question.slot == "distance":
        number = re.search(r"\b(\d{1,3})\b", text)
        if number:
            return number.group(1)
    for option in question.options + [SKIP_OPTION]:
        # A typed "nursing" stays "nursing" rather than widening to the
        # Healthcare button; only "not sure"/"skip" style answers match there.
        if question.free_text and option.value not in ("explore", UNSURE, SKIP):
            continue
        if option.match and re.search(option.match, text, re.I):
            return option.value
    return None


def looks_like_question(text: str) -> bool:
    return bool(QUESTION_PATTERN.search(text.strip()))


def next_question(journey: Journey, answers: dict[str, str]) -> Question | None:
    for question in journey.questions:
        if question.slot not in answers and question.ask_if(answers):
            return question
    return None


class JourneyEngine:
    def __init__(
        self, resolve_location: Callable[[str], dict[str, Any] | None],
        location_candidates: Callable[[str], list[str]],
        state_code: Callable[[str], str | None],
        military_code: Callable[[str], str | None] = lambda text: text,
    ) -> None:
        self.resolve_location = resolve_location
        self.location_candidates = location_candidates
        self.state_code = state_code
        self.military_code = military_code

    def step(
        self, journey_id: str, answers: dict[str, str], asked: list[str], *,
        pending: str | None = None, text: str | None = None, value: str | None = None,
        known_location: str | None = None, intro: str | None = None,
    ) -> dict[str, Any]:
        journey = JOURNEYS[journey_id]
        answers = dict(answers)
        asked = list(asked)
        notes: list[str] = []
        location_choices: list[str] = []
        starting = pending is None and not asked

        question = next(
            (q for q in journey.questions if q.slot == pending), None,
        ) if pending else None
        if question and (text or value):
            outcome = self._answer(question, (text or "").strip(), value)
            if outcome["kind"] == "offscript":
                return {"kind": "offscript", "journey": journey_id, "answers": answers,
                        "asked": asked, "pending": pending}
            if outcome["kind"] == "finish":
                return self._done(journey, answers, asked)
            if outcome["kind"] == "retry":
                notes.append(outcome["note"])
                location_choices = outcome.get("choices", [])
            else:
                answers.update(outcome["answers"])

        upcoming = next_question(journey, answers)
        if upcoming is None:
            return self._done(journey, answers, asked)

        if upcoming.slot not in asked:
            asked.append(upcoming.slot)
        if starting:
            known = [
                f"{SLOT_LABELS.get(q.slot, q.slot)}: {answer_label(q.slot, answers[q.slot])}"
                for q in journey.questions
                if answers.get(q.slot) not in (None, SKIP, UNSURE)
            ]
            intro = intro or journey.intro
            if known:
                intro += " I'll use what you already told me -- " + "; ".join(known) + "."
            notes.insert(0, intro)
        return self._question_payload(
            journey, upcoming, answers, asked, notes, location_choices, known_location,
        )

    def _answer(self, question: Question, text: str, value: str | None) -> dict[str, Any]:
        if value == FINISH or text == FINISH:
            return {"kind": "finish"}
        if value is not None and value != "":
            chosen = value
            if question.kind == "location" and value not in ("online", "anywhere", SKIP):
                return self._location_answer(value)
            if question.kind == "state" and value not in (SKIP, UNSURE):
                return self._state_answer(value)
            return {"kind": "answer", "answers": {question.slot: chosen}}
        if not text:
            return {"kind": "retry", "note": "Tap an answer or type one."}
        chosen = match_option(question, text) if question.options or question.slot in (
            "service", "rating", "distance") else None
        if chosen is None and question.kind == "location":
            chosen = match_option(Question("x", "", "", options=[SKIP_OPTION]), text)
        if chosen is not None:
            if question.kind == "location" and chosen not in ("online", "anywhere", SKIP):
                return self._location_answer(text)
            return {"kind": "answer", "answers": {question.slot: chosen}}
        if looks_like_question(text):
            return {"kind": "offscript"}
        if question.kind == "location":
            return self._location_answer(text)
        if question.kind == "state":
            return self._state_answer(text)
        if question.slot == "code":
            return self._code_answer(text)
        if question.kind == "text" or question.free_text:
            return {"kind": "answer", "answers": {question.slot: text[:120]}}
        return {"kind": "offscript"}

    def _location_answer(self, text: str) -> dict[str, Any]:
        candidates = self.location_candidates(text)
        if len(candidates) > 1:
            return {"kind": "retry", "note": "Which one do you mean?", "choices": candidates}
        resolved = self.resolve_location(candidates[0] if candidates else text)
        if not resolved:
            return {"kind": "retry", "note": (
                "I couldn't find that place. Try a city and state (like San Jose, CA) or a "
                "5-digit ZIP code."
            )}
        found = {"location": resolved["label"]}
        if resolved.get("state"):
            found["state"] = resolved["state"]
        return {"kind": "answer", "answers": found}

    def _code_answer(self, text: str) -> dict[str, Any]:
        code = self.military_code(text)
        if code:
            return {"kind": "answer", "answers": {"code": code}}
        if len(text.split()) > 1:
            # "combat medic": a description, not a code -- ask the branch next.
            return {"kind": "answer", "answers": {"code": "unknown", "duties": text[:120]}}
        return {"kind": "retry", "note": (
            "I don't recognize that code. Check it (like 68W or 0311), type what you did in a "
            "few words, or tap \"I don't know my code\"."
        )}

    def _state_answer(self, text: str) -> dict[str, Any]:
        code = self.state_code(text)
        if not code:
            return {"kind": "retry", "note": "I didn't recognize that state. Type its name or "
                                              "two-letter code (like Texas or TX)."}
        return {"kind": "answer", "answers": {"state": code}}

    def _question_payload(
        self, journey: Journey, question: Question, answers: dict[str, str],
        asked: list[str], notes: list[str], location_choices: list[str],
        known_location: str | None,
    ) -> dict[str, Any]:
        options = [{"label": option.label, "value": option.value} for option in question.options]
        if question.kind == "location":
            extra = [{"label": choice, "value": choice} for choice in location_choices]
            if not extra and known_location:
                extra = [{"label": f"Use {known_location}", "value": known_location}]
            options = extra + options
        if question.kind in ("text", "location", "state") or question.free_text or options:
            if not any(option["value"] in (SKIP, UNSURE) for option in options):
                options.append({"label": "Skip this question", "value": SKIP})
        remaining = sum(
            1 for q in journey.questions
            if q.slot not in answers and q.ask_if(answers) and q.slot not in asked
        )
        return {
            "kind": "question",
            "journey": journey.id,
            "title": journey.title,
            "answers": answers,
            "asked": asked,
            "pending": question.slot,
            "notes": notes,
            "question": question.text,
            "why": question.why,
            "options": options,
            "typed": question.kind in ("text", "location", "state") or question.free_text,
            "progress": {"number": len(asked), "total": len(asked) + remaining},
        }

    def _done(self, journey: Journey, answers: dict[str, str], asked: list[str]) -> dict[str, Any]:
        hand_off = journey.hand_off(answers)
        if hand_off:
            next_id, message = hand_off
            return self.step(next_id, answers, [], intro=message)
        request, first_tool = journey.compose(answers)
        profile: dict[str, list[str]] = {}
        if answers.get("location") not in (None, SKIP, "online", "anywhere"):
            profile["location"] = [answers["location"]]
        if answers.get("field") not in (None, SKIP, UNSURE, "explore"):
            profile["interests"] = [answer_label("field", answers["field"])]
        return {
            "kind": "done",
            "journey": journey.id,
            "answers": answers,
            "asked": asked,
            "request": request,
            "first_tool": first_tool,
            "profile": profile,
            "summary": "Thanks -- that's everything I need. Putting it together now.",
        }
