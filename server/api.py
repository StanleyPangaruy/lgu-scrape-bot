"""The API the laptop agent talks to. Authenticated with an agent token
(Authorization: Bearer …) created by the admin; never by a browser session."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from server import jobs
from server.auth import get_db, get_settings
from server.config import Settings
from server.db import audit, transaction
from server.ingest import save_scrape
from server.limits import COMMENT_TEXT_LIMITS, MAX_COMMENTS_PER_UPLOAD, POST_TEXT_LIMITS
from server.security import token_hash

router = APIRouter(prefix="/api/agent")


C, P = COMMENT_TEXT_LIMITS, POST_TEXT_LIMITS


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=C["id"])
    legacy_fbid: str | None = Field(None, max_length=C["legacy_fbid"])
    parent_id: str | None = Field(None, max_length=C["parent_id"])
    depth: int | None = Field(None, ge=0, le=100)
    author_id: str | None = Field(None, max_length=C["author_id"])
    author_name: str | None = Field(None, max_length=C["author_name"])
    author_url: str | None = Field(None, max_length=C["author_url"])
    text: str | None = Field(None, max_length=C["text"])
    created_time: int | None = Field(None, ge=0)
    reaction_count: int | None = Field(None, ge=0)
    reply_count: int | None = Field(None, ge=0)
    url: str | None = Field(None, max_length=C["url"])


class PostIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str | None = Field(None, max_length=P["url"])
    post_id: str | None = Field(None, max_length=P["post_id"], pattern=r"^\d+$")
    page_id: str | None = Field(None, max_length=P["page_id"])
    page_name: str | None = Field(None, max_length=P["page_name"])
    page_url: str | None = Field(None, max_length=P["page_url"])
    text: str | None = Field(None, max_length=P["text"])
    created_time: int | None = Field(None, ge=0)
    comment_count: int | None = Field(None, ge=0)


class CompleteIn(BaseModel):
    post: PostIn
    comments: list[CommentIn] = Field(max_length=MAX_COMMENTS_PER_UPLOAD)


class ClaimIn(BaseModel):
    status: str | None = Field(None, max_length=200)


class ProgressIn(BaseModel):
    count: int = Field(ge=0)


class FailIn(BaseModel):
    message: str = Field(max_length=1000)


def current_agent(
    authorization: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Agent token required.")
    row = conn.execute(
        "SELECT id FROM agent_tokens WHERE token_hash = ? AND revoked = 0",
        (token_hash(authorization[len("Bearer "):].strip()),),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown or revoked agent token.")
    return int(row["id"])


@router.post("/claim")
def claim(
    body: ClaimIn,
    agent_id: int = Depends(current_agent),
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    with transaction(conn):
        jobs.touch_agent(conn, agent_id, body.status)
        job = jobs.claim(conn, agent_id, settings.stale_job_minutes * 60)
    if job is None:
        return {"job": None}
    return {"job": {"id": job.id, "post_id": job.post_id, "url": job.url}}


@router.post("/jobs/{job_id}/progress")
def progress(
    job_id: int,
    body: ProgressIn,
    agent_id: int = Depends(current_agent),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    with transaction(conn):
        jobs.touch_agent(conn, agent_id, None)
        try:
            jobs.report_progress(conn, job_id, agent_id, body.count)
        except jobs.JobError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True}


@router.post("/jobs/{job_id}/complete")
def complete(
    request: Request,
    job_id: int,
    body: CompleteIn,
    agent_id: int = Depends(current_agent),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    with transaction(conn):
        jobs.touch_agent(conn, agent_id, "idle")
        try:
            job = jobs.running_job(conn, job_id, agent_id)
        except jobs.JobError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        result = save_scrape(
            conn,
            job["post_id"],
            body.post.model_dump(),
            (c.model_dump() for c in body.comments),
        )
        summary = f"{result.new} new, {result.updated} updated comments"
        if result.dropped:
            summary += f", {result.dropped} from other posts skipped"
        if result.merged_into is not None:
            summary += "; same post as an existing one, merged"
        jobs.finish(conn, job_id, "done", summary)
        audit(conn, "agent", "collected post", f"post {result.post_id}: {summary}")
    worker = request.app.state.worker
    if worker is not None:
        worker.wake.set()  # analyze the new comments now, not at the next idle check
    return {
        "post_id": result.post_id,
        "new": result.new,
        "updated": result.updated,
        "dropped": result.dropped,
        "merged": result.merged_into is not None,
    }


@router.post("/jobs/{job_id}/release")
def release(
    job_id: int,
    body: FailIn,
    agent_id: int = Depends(current_agent),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """The agent cannot run the job right now (e.g. Facebook logged out): back to the queue."""
    with transaction(conn):
        jobs.touch_agent(conn, agent_id, body.message.strip()[:200] or None)
        try:
            jobs.release(conn, job_id, agent_id, body.message.strip() or "Waiting for the agent.")
        except jobs.JobError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True}


@router.post("/jobs/{job_id}/fail")
def fail(
    job_id: int,
    body: FailIn,
    agent_id: int = Depends(current_agent),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    with transaction(conn):
        jobs.touch_agent(conn, agent_id, None)
        try:
            jobs.running_job(conn, job_id, agent_id)
        except jobs.JobError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        jobs.finish(conn, job_id, "failed", body.message.strip() or "Failed on the agent.")
    return {"ok": True}
