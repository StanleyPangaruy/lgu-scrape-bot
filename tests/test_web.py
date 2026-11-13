import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from server.db import transaction
from server.ingest import create_post, save_scrape
from tests.conftest import PASSWORD, _csrf_from, comment_id, sign_in

POST = "1557299289776837"


def seed_post(conn, texts):
    pid = create_post(conn, "https://www.facebook.com/share/p/seed/", None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": POST, "page_name": "Municipality of Lopez, Quezon",
                                "text": "Paalala sa tricycle"}, [
            {"id": comment_id(POST, n), "author_id": f"u{n % 2}", "author_name": f"Person {n % 2}",
             "text": t, "created_time": 4_000_000_000 - n, "reaction_count": n}
            for n, t in enumerate(texts, 1)
        ])
    return pid


def test_pages_need_sign_in(app, users):
    with TestClient(app) as anon:
        for path in ("/posts", "/people", "/keywords", "/jobs", "/settings", "/posts/1"):
            r = anon.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_wrong_password_then_lockout(app, users):
    with TestClient(app) as client:
        for attempt in range(5):
            page = client.get("/login")
            r = client.post("/login", data={"username": "staff", "password": "nope-nope-nope",
                                             "csrf_token": _csrf_from(page.text)})
            assert r.status_code == 401
        page = client.get("/login")
        r = client.post("/login", data={"username": "staff", "password": PASSWORD, "csrf_token": _csrf_from(page.text)})
        assert r.status_code == 401 and "Try again in" in r.text


def test_login_requires_its_form_token(app, users):
    with TestClient(app) as client:
        r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": "forged"})
        assert r.status_code == 400


def test_login_redirect_stays_on_site(app, users):
    with TestClient(app) as client:
        page = client.get("/login?next=//evil.example")
        r = client.post("/login", data={"username": "admin", "password": PASSWORD,
                                         "csrf_token": _csrf_from(page.text), "next": "//evil.example"},
                        follow_redirects=False)
        assert r.headers["location"] == "/"


def test_forms_reject_missing_or_wrong_csrf(browsers):
    admin = browsers["admin"]
    r = admin.client.post("/posts", data={"link": "https://www.facebook.com/share/p/x/", "csrf_token": "wrong"},
                          follow_redirects=False)
    assert r.status_code == 403
    r = admin.client.post("/posts", data={"link": "https://www.facebook.com/share/p/x/"}, follow_redirects=False)
    assert r.status_code == 400


def test_roles(browsers, conn):
    pid = seed_post(conn, ["hello"])
    staff, mayor = browsers["staff"], browsers["mayor"]
    for b in (staff, mayor):
        assert b.post("/posts", {"link": "https://www.facebook.com/share/p/x/"}).status_code == 403
        assert b.post(f"/posts/{pid}/recheck").status_code == 403
        assert b.get("/jobs").status_code == 403
        assert b.get("/settings").status_code == 403
        page = b.get("/posts").text
        assert 'href="/settings"' not in page and "Collect comments" not in page
    admin_page = browsers["admin"].get("/posts").text
    assert 'href="/settings"' in admin_page and "Collect comments" in admin_page


def test_every_page_renders_for_every_role(browsers, conn):
    pid = seed_post(conn, ["Hulihin ang KOLORUM", "baha na naman", "ok"])
    for role, b in browsers.items():
        for path in ("/posts", f"/posts/{pid}", f"/posts/{pid}?q=baha", f"/posts/{pid}?hits=true",
                     "/people", "/people?period=all", "/keywords", "/keywords?period=7", "/account"):
            r = b.get(path)
            assert r.status_code == 200, (role, path, r.text[:300])
    for path in ("/jobs", "/settings"):
        assert browsers["admin"].get(path).status_code == 200


def test_filters_accept_what_the_forms_send(browsers, conn):
    """The filter form sends keyword= (empty) for "Any"; that and junk mean no filter."""
    pid = seed_post(conn, ["dami ng kolorum", "salamat po"])
    staff = browsers["staff"]
    for query in ("q=author&keyword=", "keyword=", "keyword=abc", "keyword=99999", "q=&keyword=&hits=true"):
        r = staff.get(f"/posts/{pid}?{query}")
        assert r.status_code == 200, (query, r.status_code)
    kid = conn.execute("SELECT id FROM keywords WHERE term = 'colorum'").fetchone()[0]
    page = staff.get(f"/posts/{pid}?q=&keyword={kid}").text
    assert "kolorum" in page and "salamat po" not in page
    for query in ("k=", "k=abc", "k=99999", "k=&period=all"):
        assert staff.get(f"/keywords?{query}").status_code == 200, query


def test_comment_text_is_escaped_and_keywords_highlighted(browsers, conn):
    pid = seed_post(conn, ['<img src=x onerror=alert(1)> dami ng kolorum'])
    page = browsers["staff"].get(f"/posts/{pid}").text
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "<mark>kolorum</mark>" in page


