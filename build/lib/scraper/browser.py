"""Playwright persistent browser context with anti-detection setup.

The browser always runs headed (headless=False). On servers without a display,
run under a virtual display such as `xvfb-run python main.py ...`.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, async_playwright

FACEBOOK_URL = "https://www.facebook.com/"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
]

# Playwright adds --enable-automation by default, which shows the
# "controlled by automated software" bar and sets navigator.webdriver.
IGNORE_DEFAULT_ARGS = ["--enable-automation"]


@asynccontextmanager
async def open_context(user_data_dir: Path) -> AsyncIterator[BrowserContext]:
    """Launch Chromium with a persistent profile so the login survives across runs."""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            args=LAUNCH_ARGS,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        try:
            yield context
        finally:
            await context.close()


async def is_logged_in(context: BrowserContext) -> bool:
    """Facebook sets the `c_user` cookie (the account ID) once a session is authenticated."""
    cookies = await context.cookies(FACEBOOK_URL)
    return any(c["name"] == "c_user" and c.get("value") for c in cookies)


async def interactive_login(user_data_dir: Path) -> bool:
    """Open a headed browser for manual login + 2FA; the session is saved to user_data_dir."""
    async with open_context(user_data_dir) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(FACEBOOK_URL)
        print("Log in to Facebook in the opened browser (complete 2FA if asked).")
        await asyncio.to_thread(input, "Press Enter here once you can see your News Feed... ")
        logged_in = await is_logged_in(context)
    if logged_in:
        print(f"Session saved to {user_data_dir}")
    else:
        print("No Facebook session cookie found; login did not complete.")
    return logged_in
