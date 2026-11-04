"""The website's pages and forms."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server import jobs, queries
from server.auth import (
    LOGIN_CSRF_COOKIE,
    ROLE_LABELS,
    ROLES,
    SESSION_COOKIE,
    AccountError,
    User,
    attempt_login,
    check_csrf,
    create_user,
    current_user,
    end_session,
    get_db,
    get_settings,
    optional_user,
    require_admin,
    require_role,
    safe_next,
    set_password,
)
from server.ai import LABEL_NAMES, LABELS, usage_cost
from server.analysis import BRIEF_KEY, api_key
from server.config import Settings
from server.db import audit, now, transaction
from server.excel import XLSX_MEDIA_TYPE, add_sheet, from_timestamp, new_workbook, to_bytes
from server.ingest import InvalidLink, clean_post_link, create_post, find_post_by_link, safe_web_url
from server.keywords import Matcher, load_keywords, parse_variants, refresh_hits
from server.security import new_token, token_hash, tokens_equal, verify_password

router = APIRouter()

Db = Annotated[sqlite3.Connection, Depends(get_db)]
AnyUser = Annotated[User, Depends(current_user)]
Admin = Annotated[User, Depends(require_admin)]
KeywordEditor = Annotated[User, Depends(require_role("admin", "staff"))]
CsrfField = Annotated[str, Form()]

PAGE_SIZE = 100
MAX_SHOW = 100_000

NOTICES = {
    "queued": "Added to the queue. The agent on the admin laptop will collect it.",
    "untitled-queued": "Posts without details were added to the queue. Their titles fill in as the agent collects them.",
    "already-queued": "That post is already waiting in the queue.",
    "recheck-queued": "Re-check added to the queue.",
    "retried": "Job queued again.",
    "cancelled": "Job cancelled.",
    "keyword-added": "Keyword added. Existing comments were checked for it.",
    "keyword-saved": "Keyword saved. Existing comments were re-checked.",
    "keyword-deleted": "Keyword deleted.",
    "user-created": "Account created.",
    "password-reset": "Password changed. That person was signed out everywhere.",
    "user-updated": "Account updated.",
    "token-revoked": "Agent token revoked. That agent can no longer upload.",
    "label-saved": "Label saved.",
    "label-cleared": "Correction removed; the AI's label is shown again.",
    "reanalyze": "All comments will be analyzed again. Staff corrections were kept.",
    "password-changed": "Password changed. Sign in with your new password.",
}


def templates(request: Request):
    return request.app.state.templates


SECTIONS = {
    "posts.html": "posts", "post.html": "posts", "people.html": "people",
    "keywords.html": "keywords", "jobs.html": "jobs", "settings.html": "settings", "author.html": "people",
    "overview.html": "overview",
    "token.html": "settings", "account.html": "account",
}


def render(request: Request, template: str, user: User | None, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    notice = NOTICES.get(request.query_params.get("notice", ""))
    return templates(request).TemplateResponse(
        request, template,
        {"user": user, "notice": notice, "role_labels": ROLE_LABELS, "section": SECTIONS.get(template, ""), **ctx},
        status_code=status_code,
    )


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def xlsx_response(data: bytes, filename: str) -> Response:
    return Response(
        data,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


LEAN_NAMES = {**LABEL_NAMES, "mixed": "Mixed"}


def label_text(row: Any) -> str:
    """A comment's sentiment for exports: the label, or why there is none yet."""
    return LABEL_NAMES.get(row["label"] or "", "Not analyzed")


def optional_id(value: str) -> int | None:
    """A numeric id from a filter in the address bar. Forms send "" for "Any",
    and a hand-edited URL may hold anything, so both simply mean no filter."""
    value = value.strip()
    return int(value) if value.isdigit() else None


def stamp(settings: Settings) -> str:
    return datetime.now(settings.tz).strftime("%Y-%m-%d")


# --- Sign in / out -----------------------------------------------------------


@router.get("/", include_in_schema=False)
def home(user: AnyUser) -> RedirectResponse:
    return redirect("/overview")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: Annotated[User | None, Depends(optional_user)], next: str = "/") -> Response:
    if user is not None:
        return redirect(safe_next(next))
    return _login_form(request, next, None)


