from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

STOP_WORDS = {
    "and", "at", "college", "general", "of", "program", "school", "technology",
    "technician", "the", "university",
}

Fetch = Callable[[str], object]


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self.links: list[dict[str, str]] = []
        self.in_title = False
        self.current_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "a" and attributes.get("href"):
            self.current_link = {"url": attributes["href"] or "", "label": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self.current_link is not None:
            self.current_link["label"] = " ".join(self.current_link["label"].split())
            self.links.append(self.current_link)
            self.current_link = None

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        self.text.append(value)
        if self.in_title:
            self.title += value + " "
        if self.current_link is not None:
            self.current_link["label"] += value + " "


def _terms(value: str) -> set[str]:
    return {
        term for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) > 2 and term not in STOP_WORDS
    }


def _subject_terms(program: str) -> set[str]:
    segments = re.split(r"[/,;&()]|\band\b", program.lower())
    terms = _terms(program)
    distinctive = {
        term for segment in segments for term in _terms(segment)
        if term not in {"general", "technology", "technician"}
    }
    return distinctive or terms


def _same_site(candidate: str, school_url: str) -> bool:
    candidate_host = urlparse(candidate).hostname or ""
    school_host = urlparse(school_url).hostname or ""
    candidate_host = candidate_host.removeprefix("www.")
    school_host = school_host.removeprefix("www.")
    return bool(candidate_host and school_host) and (
        candidate_host == school_host or candidate_host.endswith("." + school_host)
    )


async def _safe_fetch(
    fetch: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]],
    client: httpx.AsyncClient, url: str,
) -> httpx.Response | None:
    try:
        return await fetch(client, url)
    except httpx.HTTPError:
        return None


