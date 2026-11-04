"""Saving scraped posts and comments, from the agent or from old CSV files.

A comment is stored once (keyed by Facebook's comment id). Collecting a post
again adds new comments and refreshes reaction and reply counts; it never
deletes, so a comment removed on Facebook stays in the record.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from scraper.post_info import canonical_url, comment_post_id
from server.db import now
from server.keywords import normalize, refresh_hits

FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
COMMENT_FIELDS = (
    "id", "legacy_fbid", "parent_id", "depth", "author_id", "author_name", "author_url",
    "text", "created_time", "reaction_count", "reply_count", "url",
)
POST_FIELDS = ("page_id", "page_name", "page_url", "text", "created_time")


class InvalidLink(ValueError):
    pass


def clean_post_link(raw: str) -> str:
    """A Facebook post link as typed or pasted, checked and stripped of tracking."""
    link = raw.strip()
    parts = urlsplit(link)
    if parts.scheme != "https" or parts.hostname not in FACEBOOK_HOSTS:
        raise InvalidLink("Paste a link that starts with https://www.facebook.com/")
    if parts.path in ("", "/"):
        raise InvalidLink("That is the Facebook home page, not a post. Copy the post's own link.")
    return canonical_url(link)


def safe_web_url(url: str | None) -> str | None:
    """Only http(s) links are shown as links; anything else (javascript:, data:) is dropped."""
    if url and urlsplit(url).scheme in ("http", "https"):
        return url
    return None


@dataclass
class IngestResult:
    post_id: int
    new: int = 0
    updated: int = 0
    dropped: int = 0
    merged_into: int | None = None
    notes: list[str] = field(default_factory=list)


def find_post_by_link(conn: sqlite3.Connection, link: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM posts WHERE source_url = ? OR url = ? ORDER BY id LIMIT 1", (link, link)
    ).fetchone()


def create_post(conn: sqlite3.Connection, link: str, added_by: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO posts (url, source_url, added_by, added_at) VALUES (?, ?, ?, ?)",
        (link, link, added_by, now()),
    )
    return int(cur.lastrowid)


def _merge_post(conn: sqlite3.Connection, duplicate: int, keep: int) -> None:
    """Two links turned out to be the same Facebook post: fold `duplicate` into `keep`."""
    conn.execute("UPDATE comments SET post_id = ? WHERE post_id = ?", (keep, duplicate))
    conn.execute("UPDATE jobs SET post_id = ? WHERE post_id = ?", (keep, duplicate))
    conn.execute("DELETE FROM posts WHERE id = ?", (duplicate,))


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _fb_id_or_none(value: Any) -> str | None:
    """Facebook user and comment numbers are all digits. Anything else is damaged,
    typically a CSV saved by Excel ("1.00003E+14"), and is treated as unknown so
    it can never overwrite a good id or merge different people."""
    text = _text_or_none(value)
    return text.strip() if text and text.strip().isdigit() else None


def save_scrape(
    conn: sqlite3.Connection,
    post_id: int,
    post: Mapping[str, Any],
    comments: Iterable[Mapping[str, Any]],
    checked_at: int | None = None,
) -> IngestResult:
    """Store one collection of a post. Call inside a transaction."""
    checked_at = checked_at or now()
    result = IngestResult(post_id=post_id)

    fb_post_id = _text_or_none(post.get("post_id"))
    if fb_post_id:
        other = conn.execute(
            "SELECT id FROM posts WHERE fb_post_id = ? AND id != ?", (fb_post_id, post_id)
        ).fetchone()
        if other:
            _merge_post(conn, duplicate=post_id, keep=other["id"])
            result.merged_into = post_id = result.post_id = other["id"]
            result.notes.append("This link is the same post as one already in the list; merged.")

    assignments = {name: post.get(name) for name in POST_FIELDS if post.get(name) not in (None, "")}
    if "page_id" in assignments and not _fb_id_or_none(assignments["page_id"]):
        del assignments["page_id"]
    if fb_post_id:
        assignments["fb_post_id"] = fb_post_id
    if post.get("url"):
        assignments["url"] = str(post["url"])
    if post.get("comment_count") is not None:
        assignments["fb_comment_count"] = _int_or_none(post.get("comment_count"))
    if assignments:
        sets = ", ".join(f"{name} = ?" for name in assignments)
        conn.execute(f"UPDATE posts SET {sets} WHERE id = ?", (*assignments.values(), post_id))
    # Only ever moves forward: importing an older file must not make a post look stale.
    conn.execute(
        "UPDATE posts SET last_checked_at = MAX(COALESCE(last_checked_at, 0), ?) WHERE id = ?",
        (checked_at, post_id),
    )

    existing = {
        r["id"] for r in conn.execute("SELECT id FROM comments WHERE post_id = ?", (post_id,))
    }
    touched: list[str] = []
    seen: set[str] = set()
    for raw in comments:
        cid = _text_or_none(raw.get("id"))
        if not cid or cid in seen:
            continue
        seen.add(cid)
        belongs_to = comment_post_id(cid)
        if fb_post_id and belongs_to and belongs_to != fb_post_id:
            # A comment from another story on the page (a feed suggestion), not this post.
            result.dropped += 1
            continue
        if cid not in existing:
            owner = conn.execute("SELECT post_id FROM comments WHERE id = ?", (cid,)).fetchone()
            if owner is not None:
                # Already stored under another post (that post was collected without its
                # Facebook id); it is the same comment, so leave it where it is.
                result.dropped += 1
                continue
        row = {name: raw.get(name) for name in COMMENT_FIELDS}
        text = _text_or_none(row["text"])
        values = (
            cid, post_id, _fb_id_or_none(row["legacy_fbid"]), _text_or_none(row["parent_id"]),
            _int_or_none(row["depth"]), _fb_id_or_none(row["author_id"]),
            _text_or_none(row["author_name"]), _text_or_none(row["author_url"]), text,
            normalize(text or ""), _int_or_none(row["created_time"]),
            _int_or_none(row["reaction_count"]), _int_or_none(row["reply_count"]),
            _text_or_none(row["url"]), checked_at, checked_at,
        )
        conn.execute(
            """
            INSERT INTO comments (
                id, post_id, legacy_fbid, parent_id, depth, author_id, author_name, author_url,
                text, text_norm, created_time, reaction_count, reply_count, url,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                legacy_fbid    = COALESCE(excluded.legacy_fbid, legacy_fbid),
                parent_id      = COALESCE(excluded.parent_id, parent_id),
                depth          = COALESCE(excluded.depth, depth),
                author_id      = COALESCE(excluded.author_id, author_id),
                author_name    = COALESCE(excluded.author_name, author_name),
                author_url     = COALESCE(excluded.author_url, author_url),
                text           = COALESCE(excluded.text, text),
                text_norm      = CASE WHEN excluded.text IS NULL THEN text_norm ELSE excluded.text_norm END,
                created_time   = COALESCE(excluded.created_time, created_time),
                reaction_count = COALESCE(excluded.reaction_count, reaction_count),
                reply_count    = COALESCE(excluded.reply_count, reply_count),
                url            = COALESCE(excluded.url, url),
                last_seen_at   = excluded.last_seen_at
            """,
            values,
        )
        touched.append(cid)
        if cid in existing:
            result.updated += 1
        else:
            result.new += 1

    if result.new:
        conn.execute(
            "UPDATE posts SET last_new_at = MAX(COALESCE(last_new_at, 0), ?) WHERE id = ?",
            (checked_at, post_id),
        )
    refresh_hits(conn, touched)
    return result


def read_comment_csv(path: Path) -> list[dict[str, str]]:
    """Rows of a CSV written by `python main.py --url … --output x.csv`."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "id" not in reader.fieldnames:
            raise ValueError(f"{path.name} is not a comments CSV (no 'id' column)")
        return list(reader)


def import_csv(
    conn: sqlite3.Connection, path: Path, link: str, added_by: int | None = None
) -> IngestResult:
    """Import an old scrape. The CSV has comments only, so the post's page and
    caption stay empty until someone re-checks it."""
    rows = read_comment_csv(path)
    owners = Counter(p for p in (comment_post_id(r["id"]) for r in rows if r.get("id")) if p)
    fb_post_id = owners.most_common(1)[0][0] if owners else None

    existing = find_post_by_link(conn, link)
    post_id = existing["id"] if existing else create_post(conn, link, added_by)
    checked_at = int(path.stat().st_mtime)
    return save_scrape(conn, post_id, {"post_id": fb_post_id}, rows, checked_at=checked_at)
