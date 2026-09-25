"""Search the benefits library (.cache/benefits-library.sqlite).

The library holds short passages of official VA pages, regulations (eCFR)
and PDFs, each with its source link and date -- see
scripts/init-benefits-library.py. The search_benefits_info chat tool answers
benefits questions only from these passages, so answers can cite sources
instead of relying on the model's memory (docs/counselor-plan.md).

Search combines a keyword search (FTS5) with a meaning search (the same
fastembed model as program search) by reciprocal rank fusion, so both exact
terms ("Chapter 33", "SSVF") and plain questions ("help paying rent") work.
A question naming a program (SSVF, HUD-VASH, VR&E, ...) also draws candidates
from that program's own pages and rules, and when a re-ranking model is
available (a small cross-encoder that reads question and passage together)
it puts the candidates in their final order.
The file is opened read-only; a rebuild is installed with the app stopped.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

import numpy as np

# Plain-language veteran questions should land on VA's own explanations first;
# regulations and provider-facing PDFs stay available for exact rules and
# numbers but rank a little lower when they tie.
KIND_WEIGHT = {"page": 1.0, "pdf": 0.9, "regulation": 0.85}
FUSION_K = 60
CANDIDATES = 40
# Meaning similarity below which a passage is treated as off-topic. On the
# first library build, passages that answered sample questions scored
# 0.70-0.82; an unrelated question ("best pizza in Chicago") peaked at 0.49.
MIN_SIMILARITY = 0.6
KEYWORD_ONLY_KEEP = 3
# How many of the best results also bring the passage that follows them.
CONTINUE_TOP = 4
# Candidates the re-ranker reads per question (about 1-3 s per 40 on this
# machine's CPU).
RERANK_POOL = 40
# Candidates drawn from the named program's own passages, on top of the
# library-wide ones. Needed because words like "education" or "SSVF" appear
# in thousands of passages: "Does SSVF help with education?" otherwise ranked
# the SSVF rule that says grantees must help veterans get VA education
# benefits (38 CFR 62.32) 55th by meaning and 252nd by keyword.
TOPIC_CANDIDATES = 40
# Program named in a question -> test for passages that are about it.
TOPICS: list[tuple[re.Pattern[str], Callable[[dict[str, Any]], bool]]] = [
    (re.compile(r"\bssvf\b|supportive services for veteran families", re.I),
     lambda row: "supportive-services-for-veteran-families" in row["url"] or "SSVF" in row["title"]
     or row["citation"].startswith("38 CFR 62.")),
    (re.compile(r"\bhud[\s-]?vash\b", re.I), lambda row: "hud-vash" in row["url"].lower()),
    (re.compile(r"\bgrant (?:and|&) per diem\b|\bgpd\b", re.I), lambda row: "grant-per-diem" in row["url"]),
    (re.compile(r"\bvr\s*&\s*e\b|\bvoc(?:ational)?\s+rehab|\bveteran readiness\b|\bchapter\s*31\b", re.I),
     lambda row: "vocational-rehabilitation" in row["url"] or "Vocational Rehabilitation and Employment" in row["heading"]),
    (re.compile(r"\bdea\b|\bchapter\s*35\b|\bdependents'? educational", re.I),
     lambda row: "dependents-education-assistance" in row["url"] or "chapter-35" in row["url"]
     or "Dependents' Educational Assistance" in row["heading"]),
    (re.compile(r"\bfry\b", re.I), lambda row: "fry-scholarship" in row["url"]),
    (re.compile(r"\bstate\b.*\b(?:vocational\s+)?rehab|\bdepartment of rehabilitation\b|\bdor\b|"
                r"\border of selection\b", re.I),
     lambda row: row["citation"].startswith("34 CFR 361.") or "dor.ca.gov" in row["url"]),
    (re.compile(r"\bpell\b|\bfafsa\b|\bfederal student aid\b|\bfinancial aid\b", re.I),
     lambda row: "studentaid.gov" in row["url"]),
    (re.compile(r"\bwioa\b|\bamerican job center|\bcareeronestop\b", re.I),
     lambda row: "careeronestop.org" in row["url"]),
    (re.compile(r"\byellow ribbon\b", re.I), lambda row: "yellow-ribbon" in row["url"] or "Yellow Ribbon" in row["heading"]),
]
STOPWORDS = {
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "if", "in", "is", "it", "me",
    "my", "of", "on", "or", "the", "to", "what", "when", "where", "which", "who", "why", "with",
    "you", "your", "get", "use", "about", "much", "many", "will", "be", "am", "have", "has",
}


class BenefitsLibrary:
    def __init__(
        self, path: Path, embed: Callable[[str], Any] | None = None,
        rerank: Callable[[str, list[str]], list[float]] | None = None,
    ) -> None:
        self.path = path
        self.embed = embed
        self.rerank = rerank
        self._topic_ids: list[set[int]] = []
        self.available = False
        self.info: dict[str, str] = {}
        self._rows: dict[int, dict[str, Any]] = {}
        self._ids: np.ndarray = np.zeros(0, dtype=np.int64)
        self._matrix: np.ndarray = np.zeros((0, 384), dtype=np.float32)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, check_same_thread=False)

    def load(self) -> None:
        if not self.path.exists():
            return
        database = self._connect()
        try:
            self.info = dict(database.execute("SELECT key, value FROM library_info"))
            for row in database.execute(
                "SELECT p.id, p.heading, p.text, p.url, p.citation, p.page, d.title, d.kind, "
                "d.publisher, d.updated, d.fetched FROM passages p JOIN documents d ON d.id = p.document_id"
            ):
                self._rows[row[0]] = dict(zip(
                    ("id", "heading", "text", "url", "citation", "page", "title", "kind",
                     "publisher", "updated", "fetched"), row,
                ))
            vectors = database.execute("SELECT passage_id, embedding FROM passage_embeddings").fetchall()
        finally:
            database.close()
        if vectors:
            self._ids = np.array([passage_id for passage_id, _ in vectors], dtype=np.int64)
            matrix = np.vstack([np.frombuffer(blob, dtype=np.float32) for _, blob in vectors])
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            self._matrix = matrix / np.where(norms == 0, 1, norms)
        self._topic_ids = [
            {passage_id for passage_id, row in self._rows.items() if about(row)} for _, about in TOPICS
        ]
        self.available = bool(self._rows)

    @property
    def passage_count(self) -> int:
        return len(self._rows)

    def _keyword_ranks(self, query: str, topic: set[int] | None = None) -> list[int]:
        words = [w for w in re.findall(r"[a-z0-9]+(?:/[0-9]+)?", query.lower()) if w not in STOPWORDS]
        if not words:
            return []
        # Any word may match (OR); bm25 rewards passages with more of them.
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in dict.fromkeys(words))
        database = self._connect()
        try:
            ranked = [row[0] for row in database.execute(
                "SELECT rowid FROM passages_fts WHERE passages_fts MATCH ? "
                "ORDER BY bm25(passages_fts, 2.0, 3.0, 1.0) LIMIT ?",
                (match, 1000 if topic else CANDIDATES),
            )]
            if topic:
                ranked = ranked[:CANDIDATES] + [pid for pid in ranked if pid in topic][:TOPIC_CANDIDATES]
            return list(dict.fromkeys(ranked))
        except sqlite3.OperationalError:
            return []
        finally:
            database.close()

    def _meaning_ranks(
        self, query: str, topic: set[int] | None = None,
    ) -> tuple[list[int], dict[int, float]]:
        if self.embed is None or not len(self._ids):
            return [], {}
        vector = self.embed(query)
        if vector is None:
            return [], {}
        vector = np.asarray(vector, dtype=np.float32)
        vector = vector / (np.linalg.norm(vector) or 1)
        scores = self._matrix @ vector
        order = np.argsort(-scores)
        top = [int(self._ids[i]) for i in order[:CANDIDATES]]
        if topic:
            top += [int(self._ids[i]) for i in order if int(self._ids[i]) in topic][:TOPIC_CANDIDATES]
        similarity = {int(self._ids[i]): float(scores[i]) for i in range(len(self._ids))}
        return list(dict.fromkeys(top)), similarity

    def search(self, query: str, limit: int = 6, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        """Best passages for a question, each with its source and date."""
        if not self.available or not query.strip():
            return []
        fused: dict[int, float] = {}
        topic: set[int] = set()
        for (named, _), ids in zip(TOPICS, self._topic_ids):
            if named.search(query):
                topic |= ids
        meaning_ranks, similarity = self._meaning_ranks(query, topic)
        keyword_ranks = self._keyword_ranks(query, topic)
        for ranks in (keyword_ranks, meaning_ranks):
            for rank, passage_id in enumerate(ranks):
                fused[passage_id] = fused.get(passage_id, 0.0) + 1.0 / (FUSION_K + rank)
        if similarity:
            # A passage far from the question in meaning is dropped even if
            # it shares a word ("grant" in "Pell grant" vs. SSVF grants),
            # unless it is one of the very best keyword hits (exact terms
            # like "Chapter 1606" that the meaning model handles poorly).
            best_keyword = set(keyword_ranks[:KEYWORD_ONLY_KEEP])
            fused = {
                passage_id: score for passage_id, score in fused.items()
                if similarity.get(passage_id, 0.0) >= MIN_SIMILARITY or passage_id in best_keyword
            }
            if not any(similarity.get(passage_id, 0.0) >= MIN_SIMILARITY for passage_id in fused):
                return []  # nothing in the library is about this question
        ordered = sorted(
            fused,
            key=lambda pid: -fused[pid] * KIND_WEIGHT.get(self._rows.get(pid, {}).get("kind"), 1.0)
            * (1.5 if pid in topic else 1.0),
        )
        if self.rerank is not None and ordered:
            pool = [pid for pid in ordered if pid in self._rows][:RERANK_POOL]
            try:
                scores = self.rerank(query, [
                    f"{self._rows[pid]['title']}. {self._rows[pid]['heading']}. {self._rows[pid]['text']}"
                    for pid in pool
                ])
            except Exception:  # the re-ranker is an improvement, never a requirement
                scores = None
            if scores is not None:
                reranked = dict(zip(pool, scores))
                ordered = sorted(pool, key=lambda pid: -reranked[pid]) + ordered[len(pool):]
        results = []
        used: set[int] = set()
        for passage_id in ordered:
            row = self._rows.get(passage_id)
            if row is None or passage_id in used or (kinds and row["kind"] not in kinds):
                continue
            used.add(passage_id)
            text = row["text"]
            # Passages are numbered in reading order, so the next one under
            # the same heading continues this one -- typically the rest of a
            # list or table ("Find the percentage ... 910 to 1,094 days ...")
            # that the split cut in two. The top results carry it along.
            following = self._rows.get(passage_id + 1)
            if (len(results) < CONTINUE_TOP and following and passage_id + 1 not in used
                    and following["url"] == row["url"] and following["heading"] == row["heading"]):
                text += "\n" + following["text"]
                used.add(passage_id + 1)
            results.append({**row, "text": text, "similarity": round(similarity.get(passage_id, 0.0), 3)})
            if len(results) >= limit:
                break
        return results


def source_label(row: dict[str, Any]) -> str:
    """How to cite a passage: 'Post-9/11 GI Bill (Chapter 33), VA.gov, updated 2026-07-23'."""
    if row["kind"] == "regulation" and row.get("citation"):
        return f"{row['citation']} ({row['heading'].split(' > ')[-1]}), current as of {row['updated']}"
    label = row["title"]
    if row.get("page"):
        label += f", p. {row['page']}"
    label += f", {row['publisher']}"
    if row.get("updated"):
        label += f", updated {row['updated']}"
    return label


RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-12-v2"  # ~120 MB, downloaded once by fastembed; L-6 missed SSVF job-cost rules


def fastembed_reranker() -> Callable[[str, list[str]], list[float]]:
    """A re-ranking function for BenefitsLibrary, loading the model on first
    use; if it cannot load (no download possible), search falls back to its
    fused keyword + meaning order."""
    model: list[Any] = []

    def rerank(query: str, documents: list[str]) -> list[float]:
        if not model:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            model.append(TextCrossEncoder(RERANK_MODEL))
        return [float(score) for score in model[0].rerank(query, documents)]

    return rerank
