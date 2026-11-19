"""SQLite 连接管理与建表脚本。

设计要点
========
* 所有连接开启 WAL、外键与 ``foreign_key_cascade``，并把
  ``busy_timeout`` 调到 30s：并发导入时写事务串行化，等待方不会立即报错。
* 每个操作使用独立短连接（上下文管理器自动提交/回滚），查询以
  ``BEGIN`` 开启只读快照事务，保证一次查询内价量与事件依据来自
  *同一个截止批次下已提交* 的状态。
* 行情/公告均为 *只追加* 表，修订号与业务键上的唯一索引保证
  「同号仅高修订生效、同修订异值拒收」。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS securities (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 入库批次：每次成功导入生成一行，自增 id 即批次号（提交顺序）
CREATE TABLE IF NOT EXISTS ingest_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_kind    TEXT NOT NULL CHECK (payload_kind IN ('quotes', 'corporate_actions')),
    row_count       INTEGER NOT NULL,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 日行情（只追加，修订版本化）
CREATE TABLE IF NOT EXISTS daily_quotes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    security_code    TEXT NOT NULL REFERENCES securities(code),
    trade_date       TEXT NOT NULL,
    revision         INTEGER NOT NULL CHECK (revision >= 0),
    open             REAL NOT NULL,
    high             REAL NOT NULL,
    low              REAL NOT NULL,
    close            REAL NOT NULL,
    volume           REAL NOT NULL,
    ingest_batch_id  INTEGER NOT NULL REFERENCES ingest_batches(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (security_code, trade_date, revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_sec_date
    ON daily_quotes (security_code, trade_date);

-- 拆股 / 并股公告（只追加，修订版本化）
-- ratio = 每股老股拆成的新股数：1 拆 2 => 2.0；2 并 1 => 0.5
CREATE TABLE IF NOT EXISTS corporate_actions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    security_code    TEXT NOT NULL REFERENCES securities(code),
    event_no         TEXT NOT NULL,
    ex_date          TEXT NOT NULL,
    ratio            REAL NOT NULL CHECK (ratio > 0),
    action_type      TEXT NOT NULL DEFAULT 'split'
        CHECK (action_type IN ('split', 'reverse_split')),
    revision         INTEGER NOT NULL CHECK (revision >= 0),
    ingest_batch_id  INTEGER NOT NULL REFERENCES ingest_batches(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (security_code, event_no, revision)
);

CREATE INDEX IF NOT EXISTS idx_actions_sec_exdate
    ON corporate_actions (security_code, ex_date);
"""


def connect(db_file: str | None = None) -> sqlite3.Connection:
    """打开一个带 WAL/外键/超时设置的连接，Row 工厂便于按列名取值。"""
    path = db_file or config.db_path()
    config.ensure_parent_dir(path)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # 快照隔离：读事务期间不会看到别的连接后续提交的写入
    try:
        conn.execute("PRAGMA read_uncommitted = 0")
    except sqlite3.DatabaseError:
        pass
    return conn


@contextmanager
def get_conn(db_file: str | None = None) -> Iterator[sqlite3.Connection]:
    """短连接上下文：正常退出自动关闭，异常自动回滚。"""
    conn = connect(db_file)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_file: str | None = None) -> None:
    """建表（幂等），应用启动时调用。"""
    with get_conn(db_file) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式立即写事务：一开始就拿写锁，避免并发导入在提交时才冲突。

    任何异常都会回滚——失败的导入不会留下任何数据，也不会产生批次。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务：查询全程看到同一已提交快照（deferred 首个读语句开始）。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，回滚即结束，不改动任何数据
