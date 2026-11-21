"""SQLite 持久层：收件表、打包任务表，以及一个跨线程写锁。"""
from __future__ import annotations

import os
import sqlite3
import threading

_WRITE_LOCK = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS arrivals (
    company        TEXT NOT NULL,
    batch_no       INTEGER NOT NULL,
    max_other_ref  INTEGER NOT NULL,
    file_name      TEXT NOT NULL,
    file_size      INTEGER NOT NULL,
    file_sha256    TEXT NOT NULL,
    content        BLOB NOT NULL,
    received_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (company, batch_no)
);
CREATE TABLE IF NOT EXISTS package_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status         TEXT NOT NULL,              -- running / ready / failed
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    snapshot       TEXT,                       -- 打包开始时已收原档快照(JSON)
    pair_a         INTEGER,
    pair_b         INTEGER,
    file_path      TEXT,
    file_name      TEXT,
    zip_sha256     TEXT,
    zip_size       INTEGER,
    error          TEXT
);
"""


def db_path() -> str:
    return os.environ.get("SETTLE_DB", "/data/settle.db")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(db_path()) or ".", exist_ok=True)
    with open_db() as conn:
        conn.executescript(SCHEMA)


def write_lock() -> threading.RLock:
    return _WRITE_LOCK
