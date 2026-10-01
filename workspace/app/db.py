"""SQLite 持久化层。

所有状态（场景数据、规划候选、发布命令、出发记录）都落在同一个 SQLite
文件中。进程重启后直接读库恢复，无需任何额外快照。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("EVAC_DB", os.path.join(os.path.dirname(__file__), "..", "data", "evac.db"))
DB_PATH = os.path.abspath(DB_PATH)

# 规划/发布类事务用一把进程内锁串行化，配合 BEGIN IMMEDIATE，
# 保证“多名指挥员同时发布，只有一份生效”。
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cells(
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    lat       REAL NOT NULL,
    lng       REAL NOT NULL,
    threat    TEXT NOT NULL DEFAULT 'watch',      -- watch / threatened / burning / safe
    threat_ts TEXT
);

CREATE TABLE IF NOT EXISTS nodes(
    id   TEXT PRIMARY KEY,
    lat  REAL NOT NULL,
    lng  REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'junction'         -- junction / shelter
);

CREATE TABLE IF NOT EXISTS edges(
    id       TEXT PRIMARY KEY,
    a        TEXT NOT NULL,
    b        TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    status   TEXT NOT NULL DEFAULT 'open',       -- open / closed
    capacity INTEGER NOT NULL DEFAULT 1,          -- 每批次允许通过的车次
    length_m REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shelters(
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    beds          INTEGER NOT NULL,
    accessible    INTEGER NOT NULL DEFAULT 1,
    accepting     INTEGER NOT NULL DEFAULT 1,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS vehicles(
    id           TEXT PRIMARY KEY,
    plate        TEXT NOT NULL,
    seats        INTEGER NOT NULL,
    accessible   INTEGER NOT NULL DEFAULT 0,
    home_node    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'available' -- available / broken / departed
);

CREATE TABLE IF NOT EXISTS households(
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    persons      INTEGER NOT NULL,
    needs_assist INTEGER NOT NULL DEFAULT 0,
    pets         TEXT NOT NULL DEFAULT '',
    contact      TEXT NOT NULL DEFAULT '',
    cell_id      TEXT NOT NULL,
    pickup_node  TEXT NOT NULL,
    departed     INTEGER NOT NULL DEFAULT 0,
    departure_ts TEXT,
    order_id     INTEGER,
    shelter_id   TEXT,
    batch        INTEGER
);

CREATE TABLE IF NOT EXISTS plans(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  TEXT NOT NULL,
    created_by  TEXT NOT NULL DEFAULT '',
    base_version INTEGER NOT NULL,
    profile     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'candidate', -- candidate / published / superseded
    start_batch INTEGER NOT NULL,
    num_assigned INTEGER NOT NULL,
    unassigned  INTEGER NOT NULL,
    objective   INTEGER NOT NULL,
    summary     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS plan_assignments(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id    INTEGER NOT NULL,
    household_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    shelter_id TEXT NOT NULL,
    batch      INTEGER NOT NULL,
    path       TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS orders(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL UNIQUE,
    published_ts TEXT NOT NULL,
    published_by TEXT NOT NULL DEFAULT '',
    base_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # check_same_thread=False：FastAPI 在线程池中运行同步端点；
    # 写操作由进程内 RLock + BEGIN IMMEDIATE 串行化，WAL 允许并发读
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """串行化的写事务（BEGIN IMMEDIATE）。"""
    c = conn()
    with _write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def get_meta(key: str, default: str | None = None) -> str | None:
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    conn().execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def bump_version() -> int:
    """场景版本号 +1。任何改变资源/道路/火线/出发状态的操作都会触发。"""
    with tx() as c:
        v = int(get_meta("version", "0") or "0") + 1
        c.execute(
            "INSERT INTO meta(key,value) VALUES('version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(v),),
        )
    return v


def version() -> int:
    return int(get_meta("version", "0") or "0")


def log_event(kind: str, detail: str) -> None:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with tx() as c:
        c.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)", (ts, kind, detail))
