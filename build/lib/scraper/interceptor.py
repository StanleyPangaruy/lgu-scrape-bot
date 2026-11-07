"""Network listener and GraphQL payload parser.

Comments and shared posts are read from Facebook's GraphQL JSON responses (and
the JSON blobs embedded in the initial HTML), never from obfuscated CSS classes.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from playwright.async_api import Page, Response

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
ANTI_JSON_PREFIX = "for (;;);"


@dataclass
class Comment:
    id: str
    legacy_fbid: str | None = None
    parent_id: str | None = None
    depth: int | None = None
    author_id: str | None = None
    author_name: str | None = None
    author_url: str | None = None
    text: str | None = None
    created_time: int | None = None
    created_iso: str | None = None
    reaction_count: int | None = None
    reply_count: int | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COMMENT_FIELDS = [f.name for f in fields(Comment)]


def _get(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _first(node: dict, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _get(node, *path)
        if value is not None:
            return value
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _to_iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat() if timestamp else None


def _is_comment_node(node: dict) -> bool:
    if node.get("__typename") == "Comment":
        return "id" in node and ("body" in node or "author" in node)
    # Some payloads omit __typename; require the core comment shape instead.
    return all(k in node for k in ("id", "body", "author", "created_time"))


def parse_comment(node: dict) -> Comment:
    created = _to_int(node.get("created_time"))
    return Comment(
        id=str(node["id"]),
        legacy_fbid=_first(node, ("legacy_fbid",), ("legacy_token",)),
        parent_id=_first(
            node,
            ("comment_direct_parent", "id"),
            ("comment_parent", "id"),
            ("parent_comment", "id"),
        ),
        depth=_to_int(node.get("depth")),
        author_id=_get(node, "author", "id"),
        author_name=_get(node, "author", "name"),
        author_url=_first(node, ("author", "url"), ("author", "profile_url")),
        text=_first(node, ("body", "text"), ("preferred_body", "text")),
        created_time=created,
        created_iso=_to_iso(created),
        reaction_count=_to_int(
            _first(
                node,
                ("feedback", "reactors", "count"),
                ("feedback", "reactors", "count_reduced"),
                ("feedback", "reaction_count", "count"),
                ("feedback", "i18n_reaction_count"),
            )
        ),
        reply_count=_to_int(
            _first(
                node,
                ("feedback", "replies_fields", "total_count"),
                ("feedback", "total_comment_count"),
                ("feedback", "replies_connection", "count"),
            )
        ),
        url=_first(node, ("url",), ("feedback", "url")),
    )


def iter_comment_nodes(obj: Any) -> Iterator[dict]:
    """Depth-first walk yielding every dict that looks like a Comment node."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _is_comment_node(cur):
                yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def iter_json_documents(text: str) -> Iterator[Any]:
    """Facebook may prefix responses with `for (;;);` and stream several JSON docs per body."""
    text = text.strip()
    if text.startswith(ANTI_JSON_PREFIX):
        text = text[len(ANTI_JSON_PREFIX):]
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _fill_missing(existing: Any, new: Any, names: list[str]) -> None:
    """Same object seen again (e.g. a lighter reference node); fill gaps only."""
    for name in names:
        if getattr(existing, name) is None and getattr(new, name) is not None:
            setattr(existing, name, getattr(new, name))


class _GraphQLListener:
    """Feeds every GraphQL response body on a page to `ingest_text`."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def detach(self, page: Page) -> None:
        page.remove_listener("response", self._on_response)

    def ingest_text(self, text: str) -> int:
        raise NotImplementedError

    def _on_response(self, response: Response) -> None:
        if not response.url.startswith(GRAPHQL_URL):
            return
        task = asyncio.ensure_future(self._handle_response(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_response(self, response: Response) -> None:
        try:
            body = await response.text()
        except Exception:
            # Body can be unavailable (redirects, closed page, evicted from cache).
            return
        self.ingest_text(body)

    async def drain(self) -> None:
        """Wait for in-flight response handlers to finish parsing."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


class CommentInterceptor(_GraphQLListener):
    def __init__(self, max_comments: int | None = None) -> None:
        super().__init__()
        self.max_comments = max_comments
        self.seen_ids: set[str] = set()
        self.comments: dict[str, Comment] = {}

    @property
    def count(self) -> int:
        return len(self.seen_ids)

    @property
    def limit_reached(self) -> bool:
        return self.max_comments is not None and self.count >= self.max_comments

    async def ingest_page_scripts(self, page: Page) -> None:
        """The first batch of comments ships inside the HTML as JSON <script> blobs."""
        blobs = await page.eval_on_selector_all(
            'script[type="application/json"]', "els => els.map(e => e.textContent)"
        )
        for blob in blobs:
            if blob and "created_time" in blob:
                self.ingest_text(blob)

    def ingest_text(self, text: str) -> int:
        added = 0
        for doc in iter_json_documents(text):
            for node in iter_comment_nodes(doc):
                added += self._add(parse_comment(node))
        return added

    def _add(self, comment: Comment) -> int:
        existing = self.comments.get(comment.id)
        if existing is not None:
            _fill_missing(existing, comment, COMMENT_FIELDS)
            return 0
        if self.limit_reached:
            return 0
        self.seen_ids.add(comment.id)
        self.comments[comment.id] = comment
        return 1

    def results(self) -> list[Comment]:
        return sorted(self.comments.values(), key=lambda c: (c.created_time or 0, c.id))


