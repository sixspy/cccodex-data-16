"""八段验收共享辅助。"""

from app.db import get_db
from app.services.runner import run_job


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_and_run(app, client, seed="/mock/catalog/gamma", name="t"):
    login(client)
    client.post(
        "/jobs",
        data={"name": name, "seed_path": seed},
        follow_redirects=True,
    )
    with app.app_context():
        job_id = get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        summary = run_job(job_id)
    return job_id, summary


def lock_policy(client, job_id, policy):
    return client.post(
        f"/jobs/{job_id}/policy",
        data={"conflict_policy": policy},
        follow_redirects=True,
    )


def bucket_rows(app, job_id):
    with app.app_context():
        rows = get_db().execute(
            "SELECT * FROM exception_bucket WHERE job_id = ? ORDER BY source_index",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def short_bucket_id(app, job_id):
    for r in bucket_rows(app, job_id):
        if r["bucket"] == "short":
            return r["id"]
    raise AssertionError("no short bucket row")
