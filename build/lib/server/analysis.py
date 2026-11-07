"""The sentiment worker: labels new comments, then summarizes posts and the period.

Runs as a background thread inside the website. Each step is small (one batch
of comments, or one summary), commits on its own, and is safe to repeat, so
stopping the website at any moment loses nothing.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Sequence

from server import ai
from server.config import Settings
from server.db import connect, now, transaction
from server.queries import post_title

log = logging.getLogger("lcm.analysis")

BATCH_SIZE = 40
MAX_ATTEMPTS = 3
SUMMARY_COMMENTS = 250
BRIEF_KEY = "30d"
BRIEF_DAYS = 30
BRIEF_MIN_AGE = 15 * 60
NO_TEXT_REASON = "No text (photo, sticker or GIF), so no stance to read."
IDLE_SECONDS = 30
TRANSIENT_PAUSE = 60
AUTH_PAUSE = 600


def record_usage(conn: sqlite3.Connection, purpose: str, usage: ai.Usage | None) -> None:
    if usage is None:
        return
    conn.execute(
        "INSERT INTO api_usage (at, purpose, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now(), purpose, usage.model, usage.input_tokens, usage.output_tokens,
         usage.cache_read_tokens, usage.cache_write_tokens),
    )


# --- Labeling -------------------------------------------------------------------


def pending_comments(
    conn: sqlite3.Connection, limit: int, post_ids: Sequence[int] | None = None
) -> list[sqlite3.Row]:
    """Unlabeled comments of the post with the newest arrivals, fewest failures first.
    post_ids limits the choice to those posts (the accuracy check)."""
    only = ""
    if post_ids is not None:
        if not post_ids:
            return []
        only = f"AND c.post_id IN ({','.join(str(int(i)) for i in post_ids)})"
    post = conn.execute(
        f"""
        SELECT c.post_id FROM comments c
        LEFT JOIN comment_sentiment s ON s.comment_id = c.id
        LEFT JOIN sentiment_failures f ON f.comment_id = c.id
        WHERE s.comment_id IS NULL AND COALESCE(f.attempts, 0) < {MAX_ATTEMPTS} {only}
        GROUP BY c.post_id ORDER BY MAX(c.first_seen_at) DESC LIMIT 1
        """
    ).fetchone()
    if post is None:
        return []
    return conn.execute(
        f"""
        SELECT c.id, c.text, c.reaction_count, c.post_id, pc.text AS parent_text,
               COALESCE(f.attempts, 0) AS attempts
        FROM comments c
        LEFT JOIN comments pc ON pc.id = c.parent_id
        LEFT JOIN comment_sentiment s ON s.comment_id = c.id
        LEFT JOIN sentiment_failures f ON f.comment_id = c.id
        WHERE c.post_id = ? AND s.comment_id IS NULL AND COALESCE(f.attempts, 0) < {MAX_ATTEMPTS}
        ORDER BY COALESCE(f.attempts, 0), COALESCE(c.created_time, 0), c.id
        LIMIT ?
        """,
        (post["post_id"], limit),
    ).fetchall()


def _save_label(conn: sqlite3.Connection, comment_id: str, label: str, reason: str, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO comment_sentiment (comment_id, label, reason, model, prompt_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (comment_id, label, reason, model, ai.PROMPT_VERSION, now()),
    )
    conn.execute("DELETE FROM sentiment_failures WHERE comment_id = ?", (comment_id,))


def _record_failure(conn: sqlite3.Connection, comment_ids: list[str], error: str) -> None:
    t = now()
    conn.executemany(
        """
        INSERT INTO sentiment_failures (comment_id, attempts, last_error, last_at) VALUES (?, 1, ?, ?)
        ON CONFLICT (comment_id) DO UPDATE SET attempts = attempts + 1, last_error = excluded.last_error,
            last_at = excluded.last_at
        """,
        [(cid, error[:500], t) for cid in comment_ids],
    )


def label_next_batch(
    conn: sqlite3.Connection, client: Any, settings: Settings, post_ids: Sequence[int] | None = None
) -> int:
    """Label one batch; returns how many comments it settled (0 = nothing to do)."""
    rows = pending_comments(conn, BATCH_SIZE, post_ids)
    if not rows:
        return 0
    # A comment that already failed goes alone, so one problem comment cannot
    # keep failing the whole batch around it.
    if rows[0]["attempts"] > 0:
        rows = rows[:1]

    no_text = [r for r in rows if not (r["text"] or "").strip()]
    with_text = [r for r in rows if (r["text"] or "").strip()]
    if no_text:
        with transaction(conn):
            for r in no_text:
                _save_label(conn, r["id"], "neutral", NO_TEXT_REASON, "rule")
    if not with_text:
        return len(no_text)

    post = conn.execute("SELECT text, page_name FROM posts WHERE id = ?", (with_text[0]["post_id"],)).fetchone()
    items = [
        ai.CommentItem(n=i, text=r["text"], parent_text=r["parent_text"], reactions=r["reaction_count"] or 0)
        for i, r in enumerate(with_text, 1)
    ]
    try:
        labels, usage = ai.label_comments(
            client, settings.sentiment_model, settings.sentiment_effort, post["text"], post["page_name"], items
        )
    except ai.ModelError as e:
        with transaction(conn):
            record_usage(conn, "labels", e.usage)
            _record_failure(conn, [r["id"] for r in with_text], str(e))
        log.warning("labeling failed for %d comments: %s", len(with_text), e)
        return len(no_text) + len(with_text)

    with transaction(conn):
        record_usage(conn, "labels", usage)
        missing = []
        for item, row in zip(items, with_text):
            got = labels.get(item.n)
            if got is None:
                missing.append(row["id"])
            else:
                _save_label(conn, row["id"], got.label, got.reason, settings.sentiment_model)
        if missing:
            _record_failure(conn, missing, "left out of the model's answer")
    return len(no_text) + len(with_text)


# --- Post summaries ---------------------------------------------------------------


def label_counts(conn: sqlite3.Connection, post_id: int) -> dict[str, int]:
    counts = {k: 0 for k in ai.LABELS}
    for r in conn.execute(
        """
        SELECT cl.label, COUNT(*) AS n FROM comments c JOIN comment_labels cl ON cl.comment_id = c.id
        WHERE c.post_id = ? AND cl.label IS NOT NULL GROUP BY cl.label
        """,
        (post_id,),
    ):
        counts[r["label"]] = r["n"]
    return counts


def post_needing_summary(conn: sqlite3.Connection) -> tuple[int, str] | None:
    """A post whose labeling is finished and whose label counts changed since its
    summary was written (new comments, or staff corrections). Returns (post id, basis)."""
    row = conn.execute(
        f"""
        SELECT p.id,
               printf('%d,%d,%d,%d', SUM(cl.label = 'supportive'), SUM(cl.label = 'against'),
                      SUM(cl.label = 'neutral'), SUM(cl.label = 'question')) AS new_basis,
               MAX(ps.basis) AS old_basis
        FROM posts p
        JOIN comments c ON c.post_id = p.id
        LEFT JOIN comment_labels cl ON cl.comment_id = c.id
        LEFT JOIN sentiment_failures f ON f.comment_id = c.id
        LEFT JOIN post_summaries ps ON ps.post_id = p.id
        GROUP BY p.id
        HAVING SUM(cl.label IS NULL AND COALESCE(f.attempts, 0) < {MAX_ATTEMPTS}) = 0
           AND SUM(cl.label IS NOT NULL) > 0
           AND (old_basis IS NULL OR old_basis != new_basis)
        ORDER BY MAX(c.first_seen_at) DESC LIMIT 1
        """
    ).fetchone()
    return (row["id"], row["new_basis"]) if row else None


def summarize_next_post(conn: sqlite3.Connection, client: Any, settings: Settings) -> bool:
    found = post_needing_summary(conn)
    if found is None:
        return False
    post_id, basis = found
    post = conn.execute("SELECT text, page_name FROM posts WHERE id = ?", (post_id,)).fetchone()
    counts = label_counts(conn, post_id)
    rows = conn.execute(
        """
        SELECT cl.label, c.reaction_count, c.text FROM comments c JOIN comment_labels cl ON cl.comment_id = c.id
        WHERE c.post_id = ? AND cl.label IS NOT NULL AND TRIM(COALESCE(c.text, '')) != ''
        ORDER BY COALESCE(c.reaction_count, 0) DESC, c.created_time LIMIT ?
        """,
        (post_id, SUMMARY_COMMENTS),
    ).fetchall()
    labeled = sum(counts.values())
    try:
        summary, issues, usage = ai.summarize_post(
            client, settings.sentiment_model, settings.sentiment_effort, post["text"], post["page_name"],
            counts, [(r["label"], r["reaction_count"] or 0, r["text"]) for r in rows],
        )
    except ai.ModelError as e:
        with transaction(conn):
            record_usage(conn, "summary", e.usage)
            # Mark it current anyway so a post the model will not summarize is not retried forever.
            conn.execute(
                "INSERT OR REPLACE INTO post_summaries (post_id, summary, issues, labeled_count, basis, model, "
                "created_at) VALUES (?, '', '[]', ?, ?, ?, ?)",
                (post_id, labeled, basis, settings.sentiment_model, now()),
            )
        log.warning("summary failed for post %d: %s", post_id, e)
        return True
    with transaction(conn):
        record_usage(conn, "summary", usage)
        conn.execute(
            "INSERT OR REPLACE INTO post_summaries (post_id, summary, issues, labeled_count, basis, model, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (post_id, summary, json.dumps(issues, ensure_ascii=False), labeled, basis,
             settings.sentiment_model, now()),
        )
    return True


# --- Period brief ---------------------------------------------------------------------


def brief_inputs(conn: sqlite3.Connection, since: int) -> tuple[list[tuple[str, dict[str, int], str]], str]:
    rows = conn.execute(
        """
        SELECT p.id, p.text, p.page_name, p.source_url, ps.summary, ps.created_at
        FROM posts p JOIN post_summaries ps ON ps.post_id = p.id
        WHERE ps.summary != '' AND EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.created_time >= ?)
        ORDER BY p.id
        """,
        (since,),
    ).fetchall()
    posts = [
        (post_title(r["text"], r["page_name"], r["source_url"]), label_counts(conn, r["id"]), r["summary"])
        for r in rows
    ]
    basis = ",".join(f"{r['id']}:{r['created_at']}" for r in rows)
    return posts, basis


def refresh_brief(conn: sqlite3.Connection, client: Any, settings: Settings) -> bool:
    posts, basis = brief_inputs(conn, now() - BRIEF_DAYS * 86400)
    if not posts:
        return False
    current = conn.execute("SELECT basis, created_at FROM briefs WHERE key = ?", (BRIEF_KEY,)).fetchone()
    if current and (current["basis"] == basis or now() - current["created_at"] < BRIEF_MIN_AGE):
        return False
    try:
        text, issues, usage = ai.write_brief(
            client, settings.sentiment_model, settings.sentiment_effort, f"the last {BRIEF_DAYS} days", posts
        )
    except ai.ModelError as e:
        with transaction(conn):
            record_usage(conn, "brief", e.usage)
            # Note the attempt (keeping any earlier text) so it is not retried every few seconds.
            conn.execute(
                "INSERT INTO briefs (key, text, issues, basis, model, created_at) VALUES (?, '', '[]', ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET basis = excluded.basis, created_at = excluded.created_at",
                (BRIEF_KEY, basis, settings.sentiment_model, now()),
            )
        log.warning("brief failed: %s", e)
        return False
    with transaction(conn):
        record_usage(conn, "brief", usage)
        conn.execute(
            "INSERT OR REPLACE INTO briefs (key, text, issues, basis, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (BRIEF_KEY, text, json.dumps(issues, ensure_ascii=False), basis, settings.sentiment_model, now()),
        )
    return True


def run_once(conn: sqlite3.Connection, client: Any, settings: Settings) -> bool:
    """One step of work; True if something was done (call again right away)."""
    if label_next_batch(conn, client, settings):
        return True
    if summarize_next_post(conn, client, settings):
        return True
    return refresh_brief(conn, client, settings)


# --- Background thread ----------------------------------------------------------------


def api_key() -> str | None:
    """ANTHROPIC_API_KEY from the environment. On Windows, a terminal opened before
    the key was saved does not see it, so the saved user setting is read too."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key or sys.platform != "win32":
        return key or None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
            value, _ = winreg.QueryValueEx(reg, "ANTHROPIC_API_KEY")
    except OSError:
        return None
    return str(value).strip() or None


