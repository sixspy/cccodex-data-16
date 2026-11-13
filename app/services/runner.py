import json
import sqlite3

from flask import current_app

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page
from app.services.triage import classify_items


def run_job(job_id: int) -> dict:
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "running":
        raise ValueError("job already running")
    if job["status"] == "done":
        raise ValueError("job already done")

    seed = job["seed_path"]
    if seed not in CATALOGS:
        db.execute(
            "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        db.commit()
        return {"status": "failed", "reason": "unknown seed", "inserted": 0, "skipped_dup": 0}

    html = render_catalog_page(seed)
    if isinstance(html, tuple):
        html = html[0]
    items = parse_catalog_html(html, seed)
    verdicts = classify_items(items)

    inserted = 0
    skipped_dup = 0
    bucket_counts = {"missing": 0, "collision": 0, "short": 0}
    ledger_inserted = 0
    with db_transaction() as tx:
        tx.execute(
            "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        for it, verdict in zip(items, verdicts):
            key = normalize_item_key(seed, it["title"])
            try:
                tx.execute(
                    """
                    INSERT INTO crawl_records (job_id, item_key, title, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, key, it["title"], json.dumps(it, ensure_ascii=False)),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1

            if verdict["kind"] == "exception":
                bucket_counts[verdict["bucket"]] += 1
                tx.execute(
                    """
                    INSERT INTO exception_bucket
                        (job_id, bucket, source_index, sku, title, reason,
                         priority_note, row_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'in_bucket')
                    """,
                    (
                        job_id,
                        verdict["bucket"],
                        verdict["index"],
                        verdict["sku"] or None,
                        verdict["title"],
                        verdict["reason"],
                        verdict["priority_note"],
                    ),
                )
            else:
                # 正常行直接进账册；编码唯一约束兜底，冲突不应出现在此分支
                tx.execute(
                    """
                    INSERT OR IGNORE INTO crawl_ledger
                        (job_id, sku, title, source)
                    VALUES (?, ?, ?, 'triage')
                    """,
                    (job_id, verdict["sku"], verdict["title"]),
                )
                ledger_inserted += 1

        tx.execute(
            "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )

    current_app.logger.info(
        "job %s finished inserted=%s skipped_dup=%s buckets=%s ledger=%s",
        job_id, inserted, skipped_dup, bucket_counts, ledger_inserted,
    )
    return {
        "status": "done",
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "total_source": len(items),
        "bucket_counts": bucket_counts,
        "ledger_inserted": ledger_inserted,
    }
