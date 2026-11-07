"""Accuracy check: the AI's "supportive" labels against comments a person picked by hand.

Each picks file (positive_<code>.csv) lists the comments someone chose as
supportive out of the whole post in comments_<code>.csv. Every comment in the
whole file was reviewed, so one the person left out counts as "not supportive".
Only the AI's own labels are compared; staff corrections are ignored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from server.ingest import read_comment_csv


@dataclass
class Item:
    comment_id: str
    text: str
    label: str | None  # the AI's label; None = not analyzed (or not in the database)
    reason: str = ""


@dataclass
class FileResult:
    name: str
    reviewed: int = 0
    picked: int = 0
    agreed: list[Item] = field(default_factory=list)   # picked, and the AI says supportive
    missed: list[Item] = field(default_factory=list)   # picked, the AI says something else
    extra: list[Item] = field(default_factory=list)    # not picked, the AI says supportive
    not_analyzed: int = 0
    post_ids: set[int] = field(default_factory=set)


def comments_file_for(picks: Path) -> Path:
    """positive_<code>.csv was picked from comments_<code>.csv in the same folder."""
    code = picks.stem.split("_", 1)[1] if "_" in picks.stem else picks.stem
    return picks.with_name(f"comments_{code}.csv")


def compare_file(conn: sqlite3.Connection, picks_path: Path, comments_path: Path) -> FileResult:
    picked = {r["id"] for r in read_comment_csv(picks_path) if r.get("id")}
    reviewed = [r for r in read_comment_csv(comments_path) if r.get("id")]
    result = FileResult(name=picks_path.name, reviewed=len(reviewed), picked=len(picked))
    stored = {}
    ids = [r["id"] for r in reviewed]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
            f"""
            SELECT c.id, c.post_id, s.label, s.reason FROM comments c
            LEFT JOIN comment_sentiment s ON s.comment_id = c.id
            WHERE c.id IN ({','.join('?' * len(chunk))})
            """,
            chunk,
        ):
            stored[row["id"]] = row
    for r in reviewed:
        row = stored.get(r["id"])
        if row is not None:
            result.post_ids.add(row["post_id"])
        label = row["label"] if row is not None else None
        item = Item(r["id"], (r.get("text") or "").strip(), label, (row["reason"] or "") if row is not None else "")
        if label is None:
            result.not_analyzed += 1
            continue
        is_picked = r["id"] in picked
        if is_picked and label == "supportive":
            result.agreed.append(item)
        elif is_picked:
            result.missed.append(item)
        elif label == "supportive":
            result.extra.append(item)
    return result


def percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def report(results: list[FileResult], width: int = 110) -> str:
    def clip(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= width else text[: width - 1] + "…"

    lines = []
    for r in results:
        lines.append(
            f"{r.name}: {r.reviewed} comments, you picked {r.picked}; "
            f"AI agreed on {len(r.agreed)}, missed {len(r.missed)}, added {len(r.extra)}"
            + (f" ({r.not_analyzed} not analyzed yet)" if r.not_analyzed else "")
        )
        for item in r.missed:
            lines.append(f"    MISSED (AI: {item.label}) {clip(item.text)}")
            if item.reason:
                lines.append(f"        why: {clip(item.reason)}")
        for item in r.extra:
            lines.append(f"    ADDED  {clip(item.text)}")
            if item.reason:
                lines.append(f"        why: {clip(item.reason)}")
    agreed = sum(len(r.agreed) for r in results)
    missed = sum(len(r.missed) for r in results)
    extra = sum(len(r.extra) for r in results)
    waiting = sum(r.not_analyzed for r in results)
    lines += [
        "",
        f"Your picks the AI also called supportive: {agreed} of {agreed + missed} ({percent(agreed, agreed + missed)})",
        f"AI 'supportive' labels you had picked:    {agreed} of {agreed + extra} ({percent(agreed, agreed + extra)})",
    ]
    if waiting:
        lines.append(f"{waiting} comments are not analyzed yet; run again with --label (needs ANTHROPIC_API_KEY).")
    return "\n".join(lines)