def _login_form(request: Request, next_path: str, error: str | None, status_code: int = 200) -> Response:
    token = new_token()
    response = render(
        request, "login.html", None, status_code=status_code,
        login_csrf=token, next=safe_next(next_path), error=error,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE, token, httponly=True, samesite="strict",
        secure=get_settings(request).secure_cookies, max_age=3600,
    )
    return response


@router.post("/login")
def login(
    request: Request,
    conn: Db,
    username: Annotated[str, Form(max_length=100)],
    password: Annotated[str, Form(max_length=200)],
    csrf_token: CsrfField,
    next: Annotated[str, Form()] = "/",
) -> Response:
    cookie = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not cookie or not tokens_equal(cookie, csrf_token):
        return _login_form(request, next, "The sign-in form expired. Please try again.", 400)
    settings = get_settings(request)
    outcome = attempt_login(conn, settings, username, password)
    if outcome.session_token is None:
        return _login_form(request, next, outcome.error, 401)
    response = redirect(safe_next(next))
    response.set_cookie(
        SESSION_COOKIE, outcome.session_token, httponly=True, samesite="lax",
        secure=settings.secure_cookies, max_age=settings.session_days * 86400,
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE)
    return response


@router.post("/logout")
def logout(request: Request, conn: Db, user: AnyUser, csrf_token: CsrfField) -> Response:
    check_csrf(user, csrf_token)
    end_session(conn, request.cookies.get(SESSION_COOKIE))
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- Posts --------------------------------------------------------------------


def _posts_context(conn: sqlite3.Connection, user: User, q: str, sort: str) -> dict[str, Any]:
    sort = sort if sort in queries.POST_SORTS else queries.DEFAULT_POST_SORT
    seen = user.posts_seen_at or 0
    posts = [
        {
            "row": r,
            "title": queries.post_title(r["text"], r["page_name"], r["source_url"], r["stored_comments"]),
            "has_details": queries.has_details(r),
            "is_new": max(r["last_new_at"] or 0, r["added_at"]) > seen and r["last_checked_at"] is not None,
        }
        for r in queries.list_posts(conn, q.strip(), sort)
    ]
    return {
        "posts": posts, "active": jobs.active_jobs(conn), "totals": queries.totals(conn),
        "q": q, "sort": sort, "sorts": queries.POST_SORTS,
    }


@router.get("/posts", response_class=HTMLResponse)
def posts_page(
    request: Request, conn: Db, user: AnyUser,
    q: Annotated[str, Query(max_length=200)] = "", sort: Annotated[str, Query(max_length=20)] = "",
) -> Response:
    context = _posts_context(conn, user, q, sort)
    conn.execute("UPDATE users SET posts_seen_at = ? WHERE id = ?", (now(), user.id))
    return render(request, "posts.html", user, **context)


@router.post("/posts/recheck-untitled")
def recheck_untitled(conn: Db, user: Admin, csrf_token: CsrfField) -> Response:
    """Queue every post whose details are not known yet (imported, or never collected)."""
    check_csrf(user, csrf_token)
    with transaction(conn):
        ids = [r[0] for r in conn.execute("SELECT id FROM posts WHERE text IS NULL AND page_name IS NULL")]
        queued = sum(1 for post_id in ids if jobs.enqueue(conn, post_id, user.id)[1])
        audit(conn, user.username, "re-check requested", f"{queued} posts without details", user.id)
    return redirect("/posts?notice=" + ("untitled-queued" if queued else "already-queued"))


@router.post("/posts")
def add_post(request: Request, conn: Db, user: Admin, csrf_token: CsrfField, link: Annotated[str, Form(max_length=2000)]) -> Response:
    check_csrf(user, csrf_token)
    try:
        clean = clean_post_link(link)
    except InvalidLink as e:
        return render(
            request, "posts.html", user, status_code=400, link_error=str(e), link_value=link,
            **_posts_context(conn, user, "", ""),
        )
    with transaction(conn):
        existing = find_post_by_link(conn, clean)
        post_id = existing["id"] if existing else create_post(conn, clean, user.id)
        _, created = jobs.enqueue(conn, post_id, user.id)
        audit(conn, user.username, "added post" if not existing else "re-check requested", clean, user.id)
    return redirect("/posts?notice=" + ("queued" if created else "already-queued"))


