"""CLI entry point.

    python main.py --login
    python main.py --url "<POST_URL>" --output ./output.csv --max-comments 500
    python main.py --url "<POST_URL>" --shares --output ./shares.csv
    python main.py --url "<POST_URL>" --screenshot ./positive.csv
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from scraper.actions import jitter, open_shares_dialog, scroll_shares_dialog
from scraper.browser import interactive_login, is_logged_in, open_context
from scraper.collect import collect_comments, open_post
from scraper.interceptor import ShareInterceptor, fill_share_from_page
from scraper.screenshots import all_rendered, capture, read_targets, write_index
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
        "--screenshot",
        type=Path,
        metavar="CSV",
        help="screenshot the comments listed in CSV (needs an `id` column, e.g. a filtered "
        "scrape CSV) instead of scraping",
    )
    parser.add_argument(
        "--screenshot-dir",
        type=Path,
        default=None,
        help="with --screenshot: where to save images (default: screenshots/<CSV name>/)",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=DEFAULT_SESSION_DIR,
        help="persistent browser profile directory (default: sessions/)",
    )
    args = parser.parse_args(argv)
    if args.screenshot and (args.shares or not args.url):
        parser.error("--screenshot requires --url and can't be combined with --shares")
    if args.max_comments is not None and args.max_comments < 1:
        parser.error("--max-comments must be a positive number")
    if args.max_shares is not None and args.max_shares < 1:
        parser.error("--max-shares must be a positive number")
    if args.shares and not args.url:
        parser.error("--shares requires --url")
    return args


async def screenshot_comments(url: str, ids_csv: Path, out_dir: Path, session_dir: Path) -> int:
    """Open the post, load until every listed comment is on the page, and screenshot each one."""
    targets = read_targets(ids_csv)
    if not targets:
        print(f"No comments listed in {ids_csv}.")
        return 1
    print(f"{len(targets)} comments to screenshot.")

    async with open_context(session_dir) as context:
        if not await is_logged_in(context):
            print("No saved Facebook session. Run `python main.py --login` first.")
            return 1

        page = context.pages[0] if context.pages else await context.new_page()
        await collect_comments(page, None, url, done=lambda: all_rendered(page, targets))
        await capture(page, targets, out_dir)

    index = write_index(targets, out_dir)
    captured = sum(t.status == "captured" for t in targets)
    print(f"Saved {captured} of {len(targets)} screenshots to {out_dir} (list: {index})")
    for t in targets:
        if t.status != "captured":
            print(f"  Not found on the page: {t.author_name}: {(t.text or '')[:60]}")
    return 0 if captured == len(targets) else 2


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
        if args.screenshot:
            out_dir = args.screenshot_dir or Path("screenshots") / args.screenshot.stem
            return asyncio.run(
                screenshot_comments(args.url, args.screenshot, out_dir, args.session_dir)
            )
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
