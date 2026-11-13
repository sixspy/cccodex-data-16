from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import (
    BUCKET_ROW_IN,
    BUCKET_ROW_RETURNED,
    get_db,
)
from app.routes.auth import login_required
from app.services.mock_catalog import CATALOGS
from app.services.review import (
    LedgerWriteError,
    TriageError,
    approve_review,
    lock_conflict_policy,
    reject_review,
    submit_appeal,
)
from app.services.runner import run_job
from app.services.triage import BUCKET_LABELS

bp = Blueprint("jobs", __name__)


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    jobs = db.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC"
    ).fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        seeds=sorted(CATALOGS.keys()),
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status) VALUES (?, ?, 'draft')",
        (name, seed),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
        if summary["status"] == "done":
            bc = summary.get("bucket_counts", {})
            flash(
                "运行完成：落库 {ins} 条，去重跳过 {dup} 条；分流——缺号 {m}、"
                "号相撞 {c}、短题 {s}；账册 {led} 条".format(
                    ins=summary["inserted"],
                    dup=summary["skipped_dup"],
                    m=bc.get("missing", 0),
                    c=bc.get("collision", 0),
                    s=bc.get("short", 0),
                    led=summary.get("ledger_inserted", 0),
                ),
                "ok",
            )
        else:
            flash(f"运行失败：{summary.get('reason')}", "error")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"运行失败: {e}", "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/policy")
@login_required
def set_policy(job_id: int):
    policy = request.form.get("conflict_policy", "").strip()
    try:
        lock_conflict_policy(job_id, policy)
        flash("冲突处理口径已锁定", "ok")
    except TriageError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/appeal")
@login_required
def appeal(job_id: int):
    bucket_id = request.form.get("bucket_id", type=int)
    proposed_sku = request.form.get("proposed_sku", "")
    reason = request.form.get("reason", "")
    try:
        submit_appeal(job_id, bucket_id, proposed_sku, reason)
        flash("改判已进入复核栏，尚未入账册", "ok")
    except TriageError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/reviews/<int:review_id>/approve")
@login_required
def approve(review_id: int):
    policy = request.form.get("conflict_policy", "").strip()
    db = get_db()
    row = db.execute(
        "SELECT job_id FROM review_queue WHERE id = ?", (review_id,)
    ).fetchone()
    job_id = row["job_id"] if row else None
    try:
        approve_review(review_id, policy)
        status = db.execute(
            "SELECT review_status, result_note FROM review_queue WHERE id = ?",
            (review_id,),
        ).fetchone()
        if status is not None:
            if status["review_status"] == "approved":
                flash("复核通过，已写入账册", "ok")
            else:
                # rejected（含口径不一致/reject 退回）或 conflict（copy 副本）
                flash(f"未入账：{status['result_note']}", "error")
    except TriageError as e:
        flash(str(e), "error")
    except LedgerWriteError as e:
        flash(str(e), "error")
    target = job_id or 1
    return redirect(url_for("jobs.detail", job_id=target))


@bp.post("/reviews/<int:review_id>/reject")
@login_required
def reject(review_id: int):
    note = request.form.get("note", "")
    db = get_db()
    row = db.execute(
        "SELECT job_id FROM review_queue WHERE id = ?", (review_id,)
    ).fetchone()
    job_id = row["job_id"] if row else None
    try:
        reject_review(review_id, note)
        flash("已驳回并退回短题筐", "ok")
    except TriageError as e:
        flash(str(e), "error")
    target = job_id or 1
    return redirect(url_for("jobs.detail", job_id=target))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))

    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()

    buckets = db.execute(
        """
        SELECT * FROM exception_bucket
        WHERE job_id = ?
        ORDER BY
            CASE bucket WHEN 'missing' THEN 0
                        WHEN 'collision' THEN 1
                        WHEN 'short' THEN 2 END,
            source_index
        """,
        (job_id,),
    ).fetchall()

    reviews = db.execute(
        "SELECT * FROM review_queue WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()

    ledger = db.execute(
        "SELECT * FROM crawl_ledger WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()

    # 三筐件数：在筐 + 被退回（仍占筐），已送复核/已入账不算筐内
    bucket_counts = {"missing": 0, "collision": 0, "short": 0}
    for b in buckets:
        if b["row_status"] in (BUCKET_ROW_IN, BUCKET_ROW_RETURNED):
            bucket_counts[b["bucket"]] += 1

    review_count = len(reviews)
    review_active = [r for r in reviews if r["review_status"] in
                     ("pending", "conflict", "failed")]

    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        buckets=buckets,
        bucket_counts=bucket_counts,
        bucket_labels=BUCKET_LABELS,
        reviews=reviews,
        review_active=review_active,
        review_count=review_count,
        ledger=ledger,
        appealable_statuses=(BUCKET_ROW_IN, BUCKET_ROW_RETURNED),
    )
