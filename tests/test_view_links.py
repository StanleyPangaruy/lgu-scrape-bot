"""View-only links: the mayor opens the site without signing in, and cannot change anything."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from server.db import transaction
from server.ingest import create_post, save_scrape
from tests.conftest import comment_id

POST = "1557299289776837"


def add_post(conn):
    pid = create_post(conn, "https://www.facebook.com/share/p/v1/", None)
    with transaction(conn):
        save_scrape(conn, pid, {"post_id": POST, "text": "Bagong ordinansa"}, [
            {"id": comment_id(POST, 1), "author_name": "P1", "text": "pabor po sa kolorum", "created_time": 1_790_000_000},
        ])
    return pid


def make_link(admin, name="Mayor"):
    page = admin.post("/settings/view-links", {"name": name})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    match = re.search(r"http://testserver/view/([A-Za-z0-9_\-]+)", page.text)
    assert match, "the new link is shown once"
    return match.group(1)


def test_view_link_opens_the_site_read_only(app, browsers, conn):
    pid = add_post(conn)
    token = make_link(browsers["admin"])
    assert browsers["staff"].post("/settings/view-links", {"name": "x"}).status_code == 403

    with TestClient(app) as mayor:
        opened = mayor.get(f"/view/{token}", follow_redirects=False)
        assert opened.status_code == 303 and opened.headers["location"] == "/overview"
        assert opened.headers["referrer-policy"] == "no-referrer"

        for page in ("/overview", "/posts", f"/posts/{pid}", "/people", "/keywords"):
            response = mayor.get(page)
            assert response.status_code == 200, page
            assert "View only" in response.text and "Sign out" not in response.text
            assert "Sign in with an account" in response.text
        assert mayor.get(f"/posts/{pid}/export.xlsx").status_code == 200

        post_page = mayor.get(f"/posts/{pid}").text
        assert 'class="correct"' not in post_page and "Re-check" not in post_page and "Delete post" not in post_page
        assert 'method="post" action="/keywords"' not in mayor.get("/keywords").text
        for page in ("/settings", "/jobs"):
            assert mayor.get(page).status_code == 403, page
        assert mayor.get("/account", follow_redirects=False).headers["location"] == "/overview"

        # No changes, even with the session's own CSRF token.
        token_value = conn.execute(
            "SELECT s.csrf_token FROM sessions s JOIN view_links v ON v.user_id = s.user_id"
        ).fetchone()[0]
        rid = conn.execute("SELECT rowid FROM comments").fetchone()[0]
        for url, data in ((f"/comments/{rid}/label", {"label": "against"}), ("/keywords", {"term": "pondo"}),
                          (f"/posts/{pid}/recheck", {}), (f"/posts/{pid}/delete", {}), ("/posts", {"link": "x"})):
            r = mayor.post(url, data={"csrf_token": token_value, **data}, follow_redirects=False)
            assert r.status_code == 403, url
        assert mayor.post("/account/password", data={
            "csrf_token": token_value, "current": "whatever", "new": "abcdefghijk", "confirm": "abcdefghijk",
        }).status_code == 403


def test_settings_lists_links_not_hidden_viewers(browsers):
    make_link(browsers["admin"], name="Mayor Juan")
    page = browsers["admin"].get("/settings").text
    assert "Mayor Juan" in page and "view-link-" not in page


def test_turning_off_a_link_signs_the_viewer_out(app, browsers, conn):
    token = make_link(browsers["admin"])
    with TestClient(app) as mayor:
        mayor.get(f"/view/{token}")
        assert mayor.get("/overview").status_code == 200

        link_id = conn.execute("SELECT id FROM view_links").fetchone()[0]
        assert browsers["admin"].post(f"/settings/view-links/{link_id}/revoke").status_code == 303
        assert mayor.get("/overview", follow_redirects=False).headers["location"].startswith("/login")
        again = mayor.get(f"/view/{token}", follow_redirects=False)
        assert again.status_code == 404 and "not valid or was turned off" in again.text


def test_unknown_link(app):
    with TestClient(app) as client:
        assert client.get("/view/not-a-real-token", follow_redirects=False).status_code == 404


def test_admin_opening_the_link_stays_admin(app, browsers):
    admin = browsers["admin"]
    token = make_link(admin)
    page = admin.get(f"/view/{token}", follow_redirects=False)
    assert page.status_code == 200 and "did not switch you" in page.text
    assert admin.get("/settings").status_code == 200  # still the admin


def test_viewer_can_get_back_to_the_sign_in_form(app, browsers, users):
    from tests.conftest import PASSWORD

    token = make_link(browsers["admin"])
    with TestClient(app) as browser:
        browser.get(f"/view/{token}")
        form = browser.get("/login", follow_redirects=False)
        assert form.status_code == 200 and 'name="password"' in form.text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
        signed_in = browser.post("/login", data={"username": "admin", "password": PASSWORD,
                                                 "csrf_token": csrf, "next": "/settings"}, follow_redirects=False)
        assert signed_in.status_code == 303
        assert browser.get("/settings").status_code == 200