# --- Shared posts (re-shares of a post, listed in its "N shares" dialog) ---


@dataclass
class SharedPost:
    id: str
    post_id: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    author_url: str | None = None
    text: str | None = None
    created_time: int | None = None
    created_iso: str | None = None
    comment_count: int | None = None
    url: str | None = None
    comments: list[Comment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SHARE_FIELDS = [f.name for f in fields(SharedPost) if f.name != "comments"]

# A share wraps the original post (attached_story / attachments). Searching inside
# those would return the original author's name and text instead of the sharer's.
SHARE_SKIP_KEYS = frozenset({"attached_story", "attachments", "attachment"})
POST_URL_HINT = re.compile(r"/posts/|/permalink|story_fbid=|/videos/|/photos?/|/reel/|pfbid")


def _iter_key(node: Any, key: str) -> Iterator[Any]:
    """Breadth-first: values of `key` anywhere under node, shallowest first.

    Comet nests story fields deep inside `comet_sections`, so fixed paths are brittle.
    """
    queue = deque([node])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            if cur.get(key) is not None:
                yield cur[key]
            queue.extend(v for k, v in cur.items() if k not in SHARE_SKIP_KEYS)
        elif isinstance(cur, list):
            queue.extend(cur)


def _find_key(node: Any, key: str) -> Any:
    return next(_iter_key(node, key), None)


def _find_post_url(node: dict) -> str | None:
    for key in ("permalink_url", "url"):
        for value in _iter_key(node, key):
            if isinstance(value, str) and POST_URL_HINT.search(value):
                return value
    return None


def _find_comment_count(node: dict) -> int | None:
    for key in ("comment_count", "total_comment_count"):
        value = _find_key(node, key)
        if isinstance(value, dict):
            value = value.get("total_count")
        count = _to_int(value)
        if count is not None:
            return count
    for instance in _iter_key(node, "comment_rendering_instance"):
        count = _to_int(_get(instance, "comments", "total_count"))
        if count is not None:
            return count
    return None


def _find_actor(node: dict) -> dict:
    """The sharer, merged across the story's many partial `actors` copies
    (some carry the name, others only the profile url)."""
    merged: dict[str, Any] = {}
    for actors in _iter_key(node, "actors"):
        if not isinstance(actors, list) or not actors or not isinstance(actors[0], dict):
            continue
        actor = actors[0]
        if merged and actor.get("id") != merged.get("id"):
            continue
        for key in ("id", "name", "url", "profile_url"):
            if merged.get(key) is None and actor.get(key) is not None:
                merged[key] = actor[key]
    return merged


def iter_story_nodes(obj: Any) -> Iterator[dict]:
    """Outermost Story nodes only; their nested sections repeat the same story."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("__typename") == "Story" and isinstance(cur.get("id"), str):
                yield cur
                continue
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def parse_shared_post(node: dict) -> SharedPost:
    actor = _find_actor(node)
    message = _find_key(node, "message")
    created = _to_int(_find_key(node, "creation_time"))
    post_id = _find_key(node, "post_id")
    return SharedPost(
        id=node["id"],
        post_id=str(post_id) if post_id is not None else None,
        author_id=_get(actor, "id"),
        author_name=_get(actor, "name"),
        author_url=_first(actor, ("url",), ("profile_url",)),
        text=_get(message, "text"),
        created_time=created,
        created_iso=_to_iso(created),
        comment_count=_find_comment_count(node),
        url=_find_post_url(node),
    )


class ShareInterceptor(_GraphQLListener):
    """Collects shared posts. Attach only while the shares dialog is open, so the
    original post and unrelated feed stories aren't mistaken for shares."""

    def __init__(self, max_shares: int | None = None) -> None:
        super().__init__()
        self.max_shares = max_shares
        self.shares: dict[str, SharedPost] = {}

    @property
    def count(self) -> int:
        return len(self.shares)

    @property
    def limit_reached(self) -> bool:
        return self.max_shares is not None and self.count >= self.max_shares

    def ingest_text(self, text: str) -> int:
        added = 0
        for doc in iter_json_documents(text):
            for node in iter_story_nodes(doc):
                added += self._add(parse_shared_post(node))
        return added

    def _add(self, share: SharedPost) -> int:
        existing = self.shares.get(share.id)
        if existing is not None:
            _fill_missing(existing, share, SHARE_FIELDS)
            return 0
        # Without a link there's nothing to open. This also drops the original
        # post, which appears in the share list as a bare reference.
        if self.limit_reached or share.url is None:
            return 0
        self.shares[share.id] = share
        return 1

    def results(self) -> list[SharedPost]:
        return list(self.shares.values())


async def fill_share_from_page(page: Page, share: SharedPost) -> None:
    """Fill gaps in `share` from its own post page, which is open in `page`.

    Share history leaves out the sharer's caption (and often the comment count
    and profile link); the shared post's page embeds the full story.
    """
    blobs = await page.eval_on_selector_all(
        'script[type="application/json"]', "els => els.map(e => e.textContent)"
    )
    for blob in blobs:
        if not blob or share.id not in blob:
            continue
        for doc in iter_json_documents(blob):
            for node in iter_story_nodes(doc):
                if node["id"] == share.id:
                    _fill_missing(share, parse_shared_post(node), SHARE_FIELDS)
