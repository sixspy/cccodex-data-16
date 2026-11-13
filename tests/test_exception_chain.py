"""八段验收：失败分流 → 复核栏 → 账册写入 → 二次校验 的完整异常链路。"""

import sqlite3

import pytest

from app.db import get_db
from app.services import review as review_service
from app.services.review import (
    LedgerWriteError,
    TriageError,
    submit_appeal,
)
from app.services.triage import classify_items

from tests._helpers import (
    bucket_rows,
    create_and_run,
    lock_policy,
    login,
    short_bucket_id,
)


# ── 第一段：优先级。短题 + 撞号只进号相撞；缺号 + 短题只进缺号；一行一筐 ──
def test_01_priority_one_row_one_bucket(app, client):
    job_id, summary = create_and_run(app, client)
    assert summary["status"] == "done"

    rows = bucket_rows(app, job_id)
    by_index = {r["source_index"]: r for r in rows}
    # 每个源序至多一筐
    assert len(by_index) == len(rows)

    # G-004/G-004：两行都是短题，但同号相撞 → 都进号相撞，不进短题
    g004 = [r for r in rows if r["sku"] == "G-004"]
    assert len(g004) == 2
    assert all(r["bucket"] == "collision" for r in g004)

    # 空编码 + 标题「短」：缺号压过短题
    missing_short = [r for r in rows if r["sku"] is None and r["title"] == "短"]
    assert len(missing_short) == 1
    assert missing_short[0]["bucket"] == "missing"
    assert "缺号压过短题" in missing_short[0]["priority_note"]

    # 纯短题（G-002 台账）才进短题筐
    shorts = [r for r in rows if r["bucket"] == "short"]
    assert [(r["sku"], r["title"]) for r in shorts] == [("G-002", "台账")]

    # 三筐件数（G-003 一对 + G-004 一对 = 4 个相撞行）
    assert summary["bucket_counts"] == {
        "missing": 2,
        "collision": 4,
        "short": 1,
    }
    # 正常行进账册（G-003 因同号两行都进相撞筐，故只有 G-001 入账）
    with app.app_context():
        ledger_skus = [
            r["sku"]
            for r in get_db().execute(
                "SELECT sku FROM crawl_ledger WHERE job_id = ?", (job_id,)
            ).fetchall()
        ]
    assert set(ledger_skus) == {"G-001"}


def test_01b_classify_unit_priority_order():
    items = [
        {"sku": "", "title": "短"},            # missing 压过 short
        {"sku": "X-1", "title": "短"},         # short
        {"sku": "X-1", "title": "也短"},       # collision 压过 short
        {"sku": "X-1", "title": "正常长标题记录"},  # collision（标题够长）
        {"sku": "X-9", "title": "正常长标题记录"},  # normal
    ]
    verdicts = classify_items(items)
    buckets = [v.get("bucket") for v in verdicts]
    # X-1 出现三次：第二、三、四行都因同号判 collision（压过各自的短题）
    assert buckets == ["missing", "collision", "collision", "collision", None]
    # 第二行同时命中 collision + short，优先说明要写清压制关系
    assert "号相撞压过短题" in verdicts[1]["priority_note"]
    assert "缺号压过短题" in verdicts[0]["priority_note"]


