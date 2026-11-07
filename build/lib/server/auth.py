"""Logins, sessions, roles and CSRF protection for the website.

Sessions live in the database (only a hash of the cookie value is stored).
Every form that changes something carries the session's CSRF token; the
login form, which has no session yet, uses a double-submit cookie instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterator

from fastapi import Depends, HTTPException, Request

from server.config import Settings
from server.db import audit, connect, now, transaction
from server.security import (
    DUMMY_HASH,
    hash_password,
    new_token,
    password_problem,
    token_hash,
    tokens_equal,
    verify_password,
)

SESSION_COOKIE = "lcm_session"
LOGIN_CSRF_COOKIE = "lcm_login_csrf"
ROLES = ("admin", "staff", "mayor")
ROLE_LABELS = {"admin": "Admin", "staff": "Staff", "mayor": "Mayor"}


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    csrf_token: str
    posts_seen_at: int | None
    # Signed in through a view-only link (no password; nothing to manage on /account).
    via_link: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_request(self) -> bool:
        return self.role in ("admin", "staff")

    @property
    def can_edit_keywords(self) -> bool:
        return self.role in ("admin", "staff")


class LoginRequired(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


class AccountError(ValueError):
    pass


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(get_settings(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


def _load_session(conn: sqlite3.Connection, token: str) -> User | None:
    row = conn.execute(
        """
        SELECT u.id, u.username, u.display_name, u.role, u.posts_seen_at, s.csrf_token,
               EXISTS (SELECT 1 FROM view_links v WHERE v.user_id = u.id) AS via_link
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1
        """,
        (token_hash(token), now()),
    ).fetchone()
    if row is None:
        return None
    return User(
        row["id"], row["username"], row["display_name"], row["role"], row["csrf_token"],
        row["posts_seen_at"], bool(row["via_link"]),
    )


def optional_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    return _load_session(conn, token) if token else None


def current_user(request: Request, user: User | None = Depends(optional_user)) -> User:
    if user is None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        raise LoginRequired(target if request.method == "GET" else "/")
    return user


def require_role(*roles: str) -> Callable[..., User]:
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Your account cannot do this.")
        return user

    return dependency


require_admin = require_role("admin")


def check_csrf(user: User, submitted: str) -> None:
    if not submitted or not tokens_equal(submitted, user.csrf_token):
        raise HTTPException(status_code=403, detail="This form expired. Go back, reload the page and try again.")


def safe_next(path: str | None) -> str:
    """Only same-site paths, so the login page cannot redirect somewhere else."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


@dataclass
class LoginOutcome:
    session_token: str | None
    error: str | None


def attempt_login(
    conn: sqlite3.Connection, settings: Settings, username: str, password: str
) -> LoginOutcome:
    generic = "Wrong username or password."
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username.strip(),)
        ).fetchone()
        if row is None:
            verify_password(password, DUMMY_HASH)
            return LoginOutcome(None, generic)
        t = now()
        if row["locked_until"] and row["locked_until"] > t:
            minutes = max(1, (row["locked_until"] - t + 59) // 60)
            return LoginOutcome(None, f"Too many wrong passwords. Try again in {minutes} minutes.")
        if not verify_password(password, row["password_hash"]):
            failures = row["failed_logins"] + 1
            if failures >= settings.max_failed_logins:
                conn.execute(
                    "UPDATE users SET failed_logins = 0, locked_until = ? WHERE id = ?",
                    (t + settings.lockout_minutes * 60, row["id"]),
                )
                audit(conn, row["username"], "account locked", "too many wrong passwords", row["id"])
                return LoginOutcome(
                    None, f"Too many wrong passwords. Try again in {settings.lockout_minutes} minutes."
                )
            conn.execute("UPDATE users SET failed_logins = ? WHERE id = ?", (failures, row["id"]))
            return LoginOutcome(None, generic)

        conn.execute(
            "UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (row["id"],)
        )
        token = new_token()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash(token), row["id"], new_token(), t, t + settings.session_days * 86400),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (t,))
        audit(conn, row["username"], "signed in", "", row["id"])
        return LoginOutcome(token, None)


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def create_user(
    conn: sqlite3.Connection, username: str, display_name: str, role: str, password: str
) -> int:
    username = username.strip()
    display_name = " ".join(display_name.split())
    if not username or not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise AccountError("Username: letters, numbers, dots, dashes or underscores only.")
    if len(username) > 40 or len(display_name) > 80:
        raise AccountError("Username or name is too long.")
    if not display_name:
        raise AccountError("Enter the person's name.")
    if role not in ROLES:
        raise AccountError("Choose a role.")
    problem = password_problem(password)
    if problem:
        raise AccountError(problem)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AccountError("That username is taken.")
    cur = conn.execute(
        "INSERT INTO users (username, display_name, role, password_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, display_name, role, hash_password(password), now()),
    )
    return int(cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    problem = password_problem(password)
    if problem:
        raise AccountError(problem)
    conn.execute(
        "UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL WHERE id = ?",
        (hash_password(password), user_id),
    )
    # Changing a password signs that person out everywhere.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# --- View-only links ----------------------------------------------------------------------

VIEW_LINK_SESSION_DAYS = 90


def create_view_link(conn: sqlite3.Connection, name: str, created_by: int | None) -> str:
    """A secret link that opens the site view-only (mayor role) without a password.
    Returns the token; only its hash is stored. Call inside a transaction."""
    token = new_token()
    n = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM view_links").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO users (username, display_name, role, password_hash, created_at) VALUES (?, ?, 'mayor', ?, ?)",
        (f"view-link-{n}-{new_token()[:6]}", name, hash_password(new_token()), now()),
    )
    conn.execute(
        "INSERT INTO view_links (name, token_hash, user_id, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, token_hash(token), cur.lastrowid, created_by, now()),
    )
    return token


def open_view_link(conn: sqlite3.Connection, token: str) -> str | None:
    """A new session for a valid link (None if unknown or turned off)."""
    with transaction(conn):
        row = conn.execute(
            """
            SELECT v.id, v.name, v.user_id FROM view_links v JOIN users u ON u.id = v.user_id
            WHERE v.token_hash = ? AND v.revoked = 0 AND u.active = 1
            """,
            (token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        t = now()
        session = new_token()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash(session), row["user_id"], new_token(), t, t + VIEW_LINK_SESSION_DAYS * 86400),
        )
        conn.execute("UPDATE view_links SET last_used_at = ? WHERE id = ?", (t, row["id"]))
        audit(conn, row["name"], "opened view-only link", "", row["user_id"])
    return session


def revoke_view_link(conn: sqlite3.Connection, link_id: int) -> str | None:
    """Turn a link off and sign out everyone who opened it. Returns its name. Call inside a transaction."""
    row = conn.execute("SELECT name, user_id FROM view_links WHERE id = ?", (link_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE view_links SET revoked = 1 WHERE id = ?", (link_id,))
    conn.execute("UPDATE users SET active = 0 WHERE id = ?", (row["user_id"],))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    return row["name"]
