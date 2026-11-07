"""Element screenshots of selected comments.

Each comment and each reply renders as its own [role=article] (replies are not
nested inside their parent), and its timestamp link carries the comment's IDs:
`?comment_id=<GraphQL id>` first, then `?comment_id=<fbid>[&reply_comment_id=<fbid>]`.
Those IDs are matched against the scraped CSV, so no CSS classes are involved.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page

from scraper.actions import _click, jitter

SEE_MORE = re.compile(r"^See more$", re.I)

# Copies each comment's IDs onto its article as data attributes so it can be
# located later; returns how many articles were tagged.
TAG_ARTICLES_JS = """
() => {
  let tagged = 0;
  for (const article of document.querySelectorAll('[role=article]')) {
    const own = [...article.querySelectorAll('a[href*="comment_id="]')]
      .filter(a => a.closest('[role=article]') === article);
    for (const link of own) {
      const params = new URL(link.href).searchParams;
      const cid = params.get('comment_id');
      if (!cid) continue;
      if (/^\\d+$/.test(cid)) {
        if (!article.dataset.lguFbid) article.dataset.lguFbid = params.get('reply_comment_id') || cid;
      } else if (!article.dataset.lguCid) {
        article.dataset.lguCid = cid;
      }
    }
    if (article.dataset.lguCid || article.dataset.lguFbid) tagged++;
  }
  return tagged;
}
"""


@dataclass
class Target:
    id: str
    legacy_fbid: str | None
    author_name: str | None
    text: str | None
    file: str | None = None
    status: str = "not found"


def read_targets(path: Path) -> list[Target]:
    """Read the comments to capture: any CSV with an `id` column (e.g. a filtered scrape CSV)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and "id" not in rows[0]:
        raise ValueError(f"{path} has no 'id' column")
    return [
        Target(
            id=r["id"],
            legacy_fbid=r.get("legacy_fbid") or None,
            author_name=r.get("author_name") or None,
            text=r.get("text") or None,
        )
        for r in rows
        if r.get("id")
    ]


def _safe_name(value: str | None) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", value or "unknown", flags=re.UNICODE).strip("_")
    return cleaned[:40] or "unknown"


async def tag_articles(page: Page) -> int:
    return await page.evaluate(TAG_ARTICLES_JS)


def _locator(page: Page, target: Target):
    selector = f'[role=article][data-lgu-cid="{target.id}"]'
    if target.legacy_fbid:
        selector += f', [role=article][data-lgu-fbid="{target.legacy_fbid}"]'
    # Posts opened in a dialog keep a hidden copy of the page behind it; skip that copy.
    return page.locator(selector).filter(visible=True).first


async def all_rendered(page: Page, targets: list[Target]) -> bool:
    """True once every target has a visible article on the page."""
    await tag_articles(page)
    for target in targets:
        if await _locator(page, target).count() == 0:
            return False
    return True


async def capture(page: Page, targets: list[Target], out_dir: Path) -> list[Target]:
    """Screenshot each target comment that is rendered on the page."""
    out_dir.mkdir(parents=True, exist_ok=True)
    await tag_articles(page)
    for n, target in enumerate(targets, 1):
        article = _locator(page, target)
        if await article.count() == 0:
            # Rendered after the last tagging pass (e.g. a late-loading reply).
            await tag_articles(page)
            if await article.count() == 0:
                continue

        await article.evaluate("el => el.scrollIntoView({block: 'center'})")
        # Expand truncated text so the whole comment is in the image.
        see_more = article.get_by_role("button", name=SEE_MORE)
        if await see_more.count() and await see_more.first.is_visible():
            await _click(see_more.first)
        # Park the cursor off the comment so no hover menu shows in the shot.
        await page.mouse.move(0, 0)
        await jitter(0.4, 1.0)

        name = f"{n:03d}_{_safe_name(target.author_name)}_{target.legacy_fbid or n}.png"
        await article.screenshot(path=str(out_dir / name))
        target.file = name
        target.status = "captured"
    return targets


def write_index(targets: list[Target], out_dir: Path) -> Path:
    path = out_dir / "index.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["status", "file", "id", "legacy_fbid", "author_name", "text"])
        for t in targets:
            writer.writerow([t.status, t.file, t.id, t.legacy_fbid, t.author_name, t.text])
    return path
