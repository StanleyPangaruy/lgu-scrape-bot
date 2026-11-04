"""Read-side queries for the pages: post lists, comment threads, people, keyword counts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

from server.db import now
from server.keywords import Keyword

PERIODS = {"all": ("All time", None), "30": ("Last 30 days", 30), "7": ("Last 7 days", 7)}
DEFAULT_PERIOD = "all"
TITLE_LENGTH = 90


def short_link(url: str | None) -> str | None:
    """The identifying tail of a Facebook link, e.g. "share/p/1DycFHwTiZ"."""
    if not url:
        return None
    parts = urlsplit(url)
    tail = parts.path.strip("/")
    if parts.query:
        tail += "?" + parts.query
    if len(tail) > 40:
        tail = tail[:39] + "…"
    return tail or None


def post_title(
    text: str | None, page_name: str | None = None, link: str | None = None, comments: int | None = None
) -> str:
    """First line of the caption, shortened. Before a post's details are known
    (imported from CSV, or not collected yet), the link it was added with and
    its comment count tell posts apart."""
    if text:
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first:
            return first if len(first) <= TITLE_LENGTH else first[: TITLE_LENGTH - 1].rstrip() + "…"
    if page_name:
        return f"Post by {page_name}"
    label = "Post"
    tail = short_link(link)
    if tail:
        label += f" {tail}"
    if comments:
        label += f" · {comments} comment{'' if comments == 1 else 's'}"
    return label if label != "Post" else "Untitled post"


def has_details(row: Any) -> bool:
    return bool(row["text"] or row["page_name"])


FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
# Extra columns every comment query selects so comment_link() can build a link.
LINK_COLUMNS = "pc.legacy_fbid AS parent_legacy, p.url AS post_url"
LINK_JOIN = "LEFT JOIN comments pc ON pc.id = c.parent_id"


def _facebook_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname in FACEBOOK_HOSTS else None


def comment_link(row: Any) -> str | None:
    """Where the comment opens on Facebook: the link the scraper saw, else one
    built from the post's address and the comment numbers (a reply opens as
    ?comment_id=<its comment>&reply_comment_id=<the reply>)."""
    stored = _facebook_url(row["url"])
    if stored:
        return stored
    base = _facebook_url(row["post_url"])
    own = row["legacy_fbid"]
    if not base or not own:
        return None
    if row["parent_id"]:
        if not row["parent_legacy"]:
            return None
        params = {"comment_id": row["parent_legacy"], "reply_comment_id": own}
    else:
        params = {"comment_id": own}
    return base + ("&" if urlsplit(base).query else "?") + urlencode(params)


@dataclass
class Filters:
    period: str = DEFAULT_PERIOD
    page: str = ""

    @classmethod
    def from_query(cls, period: str | None, page: str | None) -> Filters:
        return cls(period=period if period in PERIODS else DEFAULT_PERIOD, page=(page or "").strip())

    @property
    def since(self) -> int | None:
        days = PERIODS[self.period][1]
        return None if days is None else now() - days * 86400

    def where(self) -> tuple[str, list[Any]]:
        """SQL conditions on `comments c JOIN posts p`."""
        clauses, params = [], []
        if self.since is not None:
            clauses.append("c.created_time >= ?")
            params.append(self.since)
        if self.page:
            clauses.append("p.page_name = ?")
            params.append(self.page)
        return (" AND ".join(clauses) or "1"), params


def page_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT page_name FROM posts WHERE page_name IS NOT NULL ORDER BY page_name"
        )
    ]


# Sort keys for the posts list: (label, SQL ORDER BY). Ties fall back to newest added.
POST_SORTS = {
    "posted": ("Newest posted", "COALESCE(p.created_time, p.added_at) DESC"),
    "comments": ("Most comments", "stored_comments DESC"),
    "checked": ("Recently checked", "COALESCE(p.last_checked_at, 0) DESC"),
    "added": ("Recently added", "p.added_at DESC"),
}
DEFAULT_POST_SORT = "posted"


def list_posts(conn: sqlite3.Connection, search: str = "", sort: str = DEFAULT_POST_SORT) -> list[sqlite3.Row]:
    order = POST_SORTS.get(sort, POST_SORTS[DEFAULT_POST_SORT])[1]
    where, params = "1", []
    if search:
        pattern = f"%{_escape_like(search)}%"
        where = ("(p.text LIKE ? ESCAPE '\\' OR p.page_name LIKE ? ESCAPE '\\' "
                 "OR p.source_url LIKE ? ESCAPE '\\' OR p.url LIKE ? ESCAPE '\\')")
        params = [pattern] * 4
    return conn.execute(
        f"""
        SELECT p.*, COUNT(c.id) AS stored_comments,
               (SELECT status FROM jobs j WHERE j.post_id = p.id
                  AND j.status IN ('queued', 'running') LIMIT 1) AS job_status
        FROM posts p LEFT JOIN comments c ON c.post_id = p.id
        WHERE {where}
        GROUP BY p.id
        ORDER BY {order}, p.added_at DESC, p.id DESC
        """,
        params,
    ).fetchall()


def totals(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM posts) AS posts,
               (SELECT COUNT(*) FROM comments) AS comments,
               (SELECT COUNT(DISTINCT comment_id) FROM keyword_hits) AS keyword_comments,
               (SELECT COUNT(*) FROM posts WHERE text IS NULL AND page_name IS NULL) AS untitled
        """
    ).fetchone()


