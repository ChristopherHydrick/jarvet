"""Build Jarvet's benefits library (docs/counselor-plan.md, phase 1).

Official sources listed in scripts/benefits-sources.json -- VA.gov pages (by
sitemap prefix), VA homeless-program pages, regulations from the eCFR API and
official PDFs -- are downloaded, split into short passages (keeping each
passage's source link, date, section heading and PDF page number), and stored
in their own database file, .cache/benefits-library.sqlite, with a keyword
index (FTS5) and meaning vectors (fastembed, the same model as program search).
The app reads it through app/benefits.py for the search_benefits_info tool.

Two steps:

  fetch  -- Windows Python (needs pypdf: `python -m pip install --user pypdf`).
            Downloads everything into .cache/benefits-library/raw/ (one JSON
            file per document) and writes raw/manifest.json. Safe while the
            app runs: it touches no database.

            python scripts/init-benefits-library.py fetch

  build  -- splits passages and writes a NEW database file (default
            .cache/benefits-library.new.sqlite). Embeddings need fastembed,
            so run it in a throwaway jarvet-dev container:

            docker run --rm -v C:/Users/chris/jarvet:/workspace -w /workspace jarvet-dev bash -c
              "tar -C /tmp -xf .cache/_related_fields/fastembed_cache.tar &&
               .venv/bin/python scripts/init-benefits-library.py build"

Then install it with the app stopped (the app holds the old file open):
  docker stop jarvet; mv .cache/benefits-library.new.sqlite .cache/benefits-library.sqlite;
  docker start jarvet
The main database (va-comparison.sqlite) is never touched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "scripts" / "benefits-sources.json"
DEFAULT_RAW = ROOT / ".cache" / "benefits-library" / "raw"
RAW = DEFAULT_RAW  # --raw overrides (the monthly refresh keeps its own copy)
DEFAULT_OUT = ROOT / ".cache" / "benefits-library.new.sqlite"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36 Jarvet"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# Passage size in words: long enough to hold a whole rule or answer, short
# enough that the model gets several different passages per question.
PASSAGE_WORDS = 170
PASSAGE_MAX_WORDS = 260


# ---------------------------------------------------------------- fetching

def get(url: str, *, compressed: bool = False, tries: int = 3) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if compressed:
        headers["Accept-Encoding"] = "gzip"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    body = gzip.decompress(body)
                return body
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == tries - 1 or (isinstance(error, urllib.error.HTTPError) and error.code == 404):
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


SKIP_TAGS = {"script", "style", "nav", "header", "footer", "form", "svg", "noscript", "button",
             "va-button", "va-back-to-top", "select", "iframe", "aside"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "table", "tr", "section", "article", "br", "dd", "dt",
              "va-accordion-item", "va-alert", "va-summary-box", "blockquote"}
HEADINGS = {"h1", "h2", "h3", "h4"}
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}


class PageText(HTMLParser):
    """Main text of a VA page as (heading, text) sections.

    Keeps only the <article> (VA.gov) or <main> (department.va.gov) element,
    drops navigation/scripts, and starts a new section at each h2-h4. VA
    web components carry their visible text in attributes (va-link text=,
    va-accordion-item header=), which are kept.
    """

    def __init__(self, root_tag: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root_tag = root_tag
        self.depth_in_root = 0
        self.skip_depth = 0
        self.heading_tag: str | None = None
        self.heading_buffer: list[str] = []
        self.title = ""
        self.sections: list[list] = [["", []]]
        self.line: list[str] = []

    def _flush_line(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.line)).strip()
        if text:
            self.sections[-1][1].append(text)
        self.line = []

    def handle_starttag(self, tag, attrs):
        if tag == self.root_tag:
            self.depth_in_root += 1
        if not self.depth_in_root or tag in VOID_TAGS and tag != "br":
            return
        if self.skip_depth or tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        attributes = dict(attrs)
        if tag in HEADINGS:
            self._flush_line()
            self.heading_tag = tag
            self.heading_buffer = []
        elif tag in BLOCK_TAGS:
            self._flush_line()
            if tag == "li":
                self.line.append("• ")
        if tag == "va-accordion-item" and attributes.get("header"):
            self.sections[-1][1].append(attributes["header"].strip() + ":")
        if tag in {"va-link", "va-link-action"} and attributes.get("text"):
            self.line.append(" " + attributes["text"] + " ")

    def handle_endtag(self, tag):
        if not self.depth_in_root:
            return
        if self.skip_depth:
            if tag not in VOID_TAGS:
                self.skip_depth -= 1
            return
        if tag == self.heading_tag:
            heading = re.sub(r"\s+", " ", "".join(self.heading_buffer)).strip()
            if tag == "h1":
                self.title = self.title or heading
            elif heading:
                self.sections.append([heading, []])
            self.heading_tag = None
        elif tag in BLOCK_TAGS:
            self._flush_line()
        if tag == self.root_tag:
            self.depth_in_root -= 1
            self._flush_line()

    def handle_data(self, data):
        if not self.depth_in_root or self.skip_depth:
            return
        if self.heading_tag:
            self.heading_buffer.append(data)
        else:
            self.line.append(data)


PUBLISHERS = {
    "www.va.gov": "VA.gov",
    "dor.ca.gov": "California Department of Rehabilitation",
    "careeronestop.org": "CareerOneStop (U.S. Department of Labor)",
    "studentaid.gov": "Federal Student Aid, U.S. Department of Education",
    "rsa.ed.gov": "Rehabilitation Services Administration, U.S. Department of Education",
}

# Page-furniture sections (sub-menus, share buttons, "related" link lists).
NOISE_HEADINGS = re.compile(r"navigation|share this|in this section|related (?:links|content|pages)|"
                            r"^events$|^need help\??$|^on this page$", re.I)


def page_date(raw: str) -> str:
    match = re.search(r"Last updated:.{0,40}?<time dateTime=\"(\d{4}-\d{2}-\d{2})", raw, re.S | re.I)
    if match:
        return match.group(1)
    match = re.search(r'"dateModified":"(\d{4}-\d{2}-\d{2})', raw)
    return match.group(1) if match else ""


def fetch_page(url: str) -> dict:
    raw = get(url).decode("utf-8", "replace")
    vagov = "www.va.gov" in url
    # VA.gov's text is in its one <article>; department.va.gov (WordPress)
    # uses <main>, where an <article> is only a news teaser and the <h1> is
    # often a generic "Featured Article" banner, so its <title> names the page.
    parser = PageText("article" if vagov and "<article" in raw else "main")
    parser.feed(raw)
    parser._flush_line()
    page_title = html.unescape(re.search(r"<title[^>]*>(.*?)</title>", raw, re.S).group(1))
    page_title = re.split(r"\s+[|]\s+|\s+-\s+VA Homeless Programs", page_title)[0].strip()
    title = parser.title if vagov and parser.title else page_title
    sections = [
        {"heading": heading, "text": "\n".join(lines)} for heading, lines in parser.sections
        if lines and not NOISE_HEADINGS.search(heading)
    ]
    publisher = next(
        (name for domain, name in PUBLISHERS.items() if domain in url), "VA Homeless Programs Office",
    )
    return {"url": url, "title": title, "kind": "page", "publisher": publisher,
            "updated": page_date(raw), "sections": sections}


def fetch_regulation(title: int, part: int, name: str) -> dict:
    titles = json.loads(get("https://www.ecfr.gov/api/versioner/v1/titles.json"))
    as_of = next(t["up_to_date_as_of"] for t in titles["titles"] if t["number"] == title)
    xml = get(f"https://www.ecfr.gov/api/versioner/v1/full/{as_of}/title-{title}.xml?part={part}", compressed=True)
    tree = ET.fromstring(xml)
    sections = []
    subpart = ""
    for element in tree.iter():
        if element.tag == "DIV6":
            head = element.find("HEAD")
            subpart = re.sub(r"\s+", " ", "".join(head.itertext())).strip() if head is not None else ""
        if element.tag != "DIV8":
            continue
        head = element.find("HEAD")
        heading = re.sub(r"\s+", " ", "".join(head.itertext())).strip() if head is not None else ""
        paragraphs = []
        for child in element:
            if child.tag in {"HEAD", "CITA", "SECAUTH"}:
                continue
            text = re.sub(r"\s+", " ", "".join(child.itertext())).strip()
            if text:
                paragraphs.append(text)
        number = element.get("N", "")
        if paragraphs and "[Reserved]" not in heading:
            sections.append({
                "heading": heading, "text": "\n".join(paragraphs), "subpart": subpart,
                "citation": f"{title} CFR {number}",
                "url": f"https://www.ecfr.gov/current/title-{title}/section-{number}",
            })
    return {"url": f"https://www.ecfr.gov/current/title-{title}/part-{part}", "title": name,
            "kind": "regulation", "publisher": "eCFR (Code of Federal Regulations)",
            "updated": as_of, "sections": sections}


def fetch_pdf(url: str, title: str, publisher: str) -> dict:
    from io import BytesIO
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(get(url)))
    sections = []
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if len(text.split()) >= 15:
            sections.append({"heading": "", "text": text, "page": number})
    match = re.search(r"/uploads/sites/\d+/(\d{4})/(\d{2})/", url)
    updated = f"{match.group(1)}-{match.group(2)}" if match else ""
    return {"url": url, "title": title, "kind": "pdf", "publisher": publisher,
            "updated": updated, "sections": sections}


def vagov_urls(sources: dict) -> list[str]:
    urls = []
    for sitemap in sources["vagov_sitemaps"]:
        body = get(sitemap, compressed=True).decode("utf-8")
        urls += re.findall(r"<loc>([^<]+)</loc>", body)
    keep = [
        url for url in urls
        if any(url.startswith(prefix) for prefix in sources["vagov_prefixes"])
        and not any(word in url for word in sources["vagov_exclude"])
    ]
    return sorted(set(keep))


def document_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def fetch(args: argparse.Namespace) -> int:
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    RAW.mkdir(parents=True, exist_ok=True)
    jobs = [("page", url) for url in vagov_urls(sources) + sources["pages"]]
    jobs += [("regulation", item) for item in sources["regulations"]]
    jobs += [("pdf", item) for item in sources["pdfs"]]
    manifest = {"fetched": dt.date.today().isoformat(), "documents": [], "failures": []}
    for number, (kind, item) in enumerate(jobs, start=1):
        label = item if isinstance(item, str) else item.get("url") or item["name"]
        try:
            if kind == "page":
                document = fetch_page(item)
            elif kind == "regulation":
                document = fetch_regulation(item["title"], item["part"], item["name"])
            else:
                document = fetch_pdf(item["url"], item["title"], item["publisher"])
        except Exception as error:  # keep going; report at the end
            manifest["failures"].append({"source": label, "error": str(error)[:200]})
            print(f"[{number}/{len(jobs)}] FAILED {label}: {error}", flush=True)
            continue
        document["id"] = document_id(document["url"])
        document["fetched"] = manifest["fetched"]
        words = sum(len(section["text"].split()) for section in document["sections"])
        if words < 40:
            manifest["failures"].append({"source": label, "error": f"only {words} words of text"})
            print(f"[{number}/{len(jobs)}] EMPTY {label} ({words} words)", flush=True)
            continue
        (RAW / f"{document['id']}.json").write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest["documents"].append({"id": document["id"], "url": document["url"], "title": document["title"],
                                      "kind": document["kind"], "updated": document["updated"], "words": words})
        print(f"[{number}/{len(jobs)}] {kind:10} {words:6} words  {document['title'][:70]}", flush=True)
        time.sleep(0.3)
    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    kept = {entry["id"] for entry in manifest["documents"]}
    for stale in RAW.glob("*.json"):
        if stale.name != "manifest.json" and stale.stem not in kept:
            stale.unlink()
    print(f"\n{len(manifest['documents'])} documents, {len(manifest['failures'])} failures -> {RAW}")
    return 0


# ---------------------------------------------------------------- building

LETTERED_PARAGRAPH = re.compile(r"\((?![ivx]\))[a-z]\)\s")


def split_passages(text: str, split_lettered: bool = False) -> list[str]:
    """Split text into ~PASSAGE_WORDS-word passages on line boundaries."""
    passages, current, count = [], [], 0
    for line in text.split("\n"):
        words = line.split()
        if not words:
            continue
        # One very long line (a PDF page or a long regulation paragraph) is
        # cut on sentence ends.
        pieces = [line]
        if len(words) > PASSAGE_MAX_WORDS:
            pieces, piece = [], []
            for sentence in re.split(r"(?<=[.;:])\s+", line):
                piece.append(sentence)
                if len(" ".join(piece).split()) >= PASSAGE_WORDS:
                    pieces.append(" ".join(piece))
                    piece = []
            if piece:
                pieces.append(" ".join(piece))
        for piece in pieces:
            size = len(piece.split())
            # A regulation's lettered paragraph -- "(e) General housing
            # stability assistance." -- starts a new passage, so each rule is
            # found on its own instead of being buried in the tail of the
            # previous one (62.34(e), which pays for job certifications,
            # was lost behind "(4) Moving costs ..."). i, v and x are left
            # out: they are usually roman-numeral list items of the paragraph.
            if split_lettered and current and count >= 12 and LETTERED_PARAGRAPH.match(piece):
                passages.append("\n".join(current))
                current, count = [], 0
            if current and count + size > PASSAGE_MAX_WORDS:
                passages.append("\n".join(current))
                current, count = [], 0
            current.append(piece)
            count += size
            if count >= PASSAGE_WORDS:
                passages.append("\n".join(current))
                current, count = [], 0
    if current:
        # A short tail joins the passage before it rather than standing alone.
        if passages and count < 40 and len(passages[-1].split()) + count <= PASSAGE_MAX_WORDS + 40:
            passages[-1] += "\n" + "\n".join(current)
        else:
            passages.append("\n".join(current))
    return passages


SCHEMA = """
CREATE TABLE documents (
  id TEXT PRIMARY KEY, url TEXT, title TEXT, kind TEXT, publisher TEXT,
  updated TEXT, fetched TEXT
);
CREATE TABLE passages (
  id INTEGER PRIMARY KEY, document_id TEXT REFERENCES documents(id),
  heading TEXT, text TEXT, url TEXT, citation TEXT, page INTEGER
);
CREATE VIRTUAL TABLE passages_fts USING fts5(
  title, heading, text, content='', tokenize='porter unicode61'
);
CREATE TABLE passage_embeddings (passage_id INTEGER PRIMARY KEY, embedding BLOB);
CREATE TABLE library_info (key TEXT PRIMARY KEY, value TEXT);
"""


def build(args: argparse.Namespace) -> int:
    manifest = json.loads((RAW / "manifest.json").read_text(encoding="utf-8"))
    out = Path(args.out)
    if out.exists():
        out.unlink()
    database = sqlite3.connect(out)
    database.executescript(SCHEMA)
    rows = []
    # The same paragraph often appears on many pages (the homeless pages all
    # end with "Contact VA for help ..."); only its first copy is kept, so
    # one question does not return the same text several times.
    seen: set[str] = set()
    # Regulation sections written for grant administrators (see
    # "exclude_sections" in benefits-sources.json) are left out.
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    excluded = [re.compile(item["exclude_sections"]) for item in sources["regulations"]
                if item.get("exclude_sections")]
    # ... or only the listed sections are kept ("include_sections").
    included = {(str(item["title"]), str(item["part"])): re.compile(item["include_sections"])
                for item in sources["regulations"] if item.get("include_sections")}
    for entry in manifest["documents"]:
        document = json.loads((RAW / f"{entry['id']}.json").read_text(encoding="utf-8"))
        database.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
            (document["id"], document["url"], document["title"], document["kind"],
             document["publisher"], document["updated"], document["fetched"]),
        )
        for section in document["sections"]:
            number = section.get("citation", "").split(" CFR ")[-1]
            if section.get("citation") and any(pattern.match(number) for pattern in excluded):
                continue
            only = included.get((section.get("citation", "").split(" ")[0], number.split(".")[0]))
            if section.get("citation") and only and not only.match(number):
                continue
            heading = section["heading"]
            if section.get("subpart"):
                heading = f"{section['subpart']} > {heading}"
            for text in split_passages(section["text"], split_lettered=document["kind"] == "regulation"):
                key = re.sub(r"\W+", " ", text.lower()).strip()
                if len(text.split()) < 12 or key in seen:
                    continue
                seen.add(key)
                rows.append((document["id"], document["title"], heading, text,
                             section.get("url") or document["url"], section.get("citation", ""),
                             section.get("page")))
    for number, (document_id, title, heading, text, url, citation, page) in enumerate(rows, start=1):
        database.execute("INSERT INTO passages VALUES (?,?,?,?,?,?,?)",
                         (number, document_id, heading, text, url, citation, page))
        database.execute("INSERT INTO passages_fts (rowid, title, heading, text) VALUES (?,?,?,?)",
                         (number, title, heading, text))
    print(f"{len(manifest['documents'])} documents, {len(rows)} passages", flush=True)

    embedded = 0
    if not args.no_embed:
        import numpy as np
        from fastembed import TextEmbedding
        model = TextEmbedding(EMBED_MODEL)
        # The title and heading go in with the text so a passage that says
        # "you may qualify if..." is still found by "who qualifies for SSVF".
        texts = [f"{title}. {heading}. {text}" for _, title, heading, text, *_ in rows]
        for number, vector in enumerate(model.embed(texts, batch_size=64), start=1):
            database.execute("INSERT INTO passage_embeddings VALUES (?,?)",
                             (number, np.asarray(vector, dtype=np.float32).tobytes()))
            embedded += 1
        print(f"{embedded} passage embeddings", flush=True)
    info = {"built": dt.datetime.now().isoformat(timespec="seconds"), "fetched": manifest["fetched"],
            "documents": str(len(manifest["documents"])), "passages": str(len(rows)),
            "embedding_model": EMBED_MODEL if embedded else ""}
    database.executemany("INSERT INTO library_info VALUES (?,?)", info.items())
    database.commit()
    result = database.execute("PRAGMA integrity_check").fetchone()[0]
    database.close()
    print(f"integrity {result} -> {out}")
    return 0 if result == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    steps = parser.add_subparsers(dest="step", required=True)
    steps.add_parser("fetch", help="download sources into .cache/benefits-library/raw/")
    build_parser = steps.add_parser("build", help="write the passages database")
    build_parser.add_argument("--out", default=str(DEFAULT_OUT))
    build_parser.add_argument("--no-embed", action="store_true", help="skip meaning vectors (keyword search only)")
    for step_parser in steps.choices.values():
        step_parser.add_argument("--raw", default=str(DEFAULT_RAW), help="folder for the downloaded sources")
    args = parser.parse_args()
    global RAW
    RAW = Path(args.raw)
    return fetch(args) if args.step == "fetch" else build(args)


if __name__ == "__main__":
    sys.exit(main())
