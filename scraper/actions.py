"""Page interactions that make Facebook fetch more comments over GraphQL.

Selectors use ARIA roles and visible text only; data is captured by the
interceptor, not read from the DOM.
"""

from __future__ import annotations

import asyncio
import random
import re

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scraper.interceptor import CommentInterceptor

SORT_BUTTON = re.compile(r"^(Most relevant|Newest|All comments|Top comments)$", re.I)
ALL_COMMENTS_ITEM = re.compile(r"^All comments", re.I)

# Parent-level pagination.
MORE_COMMENTS = re.compile(r"^View (more|previous) comments", re.I)
# Nested thread pagination: "3 replies", "View all 12 replies", "View 2 more replies",
# "View previous replies", "Jane replied · 4 replies". Never the bare "Reply" action.
MORE_REPLIES = re.compile(
    r"(\b\d+\s+repl(y|ies)\b|^View (all|previous|\d+ more|more) repl)", re.I
)

MAX_IDLE_ROUNDS = 4


async def jitter(low: float = 1.2, high: float = 3.8) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def _click(locator: Locator) -> bool:
    try:
        await locator.scroll_into_view_if_needed(timeout=3000)
        await jitter(0.4, 1.0)
        await locator.click(timeout=3000)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception:
        # Element detached or re-rendered between lookup and click.
        return False


async def switch_to_all_comments(page: Page) -> bool:
    """Change the comment filter from the default "Most relevant" to "All comments"."""
    sort_button = page.get_by_role("button", name=SORT_BUTTON).first
    try:
        await sort_button.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        print("Comment sort dropdown not found; continuing with the default order.")
        return False

    label = (await sort_button.inner_text()).strip()
    if ALL_COMMENTS_ITEM.match(label):
        return True
    if not await _click(sort_button):
        return False

    item = page.get_by_role("menuitem", name=ALL_COMMENTS_ITEM).first
    try:
        await item.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        await page.keyboard.press("Escape")
        print('"All comments" option not found; continuing with the default order.')
        return False
    await jitter()
    return await _click(item)


async def _click_expanders(page: Page, pattern: re.Pattern, interceptor: CommentInterceptor) -> int:
    clicked = 0
    for locator in await page.get_by_role("button", name=pattern).all():
        if interceptor.limit_reached:
            break
        if not await locator.is_visible():
            continue
        if await _click(locator):
            clicked += 1
            await jitter()
    return clicked


async def _scroll(page: Page) -> None:
    await page.mouse.wheel(0, random.randint(600, 1400))
    await jitter()


async def expand_all(page: Page, interceptor: CommentInterceptor) -> None:
    """Keep paginating comments and reply threads until nothing new loads."""
    idle_rounds = 0
    while not interceptor.limit_reached and idle_rounds < MAX_IDLE_ROUNDS:
        before = interceptor.count
        clicked = await _click_expanders(page, MORE_COMMENTS, interceptor)
        clicked += await _click_expanders(page, MORE_REPLIES, interceptor)
        await _scroll(page)
        await interceptor.drain()

        gained = interceptor.count - before
        print(f"  {interceptor.count} comments captured (+{gained}, {clicked} expanders clicked)")
        # Count clicks that load nothing as idle too, so a dead button can't loop forever.
        idle_rounds = idle_rounds + 1 if gained == 0 else 0