def post_topics(conn: sqlite3.Connection, post_id: int, keywords: dict[int, Keyword]) -> list[tuple[Keyword, int]]:
    """Keywords mentioned in this post's comments, most mentioned first."""
    rows = conn.execute(
        """
        SELECT h.keyword_id, COUNT(*) AS n FROM keyword_hits h
        JOIN comments c ON c.id = h.comment_id
        WHERE c.post_id = ? GROUP BY h.keyword_id
        """,
        (post_id,),
    ).fetchall()
    found = [(keywords[r["keyword_id"]], r["n"]) for r in rows if r["keyword_id"] in keywords]
    return sorted(found, key=lambda kn: (-kn[1], kn[0].term.lower()))


def get_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.*, COUNT(c.id) AS stored_comments,
               SUM(c.parent_id IS NULL) AS top_level
        FROM posts p LEFT JOIN comments c ON c.post_id = p.id
        WHERE p.id = ? GROUP BY p.id
        """,
        (post_id,),
    ).fetchone()


def keywords_by_comment(conn: sqlite3.Connection, post_id: int) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for r in conn.execute(
        """
        SELECT h.comment_id, h.keyword_id FROM keyword_hits h
        JOIN comments c ON c.id = h.comment_id WHERE c.post_id = ?
        """,
        (post_id,),
    ):
        hits.setdefault(r["comment_id"], []).append(r["keyword_id"])
    return hits


@dataclass
class CommentNode:
    row: sqlite3.Row
    keyword_ids: list[int]
    replies: list[CommentNode] = field(default_factory=list)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


COMMENT_ORDERS = {"oldest": "Oldest first", "newest": "Newest first", "reactions": "Most reactions"}
DEFAULT_COMMENT_ORDER = "oldest"


def _order_key(order: str):
    if order == "reactions":
        return lambda n: (-(n.row["reaction_count"] or 0), n.row["created_time"] or 0)
    if order == "newest":
        return lambda n: (-(n.row["created_time"] or 0),)
    return lambda n: (n.row["created_time"] or 0,)


def post_comments(
    conn: sqlite3.Connection,
    post_id: int,
    search: str = "",
    keyword_id: int | None = None,
    hits_only: bool = False,
    order: str = DEFAULT_COMMENT_ORDER,
) -> tuple[list[CommentNode], bool]:
    """Comments of a post. Without filters: threads (replies under their
    comment, oldest first), the threads in the chosen order. With a filter: a
    flat list of matches in the chosen order. Returns (comments, threaded)."""
    hits = keywords_by_comment(conn, post_id)
    clauses, params = ["c.post_id = ?"], [post_id]
    if search:
        clauses.append("(c.text LIKE ? ESCAPE '\\' OR c.author_name LIKE ? ESCAPE '\\')")
        pattern = f"%{_escape_like(search)}%"
        params += [pattern, pattern]
    rows = conn.execute(
        f"SELECT c.*, {LINK_COLUMNS} FROM comments c JOIN posts p ON p.id = c.post_id {LINK_JOIN} "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY COALESCE(c.created_time, 0), c.id",
        params,
    ).fetchall()

    filtered = bool(search) or keyword_id is not None or hits_only
    nodes = []
    for r in rows:
        ids = hits.get(r["id"], [])
        if keyword_id is not None and keyword_id not in ids:
            continue
        if hits_only and not ids:
            continue
        nodes.append(CommentNode(r, ids))
    if filtered:
        return sorted(nodes, key=_order_key(order)), False

    # A reply attaches only to a parent already placed (parents come first in time
    # order), so bad data can never form a cycle that hides comments.
    placed: dict[str, CommentNode] = {}
    roots: list[CommentNode] = []
    for n in nodes:
        parent = placed.get(n.row["parent_id"]) if n.row["parent_id"] else None
        if parent is not None:
            parent.replies.append(n)
        else:
            roots.append(n)
        placed[n.row["id"]] = n
    # Only the threads are reordered; replies stay in conversation order under them.
    roots.sort(key=_order_key(order))
    return roots, True


@dataclass(frozen=True)
class Author:
    """A commenter: by Facebook user id when known, otherwise by name alone."""

    author_id: str | None
    name: str | None

    @classmethod
    def from_query(cls, id: str | None, name: str | None) -> Author | None:
        id = (id or "").strip()
        if id.isdigit():
            return cls(id, None)
        name = (name or "").strip()
        return cls(None, name) if name else None

    def where(self) -> tuple[str, list[Any]]:
        if self.author_id:
            return "c.author_id = ?", [self.author_id]
        return "c.author_id IS NULL AND c.author_name = ?", [self.name]


def author_href(author_id: str | None, name: str | None) -> str:
    """Link to a commenter's page; people without a known id are found by name."""
    if author_id:
        return "/people/author?" + urlencode({"id": author_id})
    return "/people/author?" + urlencode({"name": name or ""})


