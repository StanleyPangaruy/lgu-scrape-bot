"""Details of the post itself (page, caption, date), read from the post page's
embedded JSON, plus helpers that tie comments and URLs back to that post.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from scraper.interceptor import SharedPost, iter_json_documents, iter_story_nodes, parse_shared_post

if TYPE_CHECKING:
    from playwright.async_api import Page

# Query parameters that identify a post; every other one (rdid, __cft__, …) is tracking.
POST_QUERY_KEYS = ("story_fbid", "id", "fbid", "v")
NUMERIC_POST_ID = re.compile(r"/(?:posts|permalink|videos|photos?)/(\d{6,})")
# Reels and videos: /reel/<video id>, /<page>/videos/<video id>, /watch/?v=<video id>.
VIDEO_PATH_ID = re.compile(r"/(?:reel|videos)/(\d{6,})")


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


def video_id(url: str) -> str | None:
    """The video id of a reel or video page, else None."""
    parts = urlsplit(url)
    match = VIDEO_PATH_ID.search(parts.path)
    if match:
        return match.group(1)
    if parts.path.rstrip("/").endswith("/watch"):
        v = (parse_qs(parts.query).get("v") or [None])[0]
        return v if v and v.isdigit() else None
    return None


def _walk_dicts(node: Any) -> Iterator[dict]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def video_story_info(docs: list[Any], video: str, url: str) -> PostInfo | None:
    """A reel's or video's own post, from the page's JSON.

    The video node names its story (`creation_story.id`); that story's fields
    (post id, caption, date, author, comment count) are spread over several
    partial copies, so they are merged. Comments on the video are keyed by the
    story's `post_id`, not by the video id in the URL.
    """
    dicts = [d for doc in docs for d in _walk_dicts(doc)]
    story_id = next(
        (
            d["creation_story"]["id"]
            for d in dicts
            if d.get("id") == video and isinstance(d.get("creation_story"), dict)
            and isinstance(d["creation_story"].get("id"), str)
        ),
        None,
    )
    if story_id is None:
        return None
    info = PostInfo(url=url)
    for d in dicts:
        if d.get("id") != story_id:
            continue
        if info.post_id is None and str(d.get("post_id") or "").isdigit():
            info.post_id = str(d["post_id"])
        if info.created_time is None and isinstance(d.get("creation_time"), int):
            info.created_time = d["creation_time"]
        message = d.get("message")
        if info.text is None and isinstance(message, dict) and isinstance(message.get("text"), str):
            info.text = message["text"]
        actors = d.get("actors")
        if info.page_name is None and isinstance(actors, list) and actors and isinstance(actors[0], dict):
            actor = actors[0]
            info.page_id = actor.get("id")
            info.page_name = actor.get("name")
            info.page_url = actor.get("url") or actor.get("profile_url")
        feedback = d.get("feedback")
        if info.comment_count is None and isinstance(feedback, dict):
            count = feedback.get("total_comment_count")
            if isinstance(count, int):
                info.comment_count = count
    return info if info.post_id else None


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
    video = video_id(final_url)
    if video is not None:
        docs = [doc for blob in blobs if blob and video in blob for doc in iter_json_documents(blob)]
        found = video_story_info(docs, video, final_url)
        if found is not None:
            return found

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
