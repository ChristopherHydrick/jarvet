from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import run_agent
from app.cache import HttpCache, ResponseCache
from app.ipeds import IpedsIndex
from app.onet import OnetGraph
from app.va import VaComparison

ROOT = Path(__file__).resolve().parent.parent
index = OnetGraph(ROOT / ".cache" / "onet-store")
va_index = VaComparison(ROOT / ".cache" / "va-comparison.sqlite")
ipeds_index = IpedsIndex(ROOT / ".cache" / "ipeds.sqlite")
response_cache = ResponseCache(
    ROOT / ".cache" / "chat-responses.sqlite",
    version=os.getenv("JARVET_CACHE_VERSION", "23"),
    max_entries=int(os.getenv("JARVET_CACHE_MAX_ENTRIES", "500")),
    ttl_seconds=int(os.getenv("JARVET_CACHE_TTL_SECONDS", "604800")),
)
http_cache = HttpCache(ROOT / ".cache" / "http-responses.sqlite")


@asynccontextmanager
async def lifespan(_: FastAPI):
    index.load()
    va_index.load()
    ipeds_index.load()
    response_cache.load()
    http_cache.load()
    try:
        yield
    finally:
        response_cache.close()
        http_cache.close()


app = FastAPI(title="Jarvet", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


class Message(BaseModel):
    role: str
    content: str


class SavedProvider(BaseModel):
    facility_code: str = Field(max_length=20)
    institution: str = Field(max_length=160)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=10)
    detail_url: str = Field(default="", max_length=500)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=30)
    profile: dict[str, list[str]] = Field(default_factory=dict)
    selected_occupation: dict[str, str] | None = None
    saved_providers: list[SavedProvider] = Field(default_factory=list, max_length=12)


PROFILE_FIELDS = (
    "interests", "strengths", "goals", "preferences", "constraints", "education",
    "location", "notes",
)

VA_RESOURCES = {
    "compare": {"label": "Search approved schools and employers", "url": "https://www.va.gov/education/gi-bill-comparison-tool/"},
    "eligibility": {"label": "Check education benefit eligibility", "url": "https://www.va.gov/education/eligibility/"},
    "remaining": {"label": "Check remaining GI Bill benefits", "url": "https://www.va.gov/education/check-remaining-post-9-11-gi-bill-benefits/"},
    "other": {"label": "Explore other VA education benefits", "url": "https://www.va.gov/education/other-va-education-benefits/"},
    "ojt": {"label": "Learn about OJT and apprenticeships", "url": "https://www.va.gov/education/about-gi-bill-benefits/how-to-use-benefits/on-the-job-training-apprenticeships/"},
    "apprenticeship": {"label": "Search open apprenticeship opportunities", "url": "https://www.apprenticeship.gov/apprenticeship-job-finder"},
    "vocational": {"label": "Learn about non-college programs", "url": "https://www.va.gov/education/about-gi-bill-benefits/how-to-use-benefits/non-college-degree-programs/"},
    "vre": {"label": "Explore Veteran Readiness and Employment", "url": "https://www.va.gov/careers-employment/vocational-rehabilitation/"},
    "bright": {"label": "Browse all Bright Outlook occupations", "url": "https://www.onetonline.org/find/bright?b=0"},
}


async def fetch_cached_page(
    client: httpx.AsyncClient, url: str,
) -> httpx.Response | None:
    """Fetch a page through the shared HTTP cache (7-day TTL)."""
    cached = http_cache.get(url, ttl_seconds=7 * 24 * 60 * 60)
    if cached is not None:
        body, content_type = cached
        return httpx.Response(
            200, content=body, headers={"content-type": content_type},
            request=httpx.Request("GET", url),
        )
    response = await client.get(url)
    response.raise_for_status()
    http_cache.put(url, response.content, response.headers.get("content-type", ""))
    return response