def make_client() -> Any | None:
    """A Claude client when the API key is set; None otherwise (sentiment stays off)."""
    key = api_key()
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=300.0)


@dataclass
class WorkerStatus:
    state: str = "starting"
    message: str = ""
    last_error: str = ""
    last_error_at: int | None = None
    labeled_this_run: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, state: str, message: str = "") -> None:
        with self.lock:
            self.state, self.message = state, message

    def error(self, text: str) -> None:
        with self.lock:
            self.last_error, self.last_error_at = text[:300], now()


class AnalysisWorker(threading.Thread):
    def __init__(self, settings: Settings) -> None:
        super().__init__(name="sentiment-worker", daemon=True)
        self.settings = settings
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.status = WorkerStatus()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()

    def _pause(self, seconds: float) -> None:
        self.wake.wait(seconds)
        self.wake.clear()

    def run(self) -> None:
        import anthropic

        conn = connect(self.settings.db_path)
        client = None
        try:
            while not self.stop_event.is_set():
                client = client or make_client()
                if client is None:
                    self.status.set("off", "No API key: set ANTHROPIC_API_KEY on this computer and restart the website.")
                    self._pause(IDLE_SECONDS * 4)
                    continue
                try:
                    did = run_once(conn, client, self.settings)
                except anthropic.AuthenticationError as e:
                    self.status.set("paused", "The API key was rejected. Check ANTHROPIC_API_KEY.")
                    self.status.error(str(e))
                    self._pause(AUTH_PAUSE)
                    continue
                except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                    self.status.set("waiting", "Claude is busy or unreachable; retrying shortly.")
                    self.status.error(str(e))
                    self._pause(TRANSIENT_PAUSE)
                    continue
                except anthropic.APIStatusError as e:
                    self.status.set("waiting", f"Claude returned an error ({e.status_code}); retrying later.")
                    self.status.error(str(e))
                    self._pause(TRANSIENT_PAUSE)
                    continue
                except Exception as e:  # never let the worker die silently
                    log.exception("sentiment worker step failed")
                    self.status.set("waiting", "Something went wrong; retrying shortly.")
                    self.status.error(f"{type(e).__name__}: {e}")
                    self._pause(TRANSIENT_PAUSE)
                    continue
                if did:
                    self.status.set("working", "Analyzing comments.")
                else:
                    self.status.set("idle", "Up to date.")
                    self._pause(IDLE_SECONDS)
        finally:
            conn.close()
