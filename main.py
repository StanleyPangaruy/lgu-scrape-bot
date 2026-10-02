"""CLI entry point.

    python main.py --login
    python main.py --url "<POST_URL>" --output ./output.csv --max-comments 500
    python main.py --url "<POST_URL>" --shares --output ./shares.csv
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import Page

from scraper.actions import (
    expand_all,
    jitter,
    open_shares_dialog,
    scroll_shares_dialog,
    switch_to_all_comments,
)
from scraper.browser import interactive_login, is_logged_in, open_context
from scraper.interceptor import (
    Comment,
    CommentInterceptor,
    ShareInterceptor,
    fill_share_from_page,
)
from storage.exporter import export, export_shares

DEFAULT_SESSION_DIR = Path("sessions")
DEFAULT_OUTPUT = Path("output.csv")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract comments from a Facebook post.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--login", action="store_true", help="open a browser to log in and save the session"
    )
    mode.add_argument("--url", help="Facebook post URL to extract comments from")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="output file; format from extension: .csv, .json, .db/.sqlite (default: output.csv)",
    )
    parser.add_argument(
        "--max-comments", type=int, default=None, help="stop after this many comments"
    )
    parser.add_argument(
        "--shares",
        action="store_true",
        help="scrape the post's shares (sharer, caption) and the comments under each share",
    )
    parser.add_argument(
        "--max-shares", type=int, default=None, help="with --shares: stop after this many shares"
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=DEFAULT_SESSION_DIR,
        help="persistent browser profile directory (default: sessions/)",
    )
    args = parser.parse_args(argv)
    if args.max_comments is not None and args.max_comments < 1:
        parser.error("--max-comments must be a positive number")
    if args.max_shares is not None and args.max_shares < 1:
        parser.error("--max-shares must be a positive number")
    if args.shares and not args.url:
        parser.error("--shares requires --url")
    return args


async def open_post(page: Page, url: str) -> None:
    print(f"Opening {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await jitter()


async def collect_comments(
    page: Page, max_comments: int | None, url: str | None = None
) -> list[Comment]:
    """Return all comments and replies of the post at `url`, or of the post
    already open in `page` when `url` is None."""
    interceptor = CommentInterceptor(max_comments=max_comments)
    interceptor.attach(page)
    try:
        if url is not None:
            await open_post(page, url)
        await interceptor.ingest_page_scripts(page)

        order = await switch_to_all_comments(page)
        if order:
            print(f'Sorting set to "{order}".')
        await jitter()

        await expand_all(page, interceptor)
        await interceptor.drain()
    finally:
        interceptor.detach(page)
    return interceptor.results()


async def scrape(url: str, output: Path, max_comments: int | None, session_dir: Path) -> int:
    async with open_context(session_dir) as context:
        if not await is_logged_in(context):
            print("No saved Facebook session. Run `python main.py --login` first.")
            return 1

        page = context.pages[0] if context.pages else await context.new_page()
        comments = await collect_comments(page, max_comments, url)

    export(comments, output, source_url=url)
    print(f"Saved {len(comments)} comments to {output}")
    return 0


async def scrape_shares(
    url: str,
    output: Path,
    max_shares: int | None,
    max_comments: int | None,
    session_dir: Path,
) -> int:
    """Collect the posts that re-shared `url`, then the comments under each share."""
    interceptor = ShareInterceptor(max_shares=max_shares)
    async with open_context(session_dir) as context:
        if not await is_logged_in(context):
            print("No saved Facebook session. Run `python main.py --login` first.")
            return 1

        page = context.pages[0] if context.pages else await context.new_page()
        await open_post(page, url)

        # Listen only while the dialog is open: every story loaded then is a share.
        interceptor.attach(page)
        try:
            dialog = await open_shares_dialog(page)
            if dialog is not None:
                await scroll_shares_dialog(page, dialog, interceptor)
            await interceptor.drain()
        finally:
            interceptor.detach(page)
        await page.keyboard.press("Escape")

        found = interceptor.results()
        print(f"Found {len(found)} shared posts.")
        shares = []
        for n, share in enumerate(found, 1):
            await jitter()
            await open_post(page, share.url)
            await fill_share_from_page(page, share)
            who = f"[{n}/{len(found)}] {share.author_name or share.id}"
            if share.comment_count == 0:
                if not share.text:
                    print(f"{who}: no caption and no comments; skipping.")
                    continue
                print(f"{who}: no comments.")
            else:
                print(who)
                share.comments = await collect_comments(page, max_comments)
                print(f"  {len(share.comments)} comments on this share.")
            # Keep a share if it says something: a caption, or comments under it.
            if not share.text and not share.comments:
                print("  No caption and no comments; skipping.")
                continue
            shares.append(share)

    print(f"Skipped {len(found) - len(shares)} shares with no caption and no comments.")
    export_shares(shares, output, source_url=url)
    total = sum(len(s.comments) for s in shares)
    print(f"Saved {len(shares)} shared posts and {total} comments to {output}")
    return 0


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.login:
            return 0 if asyncio.run(interactive_login(args.session_dir)) else 1
        if args.shares:
            return asyncio.run(
                scrape_shares(
                    args.url, args.output, args.max_shares, args.max_comments, args.session_dir
                )
            )
        return asyncio.run(scrape(args.url, args.output, args.max_comments, args.session_dir))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(run())
