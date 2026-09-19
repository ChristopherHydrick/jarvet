from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ResponseCache:
    def __init__(
        self, path: Path, *, version: str = "1", max_entries: int = 500,
        ttl_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        self.path = path
        self.version = version
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.connection: sqlite3.Connection | None = None
        self.hits = 0
        self.misses = 0

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "cache_key TEXT PRIMARY KEY, response TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, accessed_at INTEGER NOT NULL)"
        )
        self.connection.commit()
        self._prune()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def key(self, payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            {"version": self.version, "payload": payload},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    def get(self, cache_key: str) -> dict[str, Any] | None:
        now = int(time.time())
        row = self._database().execute(
            "SELECT response, created_at FROM responses WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None or now - row[1] > self.ttl_seconds:
            if row is not None:
                self._database().execute(
                    "DELETE FROM responses WHERE cache_key = ?", (cache_key,)
                )
                self._database().commit()
            self.misses += 1
            return None
        self._database().execute(
            "UPDATE responses SET accessed_at = ? WHERE cache_key = ?",
            (now, cache_key),
        )
        self._database().commit()
        self.hits += 1
        return json.loads(row[0])

    def put(self, cache_key: str, response: dict[str, Any]) -> None:
        now = int(time.time())
        self._database().execute(
            "INSERT OR REPLACE INTO responses "
            "(cache_key, response, created_at, accessed_at) VALUES (?, ?, ?, ?)",
            (cache_key, json.dumps(response, separators=(",", ":")), now, now),
        )
        self._database().commit()
        self._prune()

    def stats(self) -> dict[str, int]:
        entries = self._database().execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        return {"entries": entries, "hits": self.hits, "misses": self.misses}

    def _prune(self) -> None:
        database = self._database()
        cutoff = int(time.time()) - self.ttl_seconds
        database.execute("DELETE FROM responses WHERE created_at < ?", (cutoff,))
        database.execute(
            "DELETE FROM responses WHERE cache_key IN ("
            "SELECT cache_key FROM responses ORDER BY accessed_at DESC LIMIT -1 OFFSET ?)",
            (self.max_entries,),
        )
        database.commit()

    def _database(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Response cache has not been loaded.")
        return self.connection