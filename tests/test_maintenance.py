"""Phase 3: deleting old comments, deleting a post, backups and restore."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from server import maintenance
from server.cli import main as cli
from server.db import now, transaction
from server.ingest import create_post, save_scrape
from tests.conftest import comment_id

POST = "1557299289776837"
DAY = 86400


def add_post(conn, comments, post_id=POST, link="https://www.facebook.com/share/p/m1/", created=None):
    """comments: (n, age in days, parent n or None)."""
    pid = create_post(conn, link, None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": post_id, "created_time": created}, [
            {"id": comment_id(post_id, n), "author_name": f"P{n}", "text": f"comment {n}",
             "created_time": now() - age * DAY,
             "parent_id": comment_id(post_id, parent) if parent else None}
            for n, age, parent in comments
        ])
    return pid


def ids(conn):
    return {r[0] for r in conn.execute("SELECT id FROM comments")}


def test_old_comments_and_their_replies_are_deleted(conn, settings):
    old_post = now() - 400 * DAY
    pid = add_post(conn, [(1, 400, None), (2, 30, 1), (3, 10, None), (4, 380, None)], created=old_post)
    with transaction(conn):
        conn.execute("INSERT INTO comment_corrections (comment_id, label, user_id, corrected_at) VALUES (?, 'against', NULL, ?)",
                     (comment_id(POST, 1), now()))
        conn.execute("INSERT INTO comment_sentiment (comment_id, label, reason, model, prompt_version, created_at) "
                     "VALUES (?, 'neutral', 'x', 'm', 3, ?)", (comment_id(POST, 4), now()))

    preview = maintenance.retention_preview(conn, settings)
    assert (preview["comments"], preview["posts"]) == (3, 0)
    assert ids(conn) == {comment_id(POST, n) for n in (1, 2, 3, 4)}  # preview deletes nothing

    assert maintenance.apply_retention(conn, settings) == (3, 0)
    # The reply (2) is newer than a year but goes with the comment it answers.
    assert ids(conn) == {comment_id(POST, 3)}
    assert conn.execute("SELECT COUNT(*) FROM comment_corrections").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM comment_sentiment").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM posts WHERE id = ?", (pid,)).fetchone()
    assert "deleted old data" in conn.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]

    # Once its last comment is old, the post goes too.
    conn.execute("UPDATE comments SET created_time = ?", (now() - 366 * DAY,))
    assert maintenance.apply_retention(conn, settings) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


def test_retention_keeps_new_posts_and_can_be_off(conn, settings):
    fresh = create_post(conn, "https://www.facebook.com/share/p/new/", None)  # not collected yet
    add_post(conn, [(1, 500, None)], created=now() - 500 * DAY)
    off = replace(settings, retention_days=0)
    assert maintenance.apply_retention(conn, off) == (0, 0)
    assert maintenance.apply_retention(conn, settings) == (1, 1)
    assert conn.execute("SELECT id FROM posts").fetchall()[0][0] == fresh


def test_admin_deletes_a_post(browsers, conn):
    pid = add_post(conn, [(1, 5, None), (2, 3, 1)])
    page = f"/posts/{pid}"
    assert browsers["staff"].post(f"{page}/delete").status_code == 403
    assert "Delete post" not in browsers["staff"].get(page).text
    assert "send its Facebook link to the admin" in browsers["staff"].get("/posts").text

    admin = browsers["admin"]
    assert "Delete post" in admin.get(page).text
    conn.execute("INSERT INTO jobs (post_id, status, created_at) VALUES (?, 'queued', ?)", (pid, now()))
    busy = admin.post(f"{page}/delete")
    assert busy.headers["location"].endswith("notice=post-busy") and ids(conn)
    conn.execute("DELETE FROM jobs")

    done = admin.post(f"{page}/delete")
    assert done.status_code == 303 and done.headers["location"] == "/posts?notice=post-deleted"
    assert not ids(conn) and admin.get(page).status_code == 404
    assert admin.post("/posts/99999/delete").status_code == 404


def test_backups_keep_the_newest(conn, settings):
    small = replace(settings, backup_keep=2)
    add_post(conn, [(1, 1, None)])
    made = [maintenance.make_backup(small, label=f"t{i}") for i in range(3)]
    kept = maintenance.list_backups(small)
    assert len(kept) == 2 and made[0] not in [b.path for b in kept]
    copy = sqlite3.connect(kept[0].path)
    assert copy.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
    copy.close()
    leftovers = [f.name for f in settings.backup_dir.iterdir() if not f.name.endswith(".db")]
    assert leftovers == []  # no .partial, -wal or -shm files beside the backups


def test_backup_now_button_and_settings_page(browsers, conn, settings):
    assert browsers["staff"].post("/settings/backup").status_code == 403
    response = browsers["admin"].post("/settings/backup")
    assert response.status_code == 303 and len(maintenance.list_backups(settings)) == 1
    page = browsers["admin"].get("/settings").text
    assert "Data and backups" in page and "365 days" in page and "No backups yet" not in page


def test_restore_replaces_the_database(conn, settings, tmp_path, monkeypatch):
    add_post(conn, [(1, 1, None)])
    backup = maintenance.make_backup(settings)
    with transaction(conn):
        conn.execute("DELETE FROM posts")
    conn.close()

    monkeypatch.setenv("LCM_DATA_DIR", str(settings.data_dir))
    assert cli(["restore", str(backup)]) == 1  # asks for --yes first
    bogus = tmp_path / "notes.db"
    bogus.write_text("not a database")
    assert cli(["restore", str(bogus), "--yes"]) == 1
    assert cli(["restore", str(backup), "--yes"]) == 0

    restored = sqlite3.connect(settings.db_path)
    assert restored.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
    restored.close()
    # The emptied database was saved before it was replaced.
    assert any("before-restore" in b.path.name for b in maintenance.list_backups(settings))


def test_cleanup_command(conn, settings, monkeypatch, capsys):
    add_post(conn, [(1, 400, None), (2, 3, None)])
    monkeypatch.setenv("LCM_DATA_DIR", str(settings.data_dir))
    assert cli(["cleanup"]) == 0 and len(ids(conn)) == 2
    assert "Run again with --yes" in capsys.readouterr().out
    assert cli(["cleanup", "--yes"]) == 0
    assert ids(conn) == {comment_id(POST, 2)}


def test_automatic_backups_are_monthly(conn, settings):
    import os

    add_post(conn, [(1, 1, None)])
    worker = maintenance.MaintenanceWorker(settings)
    worker.run_once(conn)  # no backup yet: make the first one
    # Named as if made long ago, so a new backup this second cannot reuse its name.
    first = maintenance.list_backups(settings)[0].path.rename(settings.backup_dir / "monitor-2000-01-01-000000.db")

    def age_first_backup(days):
        stamp = now() - days * DAY
        os.utime(first, (stamp, stamp))

    for days, expected in ((1, 1), (29, 1), (30, 2)):
        age_first_backup(days)
        worker.run_once(conn)
        assert len(maintenance.list_backups(settings)) == expected, days
