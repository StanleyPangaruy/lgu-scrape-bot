"""The browsing aids: titles before details load, post search/sort, comment
order, topic chips, paging and empty states."""

import re

from server.db import transaction
from server.ingest import create_post, save_scrape
from server.queries import post_title
from tests.conftest import comment_id

POST = "1557299289776837"


def add_post(conn, link, post_id, texts, details=None, reactions=None):
    pid = create_post(conn, link, None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": post_id, **(details or {})}, [
            {"id": comment_id(post_id, n), "author_id": str(100 + n), "author_name": f"P{n}",
             "text": t, "created_time": 1_790_000_000 + n,
             "reaction_count": (reactions or {}).get(n, 0)}
            for n, t in enumerate(texts, 1)
        ])
    return pid


def test_post_title_fallbacks():
    assert post_title("Paalala\nmore", None) == "Paalala"
    assert post_title(None, "Municipality") == "Post by Municipality"
    assert post_title(None, None, "https://www.facebook.com/share/p/1DycFHwTiZ/", 497) == \
        "Post share/p/1DycFHwTiZ · 497 comments"
    assert post_title(None, None, "https://www.facebook.com/share/p/x/", 1) == "Post share/p/x · 1 comment"
    assert post_title(None, None) == "Untitled post"


def test_untitled_posts_are_told_apart_and_rechecked_together(browsers, conn):
    add_post(conn, "https://www.facebook.com/share/p/AAA/", "1", ["a", "b"])
    add_post(conn, "https://www.facebook.com/share/p/BBB/", "2", ["c"])
    add_post(conn, "https://www.facebook.com/share/p/CCC/", "3", ["d"], details={"text": "May caption"})
    admin = browsers["admin"]
    page = admin.get("/posts").text
    assert "Post share/p/AAA · 2 comments" in page and "Post share/p/BBB · 1 comment" in page
    assert "2 posts have no title or page yet" in page
    assert "Re-check all untitled posts" not in browsers["staff"].get("/posts").text

    r = admin.post("/posts/recheck-untitled")
    assert r.status_code == 303 and "untitled-queued" in r.headers["location"]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0] == 2
    admin.post("/posts/recheck-untitled")  # already queued: nothing doubles
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert browsers["staff"].post("/posts/recheck-untitled").status_code == 403


def test_posts_search_sort_and_totals(browsers, conn):
    add_post(conn, "https://www.facebook.com/share/p/one/", "1", ["x"], details={"text": "Tricycle ordinance"})
    add_post(conn, "https://www.facebook.com/share/p/two/", "2", ["x", "y", "kolorum dito"],
             details={"text": "Market schedule"})
    mayor = browsers["mayor"]
    page = mayor.get("/posts").text
    assert re.search(r'Comments</div><div class="value">4<', page)
    assert re.search(r'mentioning a keyword</div><div class="value">1<', page)

    found = mayor.get("/posts?q=market").text
    assert "Market schedule" in found and "Tricycle ordinance" not in found
    by_comments = mayor.get("/posts?sort=comments").text
    assert by_comments.index("Market schedule") < by_comments.index("Tricycle ordinance")
    empty = mayor.get("/posts?q=nothing-like-this").text
    assert "No posts match" in empty and "Clear search" in empty
    assert mayor.get("/posts?sort=bogus").status_code == 200


def test_comment_order_and_topics(browsers, conn):
    pid = add_post(conn, "https://www.facebook.com/share/p/t/", POST,
                   ["unang kolorum", "mahal ang gasolina", "kolorum ulit"], reactions={2: 50})
    staff = browsers["staff"]
    oldest = staff.get(f"/posts/{pid}").text
    assert oldest.index("unang <mark>") < oldest.index("</mark> ulit")
    newest = staff.get(f"/posts/{pid}?order=newest").text
    assert newest.index("</mark> ulit") < newest.index("unang <mark>")
    top = staff.get(f"/posts/{pid}?order=reactions").text
    assert top.index("mahal ang ") < top.index("unang <mark>")

    kid = conn.execute("SELECT id FROM keywords WHERE term = 'colorum'").fetchone()[0]
    assert re.search(rf'href="/posts/{pid}\?keyword={kid}"[^>]*>colorum <span class="count">2</span>', oldest)
    chip = staff.get(f"/posts/{pid}?keyword={kid}&order=newest").text
    assert "mahal ang " not in chip and chip.index("</mark> ulit") < chip.index("unang <mark>")