@router.post("/posts/{post_id}/recheck")
def recheck(conn: Db, user: Admin, post_id: int, csrf_token: CsrfField) -> Response:
    check_csrf(user, csrf_token)
    with transaction(conn):
        if conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No such post.")
        _, created = jobs.enqueue(conn, post_id, user.id)
        audit(conn, user.username, "re-check requested", f"post {post_id}", user.id)
    return redirect(f"/posts/{post_id}?notice=" + ("recheck-queued" if created else "already-queued"))


def _load_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    post = queries.get_post(conn, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="No such post.")
    return post


@router.get("/posts/export.xlsx")
def export_posts(request: Request, conn: Db, user: AnyUser) -> Response:
    settings = get_settings(request)
    wb = new_workbook()
    add_sheet(
        wb, "Posts",
        ["Post", "Page", "Posted", "Comments stored", "Comments on Facebook", "Last checked", "Link"],
        (
            [queries.post_title(r["text"], r["page_name"], r["source_url"]), r["page_name"], from_timestamp(r["created_time"]),
             r["stored_comments"], r["fb_comment_count"], from_timestamp(r["last_checked_at"]), r["url"]]
            for r in queries.list_posts(conn)
        ),
        settings.tz, widths=[60, 30, 18, 16, 20, 18, 60],
    )
    return xlsx_response(to_bytes(wb), f"posts-{stamp(settings)}.xlsx")


@router.get("/posts/{post_id}", response_class=HTMLResponse)
def post_page(
    request: Request,
    conn: Db,
    user: AnyUser,
    post_id: int,
    q: Annotated[str, Query(max_length=200)] = "",
    keyword: Annotated[str, Query(max_length=20)] = "",
    hits: bool = False,
    order: Annotated[str, Query(max_length=20)] = "",
    show: Annotated[str, Query(max_length=10)] = "",
    sentiment: Annotated[str, Query(max_length=20)] = "",
) -> Response:
    post = _load_post(conn, post_id)
    sentiment = sentiment if sentiment in (*LABELS, "none") else ""
    keywords = load_keywords(conn)
    by_id = {k.id: k for k in keywords}
    keyword_id = optional_id(keyword)
    if keyword_id not in by_id:
        keyword_id = None
    order = order if order in queries.COMMENT_ORDERS else queries.DEFAULT_COMMENT_ORDER
    comments, threaded = queries.post_comments(conn, post_id, q.strip(), keyword_id, hits, order, sentiment)

    # Threads (or matches) are shown PAGE_SIZE at a time; "Show more" raises `show`.
    limit = min(max(optional_id(show) or PAGE_SIZE, PAGE_SIZE), MAX_SHOW)
    filters = {"q": q.strip(), "keyword": keyword_id or "", "hits": "true" if hits else "", "order": order,
               "sentiment": sentiment}

    def link(**changes: Any) -> str:
        params = {k: v for k, v in {**filters, **changes}.items() if v not in ("", None)}
        if params.get("order") == queries.DEFAULT_COMMENT_ORDER:
            params.pop("order")
        return f"/posts/{post_id}" + ("?" + urlencode(params) if params else "")

    return render(
        request, "post.html", user,
        post=post, title=queries.post_title(post["text"], post["page_name"], post["source_url"]),
        has_details=queries.has_details(post),
        post_link=safe_web_url(post["url"]), page_link=safe_web_url(post["page_url"]),
        comments=comments[:limit], total_shown=len(comments), limit=limit,
        more_link=link(show=limit + PAGE_SIZE) + f"#more-{limit}" if len(comments) > limit else None,
        threaded=threaded, matcher=Matcher(keywords), keywords=by_id,
        topics=queries.post_topics(conn, post_id, by_id), link=link,
        q=q.strip(), keyword=keyword_id, hits=hits, order=order, orders=queries.COMMENT_ORDERS,
        filtered=bool(q.strip() or keyword_id or hits or sentiment), sentiment=sentiment,
        counts=queries.sentiment_counts(post), summary_issues=json.loads(post["summary_issues"] or "[]"),
        job=jobs.active_job_for_post(conn, post_id),
    )


