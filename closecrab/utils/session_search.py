"""Per-bot local SQLite session index with FTS5 dual-tokenizer search.

Borrowed design from Hermes Agent (hermes_state.py:254/:283): unicode61 for
Latin + trigram for CJK substring matching, both backed by external-content
FTS5 tables synchronised via triggers.

Used to answer queries like "what did we talk about vLLM last month" without
roundtripping to Firestore (which has no text index). One SQLite file per bot
at ~/.closecrab/sessions/{bot_name}.db.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger("closecrab.session_search")

_DEFAULT_DIR = Path.home() / ".closecrab" / "sessions"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  bot_name  TEXT NOT NULL,
  user_id   TEXT NOT NULL,
  channel   TEXT NOT NULL,
  role      TEXT NOT NULL,
  text      TEXT NOT NULL,
  log_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_ts       ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_bot_user ON messages(bot_name, user_id);
CREATE INDEX IF NOT EXISTS idx_messages_log_id   ON messages(log_id);
"""

_FTS_MAIN = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  content='messages',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
"""

_FTS_TRIGRAM = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
  text,
  content='messages',
  content_rowid='id',
  tokenize='trigram'
);
"""

_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
  INSERT INTO messages_fts_trigram(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES('delete', old.id, old.text);
  INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, text)
    VALUES('delete', old.id, old.text);
END;
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open SQLite with sensible defaults (WAL + 5s busy timeout)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class SessionIndex:
    """Per-bot SQLite + FTS5 index. Idempotent, fire-and-forget safe."""

    def __init__(self, bot_name: str, db_dir: Path | None = None):
        self.bot_name = bot_name
        self.db_path = (db_dir or _DEFAULT_DIR) / f"{bot_name}.db"
        self._initialized = False

    def init_db(self) -> None:
        """Create tables + FTS + triggers. Safe to call repeatedly."""
        if self._initialized:
            return
        with _connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            conn.execute(_FTS_MAIN)
            conn.execute(_FTS_TRIGRAM)
            conn.executescript(_TRIGGERS)
        self._initialized = True
        log.info("Session index ready: %s", self.db_path)

    def index_turn(
        self,
        user_id: str,
        channel: str,
        user_text: str,
        assistant_text: str,
        log_id: str | None = None,
        ts: int | None = None,
    ) -> None:
        """Index one conversational turn (user + assistant) as two rows.

        Fire-and-forget: caller wraps in try/except — failure here must not
        block the firestore finalize path.
        """
        self.init_db()
        ts = ts or int(time.time())
        rows: list[tuple] = []
        if user_text:
            rows.append((ts, self.bot_name, user_id, channel, "user",
                         user_text, log_id))
        if assistant_text:
            rows.append((ts, self.bot_name, user_id, channel, "assistant",
                         assistant_text, log_id))
        if not rows:
            return
        with _connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO messages(ts, bot_name, user_id, channel, role, "
                "text, log_id) VALUES (?,?,?,?,?,?,?)",
                rows,
            )

    def search(
        self,
        query: str,
        days: int | None = None,
        user_id: str | None = None,
        role: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Search both FTS5 indexes and merge results, dedupe by row id.

        Order: most recent first. Trigram results are sorted in by ts after
        unicode61 results — unicode61 typically gives better ranking for
        Latin queries, trigram catches CJK substrings unicode61 would miss.
        """
        self.init_db()
        query = query.strip()
        if not query:
            return []

        clauses: list[str] = []
        params: list = []
        if days is not None:
            clauses.append("m.ts >= ?")
            params.append(int(time.time()) - days * 86400)
        if user_id:
            clauses.append("m.user_id = ?")
            params.append(user_id)
        if role:
            clauses.append("m.role = ?")
            params.append(role)
        where_extra = (" AND " + " AND ".join(clauses)) if clauses else ""

        # FTS5 MATCH syntax: wrap query in double quotes for phrase matching
        # to bypass FTS5 operator interpretation of `:` `-` `(` etc.
        match_arg = '"' + query.replace('"', '""') + '"'

        def _fts_query(fts_table: str) -> list[dict]:
            sql = (
                f"SELECT m.id, m.ts, m.bot_name, m.user_id, m.channel, "
                f"m.role, m.text, m.log_id "
                f"FROM {fts_table} f JOIN messages m ON m.id = f.rowid "
                f"WHERE f.text MATCH ?{where_extra} "
                f"ORDER BY m.ts DESC LIMIT ?"
            )
            with _connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, [match_arg, *params, limit])
                return [dict(r) for r in cur.fetchall()]

        # Some FTS5 queries fail on one tokenizer but work on the other
        # (e.g. trigram requires queries ≥3 chars). Tolerate per-table errors.
        rows: list[dict] = []
        for table in ("messages_fts", "messages_fts_trigram"):
            try:
                rows.extend(_fts_query(table))
            except sqlite3.OperationalError as e:
                log.debug("FTS5 query on %s failed: %s", table, e)

        # Fallback: short CJK queries (<3 chars) produce no trigrams and
        # unicode61 won't match CJK substrings either — drop to LIKE %x%.
        if not rows and len(query) < 3:
            sql = (
                "SELECT id, ts, bot_name, user_id, channel, role, text, log_id "
                "FROM messages m WHERE text LIKE ?"
                + where_extra + " ORDER BY ts DESC LIMIT ?"
            )
            with _connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    sql, [f"%{query}%", *params, limit]
                )
                rows.extend(dict(r) for r in cur.fetchall())

        # Dedupe by row id; preserve recency order
        seen: set[int] = set()
        unique: list[dict] = []
        for r in sorted(rows, key=lambda x: -x["ts"]):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            unique.append(r)
            if len(unique) >= limit:
                break
        return unique

    def stats(self) -> dict:
        self.init_db()
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM messages"
            ).fetchone()["c"]
            earliest = conn.execute(
                "SELECT MIN(ts) AS t FROM messages"
            ).fetchone()["t"]
            latest = conn.execute(
                "SELECT MAX(ts) AS t FROM messages"
            ).fetchone()["t"]
            by_role = conn.execute(
                "SELECT role, COUNT(*) AS c FROM messages GROUP BY role"
            ).fetchall()
        return {
            "bot": self.bot_name,
            "db_path": str(self.db_path),
            "total_rows": total,
            "earliest_ts": earliest,
            "latest_ts": latest,
            "by_role": {r["role"]: r["c"] for r in by_role},
        }


__all__ = ["SessionIndex"]
