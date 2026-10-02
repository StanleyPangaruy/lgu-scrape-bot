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

from scraper.interceptor import CommentInterceptor, ShareInterceptor

SORT_BUTTON = re.compile(r"^(Most relevant|Newest|All comments|Top comments)$", re.I)
ALL_COMMENTS_ITEM = re.compile(r"^All comments", re.I)
NEWEST_ITEM = re.compile(r"^Newest", re.I)

# Parent-level pagination: "View more comments", "View 12 more comments", "See previous comments".
MORE_COMMENTS = re.compile(r"^(View|See) (\d+ )?(more|previous) comments", re.I)
# Nested thread pagination: "3 replies", "View all 12 replies", "View 2 more replies",
# "View previous replies", "Jane replied · 4 replies". Never the bare "Reply" action.
MORE_REPLIES = re.compile(
    r"(\b\d+\s+repl(y|ies)\b|^View (all|previous|\d+ more|more) repl)", re.I
)

# The post's "..." menu and its "Share history" item (lists everyone who shared it).
POST_MENU_BUTTON = re.compile(r"^(Actions for this post|More options for this post)", re.I)
SHARE_HISTORY_ITEM = re.compile(r"^Share history", re.I)
# Fallback: the post's share counter, "12 shares", "1 share", "1.2K shares".
SHARES_BUTTON = re.compile(r"^\d[\d.,]*\s*[KM]?\s+shares?$", re.I)

MAX_IDLE_ROUNDS = 4
# How long to wait for a clicked expander's comments to arrive over the network.
LOAD_TIMEOUT = 10.0


async def jitter(low: float = 1.2, high: float = 3.8) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def _click(locator: Locator) -> bool:
    try:
        # Center it: scroll_into_view_if_needed can leave it at the bottom edge,
        # under the sticky comment composer, which then swallows the click.
        await locator.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=3000)
        await jitter(0.4, 1.0)
        await locator.click(timeout=3000)
        return True
    except PlaywrightTimeoutError:
        pass
    except Exception:
        # Element detached or re-rendered between lookup and click.
        return False
    # Still covered by an overlay: fire the click on the element itself.
    try:
        await locator.dispatch_event("click", timeout=3000)
        return True
    except Exception:
        return False


async def switch_to_all_comments(page: Page) -> str | None:
    """Change the comment filter from the default "Most relevant" to "All comments".

    Some posts don't offer "All comments"; then fall back to "Newest", which also
    lists every comment instead of a relevance-filtered subset. Returns the order
    selected, or None if it stayed on the default.
    """
    sort_button = page.get_by_role("button", name=SORT_BUTTON).first
    try:
        await sort_button.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        print("Comment sort dropdown not found; continuing with the default order.")
        return None

    label = (await sort_button.inner_text()).strip()
    if ALL_COMMENTS_ITEM.match(label):
        return "All comments"
    if not await _click(sort_button):
        return None

    try:
        await page.get_by_role("menuitem").first.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        print("Comment sort menu did not open; continuing with the default order.")
        return None

    for name, pattern in (("All comments", ALL_COMMENTS_ITEM), ("Newest", NEWEST_ITEM)):
        item = page.get_by_role("menuitem", name=pattern).first
        if not await item.is_visible():
            continue
        if name == "Newest" and NEWEST_ITEM.match(label):
            # Already on Newest and All comments isn't offered: nothing to change.
            await page.keyboard.press("Escape")
            return name
        await jitter()
        return name if await _click(item) else None

    await page.keyboard.press("Escape")
    print('Neither "All comments" nor "Newest" is offered; continuing with the default order.')
    return None


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


async def _wait_for_new_comments(interceptor: CommentInterceptor, before: int) -> None:
    """Return once new comments are parsed, or after LOAD_TIMEOUT seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOAD_TIMEOUT
    while loop.time() < deadline:
        await interceptor.drain()
        if interceptor.count > before:
            return
        await asyncio.sleep(0.5)


async def _scroll(page: Page) -> None:
    await page.mouse.wheel(0, random.randint(600, 1400))
    await jitter()


async def expand_all(page: Page, interceptor: CommentInterceptor) -> None:
    """Keep paginating comments and reply threads until nothing new loads."""
    idle_rounds = 0
    while not interceptor.limit_reached and idle_rounds < MAX_IDLE_ROUNDS:
        before = interceptor.count
        more = await _click_expanders(page, MORE_COMMENTS, interceptor)
        replies = await _click_expanders(page, MORE_REPLIES, interceptor)
        if more or replies:
            await _wait_for_new_comments(interceptor, before)
        await _scroll(page)
        await interceptor.drain()

        gained = interceptor.count - before
        print(
            f"  {interceptor.count} comments captured (+{gained}; clicked {more} "
            f'"more comments", {replies} reply expanders)'
        )
        # Count clicks that load nothing as idle too, so a dead button can't loop forever.
        idle_rounds = idle_rounds + 1 if gained == 0 else 0

    if not interceptor.limit_reached:
        leftover = page.get_by_role("button", name=MORE_COMMENTS)
        if any([await b.is_visible() for b in await leftover.all()]):
            print('Warning: stopped with a "View more comments" button still on the page.')


async def _wait_for_dialog(page: Page) -> Locator | None:
    dialog = page.get_by_role("dialog").last
    try:
        await dialog.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        return None
    return dialog


async def _open_share_history(page: Page) -> Locator | None:
    """Post "..." menu -> "Share history"."""
    menu_button = page.get_by_role("button", name=POST_MENU_BUTTON).first
    try:
        await menu_button.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        print('Post "..." menu not found.')
        return None
    if not await _click(menu_button):
        return None

    item = page.get_by_role("menuitem", name=SHARE_HISTORY_ITEM).first
    try:
        await item.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        await page.keyboard.press("Escape")
        print('"Share history" not in the post menu.')
        return None
    await jitter(0.4, 1.0)
    if not await _click(item):
        return None

    dialog = await _wait_for_dialog(page)
    if dialog is None:
        # Some layouts open share history as its own page instead of a dialog.
        await page.wait_for_load_state("domcontentloaded")
        return page.locator("body")
    return dialog


async def _open_shares_counter(page: Page) -> Locator | None:
    button = page.get_by_role("button", name=SHARES_BUTTON).first
    try:
        await button.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        return None
    if not await _click(button):
        return None
    return await _wait_for_dialog(page)


async def open_shares_dialog(page: Page) -> Locator | None:
    """Open the list of shares: "Share history" from the post menu, else the "N shares" counter.

    Returns the element to scroll for more shares, or None if neither opened.
    """
    container = await _open_share_history(page)
    if container is None:
        print('Trying the "N shares" counter instead.')
        container = await _open_shares_counter(page)
    if container is None:
        print("Could not open the post's share list; it may have no shares.")
    return container


async def scroll_shares_dialog(page: Page, dialog: Locator, interceptor: ShareInterceptor) -> None:
    """Scroll the share list until no new shared posts load."""
    idle_rounds = 0
    while not interceptor.limit_reached and idle_rounds < MAX_IDLE_ROUNDS:
        before = interceptor.count
        # The wheel scrolls whatever is under the cursor, so keep it over the dialog.
        box = await dialog.bounding_box()
        if box:
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await _scroll(page)
        await interceptor.drain()

        gained = interceptor.count - before
        print(f"  {interceptor.count} shared posts captured (+{gained})")
        idle_rounds = idle_rounds + 1 if gained == 0 else 0