def test_security_headers(browsers):
    r = browsers["staff"].get("/posts")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_new_badge_clears_after_viewing(browsers, conn):
    seed_post(conn, ["x"])
    # Signing in opened Posts in this same second; pretend it was earlier.
    conn.execute("UPDATE users SET posts_seen_at = 0")
    staff = browsers["staff"]
    assert staff.get("/posts").text.count('class="badge new"') == 1
    assert staff.get("/posts").text.count('class="badge new"') == 0


def test_people_and_keywords_default_to_all_time(browsers, conn):
    pid = create_post(conn, "https://www.facebook.com/share/p/old/", None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": "444"}, [
            {"id": comment_id("444", 1), "author_id": "5", "author_name": "Old Commenter",
             "text": "dami ng kolorum noon", "created_time": 1_000_000_000, "reaction_count": 3},
        ])
    staff = browsers["staff"]
    people = staff.get("/people").text
    assert "Old Commenter" in people and '<option value="all" selected>' in people
    keywords = staff.get("/keywords").text
    assert "dami ng" in keywords and '<option value="all" selected>' in keywords
    assert "Old Commenter" not in staff.get("/people?period=30").text


def test_people_counts(browsers, conn):
    seed_post(conn, ["a", "b", "c"])
    page = browsers["mayor"].get("/people?period=all").text
    # Person 1 wrote comments 1 and 3 (4 reactions), Person 0 wrote comment 2.
    assert page.index("Person 1") < page.index("Person 0")


