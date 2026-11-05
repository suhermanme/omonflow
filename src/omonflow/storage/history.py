"""Optional transcription history (SPEC §15): SQLite in the user data dir.

Stores text only, never audio. Search, delete one, clear all.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Entry:
    id: int
    created: float
    text: str
    app: str | None
    outcome: str
    duration_s: float | None
    model: str | None


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY, created REAL NOT NULL, text TEXT NOT NULL, app TEXT,
            outcome TEXT NOT NULL, duration_s REAL, model TEXT)""")
        self._db.commit()

    def add(self, text: str, app: str | None, outcome: str, duration_s: float | None, model: str | None) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO entries (created, text, app, outcome, duration_s, model) "
                                   "VALUES (?, ?, ?, ?, ?, ?)", (time.time(), text, app, outcome, duration_s, model))
            self._db.commit()
            return cur.lastrowid

    def set_outcome(self, entry_id: int, outcome: str) -> None:
        with self._lock:
            self._db.execute("UPDATE entries SET outcome = ? WHERE id = ?", (outcome, entry_id))
            self._db.commit()

    def list(self, search: str = "", limit: int = 500) -> list[Entry]:
        q = "SELECT id, created, text, app, outcome, duration_s, model FROM entries"
        args: tuple = ()
        if search:
            q += " WHERE text LIKE ? ESCAPE '\\'"
            esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args = (f"%{esc}%",)
        q += " ORDER BY created DESC, id DESC LIMIT ?"
        with self._lock:
            return [Entry(*row) for row in self._db.execute(q, args + (limit,))]

    def delete(self, entry_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM entries")
            self._db.commit()
            self._db.execute("VACUUM")  # don't leave deleted text in free pages

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
