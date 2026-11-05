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
    raw_text: str | None = None  # transcript before refinement (only when refinement changed it)


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY, created REAL NOT NULL, text TEXT NOT NULL, app TEXT,
            outcome TEXT NOT NULL, duration_s REAL, model TEXT)""")
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(entries)")}
        if "raw_text" not in cols:  # M07: transcript before refinement, when it differs
            self._db.execute("ALTER TABLE entries ADD COLUMN raw_text TEXT")
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

    def set_text(self, entry_id: int, text: str, raw_text: str | None = None) -> None:
        """Refined text replaces the entry's text; the original transcript is kept in raw_text."""
        with self._lock:
            self._db.execute("UPDATE entries SET text = ?, raw_text = COALESCE(raw_text, ?) WHERE id = ?",
                             (text, raw_text, entry_id))
            self._db.commit()

    def raw_text(self, entry_id: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT raw_text FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return row[0] if row else None

    def list(self, search: str = "", limit: int = 500) -> list[Entry]:
        q = "SELECT id, created, text, app, outcome, duration_s, model, raw_text FROM entries"
        args: tuple = ()
        if search:
            q += " WHERE text LIKE ? ESCAPE '\\' OR raw_text LIKE ? ESCAPE '\\'"
            esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args = (f"%{esc}%", f"%{esc}%")
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
