from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


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


def _same_site(candidate: str, school_url: str) -> bool:
    candidate_host = urlparse(candidate).hostname or ""
    school_host = urlparse(school_url).hostname or ""
    candidate_host = candidate_host.removeprefix("www.")
    school_host = school_host.removeprefix("www.")
    # A school's own site can redirect a subdomain registered with VA (e.g.
    # "worldwide.erau.edu") to its parent domain (e.g. "erau.edu") -- as seen
    # on every Embry-Riddle campus, whose admissions/apply links all live
    # under the bare "erau.edu", not the subdomain VA has on file for it.
    # Accepting only a candidate that's a subdomain of school_url and never
    # the reverse silently dropped every one of those links, leaving ~150
    # facilities with no Apply button despite the school plainly having one.
    return bool(candidate_host and school_host) and (
        candidate_host == school_host
        or candidate_host.endswith("." + school_host)
        or school_host.endswith("." + candidate_host)
    )


ADMISSIONS_WORDS = re.compile(
    r"\bapply\b|\bapplication\b|\badmission|\benroll|\brequest\s+info|\bget\s+started\b|"
    r"\bhow\s+to\s+apply\b",
    re.I,
)
# A link's label/URL can satisfy ADMISSIONS_WORDS above ("Apply for a Career
# at Rutgers Newark") while actually being a staff/faculty job-application
# page, not a student admissions page -- both use the exact same "apply"
# wording. Employment portals reliably live on their own "careers"/"jobs"
# subdomain or path segment (a Workday/Taleo-style HR system, distinct from
# the admissions site), so a candidate matching this is dropped regardless
# of an otherwise-matching ADMISSIONS_WORDS hit.
EMPLOYMENT_PAGE_MARKERS = re.compile(
    r"careers?\.\w|jobs?\.\w|/careers?/|/jobs?/|/employment|human[\s-]?resources|\bhiring\b|"
    r"\bjob\s+opening|\bwork(?:ing)?\s+at\b|\bstaff\s+position|\bfaculty\s+position",
    re.I,
)


_CURL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CURL_META_MARKER = "\x1e__JARVET_CURL_META__"