def test_author_page_lists_their_comments_across_posts(browsers, conn):
    seed_post(conn, ["first by Person 1", "by Person 0", "second by Person 1"])
    other = create_post(conn, "https://www.facebook.com/share/p/other/", None)
    with transaction(conn):
        save_scrape(conn, other, {"post_id": "222", "text": "Ibang post"}, [
            {"id": comment_id("222", 1), "author_id": "u1", "author_name": "Person 1",
             "text": "<b>third</b> dami ng kolorum", "created_time": 4_100_000_000, "reaction_count": 7},
        ])
    mayor = browsers["mayor"]
    people = mayor.get("/people?period=all").text
    assert 'href="/people/author?id=' not in people  # ids here are not digits ("u1"): linked by name
    assert "/people/author?name=Person+1" in people

    conn.execute("UPDATE comments SET author_id = '100064903410975' WHERE author_name = 'Person 1'")
    page = mayor.get("/people/author?id=100064903410975")
    assert page.status_code == 200
    text = page.text
    assert "first by Person 1" in text and "second by Person 1" in text and "&lt;b&gt;third" in text
    assert "by Person 0" not in text
    assert "<mark>kolorum</mark>" in text and "Ibang post" in text
    # Newest first. seed_post's times count down, so its "first" comment is newer than its "second".
    assert text.index("third") < text.index("first by Person 1") < text.index("second by Person 1")

    export = mayor.get("/people/author/export.xlsx?id=100064903410975")
    assert export.status_code == 200
    rows = list(load_workbook(io.BytesIO(export.content))["Comments"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 3 and rows[0][4] == "Not analyzed" and rows[0][5] == "colorum"


def test_author_page_by_name_unknown_and_unusual_names(browsers, conn):
    pid = create_post(conn, "https://www.facebook.com/share/p/n/", None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": "333"}, [
            {"id": comment_id("333", 1), "author_name": "ۦۦۦۦ ۦۦۦۦ", "text": "walang id"},
        ])
    staff = browsers["staff"]
    assert staff.get("/people/author?name=%DB%A6%DB%A6%DB%A6%DB%A6+%DB%A6%DB%A6%DB%A6%DB%A6").status_code == 200
    r = staff.get("/people/author/export.xlsx?name=%DB%A6%DB%A6%DB%A6%DB%A6+%DB%A6%DB%A6%DB%A6%DB%A6")
    assert r.status_code == 200 and "comments-commenter-" in r.headers["content-disposition"]
    assert staff.get("/people/author?id=999").status_code == 404
    assert staff.get("/people/author").status_code == 404
    assert staff.get("/people/author?id=abc").status_code == 404


def test_keyword_admin_cycle_updates_counts(browsers, conn):
    seed_post(conn, ["may pondo ba", "walang pondo"])
    admin = browsers["admin"]
    r = admin.post("/keywords", {"term": "pondo", "variants": "budget"})
    assert r.status_code == 303
    kid = conn.execute("SELECT id FROM keywords WHERE term = 'pondo'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM keyword_hits WHERE keyword_id = ?", (kid,)).fetchone()[0] == 2
    assert "duplicate" in admin.post("/keywords", {"term": "PONDO"}).headers["location"]
    admin.post(f"/keywords/{kid}/delete")
    assert conn.execute("SELECT COUNT(*) FROM keyword_hits WHERE keyword_id = ?", (kid,)).fetchone()[0] == 0


def test_staff_manage_keywords_mayor_cannot(browsers, conn):
    seed_post(conn, ["may baha sa kanto", "sa eskinita lang"])
    staff, mayor = browsers["staff"], browsers["mayor"]
    staff_page = staff.get("/keywords").text
    assert "Add a keyword" in staff_page and "Edit spellings or delete" in staff_page
    mayor_page = mayor.get("/keywords").text
    assert "Add a keyword" not in mayor_page and "Edit spellings or delete" not in mayor_page

    r = staff.post("/keywords", {"term": "kanto"})
    assert r.status_code == 303 and "keyword-added" in r.headers["location"]
    kid = conn.execute("SELECT id FROM keywords WHERE term = 'kanto'").fetchone()[0]
    hits = lambda: conn.execute("SELECT COUNT(*) FROM keyword_hits WHERE keyword_id = ?", (kid,)).fetchone()[0]
    assert hits() == 1

    assert staff.post(f"/keywords/{kid}", {"variants": "eskinita"}).status_code == 303
    assert hits() == 2  # the new spelling matched the second comment
    assert staff.post(f"/keywords/{kid}/delete").status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM keywords WHERE id = ?", (kid,)).fetchone()[0] == 0
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE actor = 'staff' ORDER BY id")]
    assert actions[-3:] == ["added keyword", "edited keyword", "deleted keyword"]

    some = conn.execute("SELECT id FROM keywords LIMIT 1").fetchone()[0]
    assert mayor.post("/keywords", {"term": "pondo"}).status_code == 403
    assert mayor.post(f"/keywords/{some}", {"variants": ""}).status_code == 403
    assert mayor.post(f"/keywords/{some}/delete").status_code == 403


def test_keyword_error_messages_are_not_taken_from_the_url(browsers):
    page = browsers["staff"].get("/keywords?error=Your+account+is+hacked").text
    assert "hacked" not in page


def test_excel_exports(browsers, conn):
    pid = seed_post(conn, ["=HYPERLINK(\"http://x\")", "salamat po"])
    for path in ("/posts/export.xlsx", f"/posts/{pid}/export.xlsx", "/people/export.xlsx?period=all"):
        r = browsers["mayor"].get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
        load_workbook(io.BytesIO(r.content))
    wb = load_workbook(io.BytesIO(browsers["mayor"].get(f"/posts/{pid}/export.xlsx").content))
    cells = [c for row in wb["Comments"].iter_rows(min_row=2) for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert cells and all(c.data_type == "s" for c in cells)


def test_admin_manages_accounts(browsers, app, conn):
    admin = browsers["admin"]
    r = admin.post("/settings/users", {"username": "staff2", "display_name": "Second Staff",
                                       "role": "staff", "password": "short"})
    assert r.status_code == 400 and "at least 10" in r.text
    r = admin.post("/settings/users", {"username": "staff2", "display_name": "Second Staff",
                                       "role": "staff", "password": PASSWORD})
    assert r.status_code == 303
    second = sign_in(app, "staff2")
    uid = conn.execute("SELECT id FROM users WHERE username = 'staff2'").fetchone()[0]
    admin.post(f"/settings/users/{uid}/active", {"active": "0"})
    assert second.get("/posts", follow_redirects=False).status_code == 303  # signed out
    second.client.__exit__(None, None, None)


def test_agent_token_shown_once_and_stored_hashed(browsers, conn):
    r = browsers["admin"].post("/settings/agents", {"name": "Admin laptop"})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    stored = conn.execute("SELECT token_hash FROM agent_tokens").fetchone()[0]
    assert stored not in r.text and len(stored) == 64


def test_change_own_password(browsers, app):
    staff = browsers["staff"]
    r = staff.post("/account/password", {"current": "wrong-password", "new": "new-password-123", "confirm": "new-password-123"})
    assert r.status_code == 400
    r = staff.post("/account/password", {"current": PASSWORD, "new": "new-password-123", "confirm": "new-password-123"})
    assert r.status_code == 303
    assert staff.get("/posts", follow_redirects=False).status_code == 303
    sign_in(app, "staff", "new-password-123").client.__exit__(None, None, None)


def test_unknown_post_is_404(browsers):
    assert browsers["staff"].get("/posts/9999").status_code == 404


def test_nothing_but_static_files_may_be_cached(browsers, conn, agent):
    """A proxy in front of the site (Cloudflare) must never keep a copy of a page."""
    pid = seed_post(conn, ["hello"])
    admin = browsers["admin"]
    for path in ("/posts", f"/posts/{pid}", f"/posts/{pid}/export.xlsx", "/people", "/settings", "/no-such-page"):
        assert admin.get(path).headers.get("cache-control") == "no-store", path
    with TestClient(admin.client.app) as anon:
        assert anon.get("/login").headers.get("cache-control") == "no-store"
    assert agent.post("/api/agent/claim", json={}).headers.get("cache-control") == "no-store"
    assert admin.post("/logout").headers.get("cache-control") == "no-store"
    static = admin.get("/static/app.css")
    assert static.status_code == 200 and static.headers.get("cache-control") != "no-store"