@router.get("/posts/{post_id}/export.xlsx")
def export_post(request: Request, conn: Db, user: AnyUser, post_id: int) -> Response:
    settings = get_settings(request)
    post = _load_post(conn, post_id)
    keywords = {k.id: k.term for k in load_keywords(conn)}
    hits = queries.keywords_by_comment(conn, post_id)
    rows = conn.execute(
        f"SELECT c.*, {queries.COMMENT_COLUMNS} FROM comments c JOIN posts p ON p.id = c.post_id "
        f"{queries.COMMENT_JOINS} WHERE c.post_id = ? ORDER BY COALESCE(c.created_time, 0), c.id",
        (post_id,),
    ).fetchall()
    wb = new_workbook()
    add_sheet(
        wb, "Comments",
        ["Posted", "Name", "Comment", "Reply?", "Reactions", "Replies", "Sentiment", "Why", "Keywords",
         "Profile", "Comment link"],
        (
            [from_timestamp(r["created_time"]), r["author_name"], r["text"],
             "Reply" if r["parent_id"] else "", r["reaction_count"], r["reply_count"],
             label_text(r), r["reason"] if not r["corrected"] else "Corrected by staff",
             ", ".join(sorted(keywords[k] for k in hits.get(r["id"], []) if k in keywords)),
             r["author_url"], queries.comment_link(r)]
            for r in rows
        ),
        settings.tz, widths=[18, 28, 80, 8, 11, 9, 12, 40, 24, 40, 40],
    )
    return xlsx_response(to_bytes(wb), f"post-{post['id']}-comments-{stamp(settings)}.xlsx")


# --- People ---------------------------------------------------------------------


@router.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request, conn: Db, user: AnyUser,
    period: str | None = None, page: Annotated[str | None, Query(max_length=300)] = None,
) -> Response:
    filters = queries.Filters.from_query(period, page)
    return render(
        request, "people.html", user, filters=filters, periods=queries.PERIODS,
        pages=queries.page_names(conn),
        commenters=[
            {"row": r, "lean": queries.lean(queries.sentiment_counts(r))}
            for r in queries.top_commenters(conn, filters)
        ],
        reacted=queries.most_reacted(conn, filters), post_title=queries.post_title,
    )


@router.get("/people/export.xlsx")
def export_people(
    request: Request, conn: Db, user: AnyUser,
    period: str | None = None, page: Annotated[str | None, Query(max_length=300)] = None,
) -> Response:
    settings = get_settings(request)
    filters = queries.Filters.from_query(period, page)
    wb = new_workbook()
    add_sheet(
        wb, "Most active",
        ["Name", "Comments", "Posts commented on", "Reactions received", "Usually", "Profile"],
        ([r["name"], r["comments"], r["posts"], r["reactions"],
          LEAN_NAMES.get(queries.lean(queries.sentiment_counts(r)), ""), r["url"]]
         for r in queries.top_commenters(conn, filters, limit=500)),
        settings.tz, widths=[32, 11, 18, 18, 12, 50],
    )
    add_sheet(
        wb, "Most reacted",
        ["Reactions", "Name", "Comment", "Sentiment", "Posted", "On post", "Page", "Comment link"],
        ([r["reaction_count"], r["author_name"], r["text"], label_text(r), from_timestamp(r["created_time"]),
          queries.post_title(r["post_text"], r["post_page"], r["post_link"]), r["post_page"], queries.comment_link(r)]
         for r in queries.most_reacted(conn, filters, limit=500)),
        settings.tz, widths=[11, 28, 80, 12, 18, 50, 30, 40],
    )
    return xlsx_response(to_bytes(wb), f"people-{stamp(settings)}.xlsx")


def _load_author(conn: sqlite3.Connection, id: str, name: str) -> tuple[queries.Author, sqlite3.Row]:
    author = queries.Author.from_query(id, name)
    summary = queries.author_summary(conn, author) if author else None
    if author is None or summary is None:
        raise HTTPException(status_code=404, detail="No comments from this person are stored.")
    return author, summary


@router.get("/people/author", response_class=HTMLResponse)
def author_page(
    request: Request, conn: Db, user: AnyUser,
    id: Annotated[str, Query(max_length=64)] = "", name: Annotated[str, Query(max_length=300)] = "",
) -> Response:
    author, summary = _load_author(conn, id, name)
    keywords = load_keywords(conn)
    comments = [
        {"row": r, "keyword_ids": [int(k) for k in (r["keyword_ids"] or "").split(",") if k]}
        for r in queries.author_comments(conn, author)
    ]
    export_query = urlencode({"id": author.author_id} if author.author_id else {"name": author.name})
    return render(
        request, "author.html", user, summary=summary, comments=comments,
        profile_link=safe_web_url(summary["url"]), matcher=Matcher(keywords),
        keywords={k.id: k for k in keywords}, post_title=queries.post_title, export_query=export_query,
        counts=queries.sentiment_counts(summary),
    )