async def _curl_get(url: str, *, timeout: int = 12) -> tuple[int, str, str] | None:
    """GET a URL via the system curl binary instead of httpx.

    A significant fraction of school sites sit behind bot-management (the
    kind of WAF Cloudflare/Akamai offer) that fingerprints and blocks
    httpx's TLS/HTTP client signature specifically, independent of headers
    sent -- confirmed against real school sites (for example
    moorparkcollege.edu/apply, which a human browser and curl both load
    fine but httpx got a bare 403 from, headers and HTTP version made no
    difference). curl's fingerprint reliably passes, so it's used here
    instead. Returns (status_code, final_url, body) for the last hop after
    following redirects, or None if curl itself failed to run or timed out.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "curl", "-s", "-L", "--max-time", str(timeout),
            "-A", _CURL_USER_AGENT,
            "-w", _CURL_META_MARKER + "%{http_code}|%{url_effective}",
            url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout + 5)
    except (OSError, asyncio.TimeoutError):
        return None
    output = stdout.decode("utf-8", errors="replace")
    if _CURL_META_MARKER not in output:
        return None
    body, _, meta = output.rpartition(_CURL_META_MARKER)
    try:
        status_text, final_url = meta.split("|", 1)
        return int(status_text), final_url.strip(), body
    except ValueError:
        return None


async def discover_admissions_page(school_url: str) -> dict[str, str] | None:
    """Find a school's own apply/admissions page. Only looks at links on the
    homepage itself -- admissions pages are almost always in the main
    navigation or footer with predictable wording, so a deeper multi-hop
    crawl isn't needed. Fetches via curl (see _curl_get) rather than an
    httpx client, since a large share of school sites' bot-management
    blocks httpx specifically."""
    if school_url and not re.match(r"^https?://", school_url, re.I):
        school_url = f"https://{school_url}"
    parsed_school = urlparse(school_url)
    if parsed_school.scheme not in {"http", "https"} or not parsed_school.hostname:
        return None
    fetched = await _curl_get(school_url)
    if fetched is None:
        return None
    status, final_url, body = fetched
    if status >= 400:
        return None
    root = PageParser()
    root.feed(body[:1_500_000])
    # A link's own path, ignoring any query/fragment, once resolved against
    # the page it was found on -- used below to reject a candidate that only
    # points back to this same homepage.
    homepage_path = urlparse(final_url).path.rstrip("/")

    candidates: list[tuple[int, str, str]] = []
    for link in root.links:
        absolute = urljoin(final_url, link["url"])
        if not _same_site(absolute, school_url):
            continue
        if urlparse(absolute).path.rstrip("/") == homepage_path:
            # Common on sites that build their nav as a Bootstrap-style
            # dropdown: the visible toggle button itself is an <a> tagged
            # href="#" and labeled "Apply Today"/"Apply Now", with the real
            # destination link only appearing once the dropdown is opened
            # (confirmed on evc.edu: a "Apply Today" href="#" dropdown
            # toggle sits ahead of the genuine "Apply Now" link to
            # /services/admissions/apply.html later in the same page).
            # Accepting the toggle silently sends a veteran back to the
            # homepage they were already on instead of an application.
            continue
        text = link["label"] + " " + absolute
        if EMPLOYMENT_PAGE_MARKERS.search(text):
            continue
        if ADMISSIONS_WORDS.search(text):
            priority = 2 if re.search(r"\bapply\b|\bhow\s+to\s+apply\b", text, re.I) else 1
            candidates.append((priority, link["label"].strip() or "Apply", absolute))
    if not candidates:
        return None
    _, label, url = max(candidates, key=lambda item: item[0])
    return {"url": url, "label": label, "source": "Official institution website"}


async def guess_apply_path(school_url: str) -> dict[str, str] | None:
    """Fallback for when discover_admissions_page finds no admissions link on
    the homepage itself: many school sites answer a plain "/apply" path
    directly (for example moorparkcollege.edu/apply) even without linking to
    it from the homepage nav. Only counts as a match if that path actually
    resolves to a distinct, application-flavored page of its own -- a site
    with no such path commonly either 200s a generic catch-all/"not found"
    template for any unknown path, or silently redirects back to its own
    homepage, and neither should be reported as a found apply page. The
    catch-all case is caught by comparing the page against a second fetch of
    a deliberately bogus path on the same host: a site that serves the same
    templated response for both is not actually answering "/apply"
    specifically.
    """
    if school_url and not re.match(r"^https?://", school_url, re.I):
        school_url = f"https://{school_url}"
    parsed_school = urlparse(school_url)
    if parsed_school.scheme not in {"http", "https"} or not parsed_school.hostname:
        return None
    candidate_url = urljoin(school_url, "/apply")
    fetched = await _curl_get(candidate_url)
    if fetched is None:
        return None
    status, final_url, body = fetched
    if status >= 400:
        return None
    landed = urlparse(final_url)
    if landed.path.strip("/") == "" or not _same_site(final_url, school_url):
        # Landed back on the homepage root, or off-site entirely -- not a
        # real distinct /apply page.
        return None
    page = PageParser()
    page.feed(body[:1_500_000])
    haystack = page.title + " " + " ".join(page.text[:20]) + " " + final_url
    if EMPLOYMENT_PAGE_MARKERS.search(haystack) or not ADMISSIONS_WORDS.search(haystack):
        return None
    probe = await _curl_get(urljoin(school_url, "/jarvet-apply-probe-nonexistent-8f3c1d"))
    if probe is not None and probe[0] < 400 and probe[2][:2000] == body[:2000]:
        # The site answered our made-up path with byte-identical content --
        # a catch-all/soft-404 template, so "/apply" isn't a real page.
        return None
    return {
        "url": final_url,
        "label": page.title.strip() or "Apply",
        "source": "Guessed /apply path",
    }
