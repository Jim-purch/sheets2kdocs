"""SQLite access layer: schema, per-thread connections, tiny helpers."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

_local = threading.local()
_db_path: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- source (Google Sheets)
    source_url TEXT DEFAULT '',
    spreadsheet_id TEXT NOT NULL,
    gid TEXT DEFAULT '',
    sheet_name TEXT DEFAULT '',
    source_range TEXT DEFAULT '',
    source_mode TEXT NOT NULL DEFAULT 'public',  -- public | api_key | sa
    -- target (Kdocs ksheet)
    target_file_token TEXT NOT NULL,
    target_file_name TEXT DEFAULT '',
    target_sheet_id TEXT NOT NULL,
    target_sheet_name TEXT DEFAULT '',
    -- sync behaviour
    mode TEXT NOT NULL DEFAULT 'upsert',          -- upsert | replace
    key_field TEXT DEFAULT '',
    field_mapping TEXT DEFAULT '',                -- JSON {source_header: target_field}
    auto_create_fields INTEGER NOT NULL DEFAULT 1,
    coerce_numbers INTEGER NOT NULL DEFAULT 0,
    -- schedule
    schedule_type TEXT NOT NULL DEFAULT 'manual', -- manual | interval | cron
    schedule_value TEXT DEFAULT '',
    -- runtime state
    last_run_at TEXT DEFAULT '',
    last_status TEXT DEFAULT '',                  -- ok | error | running
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    task_name TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    status TEXT NOT NULL,                         -- ok | error
    rows_total INTEGER DEFAULT 0,
    created INTEGER DEFAULT 0,
    updated INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0,
    unchanged INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kdocs_tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT DEFAULT '',
    refresh_token TEXT DEFAULT '',
    expires_at INTEGER DEFAULT 0,
    open_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
"""

# Lightweight migrations for DBs created by older versions
_MIGRATIONS = [
    ("sync_tasks", "value_max_chars", "ALTER TABLE sync_tasks ADD COLUMN value_max_chars INTEGER NOT NULL DEFAULT 8000"),
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(ddl)


def init_db(db_path: str | Path) -> None:
    global _db_path
    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _new_conn()
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()


def _new_conn() -> sqlite3.Connection:
    assert _db_path is not None, "init_db() must be called first"
    con = sqlite3.connect(str(_db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def get_db() -> sqlite3.Connection:
    """Per-thread connection (safe for FastAPI threadpool + scheduler threads)."""
    con = getattr(_local, "con", None)
    if con is None:
        con = _new_conn()
        _local.con = con
    return con


def close_thread_conn() -> None:
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
        _local.con = None


def q_all(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return get_db().execute(sql, tuple(params)).fetchall()


def q_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return get_db().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] | dict = ()) -> int:
    con = get_db()
    args = params if isinstance(params, dict) else tuple(params)
    cur = con.execute(sql, args)
    con.commit()
    return cur.lastrowid if cur.lastrowid is not None else cur.rowcount
