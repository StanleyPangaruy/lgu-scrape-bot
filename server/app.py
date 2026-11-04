"""Builds the FastAPI application."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from server import api, web
from server.auth import LoginRequired
from server.config import Settings
from server.db import connect, migrate, now
from server.keywords import DEFAULT_KEYWORDS, parse_variants
from server.queries import author_href, comment_link

HERE = Path(__file__).parent
MAX_API_BODY_BYTES = 50 * 1024 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


def prepare_database(settings: Settings) -> None:
    """Create or upgrade the database; a brand-new one gets the starting keyword list."""
    conn = connect(settings.db_path)
    try:
        fresh = conn.execute("PRAGMA user_version").fetchone()[0] == 0
        migrate(conn)
        if fresh:
            conn.executemany(
                "INSERT INTO keywords (term, variants, created_at) VALUES (?, ?, ?)",
                [
                    (term, json.dumps(parse_variants(",".join(variants)), ensure_ascii=False), now())
                    for term, variants in DEFAULT_KEYWORDS
                ],
            )
    finally:
        conn.close()


def make_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    tz = settings.tz

    def local(ts: int | None) -> datetime | None:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz) if ts else None

    # Built by hand: strftime's no-padding flags differ between Windows and Linux.
    def fmt_time(ts: int | None) -> str:
        dt = local(ts)
        if dt is None:
            return "—"
        meridiem = "AM" if dt.hour < 12 else "PM"
        return f"{dt:%b} {dt.day}, {dt.year}, {dt.hour % 12 or 12}:{dt:%M} {meridiem}"

    def fmt_date(ts: int | None) -> str:
        dt = local(ts)
        return f"{dt:%b} {dt.day}, {dt.year}" if dt else "—"

    def ago(ts: int | None) -> str:
        if not ts:
            return "never"
        seconds = max(0, now() - ts)
        if seconds < 60:
            return "just now"
        for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
            if seconds >= size:
                n = seconds // size
                return f"{n} {unit}{'' if n == 1 else 's'} ago"
        return "just now"

    def static_url(path: str) -> str:
        """/static/<path>?v=<when it last changed>: browsers fetch the new file
        after an update instead of running a cached old copy."""
        try:
            version = int((HERE / "static" / path).stat().st_mtime)
        except OSError:
            version = 0
        return f"/static/{path}?v={version}"

    templates.env.filters.update(fmt_time=fmt_time, fmt_date=fmt_date, ago=ago)
    templates.env.globals.update(author_href=author_href, comment_link=comment_link, static_url=static_url)
    return templates


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        prepare_database(settings)
        yield

    app = FastAPI(
        title="LGU Comment Monitor", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.settings = settings
    app.state.templates = make_templates(settings)

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        if request.url.path.startswith("/api/"):
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_API_BODY_BYTES:
                return JSONResponse({"detail": "Upload too large."}, status_code=413)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login?next=" + quote(exc.next_path, safe="/"), status_code=303)

    def wants_json(request: Request) -> bool:
        return request.url.path.startswith("/api/") or request.url.path.endswith(".json")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = exc.detail if exc.status_code != 404 or exc.detail != "Not Found" else "Page not found."
        return app.state.templates.TemplateResponse(
            request, "error.html", {"user": None, "message": message, "status": exc.status_code},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        if wants_json(request):
            errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
            return JSONResponse({"detail": "Invalid request.", "errors": errors}, status_code=422)
        return app.state.templates.TemplateResponse(
            request, "error.html",
            {"user": None, "message": "Some of what was sent was not valid. Go back and check the form.", "status": 400},
            status_code=400,
        )

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.include_router(web.router)
    app.include_router(api.router)
    return app