def clean_profile(raw: Any, fallback: dict[str, list[str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return fallback
    profile: dict[str, list[str]] = {}
    for field in PROFILE_FIELDS:
        values = raw.get(field, fallback.get(field, []))
        if isinstance(values, list):
            profile[field] = [str(value).strip() for value in values if str(value).strip()][:8]
    accepted: list[set[str]] = []
    for field, value in sorted(
        ((field, value) for field, values in profile.items() for value in values),
        key=lambda item: len(item[1]),
    ):
        tokens = set(re.findall(r"[a-z0-9]+", value.lower()))
        if any(
            tokens == existing
            or min(len(tokens), len(existing)) >= 3
            and len(tokens & existing) / min(len(tokens), len(existing)) >= 0.8
            for existing in accepted
        ):
            profile[field].remove(value)
        else:
            accepted.append(tokens)
    return profile


def clean_message(content: str) -> str:
    content = re.sub(r"(?is)\n\s*suggestions\s*:\s*\[.*\]\s*$", "", content)
    content = re.sub(
        r"(?im)^\s*(?:Program details|Program info|School website|Official Resources?):\s*"
        r"https?://\S+\s*$",
        "",
        content,
    )
    content = re.sub(r"https?://\S+", "", content)
    content = re.sub(r"(?m)^\s*(?:Program details|Program info|School website|Official Resources?):\s*$", "", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)
    content = re.sub(r"(?m)^\s*[-*]\s+", "- ", content)
    return content.strip()


def parse_turn(content: str, profile: dict[str, list[str]]) -> dict[str, Any]:
    candidate = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    elif not candidate.startswith("{"):
        object_start = candidate.rfind('{"message"')
        if object_start >= 0:
            candidate = candidate[object_start:]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"message": clean_message(content), "suggestions": [], "profile": profile}

    suggestions = []
    for item in parsed.get("suggestions", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", label)).strip()
        if label and value:
            suggestions.append({"label": label[:48], "value": value[:240]})
    message = clean_message(str(parsed.get("message", "")).strip() or content)
    return {
        "message": message,
        "suggestions": suggestions[:4],
        "profile": clean_profile(parsed.get("profile"), profile),
    }


def complete_suggestions(
    suggestions: list[dict[str, str]], message: str, profile: dict[str, list[str]],
) -> list[dict[str, str]]:
    context = message.lower()
    locations = profile.get("location", [])
    if re.search(r"what (?:career|kind of work)|career or work goal|field.*interest|healthcare,? trades", context):
        fallbacks = [
            {"label": "Healthcare", "value": "I'm interested in healthcare careers."},
            {"label": "Skilled trades", "value": "I'm interested in skilled trades careers."},
            {"label": "Technology", "value": "I'm interested in technology careers."},
            {"label": "Business or another field", "value": "I'm interested in business, or I want to explore another field."},
        ]
    elif re.search(r"where (?:do you|are you)|what (?:city|state|location)|zip code|your location", context):
        fallbacks = []
        if locations:
            fallbacks.append({
                "label": f"Use {locations[-1]}"[:48],
                "value": f"Use my saved location: {locations[-1]}",
            })
        fallbacks.extend([
            {"label": "Search nationwide", "value": "Search nationwide instead of limiting by location."},
            {"label": "Show remote options", "value": "Show me remote or online options."},
            {"label": "Skip location for now", "value": "Continue without using my location for now."},
        ])
    elif re.search(r"gi bill|vr&e|education benefits|benefit eligibility|benefits.*(?:have|use|left)", context):
        fallbacks = [
            {"label": "Use my GI Bill", "value": "Help me use my GI Bill benefits."},
            {"label": "Explore VR&E", "value": "Help me understand whether VR&E could apply to me."},
            {"label": "Check remaining benefits", "value": "Help me check my remaining education benefits."},
            {"label": "Compare other funding", "value": "Show me education funding options beyond the GI Bill."},
        ]
    elif re.search(r"degree|certificate|school|training|apprenticeship|on-the-job", context):
        fallbacks = [
            {"label": "Find degree programs", "value": "Help me find relevant degree programs."},
            {"label": "Find certificate training", "value": "Help me find a shorter certificate or training program."},
            {"label": "Earn while I train", "value": "Find apprenticeship or on-the-job training options."},
            {"label": "Compare career paths", "value": "Help me compare related career paths first."},
        ]
    else:
        fallbacks = [
            {"label": "Explore career ideas", "value": "Help me explore career ideas that fit me."},
            {"label": "Find school or training", "value": "Help me find school or training options."},
            {"label": "Earn while I train", "value": "Help me find paid training or apprenticeships."},
            {"label": "Understand my benefits", "value": "Help me understand which education benefits I can use."},
        ]

    completed = list(suggestions[:4])
    seen = {item["label"].casefold() for item in completed}
    for fallback in fallbacks:
        if len(completed) >= 4:
            break
        if fallback["label"].casefold() not in seen:
            completed.append(fallback)
            seen.add(fallback["label"].casefold())
    return completed


def retain_explicit_context(
    previous: dict[str, list[str]], updated: dict[str, list[str]], user_message: str,
) -> dict[str, list[str]]:
    if updated != previous or not re.search(r"\b(i|i'm|i'd|my|me)\b", user_message, re.I):
        return updated
    result = {field: list(values) for field, values in updated.items()}
    notes = result.setdefault("notes", [])
    note = user_message.strip()
    if note and note not in notes:
        notes.append(note[:240])
    return result


@app.get("/")
def home():
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "occupations": index.occupation_count,
        "va_facilities": va_index.facility_count,
        "query_engine": "Oxigraph",
        "agent": "native-tool-calling",
        "model": os.getenv("LLM_MODEL", ""),
        "response_cache": response_cache.stats(),
    }


@app.post("/api/chat")
async def chat(request: ChatRequest, response: Response):
    profile = clean_profile(request.profile, {})
    base_url = os.getenv("LLM_BASE_URL", "http://host.docker.internal:8888/v1").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "")
    cache_key = response_cache.key({
        "messages": [message.model_dump() for message in request.messages],
        "profile": profile,
        "selected_occupation": request.selected_occupation,
        "saved_providers": [provider.model_dump() for provider in request.saved_providers],
        "model": model,
    })
    cached = response_cache.get(cache_key)
    if cached is not None:
        response.headers["X-Jarvet-Cache"] = "HIT"
        return cached
    response.headers["X-Jarvet-Cache"] = "MISS"
    if not api_key:
        raise HTTPException(503, "LLM_API_KEY is not configured in the container environment.")
    try:
        result = await run_agent(
            messages=[message.model_dump() for message in request.messages],
            profile=profile,
            selected_occupation=request.selected_occupation,
            saved_providers=[provider.model_dump() for provider in request.saved_providers],
            onet=index,
            va=va_index,
            ipeds=ipeds_index,
            fetch_page=fetch_cached_page,
            official_resources=VA_RESOURCES,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(502, f"The language model agent failed: {error}") from error

    turn = parse_turn(result["content"], profile)
    turn["profile"] = retain_explicit_context(
        profile, turn["profile"], request.messages[-1].content,
    )
    turn["suggestions"] = complete_suggestions(
        turn["suggestions"], turn["message"], turn["profile"],
    )
    location_candidates = result.get("location_candidates") or []
    if location_candidates:
        turn["profile"]["location"] = list(profile.get("location", []))
        turn["suggestions"] = [
            {"label": candidate, "value": candidate}
            for candidate in location_candidates
        ]
    location = result.get("resolved_location")
    if location:
        normalized_label = re.sub(r"[^a-z0-9]+", " ", location["label"].lower()).strip()
        turn["profile"]["location"] = [
            value for value in turn["profile"].setdefault("location", [])
            if re.sub(r"[^a-z0-9]+", " ", value.lower()).strip() != normalized_label
        ]
        turn["profile"]["location"].append(location["label"])
    api_response = {
        **turn,
        "resources": result["resources"],
        "matches": result["matches"][:3],
        "selected_occupation": result["selected_occupation"],
    }
    response_cache.put(cache_key, api_response)
    return api_response
