from __future__ import annotations

import base64
import re
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from server.app import create_app, prepare_database
from server.auth import create_user
from server.config import Settings
from server.db import connect, now
from server.security import new_token, token_hash

PASSWORD = "correct-horse-battery"


def comment_id(post_id: str, n: int) -> str:
    """A Facebook-style comment id: base64 of comment:<post id>_<comment id>."""
    return base64.b64encode(f"comment:{post_id}_{1000 + n}".encode()).decode()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    prepare_database(s)
    return s


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    c = connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def users(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        role: create_user(conn, role, f"The {role.title()}", role, PASSWORD)
        for role in ("admin", "staff", "mayor")
    }


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token on the page"
    return match.group(1)


class Browser:
    """A signed-in TestClient plus the session's CSRF token."""

    def __init__(self, client: TestClient, csrf: str) -> None:
        self.client = client
        self.csrf = csrf

    def get(self, url: str, **kw):
        return self.client.get(url, **kw)

    def post(self, url: str, data: dict | None = None, **kw):
        body = {"csrf_token": self.csrf, **(data or {})}
        return self.client.post(url, data=body, follow_redirects=False, **kw)


def sign_in(app, username: str, password: str = PASSWORD) -> Browser:
    client = TestClient(app)
    client.__enter__()
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _csrf_from(page.text), "next": "/posts"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    posts = client.get("/posts")
    return Browser(client, _csrf_from(posts.text))


@pytest.fixture
def browsers(app, users):
    made = {role: sign_in(app, role) for role in users}
    yield made
    for b in made.values():
        b.client.__exit__(None, None, None)


@pytest.fixture
def agent_token(conn: sqlite3.Connection) -> str:
    token = new_token()
    conn.execute(
        "INSERT INTO agent_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
        ("Test laptop", token_hash(token), now()),
    )
    return token


@pytest.fixture
def agent(app, agent_token):
    with TestClient(app, headers={"Authorization": f"Bearer {agent_token}"}) as client:
        yield client
