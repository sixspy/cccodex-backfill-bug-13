import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发值班时先排队等写锁，而不是直接抛 "database is locked"
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_immediate(db_path: str | None = None):
    """写事务：进入即拿 SQLite 写锁（BEGIN IMMEDIATE）。

    左右值班同时点补交时，第二路会在 busy_timeout 内排队，等第一路提交后
    再看到真实落库/补交记录，因此不会双写补交行。异常时整笔回滚。
    """
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extract_marks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_key TEXT NOT NULL,
  extract_ok INTEGER NOT NULL,
  load_ok INTEGER NOT NULL,
  attempt TEXT NOT NULL DEFAULT ''
);

-- 半截补交样本：目标表、应有行数、本次实际抽取行数、收口状态
CREATE TABLE IF NOT EXISTS load_samples (
  slice_key TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  expected_rows INTEGER NOT NULL,
  extracted_rows INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('半截','假齐','已收口')),
  reason TEXT NOT NULL DEFAULT ''
);

-- 落库行（目标表）。唯一索引是写入瞬间的硬闸：
-- 同一 (目标表, 切片, 行键) 只能有一行，半截残行占住落点时重灌会被拦下。
CREATE TABLE IF NOT EXISTS landing_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_table TEXT NOT NULL,
  slice_key TEXT NOT NULL,
  row_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('半截','已落库')),
  attempt TEXT NOT NULL,
  UNIQUE (target_table, slice_key, row_key)
);

-- 补交台账：一个切片只允许一行；左右值班并发点补交时由唯一索引 + 写锁兜住，不双写。
-- attempt 记录这笔记账对应的抽取批次，用来区分“重复点击”与“上游重抽后再补交”。
CREATE TABLE IF NOT EXISTS resubmit_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_key TEXT NOT NULL UNIQUE,
  attempt TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 只读归档：补交时把半截残行整行留存进来，再清出目标表。
CREATE TABLE IF NOT EXISTS landing_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_table TEXT NOT NULL,
  slice_key TEXT NOT NULL,
  row_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  attempt TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_landing_archive_slice
  ON landing_archive(target_table, slice_key);

-- 同一批次同一行只归档一次：重挂样本/重试补交不会把归档越堆越多
CREATE UNIQUE INDEX IF NOT EXISTS uq_landing_archive_row
  ON landing_archive(target_table, slice_key, row_key, attempt);

CREATE TRIGGER IF NOT EXISTS landing_archive_no_update
BEFORE UPDATE ON landing_archive
BEGIN
  SELECT RAISE(ABORT, '归档只读，不可改写');
END;

CREATE TRIGGER IF NOT EXISTS landing_archive_no_delete
BEFORE DELETE ON landing_archive
BEGIN
  SELECT RAISE(ABORT, '归档只读，不可删除');
END;

"""


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列：extract_marks.attempt / resubmit_ledger.attempt 是抽取批次号，
    用于让抽取标记与落库行同源、并区分重复点击与上游重抽后的再次补交。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(extract_marks)").fetchall()}
    if "attempt" not in cols:
        conn.execute("ALTER TABLE extract_marks ADD COLUMN attempt TEXT NOT NULL DEFAULT ''")
    lcols = {r["name"] for r in conn.execute("PRAGMA table_info(resubmit_ledger)").fetchall()}
    if "attempt" not in lcols:
        conn.execute("ALTER TABLE resubmit_ledger ADD COLUMN attempt TEXT NOT NULL DEFAULT ''")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        wid = cur.lastrowid
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        # placeholder so list is non-empty; slices created by plan
        _ = wid
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('extract_flag','齐全')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('load_flag','半截')")
