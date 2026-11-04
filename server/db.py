"""SQLite storage: connection setup and schema migrations.

Migrations are numbered SQL scripts applied in order; PRAGMA user_version
records how many have run, so an existing database upgrades in place.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
        id             INTEGER PRIMARY KEY,
        username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name   TEXT NOT NULL,
        role           TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'mayor')),
        password_hash  TEXT NOT NULL,
        active         INTEGER NOT NULL DEFAULT 1,
        failed_logins  INTEGER NOT NULL DEFAULT 0,
        locked_until   INTEGER,
        posts_seen_at  INTEGER,
        created_at     INTEGER NOT NULL
    );

    CREATE TABLE sessions (
        token_hash  TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token  TEXT NOT NULL,
        created_at  INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL
    );
    CREATE INDEX sessions_user ON sessions(user_id);

    CREATE TABLE agent_tokens (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        token_hash    TEXT NOT NULL UNIQUE,
        created_at    INTEGER NOT NULL,
        last_seen_at  INTEGER,
        status        TEXT,
        revoked       INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE posts (
        id                INTEGER PRIMARY KEY,
        fb_post_id        TEXT UNIQUE,
        url               TEXT NOT NULL,
        source_url        TEXT NOT NULL,
        page_id           TEXT,
        page_name         TEXT,
        page_url          TEXT,
        text              TEXT,
        created_time      INTEGER,
        fb_comment_count  INTEGER,
        added_by          INTEGER REFERENCES users(id) ON DELETE SET NULL,
        added_at          INTEGER NOT NULL,
        last_checked_at   INTEGER,
        last_new_at       INTEGER
    );
    CREATE INDEX posts_source_url ON posts(source_url);
    CREATE INDEX posts_url ON posts(url);

    CREATE TABLE comments (
        id              TEXT PRIMARY KEY,
        post_id         INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        legacy_fbid     TEXT,
        parent_id       TEXT,
        depth           INTEGER,
        author_id       TEXT,
        author_name     TEXT,
        author_url      TEXT,
        text            TEXT,
        text_norm       TEXT NOT NULL DEFAULT '',
        created_time    INTEGER,
        reaction_count  INTEGER,
        reply_count     INTEGER,
        url             TEXT,
        first_seen_at   INTEGER NOT NULL,
        last_seen_at    INTEGER NOT NULL
    );
    CREATE INDEX comments_post ON comments(post_id, created_time);
    CREATE INDEX comments_author ON comments(author_id);
    CREATE INDEX comments_created ON comments(created_time);

    CREATE TABLE keywords (
        id          INTEGER PRIMARY KEY,
        term        TEXT NOT NULL UNIQUE COLLATE NOCASE,
        variants    TEXT NOT NULL DEFAULT '[]',
        created_at  INTEGER NOT NULL
    );

    CREATE TABLE keyword_hits (
        keyword_id  INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
        comment_id  TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
        PRIMARY KEY (keyword_id, comment_id)
    );
    CREATE INDEX keyword_hits_comment ON keyword_hits(comment_id);

    CREATE TABLE jobs (
        id            INTEGER PRIMARY KEY,
        post_id       INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
        requested_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        agent_id      INTEGER REFERENCES agent_tokens(id) ON DELETE SET NULL,
        created_at    INTEGER NOT NULL,
        started_at    INTEGER,
        finished_at   INTEGER,
        heartbeat_at  INTEGER,
        progress      INTEGER,
        message       TEXT
    );
    CREATE INDEX jobs_status ON jobs(status, created_at);
    CREATE INDEX jobs_post ON jobs(post_id);

    CREATE TABLE audit_log (
        id       INTEGER PRIMARY KEY,
        at       INTEGER NOT NULL,
        user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
        actor    TEXT NOT NULL,
        action   TEXT NOT NULL,
        detail   TEXT
    );
    CREATE INDEX audit_log_at ON audit_log(at);
    """,
    # 2: sentiment (AI labels, staff corrections), summaries, API usage
    """
    CREATE TABLE comment_sentiment (
        comment_id      TEXT PRIMARY KEY REFERENCES comments(id) ON DELETE CASCADE,
        label           TEXT NOT NULL CHECK (label IN ('supportive', 'against', 'neutral', 'question')),
        reason          TEXT,
        model           TEXT NOT NULL,
        prompt_version  INTEGER NOT NULL,
        created_at      INTEGER NOT NULL
    );

    CREATE TABLE comment_corrections (
        comment_id    TEXT PRIMARY KEY REFERENCES comments(id) ON DELETE CASCADE,
        label         TEXT NOT NULL CHECK (label IN ('supportive', 'against', 'neutral', 'question')),
        user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
        corrected_at  INTEGER NOT NULL
    );

    -- Comments the model failed on; retried a few times, then left unlabeled.
    CREATE TABLE sentiment_failures (
        comment_id  TEXT PRIMARY KEY REFERENCES comments(id) ON DELETE CASCADE,
        attempts    INTEGER NOT NULL,
        last_error  TEXT,
        last_at     INTEGER NOT NULL
    );

    CREATE TABLE post_summaries (
        post_id        INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
        summary        TEXT NOT NULL,
        issues         TEXT NOT NULL DEFAULT '[]',
        labeled_count  INTEGER NOT NULL,
        -- label counts the summary was written from ("supportive,against,neutral,question")
        basis          TEXT NOT NULL,
        model          TEXT NOT NULL,
        created_at     INTEGER NOT NULL
    );

    CREATE TABLE briefs (
        key         TEXT PRIMARY KEY,
        text        TEXT NOT NULL,
        issues      TEXT NOT NULL DEFAULT '[]',
        basis       TEXT NOT NULL,
        model       TEXT NOT NULL,
        created_at  INTEGER NOT NULL
    );

    CREATE TABLE api_usage (
        id                  INTEGER PRIMARY KEY,
        at                  INTEGER NOT NULL,
        purpose             TEXT NOT NULL,
        model               TEXT NOT NULL,
        input_tokens        INTEGER NOT NULL,
        output_tokens       INTEGER NOT NULL,
        cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX api_usage_at ON api_usage(at);

    -- The label everyone sees: a staff correction wins over the AI's label.
    CREATE VIEW comment_labels AS
        SELECT c.id AS comment_id,
               COALESCE(k.label, s.label) AS label,
               k.label IS NOT NULL AS corrected
        FROM comments c
        LEFT JOIN comment_sentiment s ON s.comment_id = c.id
        LEFT JOIN comment_corrections k ON k.comment_id = c.id;
    """,
]


def now() -> int:
    return int(time.time())


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: autocommit, with explicit transactions where needed.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the schema version now in place."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS, 1):
        if number <= version:
            continue
        # executescript commits first, so wrap the script itself in a transaction.
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        version = number
    return version


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front, so read-then-write logic
    (claiming a job, merging posts) cannot interleave with another writer."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def audit(
    conn: sqlite3.Connection, actor: str, action: str, detail: str = "", user_id: int | None = None
) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, user_id, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
        (now(), user_id, actor, action, detail),
    )
