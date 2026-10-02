"""Write captured comments and shared posts to JSON, CSV, or SQLite (chosen by file extension)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Sequence

from scraper.interceptor import COMMENT_FIELDS, SHARE_FIELDS, Comment, SharedPost

SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
INTEGER_FIELDS = {"depth", "created_time", "reaction_count", "reply_count", "comment_count"}


def _column_defs(names: Sequence[str]) -> str:
    return ", ".join(f"{n} INTEGER" if n in INTEGER_FIELDS else f"{n} TEXT" for n in names)


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
    columns = _column_defs(COMMENT_FIELDS)
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


def export_shares(shares: Sequence[SharedPost], path: Path, source_url: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _write_shares_csv(shares, path)
    elif suffix in SQLITE_SUFFIXES:
        _write_shares_sqlite(shares, path, source_url)
    else:
        _write_shares_json(shares, path, source_url)
    return path


def _write_shares_json(shares: Sequence[SharedPost], path: Path, source_url: str | None) -> None:
    payload = {
        "source_url": source_url,
        "share_count": len(shares),
        "comment_count": sum(len(s.comments) for s in shares),
        "shares": [s.to_dict() for s in shares],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_shares_csv(shares: Sequence[SharedPost], path: Path) -> None:
    # One row per comment, repeating its shared post's columns. A share without
    # comments still gets one row (with empty comment_* columns).
    share_cols = [f"share_{n}" for n in SHARE_FIELDS]
    comment_cols = [f"comment_{n}" for n in COMMENT_FIELDS]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(share_cols + comment_cols)
        for share in shares:
            share_row = [getattr(share, n) for n in SHARE_FIELDS]
            if not share.comments:
                writer.writerow(share_row + [None] * len(comment_cols))
            for comment in share.comments:
                writer.writerow(share_row + [getattr(comment, n) for n in COMMENT_FIELDS])


def _write_shares_sqlite(shares: Sequence[SharedPost], path: Path, source_url: str | None) -> None:
    share_names = SHARE_FIELDS + ["source_url"]
    comment_names = ["share_id"] + COMMENT_FIELDS
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS shares ({_column_defs(share_names)}, PRIMARY KEY (id))"
            )
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS share_comments ({_column_defs(comment_names)}, "
                "PRIMARY KEY (share_id, id))"
            )
            conn.executemany(
                f"INSERT OR REPLACE INTO shares ({', '.join(share_names)}) "
                f"VALUES ({', '.join('?' for _ in share_names)})",
                [tuple(getattr(s, n) for n in SHARE_FIELDS) + (source_url,) for s in shares],
            )
            conn.executemany(
                f"INSERT OR REPLACE INTO share_comments ({', '.join(comment_names)}) "
                f"VALUES ({', '.join('?' for _ in comment_names)})",
                [
                    (s.id,) + tuple(getattr(c, n) for n in COMMENT_FIELDS)
                    for s in shares
                    for c in s.comments
                ],
            )
    finally:
        conn.close()
