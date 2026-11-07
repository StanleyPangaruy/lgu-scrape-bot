"""Details of the post itself (page, caption, date), read from the post page's
embedded JSON, plus helpers that tie comments and URLs back to that post.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from scraper.interceptor import SharedPost, iter_json_documents, iter_story_nodes, parse_shared_post

if TYPE_CHECKING:
    from playwright.async_api import Page

# Query parameters that identify a post; every other one (rdid, __cft__, …) is tracking.
POST_QUERY_KEYS = ("story_fbid", "id", "fbid")
NUMERIC_POST_ID = re.compile(r"/(?:posts|permalink|videos|photos?)/(\d{6,})")


@dataclass
class PostInfo:
    url: str
    post_id: str | None = None
    page_id: str | None = None
    page_name: str | None = None
    page_url: str | None = None
    text: str | None = None
    created_time: int | None = None
    comment_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_url(url: str) -> str:
    """Drop tracking parameters and fragments, keeping only what identifies the post."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    kept = [(k, query[k][0]) for k in POST_QUERY_KEYS if k in query]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def comment_post_id(comment_id: str) -> str | None:
    """Facebook comment ids are base64 of `comment:<post id>_<comment id>`."""
    try:
        decoded = base64.b64decode(comment_id + "=" * (-len(comment_id) % 4), validate=True)
        text = decoded.decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not text.startswith("comment:") or "_" not in text:
        return None
    post_id = text[len("comment:"):].split("_", 1)[0]
    return post_id if post_id.isdigit() else None


def _path_slug(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return segments[0].lower() if segments else ""


def pick_main_story(stories: list[SharedPost], final_url: str) -> SharedPost | None:
    """The post the page is about; post pages also embed unrelated feed stories.

    Matched by the numeric post id in the URL when there is one, otherwise by
    the page (profile slug or `id=` parameter) the URL belongs to.
    """
    parts = urlsplit(final_url)
    query = parse_qs(parts.query)

    numeric = NUMERIC_POST_ID.search(parts.path)
    wanted_post_id = numeric.group(1) if numeric else None
    fbid = (query.get("story_fbid") or query.get("fbid") or [None])[0]
    if wanted_post_id is None and fbid and fbid.isdigit():
        wanted_post_id = fbid
    if wanted_post_id:
        for story in stories:
            if story.post_id == wanted_post_id:
                return story

    owner_id = (query.get("id") or [None])[0]
    slug = _path_slug(final_url)
    for story in stories:
        if owner_id and story.author_id == owner_id:
            return story
        if slug and slug not in ("permalink.php", "groups", "story.php") and story.author_url:
            if _path_slug(story.author_url) == slug:
                return story
    return None


async def read_post_info(page: Page) -> PostInfo:
    """Details of the post open in `page`, as far as its embedded JSON tells."""
    final_url = canonical_url(page.url)
    blobs = await page.eval_on_selector_all(
        'script[type="application/json"]', "els => els.map(e => e.textContent)"
    )
    stories: dict[str, SharedPost] = {}
    for blob in blobs:
        if not blob or '"Story"' not in blob:
            continue
        for doc in iter_json_documents(blob):
            for node in iter_story_nodes(doc):
                story = parse_shared_post(node)
                stories.setdefault(story.id, story)

    main = pick_main_story(list(stories.values()), final_url)
    if main is None:
        return PostInfo(url=final_url)
    return PostInfo(
        url=final_url,
        post_id=main.post_id,
        page_id=main.author_id,
        page_name=main.author_name,
        page_url=main.author_url,
        text=main.text,
        created_time=main.created_time,
        comment_count=main.comment_count,
    )
