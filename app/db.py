import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


# 账册编码冲突时的两种口径（一旦在本任务锁定，不可改口）
POLICY_REJECT = "reject"   # 整笔改判失败，退回短题筐
POLICY_COPY = "copy"       # 生成冲突副本，留复核栏，禁止入账
CONFLICT_POLICIES = (POLICY_REJECT, POLICY_COPY)

# 筐内状态
BUCKET_ROW_IN = "in_bucket"          # 在筐
BUCKET_ROW_REVIEW = "in_review"      # 已送复核栏
BUCKET_ROW_RETURNED = "returned"     # 退回（复核驳回/冲突按 reject 口径退回）
BUCKET_ROW_APPROVED = "approved"     # 复核通过，已入账（离筐）

# 复核栏状态
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_CONFLICT = "conflict"
REVIEW_FAILED = "failed"
# 仍占用复核栏的活动状态
REVIEW_ACTIVE_STATES = (REVIEW_PENDING, REVIEW_CONFLICT, REVIEW_FAILED)


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );
            -- 账册：真正的编码账，编码在同一任务内唯一
            CREATE TABLE IF NOT EXISTS crawl_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                sku TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, sku)
            );
            -- 异常筐：一行只进一筐
            CREATE TABLE IF NOT EXISTS exception_bucket (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                bucket TEXT NOT NULL,
                source_index INTEGER NOT NULL,
                sku TEXT,
                title TEXT NOT NULL,
                reason TEXT NOT NULL,
                priority_note TEXT NOT NULL DEFAULT '',
                row_status TEXT NOT NULL DEFAULT 'in_bucket',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, source_index)
            );
            -- 复核栏：仅短题可改判进入
            CREATE TABLE IF NOT EXISTS review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                bucket_id INTEGER NOT NULL REFERENCES exception_bucket(id) ON DELETE CASCADE,
                proposed_sku TEXT NOT NULL,
                reason_text TEXT NOT NULL,
                locked_policy TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'pending',
                result_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                reviewed_at TEXT
            );
            """
        )
        # 同一短题筐行同时只允许一笔活动复核（并发只进一笔）
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_review_active
            ON review_queue(bucket_id)
            WHERE review_status IN ('pending', 'conflict', 'failed')
            """
        )
        # 老库平滑升级：补口径锁定列
        existing = {
            r["name"] for r in db.execute("PRAGMA table_info(crawl_jobs)").fetchall()
        }
        if "conflict_policy" not in existing:
            db.execute(
                "ALTER TABLE crawl_jobs ADD COLUMN conflict_policy TEXT"
            )
        db.commit()