def test_comment_paging(browsers, conn):
    pid = add_post(conn, "https://www.facebook.com/share/p/big/", POST, [f"comment number {n}" for n in range(1, 251)])
    staff = browsers["staff"]
    first = staff.get(f"/posts/{pid}").text
    assert first.count('<article class="comment" id="c') == 100
    assert "250 conversations" in first and "showing the first 100" in first
    assert f'href="/posts/{pid}?show=200#more-100"' in first
    second = staff.get(f"/posts/{pid}?show=200").text
    assert second.count('<article class="comment" id="c') == 200 and 'id="more-100"' in second
    last = staff.get(f"/posts/{pid}?show=300").text
    assert last.count('<article class="comment" id="c') == 250 and "Show " not in last.split("</article>")[-1]
    assert staff.get(f"/posts/{pid}?show=junk").text.count('<article class="comment" id="c') == 100


def test_empty_states(browsers, conn):
    pid = add_post(conn, "https://www.facebook.com/share/p/e/", POST, ["salamat"])
    staff = browsers["staff"]
    page = staff.get(f"/posts/{pid}?q=wala").text
    assert "No comments match “wala”" in page and "Clear filters" in page

    fresh = create_post(conn, "https://www.facebook.com/share/p/fresh/", None)
    assert "has not been collected yet" in staff.get(f"/posts/{fresh}").text


def link_row(**kw):
    row = {"url": None, "post_url": None, "legacy_fbid": None, "parent_id": None, "parent_legacy": None}
    row.update(kw)
    return row


def test_comment_link_prefers_stored_and_builds_missing():
    from server.queries import comment_link

    post = "https://www.facebook.com/municipalityoflopezquezon/posts/pfbid0abc"
    stored = post + "?comment_id=111"
    assert comment_link(link_row(url=stored, post_url=post, legacy_fbid="999")) == stored
    assert comment_link(link_row(post_url=post, legacy_fbid="111")) == post + "?comment_id=111"
    assert comment_link(link_row(post_url=post, legacy_fbid="222", parent_id="x", parent_legacy="111")) == \
        post + "?comment_id=111&reply_comment_id=222"
    permalink = "https://www.facebook.com/permalink.php?story_fbid=5&id=6"
    assert comment_link(link_row(post_url=permalink, legacy_fbid="7")) == permalink + "&comment_id=7"
    # Not enough to build one, or not a Facebook address: no link at all.
    assert comment_link(link_row(post_url=post)) is None
    assert comment_link(link_row(post_url=post, legacy_fbid="2", parent_id="x")) is None
    assert comment_link(link_row(url="javascript:alert(1)", post_url="https://evil.example/p", legacy_fbid="1")) is None
    assert comment_link(link_row(url="http://www.facebook.com/x")) is None


def test_comments_show_facebook_links_on_every_page(browsers, conn):
    pid = create_post(conn, "https://www.facebook.com/share/p/L/", None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": POST, "url": "https://www.facebook.com/lgu/posts/pfbid0x"}, [
            {"id": comment_id(POST, 1), "legacy_fbid": "501", "author_id": "9", "author_name": "Ana",
             "text": "may kolorum", "created_time": 4_000_000_000, "reaction_count": 3,
             "url": "https://www.facebook.com/lgu/posts/pfbid0x?comment_id=501"},
            {"id": comment_id(POST, 2), "legacy_fbid": "502", "parent_id": comment_id(POST, 1),
             "author_id": "9", "author_name": "Ana", "text": "sagot", "created_time": 4_000_000_100},
        ])
    built = "https://www.facebook.com/lgu/posts/pfbid0x?comment_id=501&amp;reply_comment_id=502"
    staff = browsers["staff"]
    post_page = staff.get(f"/posts/{pid}").text
    assert post_page.count("View on Facebook") == 2 and built in post_page
    assert built in staff.get("/people/author?id=9").text
    assert "View on Facebook" in staff.get("/people").text
    kid = conn.execute("SELECT id FROM keywords WHERE term = 'colorum'").fetchone()[0]
    assert "pfbid0x?comment_id=501" in staff.get(f"/keywords?k={kid}").text

    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(staff.get(f"/posts/{pid}/export.xlsx").content))
    links = [row[-1] for row in wb["Comments"].iter_rows(min_row=2, values_only=True)]
    assert links[1] == built.replace("&amp;", "&")