@router.get("/people/author/export.xlsx")
def export_author(
    request: Request, conn: Db, user: AnyUser,
    id: Annotated[str, Query(max_length=64)] = "", name: Annotated[str, Query(max_length=300)] = "",
) -> Response:
    settings = get_settings(request)
    author, summary = _load_author(conn, id, name)
    terms = {k.id: k.term for k in load_keywords(conn)}
    wb = new_workbook()
    add_sheet(
        wb, "Comments",
        ["Posted", "Comment", "Reply?", "Reactions", "Sentiment", "Keywords", "On post", "Page", "Comment link"],
        (
            [from_timestamp(r["created_time"]), r["text"], "Reply" if r["parent_id"] else "",
             r["reaction_count"], label_text(r),
             ", ".join(sorted(terms[int(k)] for k in (r["keyword_ids"] or "").split(",") if k and int(k) in terms)),
             queries.post_title(r["post_text"], r["post_page"], r["post_link"]), r["post_page"], queries.comment_link(r)]
            for r in queries.author_comments(conn, author)
        ),
        settings.tz, widths=[18, 80, 8, 11, 12, 24, 50, 30, 40],
    )
    # HTTP headers only carry plain ASCII safely, so the file name uses letters and digits only.
    slug = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in (summary["name"] or ""))
    slug = "-".join(part for part in slug.split("-") if part)[:40]
    return xlsx_response(to_bytes(wb), f"comments-{slug or 'commenter'}-{stamp(settings)}.xlsx")


# --- Sentiment corrections --------------------------------------------------------


