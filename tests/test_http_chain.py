"""HTTP 端到端：通过浏览器表单走通 分流 → 改判 → 复核 → 账册。"""

from app.db import get_db

from tests._helpers import bucket_rows, create_and_run, lock_policy, login


def test_full_chain_via_http(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")

    short = next(r for r in bucket_rows(app, job_id) if r["bucket"] == "short")

    # 1) 缺原因的改判被拒
    resp = client.post(
        f"/jobs/{job_id}/appeal",
        data={"bucket_id": short["id"], "proposed_sku": "G-900", "reason": ""},
        follow_redirects=True,
    )
    assert "必须填写原因".encode() in resp.data

    # 2) 合规改判进复核栏
    resp = client.post(
        f"/jobs/{job_id}/appeal",
        data={
            "bucket_id": short["id"],
            "proposed_sku": "G-900",
            "reason": "简称，实为正式台账",
        },
        follow_redirects=True,
    )
    assert "已进入复核栏".encode() in resp.data

    with app.app_context():
        review_id = get_db().execute(
            "SELECT id FROM review_queue WHERE bucket_id = ?", (short["id"],)
        ).fetchone()["id"]
        # 账册此时无 G-900
        assert get_db().execute(
            "SELECT 1 FROM crawl_ledger WHERE job_id=? AND sku='G-900'",
            (job_id,),
        ).fetchone() is None

    # 3) 复核通过（表单带锁定口径 reject）
    resp = client.post(
        f"/reviews/{review_id}/approve",
        data={"conflict_policy": "reject"},
        follow_redirects=True,
    )
    assert "已写入账册".encode() in resp.data

    with app.app_context():
        led = get_db().execute(
            "SELECT * FROM crawl_ledger WHERE job_id=? AND sku='G-900'",
            (job_id,),
        ).fetchone()
        rv = get_db().execute(
            "SELECT review_status FROM review_queue WHERE id=?", (review_id,)
        ).fetchone()
        bk = get_db().execute(
            "SELECT row_status FROM exception_bucket WHERE id=?", (short["id"],)
        ).fetchone()
    assert led is not None and led["source"] == "appeal"
    assert rv["review_status"] == "approved"
    assert bk["row_status"] == "approved"


def test_http_policy_mismatch_flashes_and_returns(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "reject")
    short = next(r for r in bucket_rows(app, job_id) if r["bucket"] == "short")
    client.post(
        f"/jobs/{job_id}/appeal",
        data={"bucket_id": short["id"], "proposed_sku": "G-901", "reason": "改判"},
        follow_redirects=True,
    )
    with app.app_context():
        review_id = get_db().execute(
            "SELECT id FROM review_queue WHERE bucket_id = ?", (short["id"],)
        ).fetchone()["id"]

    # 锁定 reject，却用 copy 点通过
    resp = client.post(
        f"/reviews/{review_id}/approve",
        data={"conflict_policy": "copy"},
        follow_redirects=True,
    )
    assert "处理不对".encode() in resp.data
    with app.app_context():
        bk = get_db().execute(
            "SELECT row_status FROM exception_bucket WHERE id=?", (short["id"],)
        ).fetchone()
    assert bk["row_status"] == "returned"


def test_http_reject_sends_back_to_short_bucket(app, client):
    job_id, _ = create_and_run(app, client)
    lock_policy(client, job_id, "copy")
    short = next(r for r in bucket_rows(app, job_id) if r["bucket"] == "short")
    client.post(
        f"/jobs/{job_id}/appeal",
        data={"bucket_id": short["id"], "proposed_sku": "G-902", "reason": "改判"},
        follow_redirects=True,
    )
    with app.app_context():
        review_id = get_db().execute(
            "SELECT id FROM review_queue WHERE bucket_id = ?", (short["id"],)
        ).fetchone()["id"]

    client.post(
        f"/reviews/{review_id}/reject",
        data={"note": "依据不足"},
        follow_redirects=True,
    )
    with app.app_context():
        bk = get_db().execute(
            "SELECT row_status FROM exception_bucket WHERE id=?", (short["id"],)
        ).fetchone()
        rv = get_db().execute(
            "SELECT review_status, result_note FROM review_queue WHERE id=?",
            (review_id,),
        ).fetchone()
    assert bk["row_status"] == "returned"
    assert rv["review_status"] == "rejected"
    assert "依据不足" in rv["result_note"]
