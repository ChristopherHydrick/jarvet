"""Precompute semantic embeddings for every distinct VA program description.

app/va.py's programs_for() matches a search keyword against program titles by
exact word (with word-form variants and known compound/synonym exceptions --
see DEGREE_LEVEL_TERMS, KNOWN_COMPOUND_VARIANTS, TERM_PHRASE_SYNONYMS in
app/va.py). That catches spelling variants of the SAME word, but nothing
connects a genuine synonym or abbreviation that shares no letters with the
query -- "EMT" and "EMERGENCY MEDICAL TECHNICIAN" needed a hand-written
exception before this script existed, and the same gap exists for CNA, HVAC,
CDL, and any other credential VA's program titles don't spell out the way a
veteran searches for it. Hand-curating each one as it's reported doesn't
scale, so this embeds every distinct program description once, offline, the
same way scripts/init-va-data.py already embeds provider names for semantic
employer/trade matching in search_nearby(). programs_for() uses this table as
a fallback ONLY when its precise word-match search finds nothing, comparing
the query's own embedding against this table and keeping only descriptions
above a similarity threshold -- see _semantic_program_matches() in app/va.py.

Keyed by the description TEXT itself (not facility_code + description) since
the same title repeats verbatim across many schools ("AS FIRE TECHNOLOGY"),
so this only pays the embedding cost once per distinct string, about 237,000
rows nationwide rather than 388,000.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DATABASE = Path(__file__).resolve().parent.parent / ".cache" / "va-comparison.sqlite"
BATCH_SIZE = 256


def main() -> None:
    if not DATABASE.exists():
        raise SystemExit("VA Comparison Tool index is missing. Run scripts/init-va-data.py first.")
    from fastembed import TextEmbedding
    import numpy as np

    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    if connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'program_embeddings'"
    ).fetchone() is None:
        connection.execute(
            "CREATE TABLE program_embeddings ("
            "description TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
        )
        connection.commit()

    already = {
        row[0] for row in connection.execute("SELECT description FROM program_embeddings")
    }
    all_descriptions = [
        row[0] for row in connection.execute(
            "SELECT DISTINCT description FROM va_program_search "
            "WHERE description IS NOT NULL AND description != ''"
        )
    ]
    targets = [d for d in all_descriptions if d not in already]
    if not targets:
        print(f"All {len(all_descriptions):,} distinct program descriptions already embedded.")
        connection.close()
        return
    print(
        f"Embedding {len(targets):,} of {len(all_descriptions):,} distinct program "
        f"descriptions ({len(already):,} already done)..."
    )

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    start = time.time()
    done = 0
    for offset in range(0, len(targets), BATCH_SIZE):
        chunk = targets[offset:offset + BATCH_SIZE]
        vectors = model.embed(chunk, batch_size=BATCH_SIZE)
        batch = [
            (description, np.asarray(vector, dtype=np.float32).tobytes())
            for description, vector in zip(chunk, vectors)
        ]
        connection.executemany(
            "INSERT OR REPLACE INTO program_embeddings VALUES (?, ?)", batch,
        )
        connection.commit()
        done += len(batch)
        if done % (BATCH_SIZE * 20) == 0 or done == len(targets):
            rate = done / (time.time() - start)
            print(f"  {done:,}/{len(targets):,} ({rate:.0f}/s)", flush=True)
    connection.close()
    print(f"Done in {time.time() - start:.0f}s.")


if __name__ == "__main__":
    main()
