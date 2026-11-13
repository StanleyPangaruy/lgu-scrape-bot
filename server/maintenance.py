"""Keeping the data tidy: delete comments after the retention period, and back up the database.

A background thread runs both inside the website: retention every hour (it is a
quick query), a backup when the newest one is a month (30 days) old. The same functions back
the admin's "Back up now" button and `python -m server backup / restore`.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from server.config import Settings
from server.db import audit, connect, now, transaction

log = logging.getLogger("lcm.maintenance")

BACKUP_GLOB = "monitor-*.db"
BACKUP_EVERY = 30 * 24 * 3600  # monthly
TICK_SECONDS = 3600
FIRST_TICK_SECONDS = 60


# --- Retention -------------------------------------------------------------------------


def cutoff(settings: Settings) -> int | None:
    """Comments posted before this time are deleted; None when retention is off."""
    return now() - settings.retention_days * 86400 if settings.retention_days > 0 else None


def _mark_doomed(conn: sqlite3.Connection, before: int) -> None:
    """Fill temp table `doomed` with comments past retention, plus every reply under
    them (a reply is not kept without the comment it answers)."""
    conn.execute("DROP TABLE IF EXISTS temp.doomed")
    conn.execute("CREATE TEMP TABLE doomed (id TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT INTO doomed SELECT id FROM comments WHERE COALESCE(created_time, first_seen_at) < ?", (before,)
    )
    while conn.execute(
        """
        INSERT OR IGNORE INTO doomed
        SELECT c.id FROM comments c
        WHERE c.parent_id IN (SELECT id FROM doomed) AND c.id NOT IN (SELECT id FROM doomed)
        """
    ).rowcount:
        pass


_EMPTY_OLD_POSTS = """
    SELECT p.id FROM posts p
    WHERE COALESCE(p.created_time, p.added_at) < ?
      AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id {extra})
      AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.post_id = p.id AND j.status IN ('queued', 'running'))
