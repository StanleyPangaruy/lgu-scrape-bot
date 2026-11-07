"""Size limits for uploaded fields, shared by the API (which enforces them) and
the agent (which trims to them, so one odd comment cannot fail a whole upload)."""

COMMENT_TEXT_LIMITS = {
    "id": 200,
    "legacy_fbid": 64,
    "parent_id": 200,
    "author_id": 64,
    "author_name": 300,
    "author_url": 2000,
    "text": 50_000,
    "url": 2000,
}

POST_TEXT_LIMITS = {
    "url": 2000,
    "post_id": 64,
    "page_id": 64,
    "page_name": 300,
    "page_url": 2000,
    "text": 100_000,
}

MAX_COMMENTS_PER_UPLOAD = 100_000
