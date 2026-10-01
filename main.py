"""CLI entry point.

    python main.py --login
    python main.py --url "<POST_URL>" --output ./output.csv --max-comments 500
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from scraper.actions import expand_all, jitter, switch_to_all_comments
from scraper.browser import interactive_login, is_logged_in, open_context
from scraper.interceptor import CommentInterceptor
from storage.exporter import export

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
        "--session-dir",
        type=Path,
        default=DEFAULT_SESSION_DIR,
        help="persistent browser profile directory (default: sessions/)",
    )
    args = parser.parse_args(argv)
    if args.max_comments is not None and args.max_comments < 1:
        parser.error("--max-comments must be a positive number")
    return args


async def scrape(url: str, output: Path, max_comments: int | None, session_dir: Path) -> int:
    interceptor = CommentInterceptor(max_comments=max_comments)
    async with open_context(session_dir) as context:
        if not await is_logged_in(context):
            print("No saved Facebook session. Run `python main.py --login` first.")
            return 1

        page = context.pages[0] if context.pages else await context.new_page()
        interceptor.attach(page)

        print(f"Opening {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await jitter()
        await interceptor.ingest_page_scripts(page)

        if await switch_to_all_comments(page):
            print('Sorting set to "All comments".')
        await jitter()

        await expand_all(page, interceptor)
        await interceptor.drain()

    comments = interceptor.results()
    export(comments, output, source_url=url)
    print(f"Saved {len(comments)} comments to {output}")
    return 0


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.login:
            return 0 if asyncio.run(interactive_login(args.session_dir)) else 1
        return asyncio.run(scrape(args.url, args.output, args.max_comments, args.session_dir))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(run())
