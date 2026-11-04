"""Agent loop: claim a job, collect the post, upload, repeat.

    python -m agent --server http://127.0.0.1:8000 --token <agent token>

The server address and token can also come from LCM_SERVER and LCM_AGENT_TOKEN.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Page

from scraper.browser import is_logged_in, open_context
from scraper.collect import collect_comments
from scraper.interceptor import Comment, CommentInterceptor
from scraper.post_info import PostInfo, read_post_info
from server.limits import COMMENT_TEXT_LIMITS, MAX_COMMENTS_PER_UPLOAD, POST_TEXT_LIMITS

IDLE_POLL_SECONDS = 15
PROGRESS_EVERY_SECONDS = 10
UPLOAD_ATTEMPTS = 5
LOGIN_NEEDED = "Facebook login needed on the admin laptop: run `python main.py --login`, then start the agent again."


class FacebookLoggedOut(Exception):
    pass


class ServerError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """Network trouble or a server-side error; a 4xx refusal will not change on retry."""
        return self.status is None or self.status >= 500


def _clip(values: dict[str, Any], limits: dict[str, int]) -> dict[str, Any]:
    out = dict(values)
    for name, limit in limits.items():
        value = out.get(name)
        if isinstance(value, str) and len(value) > limit:
            out[name] = value[:limit]
    return out


def build_payload(info: PostInfo, comments: list[Comment]) -> dict[str, Any]:
    """The upload body, trimmed to what the server accepts."""
    post = _clip(info.to_dict(), POST_TEXT_LIMITS)
    if not (post.get("post_id") or "").isdigit():
        post["post_id"] = None
    rows = []
    for c in comments[:MAX_COMMENTS_PER_UPLOAD]:
        row = _clip(c.to_dict(), COMMENT_TEXT_LIMITS)
        if not row.get("id") or len(c.id) > COMMENT_TEXT_LIMITS["id"]:
            continue  # an id cannot be trimmed without becoming a different comment
        rows.append(row)
    return {"post": post, "comments": rows}


class Client:
    def __init__(self, server: str, token: str) -> None:
        self.http = httpx.AsyncClient(
            base_url=server.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.http.post(path, json=body)
        except httpx.HTTPError as e:
            raise ServerError(f"cannot reach the website: {e}") from e
        if response.status_code == 401:
            raise ServerError("the website rejected the agent token (revoked or mistyped)", 401)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text[:200]
            raise ServerError(f"{response.status_code}: {detail}", response.status_code)
        return response.json()

    async def claim(self, status: str) -> dict[str, Any] | None:
        return (await self._post("/api/agent/claim", {"status": status}))["job"]

    async def progress(self, job_id: int, count: int) -> None:
        await self._post(f"/api/agent/jobs/{job_id}/progress", {"count": count})

    async def complete(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        last: ServerError | None = None
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                return await self._post(f"/api/agent/jobs/{job_id}/complete", payload)
            except ServerError as e:
                last = e
                if not e.retryable or attempt == UPLOAD_ATTEMPTS - 1:
                    break
                await asyncio.sleep(2 ** attempt * 3)
        raise last or ServerError("upload failed")

    async def release(self, job_id: int, message: str) -> None:
        await self._post(f"/api/agent/jobs/{job_id}/release", {"message": message})

    async def fail(self, job_id: int, message: str) -> None:
        await self._post(f"/api/agent/jobs/{job_id}/fail", {"message": message[:1000]})


async def check_facebook_login(session_dir: Path) -> bool:
    async with open_context(session_dir) as context:
        return await is_logged_in(context)


async def collect(job: dict[str, Any], session_dir: Path, client: Client) -> dict[str, Any]:
    interceptor = CommentInterceptor()
    info: dict[str, PostInfo] = {}

    async def read_info(page: Page) -> None:
        info["post"] = await read_post_info(page)
        print(f"  Page: {info['post'].page_name or 'unknown'}")

    async def report_progress() -> None:
        while True:
            await asyncio.sleep(PROGRESS_EVERY_SECONDS)
            try:
                await client.progress(job["id"], interceptor.count)
            except ServerError as e:
                print(f"  (could not report progress: {e})")

    async with open_context(session_dir) as context:
        if not await is_logged_in(context):
            raise FacebookLoggedOut()
        page = context.pages[0] if context.pages else await context.new_page()
        reporter = asyncio.create_task(report_progress())
        try:
            comments = await collect_comments(
                page, None, url=job["url"], interceptor=interceptor, after_open=read_info
            )
        finally:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)

    post = info.get("post") or PostInfo(url=job["url"])
    return build_payload(post, comments)


async def _tell_server(call: Any) -> None:
    """Report a job's outcome; if the website is unreachable, the job is
    requeued there automatically once it notices the agent went quiet."""
    try:
        await call
    except ServerError as e:
        print(f"  (could not tell the website: {e})")


async def run_job(job: dict[str, Any], session_dir: Path, client: Client) -> bool:
    """Returns False when the agent should stop (Facebook needs a login)."""
    print(f"Job {job['id']}: {job['url']}")
    try:
        payload = await collect(job, session_dir, client)
    except FacebookLoggedOut:
        print(LOGIN_NEEDED)
        await _tell_server(client.release(job["id"], LOGIN_NEEDED))
        return False
    except asyncio.CancelledError:
        raise
    except Exception as e:  # any scraping problem: report it, keep the agent running
        message = f"Collecting failed on the agent: {type(e).__name__}: {e}"
        print("  " + message)
        await _tell_server(client.fail(job["id"], message))
        return True

    print(f"  Uploading {len(payload['comments'])} comments…")
    try:
        result = await client.complete(job["id"], payload)
    except ServerError as e:
        print(f"  Upload failed: {e}. The website will queue this post again later.")
        return True
    note = " (same post as one already listed; merged)" if result.get("merged") else ""
    print(f"  Done: {result['new']} new, {result['updated']} updated{note}")
    return True


async def run(server: str, token: str, session_dir: Path, once: bool) -> int:
    if not await check_facebook_login(session_dir):
        print(LOGIN_NEEDED)
        return 1
    client = Client(server, token)
    current: dict[str, Any] | None = None
    try:
        print(f"Agent running. Waiting for jobs from {server} (Ctrl+C to stop).")
        while True:
            try:
                current = await client.claim("idle")
            except ServerError as e:
                if not e.retryable:
                    print(f"Cannot get jobs: {e}.")
                    return 1
                print(f"Cannot get jobs: {e}. Retrying in a minute.")
                if once:
                    return 1
                await asyncio.sleep(60)
                continue
            if current is None:
                if once:
                    return 0
                await asyncio.sleep(IDLE_POLL_SECONDS + random.uniform(0, 5))
                continue
            keep_going = await run_job(current, session_dir, client)
            current = None
            if not keep_going:
                return 1
            if once:
                return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        if current is not None:
            await _tell_server(client.release(current["id"], "The agent was stopped; queued again."))
        raise
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description="LGU Comment Monitor laptop agent")
    parser.add_argument("--server", default=os.environ.get("LCM_SERVER"), help="website address (or LCM_SERVER)")
    parser.add_argument("--token", default=os.environ.get("LCM_AGENT_TOKEN"), help="agent token (or LCM_AGENT_TOKEN)")
    parser.add_argument("--session-dir", type=Path, default=Path("sessions"), help="Facebook login profile (default: sessions/)")
    parser.add_argument("--once", action="store_true", help="run at most one job, then exit")
    args = parser.parse_args(argv)
    if not args.server or not args.token:
        parser.error("give --server and --token (or set LCM_SERVER and LCM_AGENT_TOKEN)")
    try:
        return asyncio.run(run(args.server, args.token, args.session_dir, args.once))
    except KeyboardInterrupt:
        print("\nAgent stopped.")
        return 130
