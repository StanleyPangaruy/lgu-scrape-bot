"""Network listener and GraphQL payload parser.

Comments are read from Facebook's GraphQL JSON responses (and the JSON blobs
embedded in the initial HTML), never from obfuscated CSS classes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, fields
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
        parent_id=_first(node, ("comment_parent", "id"), ("parent_comment", "id")),
        depth=_to_int(node.get("depth")),
        author_id=_get(node, "author", "id"),
        author_name=_get(node, "author", "name"),
        author_url=_first(node, ("author", "url"), ("author", "profile_url")),
        text=_first(node, ("body", "text"), ("preferred_body", "text")),
        created_time=created,
        created_iso=(
            datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None
        ),
        reaction_count=_to_int(
            _first(
                node,
                ("feedback", "reactors", "count"),
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


class CommentInterceptor:
    def __init__(self, max_comments: int | None = None) -> None:
        self.max_comments = max_comments
        self.seen_ids: set[str] = set()
        self.comments: dict[str, Comment] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def count(self) -> int:
        return len(self.seen_ids)

    @property
    def limit_reached(self) -> bool:
        return self.max_comments is not None and self.count >= self.max_comments

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

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
            # Same comment seen again (e.g. a lighter reference node); fill gaps only.
            for name in COMMENT_FIELDS:
                if getattr(existing, name) is None and getattr(comment, name) is not None:
                    setattr(existing, name, getattr(comment, name))
            return 0
        if self.limit_reached:
            return 0
        self.seen_ids.add(comment.id)
        self.comments[comment.id] = comment
        return 1

    async def drain(self) -> None:
        """Wait for in-flight response handlers to finish parsing."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def results(self) -> list[Comment]:
        return sorted(self.comments.values(), key=lambda c: (c.created_time or 0, c.id))