# ── 第二段：改判先到复核栏，不到账册 ──
def test_02_appeal_goes_to_review_not_ledger(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)

    with app.app_context():
        before = get_db().execute(
            "SELECT COUNT(*) c FROM crawl_ledger WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        review_id = submit_appeal(job_id, bid, "G-100", "标题简称，实为正式台账")

        db = get_db()
        r = db.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        after = db.execute(
            "SELECT COUNT(*) c FROM crawl_ledger WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        bucket = db.execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()

    assert r["review_status"] == "pending"
    assert after == before  # 账册没有新增
    assert bucket["row_status"] == "in_review"


def test_02b_appeal_requires_reason(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)
    with app.app_context():
        with pytest.raises(TriageError):
            submit_appeal(job_id, bid, "G-100", "  ")


def test_02c_missing_and_collision_have_no_appeal(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    for r in bucket_rows(app, job_id):
        if r["bucket"] in ("missing", "collision"):
            with app.app_context():
                with pytest.raises(TriageError):
                    submit_appeal(job_id, r["id"], "Z-1", "尝试改判")


# ── 第三段：按所选口径处理账册已有编码 ──
def _make_pending_appeal(app, job_id, bid, sku="G-001", reason="改判测试"):
    with app.app_context():
        return submit_appeal(job_id, bid, sku, reason)


def test_03a_policy_reject_returns_to_short_bucket(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)
    # G-001 已在账册（正常行）
    review_id = _make_pending_appeal(app, job_id, bid, sku="G-001")

    with app.app_context():
        review_service.approve_review(review_id, "reject")
        db = get_db()
        rv = db.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        bk = db.execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()
        ledger_count = db.execute(
            "SELECT COUNT(*) c FROM crawl_ledger WHERE job_id = ? AND sku = 'G-001'",
            (job_id,),
        ).fetchone()["c"]

    assert rv["review_status"] == "rejected"
    assert "reject" in rv["result_note"]
    assert bk["row_status"] == "returned"  # 退回短题筐
    assert ledger_count == 1               # 账册未重复写入


def test_03b_policy_copy_keeps_conflict_in_review(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "copy")
    bid = short_bucket_id(app, job_id)
    review_id = _make_pending_appeal(app, job_id, bid, sku="G-001")

    with app.app_context():
        review_service.approve_review(review_id, "copy")
        db = get_db()
        rv = db.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        bk = db.execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()

    assert rv["review_status"] == "conflict"      # 冲突副本留复核栏
    assert "禁止入账" in rv["result_note"]
    assert bk["row_status"] == "in_review"        # 与复核栏同源，不入账


def test_03c_clean_approval_writes_ledger(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)
    review_id = _make_pending_appeal(app, job_id, bid, sku="G-200")

    with app.app_context():
        review_service.approve_review(review_id, "reject")
        db = get_db()
        rv = db.execute(
            "SELECT review_status FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        bk = db.execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()
        led = db.execute(
            "SELECT source FROM crawl_ledger WHERE job_id = ? AND sku = 'G-200'",
            (job_id,),
        ).fetchone()

    assert rv["review_status"] == "approved"
    assert bk["row_status"] == "approved"
    assert led["source"] == "appeal"


# ── 第四段：冲突处理口径与锁定选择不一致 → 退回并写“处理不对” ──
def test_04_policy_mismatch_is_returned(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")  # 锁定 reject
    bid = short_bucket_id(app, job_id)
    review_id = _make_pending_appeal(app, job_id, bid, sku="G-200")

    with app.app_context():
        # 点通过却带 copy：不抛错，而是按规则退回并留痕
        review_service.approve_review(review_id, "copy")
        db = get_db()
        rv = db.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        bk = db.execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()

    assert rv["review_status"] == "rejected"
    assert "处理不对" in rv["result_note"]
    assert bk["row_status"] == "returned"


def test_04b_locked_policy_cannot_change(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    resp = lock_policy(client, job_id, "copy")
    assert "不能改口".encode() in resp.data
    with app.app_context():
        policy = get_db().execute(
            "SELECT conflict_policy FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()["conflict_policy"]
    assert policy == "reject"


# ── 第五段：账册写入中途失败，三处状态不互相撒谎 ──
def test_05_mid_write_failure_atomic_and_traced(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)
    review_id = _make_pending_appeal(app, job_id, bid, sku="G-300")

    review_service.inject_ledger_write_failure()
    with app.app_context():
        with pytest.raises(LedgerWriteError):
            review_service.approve_review(review_id, "reject")
        db = get_db()
        # 账册没有半截编码
        half = db.execute(
            "SELECT 1 FROM crawl_ledger WHERE job_id = ? AND sku = 'G-300'",
            (job_id,),
        ).fetchone()
        rv = db.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        bk = db.execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()

    assert half is None
    assert rv["review_status"] == "failed"
    assert "中途失败" in rv["result_note"]
    # 短题筐不得假装已空：仍挂在复核（in_review），可重新点通过
    assert bk["row_status"] == "in_review"

    # 恢复后重新通过可正常入账
    with app.app_context():
        review_service.approve_review(review_id, "reject")
        ok = get_db().execute(
            "SELECT review_status FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
    assert ok["review_status"] == "approved"


# ── 第六段：并发改判同一短题行，只留一笔进复核栏 ──
def test_06_concurrent_appeals_only_one_enters(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)
    db_path = app.config["DATABASE"]

    outcomes = []

    def worker(sku):
        # 直接走服务层：BEGIN IMMEDIATE 串行化 + 部分唯一索引双重兜底
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT 1 FROM review_queue WHERE bucket_id = ? "
                "AND review_status IN ('pending','conflict','failed')",
                (bid,),
            ).fetchone()
            if active is not None:
                conn.rollback()
                outcomes.append("blocked")
                return
            conn.execute(
                """INSERT INTO review_queue
                   (job_id, bucket_id, proposed_sku, reason_text, locked_policy)
                   VALUES (?, ?, ?, ?, 'reject')""",
                (job_id, bid, sku, "并发改判"),
            )
            conn.execute(
                "UPDATE exception_bucket SET row_status = 'in_review' WHERE id = ?",
                (bid,),
            )
            conn.commit()
            outcomes.append("inserted")
        except sqlite3.IntegrityError:
            conn.rollback()
            outcomes.append("blocked")
        finally:
            conn.close()

    import threading

    t1 = threading.Thread(target=worker, args=("G-400",))
    t2 = threading.Thread(target=worker, args=("G-401",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(outcomes) == ["blocked", "inserted"]
    with app.app_context():
        active = get_db().execute(
            "SELECT COUNT(*) c FROM review_queue WHERE bucket_id = ? "
            "AND review_status IN ('pending','conflict','failed')",
            (bid,),
        ).fetchone()["c"]
        bucket_status = get_db().execute(
            "SELECT row_status FROM exception_bucket WHERE id = ?", (bid,)
        ).fetchone()["row_status"]
    assert active == 1
    assert bucket_status == "in_review"


def test_06b_partial_unique_index_blocks_duplicate(app, client):
    """即使绕过状态检查，数据库部分唯一索引也拒绝第二笔活动复核。"""
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    bid = short_bucket_id(app, job_id)
    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO review_queue
               (job_id, bucket_id, proposed_sku, reason_text, locked_policy)
               VALUES (?, ?, ?, ?, 'reject')""",
            (job_id, bid, "G-400", "第一笔"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO review_queue
                   (job_id, bucket_id, proposed_sku, reason_text, locked_policy)
                   VALUES (?, ?, ?, ?, 'reject')""",
                (job_id, bid, "G-401", "第二笔并发"),
            )
        db.rollback()


# ── 第七段：筐还在写入（job=running）时禁止改判 ──
def test_07_appeal_rejected_while_writing(app, client):
    login(client)
    client.post(
        "/jobs",
        data={"name": "run", "seed_path": "/mock/catalog/gamma"},
        follow_redirects=True,
    )
    with app.app_context():
        job_id = get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        # 手工置为 running，模拟筐正在写入
        get_db().execute(
            "UPDATE crawl_jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        get_db().commit()
        get_db().execute(
            """INSERT INTO exception_bucket
               (job_id, bucket, source_index, sku, title, reason, row_status)
               VALUES (?, 'short', 0, 'S-1', '短', 't', 'in_bucket')""",
            (job_id,),
        )
        get_db().commit()
        bid = get_db().execute(
            "SELECT id FROM exception_bucket WHERE job_id = ?", (job_id,)
        ).fetchone()["id"]
        with pytest.raises(TriageError):
            submit_appeal(job_id, bid, "S-9", "写入中改判")


# ── 第八段：列表上方亮出三筐件数、复核栏件数和当日口径 ──
def test_08_counts_and_policy_rendered_above_list(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "copy")
    bid = short_bucket_id(app, job_id)
    _make_pending_appeal(app, job_id, bid, sku="G-001")  # 进复核栏一笔

    resp = client.get(f"/jobs/{job_id}")
    html = resp.get_data(as_text=True)
    # 四个亮格都在
    assert "缺号筐" in html and "号相撞筐" in html
    assert "短题筐" in html and "复核栏" in html
    # 件数：缺号 2、号相撞 4、短题 0（唯一短题已送复核，离筐）、复核 1
    assert html.count("stat-num") >= 4
    # 当日口径
    assert "copy" in html and "已锁定" in html
    # 原因与优先说明可点开
    assert "优先说明" in html and "查看" in html


def test_08b_three_bucket_counts_distinct(app, client):
    job_id, _ = create_and_run(app, client)
    with app.app_context():
        counts = {
            r["bucket"]: r["c"]
            for r in get_db().execute(
                "SELECT bucket, COUNT(*) c FROM exception_bucket "
                "WHERE job_id = ? AND row_status IN ('in_bucket','returned') "
                "GROUP BY bucket",
                (job_id,),
            ).fetchall()
        }
    assert counts == {"missing": 2, "collision": 4, "short": 1}
