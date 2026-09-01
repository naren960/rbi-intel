"""SQLite access for the Python analysis layer.

Points at exactly the same file the Node ingestion/MCP layer uses. That shared
file is the whole contract between the two languages: Node owns `documents`,
`document_revisions` and `md_categories`; Python owns `clauses`, `relations`
and `requirements`; both read everything.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB = Path.home() / ".rbi-intel" / "regdata.db"


def db_path() -> Path:
    return Path(os.environ.get("RBI_INTEL_DB", DEFAULT_DB))


def connect(readonly: bool = False) -> sqlite3.Connection:
    p = db_path()
    if not p.exists():
        raise SystemExit(
            f"Database not found at {p}.\n"
            "Run the Node ingestion layer first:  npm run sync\n"
            "Or point at another file with  RBI_INTEL_DB=/path/to/regdata.db"
        )
    uri = f"file:{p}?mode=ro" if readonly else f"file:{p}"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def documents(conn: sqlite3.Connection, with_body: bool = True, doc_types: Iterable[str] | None = None):
    cols = "id, doc_type, title, date, category, ref_no, source_url, status" + (", body" if with_body else "")
    sql = f"SELECT {cols} FROM documents"
    params: list[Any] = []
    if doc_types:
        types = list(doc_types)
        sql += " WHERE doc_type IN (%s)" % ",".join("?" * len(types))
        params.extend(types)
    sql += " ORDER BY date"
    return conn.execute(sql, params).fetchall()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
