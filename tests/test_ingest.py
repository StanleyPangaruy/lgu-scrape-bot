import csv

from server.db import transaction
from server.ingest import (
    InvalidLink,
    clean_post_link,
    create_post,
    import_csv,
    safe_web_url,
    save_scrape,
)
from tests.conftest import comment_id

POST = "1553704276793509"
OTHER = "999"


def comment(n, post=POST, **extra):
    row = {"id": comment_id(post, n), "author_id": f"u{n}", "author_name": f"Person {n}",
           "text": f"comment {n}", "created_time": 1_790_000_000 + n, "reaction_count": n}
    row.update(extra)
    return row


def scrape(conn, post_id, comments, post=None, checked_at=None):
    with transaction(conn):
        return save_scrape(conn, post_id, post or {"post_id": POST}, comments, checked_at=checked_at)


def test_first_scrape_stores_post_details_and_comments(conn):
    pid = create_post(conn, "https://www.facebook.com/share/p/abc", None)
    result = scrape(conn, pid, [comment(1), comment(2)], post={
        "post_id": POST, "url": "https://www.facebook.com/page/posts/pfbid1", "page_name": "Municipality",
        "text": "Paalala", "created_time": 1_789_000_000, "comment_count": 2,
    })
    assert (result.new, result.updated, result.dropped) == (2, 0, 0)
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (pid,)).fetchone()
    assert post["fb_post_id"] == POST
    assert post["page_name"] == "Municipality"
    assert post["url"] == "https://www.facebook.com/page/posts/pfbid1"
    assert post["source_url"] == "https://www.facebook.com/share/p/abc"
    assert post["fb_comment_count"] == 2
    assert post["last_checked_at"] is not None


def test_recheck_adds_new_and_refreshes_counts_without_losing_data(conn):
    pid = create_post(conn, "link", None)
    scrape(conn, pid, [comment(1), comment(2, author_url="https://facebook.com/p2")])
    result = scrape(conn, pid, [
        comment(1, reaction_count=50),
        comment(2, author_url=None, text=None),  # partial node: must not erase what we had
        comment(3),
    ])
    assert (result.new, result.updated) == (1, 2)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM comments")}
    assert rows[comment_id(POST, 1)]["reaction_count"] == 50
    assert rows[comment_id(POST, 2)]["author_url"] == "https://facebook.com/p2"
    assert rows[comment_id(POST, 2)]["text"] == "comment 2"
    assert len(rows) == 3


def test_comments_from_other_posts_on_the_page_are_dropped(conn):
    pid = create_post(conn, "link", None)
    result = scrape(conn, pid, [comment(1), comment(2, post=OTHER)])
    assert (result.new, result.dropped) == (1, 1)


def test_duplicate_ids_in_one_upload_count_once(conn):
    pid = create_post(conn, "link", None)
    result = scrape(conn, pid, [comment(1), comment(1)])
    assert result.new == 1


def test_two_links_to_the_same_post_merge(conn):
    first = create_post(conn, "https://www.facebook.com/share/p/one", None)
    scrape(conn, first, [comment(1)])
    second = create_post(conn, "https://www.facebook.com/share/p/two", None)
    conn.execute("INSERT INTO jobs (post_id, status, created_at) VALUES (?, 'queued', 0)", (second,))
    result = scrape(conn, second, [comment(1), comment(2)])
    assert result.merged_into == first and result.post_id == first
    assert (result.new, result.updated) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    assert conn.execute("SELECT post_id FROM jobs").fetchone()[0] == first


def test_text_change_updates_keyword_hits(conn):
    pid = create_post(conn, "link", None)
    scrape(conn, pid, [comment(1, text="walang problema")])
    assert conn.execute("SELECT COUNT(*) FROM keyword_hits").fetchone()[0] == 0
    scrape(conn, pid, [comment(1, text="hulihin ang kolorum")])
    term = conn.execute(
        "SELECT k.term FROM keyword_hits h JOIN keywords k ON k.id = h.keyword_id"
    ).fetchone()[0]
    assert term == "colorum"


def test_excel_damaged_ids_never_overwrite_good_ones(conn):
    pid = create_post(conn, "link", None)
    scrape(conn, pid, [comment(1, author_id="100064903410975", legacy_fbid="1869535050874606")])
    scrape(conn, pid, [comment(1, author_id="1.00065E+14", legacy_fbid="1.86954E+15"),
                       comment(2, author_id="1.00065E+14")])
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM comments")}
    assert rows[comment_id(POST, 1)]["author_id"] == "100064903410975"
    assert rows[comment_id(POST, 1)]["legacy_fbid"] == "1869535050874606"
    assert rows[comment_id(POST, 2)]["author_id"] is None  # unknown, grouped by name instead


def test_last_checked_never_moves_backwards(conn):
    pid = create_post(conn, "link", None)
    scrape(conn, pid, [comment(1)], checked_at=2_000)
    scrape(conn, pid, [comment(2)], checked_at=1_000)
    assert conn.execute("SELECT last_checked_at FROM posts").fetchone()[0] == 2_000


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_import_csv_detects_post_and_merges_duplicate_files(conn, tmp_path):
    a, b = tmp_path / "comments_one.csv", tmp_path / "comments_two.csv"
    rows = [{k: ("" if v is None else v) for k, v in comment(n).items()} for n in (1, 2)]
    write_csv(a, rows)
    write_csv(b, rows + [{k: ("" if v is None else v) for k, v in comment(3).items()}])
    with transaction(conn):
        first = import_csv(conn, a, "https://www.facebook.com/share/p/one/")
    with transaction(conn):
        second = import_csv(conn, b, "https://www.facebook.com/share/p/two/")
    assert first.new == 2
    assert (second.new, second.updated, second.merged_into) == (1, 2, first.post_id)
    post = conn.execute("SELECT * FROM posts").fetchone()
    assert post["fb_post_id"] == POST
    stored = conn.execute("SELECT reaction_count, created_time FROM comments ORDER BY created_time").fetchall()
    assert [tuple(r) for r in stored] == [(1, 1_790_000_001), (2, 1_790_000_002), (3, 1_790_000_003)]


def test_clean_post_link():
    assert clean_post_link(" https://www.facebook.com/share/p/1CYuJh1ntq/?mibextid=abc ") == \
        "https://www.facebook.com/share/p/1CYuJh1ntq/"
    assert clean_post_link("https://www.facebook.com/permalink.php?story_fbid=12&id=34&rdid=x") == \
        "https://www.facebook.com/permalink.php?story_fbid=12&id=34"
    for bad in ("http://www.facebook.com/x", "https://evil.example/facebook.com", "https://www.facebook.com/",
                "javascript:alert(1)", "https://facebook.com.evil.example/post"):
        try:
            clean_post_link(bad)
        except InvalidLink:
            continue
        raise AssertionError(f"accepted {bad}")


def test_safe_web_url():
    assert safe_web_url("https://www.facebook.com/x") == "https://www.facebook.com/x"
    assert safe_web_url("javascript:alert(1)") is None
    assert safe_web_url(None) is None
