"""Fixed safety replies that do not depend on the language model.

A message that mentions suicide or self-harm always gets the Veterans Crisis
Line first; one that mentions being homeless or about to lose housing always
gets the National Call Center for Homeless Veterans first. The chat endpoint
puts these above the model's reply (and returns them alone if the model call
fails), so they reach the veteran even when the model forgets or errors.

Keyword matching is deliberately broad: showing the crisis line to someone who
did not need it costs little, missing someone who did costs a great deal.
Only phrases that are plainly about something else are left out (for example
"killing it at work" or "my phone died").

Numbers and links checked 2026-09-25 on veteranscrisisline.net and
department.va.gov/homeless.
"""
from __future__ import annotations

import re
from typing import Any

CRISIS_PATTERN = re.compile(
    r"\bsuicid\w*|\bself[\s-]?harm\w*|"
    r"\b(?:kill|killing|hurt|hurting|harm|harming|cut|cutting|hang|hanging|shoot|shooting)\s+myself\b|"
    r"\b(?:end|ending|take|taking)\s+(?:my\s+(?:own\s+)?life|it\s+all)\b|"
    r"\b(?:want|wanna|wish|going|ready)\s+(?:to\s+)?(?:die|be\s+dead)\b|"
    r"\bwish\s+i\s+(?:was|were)\s+dead\b|"
    r"\bbetter\s+off\s+(?:dead|without\s+me)\b|"
    r"\b(?:no|nothing\s+to)\s+(?:reason\s+to\s+)?live\s+for\b|\bno\s+reason\s+to\s+live\b|"
    r"\b(?:don'?t|do\s+not)\s+want\s+to\s+(?:live|be\s+alive|be\s+here\s+anymore|wake\s+up)\b|"
    r"\bnot\s+worth\s+living\b|\bcan'?t\s+go\s+on\b|\boverdos\w*",
    re.I,
)

HOUSING_PATTERN = re.compile(
    r"\bhomeless\w*|\bunhoused\b|"
    r"\b(?:living|sleeping|staying)\s+(?:in\s+(?:my|a|the)\s+(?:car|truck|van|tent)|"
    r"on\s+the\s+streets?|outside|rough)\b|"
    r"\bevict\w*|\bforeclos\w*|"
    r"\b(?:los(?:e|ing)|lost)\s+my\s+(?:home|house|housing|apartment|place)\b|"
    r"\bkicked\s+out\s+of\s+my\s+(?:home|house|apartment|place)\b|"
    r"\bnowhere\s+to\s+(?:live|stay|sleep|go)\b|\bno\s+place\s+to\s+(?:live|stay|sleep)\b|"
    r"\bcouch[\s-]?surf\w*|\b(?:in|at)\s+a\s+(?:homeless\s+)?shelter\b|"
    r"\b(?:behind\s+on|can'?t\s+(?:pay|afford|make))\s+(?:my\s+|the\s+)?rent\b",
    re.I,
)

CRISIS_TEXT = (
    "If you're thinking about suicide or hurting yourself, please reach out right now. "
    "The Veterans Crisis Line is free, confidential and open 24/7, and you don't need to be "
    "enrolled in VA benefits or health care to use it:\n"
    "• Call: dial 988, then press 1\n"
    "• Chat: chat live online at veteranscrisisline.net\n"
    "• Text: send a text to 838255\n"
    "If you are in immediate danger, call 911."
)

HOUSING_TEXT = (
    "If you're homeless or at risk of losing your housing, call the National Call Center for "
    "Homeless Veterans at 877-424-3838. It's free, confidential and open 24/7, and they can "
    "connect you with local VA and community housing help, such as the Supportive Services "
    "for Veteran Families (SSVF) program near you."
)

CRISIS_ACTIONS = [
    {"label": "Call 988, then press 1", "url": "tel:988"},
    {"label": "Chat live now", "url": "https://www.veteranscrisisline.net/get-help-now/chat/"},
    {"label": "Text 838255", "url": "sms:838255"},
]

HOUSING_ACTIONS = [
    {"label": "Call 877-424-3838", "url": "tel:18774243838"},
    {"label": "VA homeless programs", "url": "https://www.va.gov/homeless/nationalcallcenter.asp"},
]

# Told to the model when the server has already put a safety block above its
# reply, so it does not repeat the numbers but still responds to the person.
MODEL_NOTES = {
    "crisis": (
        "Safety: the user's latest message may mention suicide or self-harm. Jarvet has already "
        "placed the Veterans Crisis Line (988 then 1, chat, text 838255) at the top of your reply, "
        "so do not repeat those numbers. Respond with warmth and without judgment, keep it short, "
        "gently ask whether they are safe right now, and do not steer to schools or careers unless "
        "they ask."
    ),
    "housing": (
        "Safety: the user's latest message may mention being homeless or at risk of losing housing. "
        "Jarvet has already placed the National Call Center for Homeless Veterans (877-424-3838) at "
        "the top of your reply, so do not repeat that number. Acknowledge their situation, keep it "
        "practical, and still help with what they asked."
    ),
}


def check(message: str) -> list[str]:
    """Safety concerns in a user message, most urgent first: "crisis", "housing"."""
    concerns = []
    if CRISIS_PATTERN.search(message or ""):
        concerns.append("crisis")
    if HOUSING_PATTERN.search(message or ""):
        concerns.append("housing")
    return concerns


def notices(concerns: list[str]) -> list[dict[str, Any]]:
    """The fixed safety blocks for the page, in the order they should appear."""
    blocks = {
        "crisis": {"kind": "crisis", "title": "Veterans Crisis Line", "text": CRISIS_TEXT, "actions": CRISIS_ACTIONS},
        "housing": {"kind": "housing", "title": "Help with housing, 24/7", "text": HOUSING_TEXT, "actions": HOUSING_ACTIONS},
    }
    return [blocks[concern] for concern in concerns if concern in blocks]


def prefix(concerns: list[str]) -> str:
    """Safety text to put at the top of the reply (kept in the chat history too)."""
    return "\n\n".join(block["text"] for block in notices(concerns))
