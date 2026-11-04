from fastapi.testclient import TestClient

from server.db import now
from tests.conftest import comment_id

POST = "1557299289776837"


def payload(*ns, post_id=POST):
    return {
        "post": {"url": "https://www.facebook.com/municipalityoflopezquezon/posts/pfbid0x", "post_id": post_id,
                 "page_name": "Municipality of Lopez, Quezon", "text": "BILANG PAGHAHANDA", "comment_count": len(ns)},
        "comments": [{"id": comment_id(POST, n), "author_name": f"P{n}", "text": f"text {n}",
                      "created_time": 1_790_000_000 + n, "reaction_count": n, "created_iso": "ignored"} for n in ns],
    }


def add_post(admin, link="https://www.facebook.com/share/p/1CYuJh1ntq/"):
    r = admin.post("/posts", {"link": link})
    assert r.status_code == 303 and "notice=queued" in r.headers["location"]


def test_full_job_cycle(browsers, agent, conn):
    add_post(browsers["admin"])
    job = agent.post("/api/agent/claim", json={"status": "idle"}).json()["job"]
    assert job["url"] == "https://www.facebook.com/share/p/1CYuJh1ntq/"
    assert agent.post("/api/agent/claim", json={}).json()["job"] is None  # one job only

    assert agent.post(f"/api/agent/jobs/{job['id']}/progress", json={"count": 4}).status_code == 200
    status = browsers["staff"].get("/jobs/status.json").json()
    assert status["agent_online"] and status["active"][0]["progress"] == 4

    result = agent.post(f"/api/agent/jobs/{job['id']}/complete", json=payload(1, 2, 3)).json()
    assert (result["new"], result["updated"]) == (3, 0)
    row = conn.execute("SELECT status, message FROM jobs").fetchone()
    assert row["status"] == "done" and "3 new" in row["message"]

    page = browsers["mayor"].get(f"/posts/{job['post_id']}")
    assert page.status_code == 200 and "BILANG PAGHAHANDA" in page.text and "text 3" in page.text

    # Re-check: only new comments are added.
    browsers["admin"].post(f"/posts/{job['post_id']}/recheck")
    job2 = agent.post("/api/agent/claim", json={}).json()["job"]
    assert job2["url"].startswith("https://www.facebook.com/municipalityoflopezquezon/")
    result = agent.post(f"/api/agent/jobs/{job2['id']}/complete", json=payload(2, 3, 4)).json()
    assert (result["new"], result["updated"]) == (1, 2)


def test_adding_the_same_link_twice_does_not_double_queue(browsers):
    add_post(browsers["admin"])
    r = browsers["admin"].post("/posts", {"link": "https://www.facebook.com/share/p/1CYuJh1ntq/?x=1"})
    assert "notice=already-queued" in r.headers["location"]


def test_agent_can_only_touch_its_own_running_job(browsers, agent, app, conn):
    from server.security import new_token, token_hash

    add_post(browsers["admin"])
    job = agent.post("/api/agent/claim", json={}).json()["job"]
    other_token = new_token()
    conn.execute("INSERT INTO agent_tokens (name, token_hash, created_at) VALUES ('other', ?, 0)",
                 (token_hash(other_token),))
    with TestClient(app, headers={"Authorization": f"Bearer {other_token}"}) as other:
        r = other.post(f"/api/agent/jobs/{job['id']}/complete", json=payload(1))
        assert r.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 0


def test_stale_job_is_requeued_and_late_upload_refused(browsers, agent, conn):
    add_post(browsers["admin"])
    job = agent.post("/api/agent/claim", json={}).json()["job"]
    conn.execute("UPDATE jobs SET heartbeat_at = ?, started_at = ?", (now() - 3600, now() - 3600))
    again = agent.post("/api/agent/claim", json={}).json()["job"]
    assert again["id"] == job["id"]  # requeued, then claimed afresh
    conn.execute("UPDATE jobs SET agent_id = NULL")  # pretend another agent took it
    assert agent.post(f"/api/agent/jobs/{job['id']}/complete", json=payload(1)).status_code == 409


def test_release_puts_job_back_and_fail_records_message(browsers, agent, conn):
    add_post(browsers["admin"])
    job = agent.post("/api/agent/claim", json={}).json()["job"]
    assert agent.post(f"/api/agent/jobs/{job['id']}/release", json={"message": "Facebook login needed"}).status_code == 200
    row = conn.execute("SELECT status, message FROM jobs").fetchone()
    assert (row["status"], row["message"]) == ("queued", "Facebook login needed")

    job = agent.post("/api/agent/claim", json={}).json()["job"]
    agent.post(f"/api/agent/jobs/{job['id']}/fail", json={"message": "Timeout"})
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "failed"
    r = browsers["admin"].post(f"/jobs/{job['id']}/retry")
    assert r.status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0] == 1


def test_bad_tokens_are_rejected(app, conn, agent_token):
    with TestClient(app) as anon:
        assert anon.post("/api/agent/claim", json={}).status_code == 401
    with TestClient(app, headers={"Authorization": "Bearer wrong"}) as wrong:
        assert wrong.post("/api/agent/claim", json={}).status_code == 401
    conn.execute("UPDATE agent_tokens SET revoked = 1")
    with TestClient(app, headers={"Authorization": f"Bearer {agent_token}"}) as revoked:
        assert revoked.post("/api/agent/claim", json={}).status_code == 401


def test_session_cookie_cannot_use_agent_api(browsers):
    assert browsers["admin"].client.post("/api/agent/claim", json={}).status_code == 401


def test_invalid_and_oversized_uploads(browsers, agent):
    add_post(browsers["admin"])
    job = agent.post("/api/agent/claim", json={}).json()["job"]
    bad = payload(1)
    bad["post"]["post_id"] = "not-digits"
    assert agent.post(f"/api/agent/jobs/{job['id']}/complete", json=bad).status_code == 422
    huge = agent.post(
        f"/api/agent/jobs/{job['id']}/complete", content=b"x", headers={"content-length": str(60 * 1024 * 1024)}
    )
    assert huge.status_code == 413
