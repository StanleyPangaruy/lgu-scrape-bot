"""The job queue between the website and the agent on the admin's laptop.

queued → running (claimed by the agent) → done | failed. One job per post at a
time. If the agent goes silent mid-job (laptop closed), the job goes back in
the queue after `stale_job_minutes`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from server.db import now

ACTIVE = ("queued", "running")
AGENT_ONLINE_SECONDS = 90


class JobError(Exception):
    pass


@dataclass
class ClaimedJob:
    id: int
    post_id: int
    url: str


def active_job_for_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE post_id = ? AND status IN ('queued', 'running')", (post_id,)
    ).fetchone()


def enqueue(conn: sqlite3.Connection, post_id: int, requested_by: int | None) -> tuple[int, bool]:
    """Queue a collection of the post. Returns (job id, created); an already
    queued or running job for the post is returned instead of adding another."""
    current = active_job_for_post(conn, post_id)
    if current is not None:
        return current["id"], False
    cur = conn.execute(
        "INSERT INTO jobs (post_id, status, requested_by, created_at) VALUES (?, 'queued', ?, ?)",
        (post_id, requested_by, now()),
    )
    return int(cur.lastrowid), True


def requeue_stale(conn: sqlite3.Connection, stale_after_seconds: int) -> int:
    cutoff = now() - stale_after_seconds
    cur = conn.execute(
        """
        UPDATE jobs SET status = 'queued', agent_id = NULL, started_at = NULL,
               heartbeat_at = NULL, progress = NULL,
               message = 'The agent stopped responding; queued again.'
        WHERE status = 'running' AND COALESCE(heartbeat_at, started_at) < ?
        """,
        (cutoff,),
    )
    return cur.rowcount


def claim(conn: sqlite3.Connection, agent_id: int, stale_after_seconds: int) -> ClaimedJob | None:
    """Hand the oldest queued job to the agent. Call inside a write transaction."""
    requeue_stale(conn, stale_after_seconds)
    row = conn.execute(
        """
        SELECT jobs.id, jobs.post_id, posts.url, posts.source_url
        FROM jobs JOIN posts ON posts.id = jobs.post_id
        WHERE jobs.status = 'queued' ORDER BY jobs.created_at, jobs.id LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    t = now()
    conn.execute(
        """
        UPDATE jobs SET status = 'running', agent_id = ?, started_at = ?, heartbeat_at = ?,
               progress = 0, message = NULL
        WHERE id = ?
        """,
        (agent_id, t, t, row["id"]),
    )
    # The canonical URL once a scrape has found it; the link as added before that.
    return ClaimedJob(id=row["id"], post_id=row["post_id"], url=row["url"] or row["source_url"])


def running_job(conn: sqlite3.Connection, job_id: int, agent_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobError("No such job.")
    if row["status"] != "running" or row["agent_id"] != agent_id:
        raise JobError("This job is not running on this agent (it may have been retried).")
    return row


def report_progress(conn: sqlite3.Connection, job_id: int, agent_id: int, count: int) -> None:
    running_job(conn, job_id, agent_id)
    conn.execute(
        "UPDATE jobs SET progress = ?, heartbeat_at = ? WHERE id = ?", (max(count, 0), now(), job_id)
    )


def release(conn: sqlite3.Connection, job_id: int, agent_id: int, message: str) -> None:
    running_job(conn, job_id, agent_id)
    conn.execute(
        "UPDATE jobs SET status = 'queued', agent_id = NULL, started_at = NULL, heartbeat_at = NULL, "
        "progress = NULL, message = ? WHERE id = ?",
        (message, job_id),
    )


def finish(conn: sqlite3.Connection, job_id: int, status: str, message: str | None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, finished_at = ?, heartbeat_at = ?, message = ? WHERE id = ?",
        (status, now(), now(), message, job_id),
    )


def retry(conn: sqlite3.Connection, job_id: int, requested_by: int) -> int:
    row = conn.execute("SELECT post_id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobError("No such job.")
    if row["status"] != "failed":
        raise JobError("Only failed jobs can be retried.")
    new_job_id, _ = enqueue(conn, row["post_id"], requested_by)
    return new_job_id


def cancel(conn: sqlite3.Connection, job_id: int) -> None:
    cur = conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = ?, message = 'Cancelled by admin.' "
        "WHERE id = ? AND status = 'queued'",
        (now(), job_id),
    )
    if cur.rowcount == 0:
        raise JobError("Only jobs still waiting in the queue can be cancelled.")


def touch_agent(conn: sqlite3.Connection, agent_id: int, status: str | None) -> None:
    conn.execute(
        "UPDATE agent_tokens SET last_seen_at = ?, status = COALESCE(?, status) WHERE id = ?",
        (now(), status, agent_id),
    )


@dataclass
class AgentStatus:
    online: bool
    name: str | None
    last_seen_at: int | None
    status: str | None


def agent_status(conn: sqlite3.Connection) -> AgentStatus:
    row = conn.execute(
        "SELECT name, last_seen_at, status FROM agent_tokens WHERE revoked = 0 "
        "ORDER BY COALESCE(last_seen_at, 0) DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return AgentStatus(False, None, None, None)
    seen = row["last_seen_at"]
    online = seen is not None and now() - seen <= AGENT_ONLINE_SECONDS
    return AgentStatus(online, row["name"], seen, row["status"])


def active_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT jobs.id, jobs.status, jobs.progress, jobs.post_id, posts.text, posts.page_name,
               posts.source_url, posts.last_checked_at
        FROM jobs JOIN posts ON posts.id = jobs.post_id
        WHERE jobs.status IN ('queued', 'running')
        ORDER BY jobs.status = 'running' DESC, jobs.created_at, jobs.id
        """
    ).fetchall()