def author_summary(conn: sqlite3.Connection, author: Author) -> sqlite3.Row | None:
    where, params = author.where()
    row = conn.execute(
        f"""
        SELECT MAX(c.author_name) AS name, MAX(c.author_url) AS url, COUNT(*) AS comments,
               COUNT(DISTINCT c.post_id) AS posts, COALESCE(SUM(c.reaction_count), 0) AS reactions,
               MIN(c.created_time) AS first_at, MAX(c.created_time) AS last_at
        FROM comments c WHERE {where}
        """,
        params,
    ).fetchone()
    return row if row["comments"] else None


def author_comments(conn: sqlite3.Connection, author: Author) -> list[sqlite3.Row]:
    where, params = author.where()
    return conn.execute(
        f"""
        SELECT c.*, p.text AS post_text, p.page_name AS post_page, p.source_url AS post_link, {LINK_COLUMNS},
               (SELECT GROUP_CONCAT(h.keyword_id) FROM keyword_hits h WHERE h.comment_id = c.id) AS keyword_ids
        FROM comments c JOIN posts p ON p.id = c.post_id {LINK_JOIN}
        WHERE {where}
        ORDER BY COALESCE(c.created_time, 0) DESC, c.id
        """,
        params,
    ).fetchall()


def top_commenters(conn: sqlite3.Connection, filters: Filters, limit: int = 50) -> list[sqlite3.Row]:
    where, params = filters.where()
    return conn.execute(
        f"""
        SELECT COALESCE(c.author_id, 'name:' || c.author_name) AS who,
               MAX(c.author_id) AS author_id,
               MAX(c.author_name) AS name, MAX(c.author_url) AS url,
               COUNT(*) AS comments, COUNT(DISTINCT c.post_id) AS posts,
               COALESCE(SUM(c.reaction_count), 0) AS reactions
        FROM comments c JOIN posts p ON p.id = c.post_id
        WHERE {where} AND (c.author_id IS NOT NULL OR c.author_name IS NOT NULL)
        GROUP BY who
        ORDER BY comments DESC, reactions DESC, name
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def most_reacted(conn: sqlite3.Connection, filters: Filters, limit: int = 20) -> list[sqlite3.Row]:
    where, params = filters.where()
    return conn.execute(
        f"""
        SELECT c.*, p.text AS post_text, p.page_name AS post_page, p.source_url AS post_link, {LINK_COLUMNS}
        FROM comments c JOIN posts p ON p.id = c.post_id {LINK_JOIN}
        WHERE {where} AND c.reaction_count > 0
        ORDER BY c.reaction_count DESC, c.created_time DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def keyword_counts(conn: sqlite3.Connection, keywords: list[Keyword], filters: Filters) -> dict[int, int]:
    where, params = filters.where()
    counts = {k.id: 0 for k in keywords}
    for r in conn.execute(
        f"""
        SELECT h.keyword_id, COUNT(*) AS n FROM keyword_hits h
        JOIN comments c ON c.id = h.comment_id JOIN posts p ON p.id = c.post_id
        WHERE {where} GROUP BY h.keyword_id
        """,
        params,
    ):
        counts[r["keyword_id"]] = r["n"]
    return counts


def keyword_comments(
    conn: sqlite3.Connection, keyword_id: int, filters: Filters, limit: int = 200
) -> list[sqlite3.Row]:
    where, params = filters.where()
    return conn.execute(
        f"""
        SELECT c.*, p.text AS post_text, p.page_name AS post_page, p.source_url AS post_link, {LINK_COLUMNS}
        FROM keyword_hits h JOIN comments c ON c.id = h.comment_id
        JOIN posts p ON p.id = c.post_id {LINK_JOIN}
        WHERE h.keyword_id = ? AND {where}
        ORDER BY c.created_time DESC
        LIMIT ?
        """,
        (keyword_id, *params, limit),
    ).fetchall()