@router.post("/comments/{rid}/label")
def correct_label(
    conn: Db, user: Annotated[User, Depends(require_role("admin", "staff"))], rid: int, csrf_token: CsrfField,
    label: Annotated[str, Form(max_length=20)] = "", next: Annotated[str, Form(max_length=2000)] = "/posts",
) -> Response:
    """Staff set the right label; an empty label removes the correction (back to the AI's)."""
    check_csrf(user, csrf_token)
    if label and label not in LABELS:
        raise HTTPException(status_code=400, detail="Choose one of the labels.")
    with transaction(conn):
        row = conn.execute("SELECT id, post_id FROM comments WHERE rowid = ?", (rid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such comment.")
        if label:
            conn.execute(
                "INSERT OR REPLACE INTO comment_corrections (comment_id, label, user_id, corrected_at) "
                "VALUES (?, ?, ?, ?)",
                (row["id"], label, user.id, now()),
            )
            audit(conn, user.username, "corrected label", f"post {row['post_id']}, comment {rid}: {label}", user.id)
        else:
            conn.execute("DELETE FROM comment_corrections WHERE comment_id = ?", (row["id"],))
            audit(conn, user.username, "removed label correction", f"post {row['post_id']}, comment {rid}", user.id)
    target = safe_next(next).split("#")[0]
    joiner = "&" if "?" in target else "?"
    return redirect(f"{target}{joiner}notice={'label-saved' if label else 'label-cleared'}#c{rid}")


# --- Overview ---------------------------------------------------------------------

OVERVIEW_PERIODS = {"30": ("Last 30 days", 30), "7": ("Last 7 days", 7), "all": ("All time", None)}
WEEKS_SHOWN = 8


@router.get("/overview", response_class=HTMLResponse)
def overview_page(
    request: Request, conn: Db, user: AnyUser,
    period: str | None = None, page: Annotated[str | None, Query(max_length=300)] = None,
) -> Response:
    settings = get_settings(request)
    period = period if period in OVERVIEW_PERIODS else "30"
    page = (page or "").strip()
    days = OVERVIEW_PERIODS[period][1]
    start = now() - days * 86400 if days else None
    current = queries.window_counts(conn, start, None, page)
    previous = queries.window_counts(conn, start - days * 86400, start, page) if days else None

    # Weeks start on Monday, Philippine time.
    today = datetime.now(settings.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today.timestamp() - today.weekday() * 86400
    week_starts = [int(monday - 7 * 86400 * i) for i in range(WEEKS_SHOWN - 1, -1, -1)]
    weeks = queries.weekly_sentiment(conn, week_starts, page)
    for w in weeks:
        day = datetime.fromtimestamp(w["start"], settings.tz)
        w["label"] = f"{day:%b} {day.day}"

    keywords = load_keywords(conn)
    counts = queries.keyword_counts(conn, keywords, queries.Filters.from_query(period, page))
    issues = sorted(((k, counts[k.id]) for k in keywords if counts[k.id]), key=lambda kn: -kn[1])[:6]
    brief = queries.latest_brief(conn, BRIEF_KEY)

    return render(
        request, "overview.html", user, period=period, periods=OVERVIEW_PERIODS, page=page,
        pages=queries.page_names(conn), current=current, previous=previous, share=queries.share,
        weeks=weeks, week_max=max((w["total"] for w in weeks), default=0),
        issues=issues, issue_max=max((n for _, n in issues), default=0),
        attention=queries.posts_needing_attention(conn, start, page),
        supporters=queries.top_supportive(conn, start, page),
        brief=brief, brief_issues=json.loads(brief["issues"]) if brief else [],
        matcher=Matcher(keywords), post_title=queries.post_title,
    )


# --- Keywords -------------------------------------------------------------------


@router.get("/keywords", response_class=HTMLResponse)
def keywords_page(
    request: Request, conn: Db, user: AnyUser,
    period: str | None = None, page: Annotated[str | None, Query(max_length=300)] = None,
    k: Annotated[str, Query(max_length=20)] = "", error: str | None = None,
) -> Response:
    filters = queries.Filters.from_query(period, page)
    keywords = load_keywords(conn)
    counts = queries.keyword_counts(conn, keywords, filters)
    ranked = sorted(keywords, key=lambda kw: (-counts[kw.id], kw.term.lower()))
    wanted = optional_id(k)
    selected = next((kw for kw in keywords if kw.id == wanted), None) or (ranked[0] if ranked else None)
    return render(
        request, "keywords.html", user, filters=filters, periods=queries.PERIODS,
        pages=queries.page_names(conn), keywords=ranked, counts=counts,
        top=max(counts.values(), default=0), selected=selected,
        matches=queries.keyword_comments(conn, selected.id, filters) if selected else [],
        matcher=Matcher([selected]) if selected else None, post_title=queries.post_title,
        error=KEYWORD_ERRORS.get(error or ""),
    )


KEYWORD_ERRORS = {
    "empty": "Type the keyword.",
    "duplicate": "That keyword is already in the list.",
}


def _keyword_redirect(notice: str, keyword_id: int | None = None) -> RedirectResponse:
    return redirect(f"/keywords?notice={notice}" + (f"&k={keyword_id}" if keyword_id else ""))


def _keyword_error(code: str) -> RedirectResponse:
    return redirect(f"/keywords?error={code}")


@router.post("/keywords")
def add_keyword(
    conn: Db, user: KeywordEditor, csrf_token: CsrfField,
    term: Annotated[str, Form(max_length=100)], variants: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    check_csrf(user, csrf_token)
    term = " ".join(term.split())
    if not term or not parse_variants(term):
        return _keyword_error("empty")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM keywords WHERE term = ?", (term,)).fetchone():
            return _keyword_error("duplicate")
        cur = conn.execute(
            "INSERT INTO keywords (term, variants, created_at) VALUES (?, ?, ?)",
            (term, json.dumps(parse_variants(variants), ensure_ascii=False), now()),
        )
        refresh_hits(conn)
        audit(conn, user.username, "added keyword", term, user.id)
    return _keyword_redirect("keyword-added", int(cur.lastrowid))


@router.post("/keywords/{keyword_id}")
def edit_keyword(
    conn: Db, user: KeywordEditor, keyword_id: int, csrf_token: CsrfField,
    variants: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    check_csrf(user, csrf_token)
    with transaction(conn):
        row = conn.execute("SELECT term FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such keyword.")
        conn.execute(
            "UPDATE keywords SET variants = ? WHERE id = ?",
            (json.dumps(parse_variants(variants), ensure_ascii=False), keyword_id),
        )
        refresh_hits(conn)
        audit(conn, user.username, "edited keyword", row["term"], user.id)
    return _keyword_redirect("keyword-saved", keyword_id)


@router.post("/keywords/{keyword_id}/delete")
def delete_keyword(conn: Db, user: KeywordEditor, keyword_id: int, csrf_token: CsrfField) -> Response:
    check_csrf(user, csrf_token)
    with transaction(conn):
        row = conn.execute("SELECT term FROM keywords WHERE id = ?", (keyword_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such keyword.")
        conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
        audit(conn, user.username, "deleted keyword", row["term"], user.id)
    return _keyword_redirect("keyword-deleted")


# --- Jobs (admin) -------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, conn: Db, user: Admin) -> Response:
    rows = conn.execute(
        """
        SELECT j.*, p.text, p.page_name, p.source_url, u.display_name AS requester
        FROM jobs j JOIN posts p ON p.id = j.post_id LEFT JOIN users u ON u.id = j.requested_by
        ORDER BY j.status IN ('queued', 'running') DESC, j.id DESC LIMIT 100
        """
    ).fetchall()
    return render(
        request, "jobs.html", user, jobs=rows, agent=jobs.agent_status(conn),
        post_title=queries.post_title,
    )


@router.get("/jobs/status.json")
def jobs_status(conn: Db, user: AnyUser) -> JSONResponse:
    active = jobs.active_jobs(conn)
    agent = jobs.agent_status(conn)
    return JSONResponse({
        "agent_online": agent.online,
        "active": [
            {
                "id": r["id"], "post_id": r["post_id"], "status": r["status"],
                "progress": r["progress"], "title": queries.post_title(r["text"], r["page_name"], r["source_url"]),
            }
            for r in active
        ],
    })


@router.post("/jobs/{job_id}/retry")
def retry_job(conn: Db, user: Admin, job_id: int, csrf_token: CsrfField) -> Response:
    check_csrf(user, csrf_token)
    with transaction(conn):
        try:
            jobs.retry(conn, job_id, user.id)
        except jobs.JobError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        audit(conn, user.username, "retried job", f"job {job_id}", user.id)
    return redirect("/jobs?notice=retried")


@router.post("/jobs/{job_id}/cancel")
def cancel_job(conn: Db, user: Admin, job_id: int, csrf_token: CsrfField) -> Response:
    check_csrf(user, csrf_token)
    with transaction(conn):
        try:
            jobs.cancel(conn, job_id)
        except jobs.JobError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        audit(conn, user.username, "cancelled job", f"job {job_id}", user.id)
    return redirect("/jobs?notice=cancelled")


# --- Settings (admin) --------------------------------------------------------------


def _settings_context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "users": conn.execute("SELECT * FROM users ORDER BY active DESC, role, display_name").fetchall(),
        "tokens": conn.execute("SELECT * FROM agent_tokens ORDER BY revoked, id DESC").fetchall(),
        "log": conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50").fetchall(),
        "roles": ROLES,
        "now": now(),
        **_ai_context(request, conn),
    }


def _ai_context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    settings = get_settings(request)
    month_start = datetime.now(settings.tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    usage = queries.usage_since(conn, int(month_start.timestamp()))
    worker = request.app.state.worker
    return {
        "ai_model": settings.sentiment_model,
        "ai_effort": settings.sentiment_effort,
        "ai_key_set": api_key() is not None,
        "ai_worker": worker.status if worker is not None else None,
        "ai_progress": queries.analysis_progress(conn),
        "ai_usage": usage,
        "ai_cost": sum(
            usage_cost(u["model"], u["input_tokens"] or 0, u["output_tokens"] or 0,
                       u["cache_read"] or 0, u["cache_write"] or 0) for u in usage
        ),
        "ai_month": f"{month_start:%B} {month_start.year}",
    }


@router.post("/settings/ai/reanalyze")
def reanalyze(request: Request, conn: Db, user: Admin, csrf_token: CsrfField) -> Response:
    """Forget the AI's labels and summaries (after changing the model, say). Corrections stay."""
    check_csrf(user, csrf_token)
    with transaction(conn):
        conn.execute("DELETE FROM comment_sentiment")
        conn.execute("DELETE FROM sentiment_failures")
        conn.execute("DELETE FROM post_summaries")
        conn.execute("DELETE FROM briefs")
        audit(conn, user.username, "re-analyze all comments", "", user.id)
    worker = request.app.state.worker
    if worker is not None:
        worker.wake.set()
    return redirect("/settings?notice=reanalyze")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: Db, user: Admin) -> Response:
    return render(request, "settings.html", user, **_settings_context(request, conn))


@router.post("/settings/users")
def add_user(
    request: Request, conn: Db, user: Admin, csrf_token: CsrfField,
    username: Annotated[str, Form(max_length=100)], display_name: Annotated[str, Form(max_length=200)],
    role: Annotated[str, Form(max_length=20)], password: Annotated[str, Form(max_length=200)],
) -> Response:
    check_csrf(user, csrf_token)
    try:
        with transaction(conn):
            create_user(conn, username, display_name, role, password)
            audit(conn, user.username, "created account", f"{username} ({role})", user.id)
    except AccountError as e:
        return render(
            request, "settings.html", user, status_code=400, user_error=str(e),
            form={"username": username, "display_name": display_name, "role": role},
            **_settings_context(request, conn),
        )
    return redirect("/settings?notice=user-created")


def _other_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such account.")
    return row


@router.post("/settings/users/{user_id}/password")
def reset_password(
    request: Request, conn: Db, user: Admin, user_id: int, csrf_token: CsrfField,
    password: Annotated[str, Form(max_length=200)],
) -> Response:
    check_csrf(user, csrf_token)
    target = _other_user(conn, user_id)
    try:
        with transaction(conn):
            set_password(conn, user_id, password)
            audit(conn, user.username, "reset password", target["username"], user.id)
    except AccountError as e:
        return render(
            request, "settings.html", user, status_code=400, reset_error=str(e),
            reset_for=user_id, **_settings_context(request, conn),
        )
    if user_id == user.id:
        return redirect("/login?notice=password-changed")
    return redirect("/settings?notice=password-reset")


@router.post("/settings/users/{user_id}/active")
def set_active(
    conn: Db, user: Admin, user_id: int, csrf_token: CsrfField, active: Annotated[int, Form()],
) -> Response:
    check_csrf(user, csrf_token)
    target = _other_user(conn, user_id)
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot turn off your own account.")
    with transaction(conn):
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        audit(conn, user.username, "enabled account" if active else "disabled account", target["username"], user.id)
    return redirect("/settings?notice=user-updated")


@router.post("/settings/agents", response_class=HTMLResponse)
def add_agent_token(
    request: Request, conn: Db, user: Admin, csrf_token: CsrfField,
    name: Annotated[str, Form(max_length=80)],
) -> Response:
    check_csrf(user, csrf_token)
    name = " ".join(name.split()) or "Admin laptop"
    token = new_token()
    with transaction(conn):
        conn.execute(
            "INSERT INTO agent_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
            (name, token_hash(token), now()),
        )
        audit(conn, user.username, "created agent token", name, user.id)
    # Shown once: only its hash is stored. no-store keeps it out of the browser cache.
    response = render(request, "token.html", user, name=name, token=token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/settings/agents/{token_id}/revoke")
def revoke_agent_token(conn: Db, user: Admin, token_id: int, csrf_token: CsrfField) -> Response:
    check_csrf(user, csrf_token)
    with transaction(conn):
        row = conn.execute("SELECT name FROM agent_tokens WHERE id = ?", (token_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such token.")
        conn.execute("UPDATE agent_tokens SET revoked = 1 WHERE id = ?", (token_id,))
        audit(conn, user.username, "revoked agent token", row["name"], user.id)
    return redirect("/settings?notice=token-revoked")


# --- Own account -------------------------------------------------------------------


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: AnyUser) -> Response:
    return render(request, "account.html", user)


@router.post("/account/password")
def change_own_password(
    request: Request, conn: Db, user: AnyUser, csrf_token: CsrfField,
    current: Annotated[str, Form(max_length=200)], new: Annotated[str, Form(max_length=200)],
    confirm: Annotated[str, Form(max_length=200)],
) -> Response:
    check_csrf(user, csrf_token)
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    error = None
    if not verify_password(current, row["password_hash"]):
        error = "Your current password is wrong."
    elif new != confirm:
        error = "The two new passwords do not match."
    else:
        try:
            with transaction(conn):
                set_password(conn, user.id, new)
                audit(conn, user.username, "changed own password", "", user.id)
        except AccountError as e:
            error = str(e)
    if error:
        return render(request, "account.html", user, status_code=400, error=error)
    response = redirect("/login?notice=password-changed")
    response.delete_cookie(SESSION_COOKIE)
    return response
