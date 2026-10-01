"""Write captured comments to JSON, CSV, or SQLite (chosen by file extension)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Sequence

from scraper.interceptor import COMMENT_FIELDS, Comment

SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def export(comments: Sequence[Comment], path: Path, source_url: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _write_csv(comments, path)
    elif suffix in SQLITE_SUFFIXES:
        _write_sqlite(comments, path, source_url)
    else:
        _write_json(comments, path, source_url)
    return path


def _write_json(comments: Sequence[Comment], path: Path, source_url: str | None) -> None:
    payload = {
        "source_url": source_url,
        "count": len(comments),
        "comments": [c.to_dict() for c in comments],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(comments: Sequence[Comment], path: Path) -> None:
    # utf-8-sig so Excel opens non-ASCII text (e.g. Filipino names, emoji) correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COMMENT_FIELDS)
        writer.writeheader()
        writer.writerows(c.to_dict() for c in comments)


def _write_sqlite(comments: Sequence[Comment], path: Path, source_url: str | None) -> None:
    columns = ", ".join(
        f"{name} INTEGER" if name in ("depth", "created_time", "reaction_count", "reply_count")
        else f"{name} TEXT"
        for name in COMMENT_FIELDS
    )
    placeholders = ", ".join("?" for _ in COMMENT_FIELDS + ["source_url"])
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS comments ({columns}, source_url TEXT, PRIMARY KEY (id))"
            )
            # Re-running on the same post updates rows instead of duplicating them.
            conn.executemany(
                f"INSERT OR REPLACE INTO comments VALUES ({placeholders})",
                [
                    tuple(getattr(c, name) for name in COMMENT_FIELDS) + (source_url,)
                    for c in comments
                ],
            )
    finally:
        conn.close()
