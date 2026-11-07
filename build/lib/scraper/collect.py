"""Collecting a post's comments: open it, sort by all comments, expand everything."""

from __future__ import annotations

from typing import Awaitable, Callable

from playwright.async_api import Page

from scraper.actions import expand_all, jitter, switch_to_all_comments
from scraper.interceptor import Comment, CommentInterceptor


async def open_post(page: Page, url: str) -> None:
    print(f"Opening {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await jitter()


async def collect_comments(
    page: Page,
    max_comments: int | None,
    url: str | None = None,
    done: Callable[[], Awaitable[bool]] | None = None,
    interceptor: CommentInterceptor | None = None,
    after_open: Callable[[Page], Awaitable[None]] | None = None,
) -> list[Comment]:
    """Return all comments and replies of the post at `url`, or of the post
    already open in `page` when `url` is None. With `done`, stop paginating
    as soon as it returns True. Pass `interceptor` to watch progress while it
    runs, and `after_open` to read the freshly loaded page before scrolling."""
    if interceptor is None:
        interceptor = CommentInterceptor(max_comments=max_comments)
    interceptor.attach(page)
    try:
        if url is not None:
            await open_post(page, url)
        if after_open is not None:
            await after_open(page)
        await interceptor.ingest_page_scripts(page)

        order = await switch_to_all_comments(page)
        if order:
            print(f'Sorting set to "{order}".')
        await jitter()

        await expand_all(page, interceptor, done)
        await interceptor.drain()
    finally:
        interceptor.detach(page)
    return interceptor.results()
