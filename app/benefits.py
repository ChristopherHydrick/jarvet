"""Search the benefits library (.cache/benefits-library.sqlite).

The library holds short passages of official VA pages, regulations (eCFR)
and PDFs, each with its source link and date -- see
scripts/init-benefits-library.py. The search_benefits_info chat tool answers
benefits questions only from these passages, so answers can cite sources
instead of relying on the model's memory (docs/counselor-plan.md).

Search combines a keyword search (FTS5) with a meaning search (the same
fastembed model as program search) by reciprocal rank fusion, so both exact
terms ("Chapter 33", "SSVF") and plain questions ("help paying rent") work.
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
STOPWORDS = {
    "a", "an", "and", "are", "can", "do", "does", "for", "how", "i", "if", "in", "is", "it", "me",
    "my", "of", "on", "or", "the", "to", "what", "when", "where", "which", "who", "why", "with",
    "you", "your", "get", "use", "about", "much", "many", "will", "be", "am", "have", "has",
}


class BenefitsLibrary:
    def __init__(self, path: Path, embed: Callable[[str], Any] | None = None) -> None:
        self.path = path
        self.embed = embed
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
        self.available = bool(self._rows)

    @property
    def passage_count(self) -> int:
        return len(self._rows)

    def _keyword_ranks(self, query: str) -> list[int]:
        words = [w for w in re.findall(r"[a-z0-9]+(?:/[0-9]+)?", query.lower()) if w not in STOPWORDS]
        if not words:
            return []
        # Any word may match (OR); bm25 rewards passages with more of them.
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in dict.fromkeys(words))
        database = self._connect()
        try:
            return [row[0] for row in database.execute(
                "SELECT rowid FROM passages_fts WHERE passages_fts MATCH ? "
                "ORDER BY bm25(passages_fts, 2.0, 3.0, 1.0) LIMIT ?", (match, CANDIDATES),
            )]
        except sqlite3.OperationalError:
            return []
        finally:
            database.close()

    def _meaning_ranks(self, query: str) -> tuple[list[int], dict[int, float]]:
        if self.embed is None or not len(self._ids):
            return [], {}
        vector = self.embed(query)
        if vector is None:
            return [], {}
        vector = np.asarray(vector, dtype=np.float32)
        vector = vector / (np.linalg.norm(vector) or 1)
        scores = self._matrix @ vector
        top = np.argsort(-scores)[:CANDIDATES]
        return [int(self._ids[i]) for i in top], {int(self._ids[i]): float(scores[i]) for i in top}

    def search(self, query: str, limit: int = 6, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        """Best passages for a question, each with its source and date."""
        if not self.available or not query.strip():
            return []
        fused: dict[int, float] = {}
        meaning_ranks, similarity = self._meaning_ranks(query)
        keyword_ranks = self._keyword_ranks(query)
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
        results = []
        used: set[int] = set()
        for passage_id, score in sorted(
            fused.items(),
            key=lambda item: -item[1] * KIND_WEIGHT.get(self._rows.get(item[0], {}).get("kind"), 1.0),
        ):
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
