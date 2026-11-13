"""异常链路服务：改判 → 复核栏 → 账册，保证三处状态同源。

约定：
- 只有短题筐行可改判；缺号、号相撞无改判入口。
- 改判先进复核栏，绝不直接进账册。
- 账册写入在单个事务内完成，失败整体回滚，不允许半截编码。
"""

import sqlite3

from app.db import (
    BUCKET_ROW_APPROVED,
    BUCKET_ROW_IN,
    BUCKET_ROW_RETURNED,
    BUCKET_ROW_REVIEW,
    CONFLICT_POLICIES,
    POLICY_REJECT,
    REVIEW_ACTIVE_STATES,
    db_transaction,
    get_db,
)
from app.services.triage import BUCKET_SHORT


class TriageError(ValueError):
    """业务规则冲突，消息可直接展示给用户。"""


class LedgerWriteError(RuntimeError):
    """账册写入中途失败（含故障注入）。"""


# 测试用故障注入：置 True 后，下一次通过写入账册会在事务中途抛错
_fail_next_ledger_write = False


def inject_ledger_write_failure() -> None:
    global _fail_next_ledger_write
    _fail_next_ledger_write = True


def lock_conflict_policy(job_id: int, policy: str) -> None:
    """锁定本任务的冲突口径，锁定后不能改口。"""
    if policy not in CONFLICT_POLICIES:
        raise TriageError("冲突口径必须是 reject 或 copy")
    db = get_db()
    job = db.execute(
        "SELECT id, status, conflict_policy FROM crawl_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise TriageError("任务不存在")
    if job["conflict_policy"] is not None and job["conflict_policy"] != policy:
        raise TriageError(
            "冲突口径已锁定为 "
            + job["conflict_policy"]
            + "，不能改口"
        )
    db.execute(
        "UPDATE crawl_jobs SET conflict_policy = ? WHERE id = ?",
        (policy, job_id),
    )
    db.commit()


def submit_appeal(job_id: int, bucket_id: int, proposed_sku: str, reason: str) -> int:
    """短题改判：先入复核栏。返回复核栏记录 id。"""
    proposed_sku = (proposed_sku or "").strip()
    reason = (reason or "").strip()
    if not reason:
        raise TriageError("改判必须填写原因")
    if not proposed_sku:
        raise TriageError("改判必须填写拟入账编码")

    db = get_db()
    # BEGIN IMMEDIATE 立即拿写锁，两人同时改判同一行时在此串行化
    db.execute("BEGIN IMMEDIATE")
    try:
        job = db.execute(
            "SELECT status, conflict_policy FROM crawl_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise TriageError("任务不存在")
        if job["status"] == "running":
            raise TriageError("筐还在写入中，禁止改判")
        if job["conflict_policy"] is None:
            raise TriageError("请先锁定冲突处理口径，再提交改判")

        row = db.execute(
            "SELECT * FROM exception_bucket WHERE id = ? AND job_id = ?",
            (bucket_id, job_id),
        ).fetchone()
        if row is None:
            raise TriageError("异常行不存在")
        if row["bucket"] != BUCKET_SHORT:
            raise TriageError("只有短题可申请改判，缺号与号相撞没有改判入口")
        if row["row_status"] == BUCKET_ROW_REVIEW:
            raise TriageError("该行已在复核栏，不能重复改判")
        if row["row_status"] not in (BUCKET_ROW_IN, BUCKET_ROW_RETURNED):
            raise TriageError("当前状态不允许改判")

        existing = db.execute(
            "SELECT id FROM review_queue WHERE bucket_id = ? "
            "AND review_status IN (?, ?, ?)",
            (bucket_id, *REVIEW_ACTIVE_STATES),
        ).fetchone()
        if existing is not None:
            raise TriageError("该短题行已有一笔复核在处理中")

        cur = db.execute(
            """
            INSERT INTO review_queue
                (job_id, bucket_id, proposed_sku, reason_text, locked_policy,
                 review_status, result_note)
            VALUES (?, ?, ?, ?, ?, 'pending', '')
            """,
            (job_id, bucket_id, proposed_sku, reason, job["conflict_policy"]),
        )
        review_id = cur.lastrowid
        db.execute(
            "UPDATE exception_bucket SET row_status = ? WHERE id = ?",
            (BUCKET_ROW_REVIEW, bucket_id),
        )
        db.commit()
        return review_id
    except sqlite3.IntegrityError as exc:
        db.rollback()
        # 部分唯一索引兜底：并发下只留一笔
        raise TriageError("该短题行的复核已存在，只保留一笔") from exc
    except Exception:
        db.rollback()
        raise


class _CommittedReturn(Exception):
    """业务分支已自行提交并正常结束，用于跳出事务块且不再回滚。"""


def approve_review(review_id: int, policy: str) -> None:
    """复核通过：按锁定口径处理账册，三处状态同源。"""
    if policy not in CONFLICT_POLICIES:
        raise TriageError("冲突口径必须是 reject 或 copy")

    global _fail_next_ledger_write
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        review = db.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        if review is None:
            raise TriageError("复核记录不存在")
        if review["review_status"] not in REVIEW_ACTIVE_STATES:
            raise TriageError("该复核已结案，不能重复通过")

        job = db.execute(
            "SELECT conflict_policy FROM crawl_jobs WHERE id = ?",
            (review["job_id"],),
        ).fetchone()
        locked = job["conflict_policy"]

        # 点通过时携带的口径与已锁定选择对不上：直接退回并写“处理不对”
        if policy != locked or review["locked_policy"] != locked:
            db.execute(
                """
                UPDATE review_queue
                SET review_status = 'rejected',
                    result_note = ?,
                    reviewed_at = datetime('now')
                WHERE id = ?
                """,
                (
                    "冲突处理口径与已锁定选择不一致（处理不对），整笔退回短题筐",
                    review_id,
                ),
            )
            db.execute(
                "UPDATE exception_bucket SET row_status = ? WHERE id = ?",
                (BUCKET_ROW_RETURNED, review["bucket_id"]),
            )
            db.commit()
            raise _CommittedReturn

        sku = review["proposed_sku"]
        in_ledger = db.execute(
            "SELECT 1 FROM crawl_ledger WHERE job_id = ? AND sku = ?",
            (review["job_id"], sku),
        ).fetchone()

        if in_ledger is not None:
            if locked == POLICY_REJECT:
                # 整笔改判失败，退回短题筐
                db.execute(
                    """
                    UPDATE review_queue
                    SET review_status = 'rejected',
                        result_note = ?,
                        reviewed_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        f"编码 {sku} 已在账册，按 reject 口径整笔改判失败，退回短题筐",
                        review_id,
                    ),
                )
                db.execute(
                    "UPDATE exception_bucket SET row_status = ? WHERE id = ?",
                    (BUCKET_ROW_RETURNED, review["bucket_id"]),
                )
                db.commit()
                raise _CommittedReturn
            # copy：生成冲突副本留在复核栏，禁止入账
            db.execute(
                """
                UPDATE review_queue
                SET review_status = 'conflict',
                    result_note = ?,
                    reviewed_at = datetime('now')
                WHERE id = ?
                """,
                (
                    f"编码 {sku} 已在账册，按 copy 口径生成冲突副本，"
                    "留在复核栏且禁止入账",
                    review_id,
                ),
            )
            # 筐行保持 in_review（与复核栏同源），不进账册
            db.commit()
            raise _CommittedReturn

        # 无冲突：原子写账。中途任何失败整体回滚，不留半截编码
        bucket_row = db.execute(
            "SELECT title FROM exception_bucket WHERE id = ?",
            (review["bucket_id"],),
        ).fetchone()
        try:
            if _fail_next_ledger_write:
                _fail_next_ledger_write = False
                raise LedgerWriteError("故障注入：账册写入中途失败")
            db.execute(
                """
                INSERT INTO crawl_ledger (job_id, sku, title, source)
                VALUES (?, ?, ?, 'appeal')
                """,
                (review["job_id"], sku, bucket_row["title"]),
            )
        except Exception as write_exc:
            # 先回滚全部写入，再在独立事务里留失败原因；筐行仍是 in_review
            db.rollback()
            _record_failure(review_id, str(write_exc))
            raise LedgerWriteError(
                f"账册写入失败，已回滚：{write_exc}"
            ) from write_exc

        db.execute(
            """
            UPDATE review_queue
            SET review_status = 'approved',
                result_note = ?,
                reviewed_at = datetime('now')
            WHERE id = ?
            """,
            (f"复核通过，编码 {sku} 已写入账册", review_id),
        )
        db.execute(
            "UPDATE exception_bucket SET row_status = ? WHERE id = ?",
            (BUCKET_ROW_APPROVED, review["bucket_id"]),
        )
        db.commit()
    except _CommittedReturn:
        return
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise


def _record_failure(review_id: int, message: str) -> None:
    db = get_db()
    with db_transaction() as tx:
        tx.execute(
            """
            UPDATE review_queue
            SET review_status = 'failed',
                result_note = ?
            WHERE id = ?
            """,
            (f"账册写入中途失败，未留下半截编码：{message}", review_id),
        )
        # 筐行保持 in_review，短题筐不得假装已空
        tx.execute(
            """
            UPDATE exception_bucket
            SET row_status = ?
            WHERE id = (SELECT bucket_id FROM review_queue WHERE id = ?)
            """,
            (BUCKET_ROW_REVIEW, review_id),
        )


def reject_review(review_id: int, note: str) -> None:
    """复核驳回：退回短题筐。"""
    note = (note or "").strip() or "复核驳回，退回短题筐"
    db = get_db()
    with db_transaction() as tx:
        review = tx.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        if review is None:
            raise TriageError("复核记录不存在")
        if review["review_status"] not in REVIEW_ACTIVE_STATES:
            raise TriageError("该复核已结案，不能驳回")
        tx.execute(
            """
            UPDATE review_queue
            SET review_status = 'rejected',
                result_note = ?,
                reviewed_at = datetime('now')
            WHERE id = ?
            """,
            (note, review_id),
        )
        tx.execute(
            "UPDATE exception_bucket SET row_status = ? WHERE id = ?",
            (BUCKET_ROW_RETURNED, review["bucket_id"]),
        )