"""


def retention_preview(conn: sqlite3.Connection, settings: Settings) -> dict[str, int | None]:
    """What a cleanup would delete right now, and the oldest comment kept."""
    oldest = conn.execute("SELECT MIN(COALESCE(created_time, first_seen_at)) FROM comments").fetchone()[0]
    before = cutoff(settings)
    if before is None:
        return {"comments": 0, "posts": 0, "oldest": oldest, "cutoff": None}
    with transaction(conn):
        _mark_doomed(conn, before)
        comments = conn.execute("SELECT COUNT(*) FROM doomed").fetchone()[0]
        posts = conn.execute(
            "SELECT COUNT(*) FROM (" + _EMPTY_OLD_POSTS.format(extra="AND c.id NOT IN (SELECT id FROM doomed)") + ")",
            (before,),
        ).fetchone()[0]
        conn.execute("DROP TABLE temp.doomed")
    return {"comments": comments, "posts": posts, "oldest": oldest, "cutoff": before}


def apply_retention(conn: sqlite3.Connection, settings: Settings, who: str = "automatic cleanup") -> tuple[int, int]:
    """Delete comments past retention (labels, corrections and keyword matches go with
    them), then posts left with no comments. Returns (comments, posts) deleted."""
    before = cutoff(settings)
    if before is None:
        return 0, 0
    with transaction(conn):
        _mark_doomed(conn, before)
        comments = conn.execute("DELETE FROM comments WHERE id IN (SELECT id FROM doomed)").rowcount
        conn.execute("DROP TABLE temp.doomed")
        posts = conn.execute(
            "DELETE FROM posts WHERE id IN (" + _EMPTY_OLD_POSTS.format(extra="") + ")", (before,)
        ).rowcount
        if comments or posts:
            audit(conn, who, "deleted old data",
                  f"{comments} comments and {posts} posts older than {settings.retention_days} days")
    return comments, posts


def delete_post(conn: sqlite3.Connection, post_id: int) -> int:
    """Delete one post and everything stored with it; returns the number of comments removed.
    Call inside a transaction."""
    comments = conn.execute("SELECT COUNT(*) FROM comments WHERE post_id = ?", (post_id,)).fetchone()[0]
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    return comments


# --- Backups ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Backup:
    path: Path
    size: int
    made_at: int


def list_backups(settings: Settings) -> list[Backup]:
    folder = settings.backup_dir
    if not folder.is_dir():
        return []
    found = []
    for p in folder.glob(BACKUP_GLOB):
        st = p.stat()
        found.append(Backup(p, st.st_size, int(st.st_mtime)))
    return sorted(found, key=lambda b: (b.made_at, b.path.name), reverse=True)


def _check(path: Path) -> None:
    """Raise ValueError unless path is a sound LGU Comment Monitor database."""
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError(f"{path.name} is damaged")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'comments'").fetchone():
                raise ValueError(f"{path.name} is not an LGU Comment Monitor database")
        finally:
            db.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"{path.name} is not a database: {e}") from e


def make_backup(settings: Settings, label: str = "") -> Path:
    """Copy the live database (safe while the website runs), check it, and keep only
    the newest `backup_keep` backups."""
    folder = settings.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(settings.tz).strftime("%Y-%m-%d-%H%M%S")
    final = folder / f"monitor-{stamp}{'-' + label if label else ''}.db"
    partial = final.with_suffix(".partial")
    src = sqlite3.connect(settings.db_path, timeout=30)
    dst = sqlite3.connect(partial)
    try:
        src.backup(dst)
        # The copy is a single self-contained file (no -wal/-shm beside it).
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        dst.close()
        src.close()
    try:
        _check(partial)
    except ValueError:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(final)
    for leftover in folder.glob("*.partial*"):  # from older versions or an interrupted backup
        leftover.unlink(missing_ok=True)
    for old in list_backups(settings)[settings.backup_keep:]:
        old.path.unlink(missing_ok=True)
    return final


def restore_backup(settings: Settings, source: Path) -> Path:
    """Replace the database with a backup. The website must be stopped first.
    The current database is backed up before it is replaced; returns that copy."""
    _check(source)
    safety = make_backup(settings, label="before-restore") if settings.db_path.exists() else None
    for suffix in ("-wal", "-shm"):
        Path(str(settings.db_path) + suffix).unlink(missing_ok=True)
    shutil.copyfile(source, settings.db_path)
    return safety


# --- Background thread ---------------------------------------------------------------------


@dataclass
class MaintenanceStatus:
    last_cleanup_at: int | None = None
    last_cleanup: str = ""
    last_error: str = ""
    last_error_at: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def error(self, text: str) -> None:
        with self.lock:
            self.last_error, self.last_error_at = text[:300], now()


class MaintenanceWorker(threading.Thread):
    def __init__(self, settings: Settings) -> None:
        super().__init__(name="maintenance", daemon=True)
        self.settings = settings
        self.stop_event = threading.Event()
        self.status = MaintenanceStatus()

    def stop(self) -> None:
        self.stop_event.set()

    def run_once(self, conn: sqlite3.Connection) -> None:
        backups = list_backups(self.settings)
        if not backups or now() - backups[0].made_at >= BACKUP_EVERY:
            make_backup(self.settings)
        comments, posts = apply_retention(conn, self.settings)
        with self.status.lock:
            self.status.last_cleanup_at = now()
            self.status.last_cleanup = f"{comments} comments and {posts} posts deleted" if comments or posts else "nothing to delete"

    def run(self) -> None:
        if self.stop_event.wait(FIRST_TICK_SECONDS):
            return
        conn = connect(self.settings.db_path)
        try:
            while not self.stop_event.is_set():
                try:
                    self.run_once(conn)
                except Exception as e:  # keep the thread alive; show the problem in Settings
                    log.exception("maintenance failed")
                    self.status.error(f"{type(e).__name__}: {e}")
                self.stop_event.wait(TICK_SECONDS)
        finally:
            conn.close()