async def discover_program_page(
    school: str, program: str, school_url: str,
    fetch: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]] | None = None,
) -> dict[str, str] | None:
    # Source website fields (VA workbook, IPEDS) often omit the scheme
    # (e.g. "www.example.edu/"); urlparse treats that as a bare path with no
    # hostname, so without this it silently bails out before crawling at all.
    if school_url and not re.match(r"^https?://", school_url, re.I):
        school_url = f"https://{school_url}"
    parsed_school = urlparse(school_url)
    if parsed_school.scheme not in {"http", "https"} or not parsed_school.hostname:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Jarvet/1.0; program-link-verifier)"}

    async def default_fetch(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        return await client.get(url)

    fetch = fetch or default_fetch
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
            response = await fetch(client, school_url)
            if response is None:
                return None
            response.raise_for_status()
            root = PageParser()
            root.feed(response.text[:1_500_000])
            subject_terms = _subject_terms(program)
            discovery_words = re.compile(
                r"academic|career|catalog|certificate|degree|department|field|program|study",
                re.I,
            )
            ranked_frontier: list[tuple[int, str]] = []
            for link in root.links:
                absolute = urljoin(str(response.url), link["url"])
                label_terms = _terms(link["label"] + " " + absolute)
                if _same_site(absolute, school_url) and (
                    subject_terms & label_terms or discovery_words.search(link["label"] + " " + absolute)
                ):
                    priority = len(subject_terms & label_terms) * 10
                    priority += 4 if re.search(r"academic|program|field|study", link["label"] + " " + absolute, re.I) else 0
                    ranked_frontier.append((priority, absolute))
            frontier = list(dict.fromkeys(
                url for _, url in sorted(ranked_frontier, reverse=True)
            ))[:18]

            scored: list[tuple[float, PageParser, str]] = []
            visited = {str(response.url).rstrip("/")}
            for depth in range(2):
                next_frontier: list[tuple[int, str]] = []
                candidates = []
                for url in frontier:
                    normalized_url = url.rstrip("/")
                    if normalized_url in visited:
                        continue
                    visited.add(normalized_url)
                    candidates.append(url)
                responses = await asyncio.gather(*(
                    _safe_fetch(fetch, client, url) for url in candidates
                ))
                for candidate_url, page_response in zip(candidates, responses):
                    if page_response is None:
                        continue
                    content_type = page_response.headers.get("content-type", "")
                    if "html" not in content_type:
                        continue
                    page = PageParser()
                    page.feed(page_response.text[:1_500_000])
                    final_url = str(page_response.url)
                    if not _same_site(final_url, school_url):
                        continue
                    heading_text = page.title + " " + " ".join(page.text[:120])
                    page_terms = _terms(heading_text)
                    overlap = len(subject_terms & page_terms) / max(len(subject_terms), 1)
                    identity_terms = _terms(page.title + " " + final_url)
                    subject_identity = subject_terms & identity_terms
                    detail_bonus = 0.3 if re.search(
                        r"degree|certificate|curriculum|course|program", final_url + " " + page.title,
                        re.I,
                    ) else 0
                    score = overlap * 2 + detail_bonus
                    if overlap >= 0.5 and subject_identity:
                        scored.append((score, page, final_url))
                    if depth == 0:
                        for link in page.links:
                            absolute = urljoin(final_url, link["url"])
                            link_terms = _terms(link["label"] + " " + absolute)
                            if _same_site(absolute, school_url) and (
                                subject_terms & link_terms or discovery_words.search(link["label"] + " " + absolute)
                            ):
                                priority = len(subject_terms & link_terms) * 10
                                priority += 4 if discovery_words.search(link["label"] + " " + absolute) else 0
                                next_frontier.append((priority, absolute))
                frontier = list(dict.fromkeys(
                    url for _, url in sorted(next_frontier, reverse=True)
                ))[:24]
    except httpx.HTTPError:
        return None

    if not scored:
        return None
    _, page, final_url = max(scored, key=lambda item: item[0])
    detail_links: list[tuple[int, str, str]] = []
    for link in page.links:
        absolute = urljoin(final_url, link["url"])
        if not _same_site(absolute, school_url):
            continue
        link_text = link["label"] + " " + absolute
        retains_subject = bool(subject_terms & _terms(link_text)) or absolute.startswith(final_url)
        if retains_subject and re.search(
            r"degree|certificate|curriculum|course|catalog", link_text, re.I
        ):
            priority = len(subject_terms & _terms(link_text)) * 10
            priority += 5 if re.search(r"degree|certificate", link_text, re.I) else 0
            priority -= 2 if re.search(r"college.catalog|/catalog/?$", link_text, re.I) else 0
            detail_links.append((priority, link["label"], absolute))
    preferred = max(detail_links, default=None, key=lambda item: item[0])
    preferred_url = preferred[2] if preferred else final_url
    preferred_label = preferred[1] if preferred else page.title.strip()
    return {
        "url": preferred_url,
        "label": preferred_label,
        "landing_url": final_url,
        "source": "Official institution website",
    }


ADMISSIONS_WORDS = re.compile(
    r"\bapply\b|\bapplication\b|\badmission|\benroll|\brequest\s+info|\bget\s+started\b|"
    r"\bhow\s+to\s+apply\b",
    re.I,
)


async def discover_admissions_page(
    school_url: str,
    fetch: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]] | None = None,
) -> dict[str, str] | None:
    """Find a school's own apply/admissions page. Unlike discover_program_page,
    this only looks at links on the homepage itself -- admissions pages are
    almost always in the main navigation or footer with predictable wording,
    so a deeper multi-hop crawl isn't needed."""
    if school_url and not re.match(r"^https?://", school_url, re.I):
        school_url = f"https://{school_url}"
    parsed_school = urlparse(school_url)
    if parsed_school.scheme not in {"http", "https"} or not parsed_school.hostname:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Jarvet/1.0; program-link-verifier)"}

    async def default_fetch(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        return await client.get(url)

    fetch = fetch or default_fetch
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
            response = await fetch(client, school_url)
            if response is None:
                return None
            response.raise_for_status()
            root = PageParser()
            root.feed(response.text[:1_500_000])
    except httpx.HTTPError:
        return None

    candidates: list[tuple[int, str, str]] = []
    for link in root.links:
        absolute = urljoin(str(response.url), link["url"])
        if not _same_site(absolute, school_url):
            continue
        text = link["label"] + " " + absolute
        if ADMISSIONS_WORDS.search(text):
            priority = 2 if re.search(r"\bapply\b|\bhow\s+to\s+apply\b", text, re.I) else 1
            candidates.append((priority, link["label"].strip() or "Apply", absolute))
    if not candidates:
        return None
    _, label, url = max(candidates, key=lambda item: item[0])
    return {"url": url, "label": label, "source": "Official institution website"}


async def discover_program_pages(
    programs: list[dict[str, str]],
    fetch: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response | None]] | None = None,
) -> list[dict[str, str]]:
    discoveries = await asyncio.gather(*(
        discover_program_page(
            program["school"], program["program"], program.get("url", ""), fetch,
        )
        for program in programs
    ))
    enriched = []
    for program, discovery in zip(programs, discoveries):
        item = dict(program)
        item["school_url"] = item.pop("url", "")
        if discovery:
            item["program_url"] = discovery["url"]
            item["program_page_label"] = discovery["label"]
            item["program_landing_url"] = discovery["landing_url"]
            item["link_status"] = "verified_official_program_page"
        else:
            item["link_status"] = "source_listing_only"
        enriched.append(item)
    return enriched
