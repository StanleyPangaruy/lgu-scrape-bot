"""Admin commands.

    python -m server serve [--host 127.0.0.1] [--port 8000]
    python -m server create-user USERNAME --name "Full Name" --role admin
    python -m server import-csv comments_1CYuJh1ntq.csv [more.csv …]
    python -m server create-agent-token "Admin laptop"
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

from server.app import create_app, prepare_database
from server.auth import ROLES, AccountError, create_user
from server.config import Settings
from server.db import audit, connect, now, transaction
from server.ingest import import_csv
from server.security import new_token, token_hash

SHARE_CODE = re.compile(r"^comments_([A-Za-z0-9]+)$")


def _connect(settings: Settings):
    prepare_database(settings)
    return connect(settings.db_path)


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    prepare_database(settings)
    print(f"LGU Comment Monitor on http://{args.host}:{args.port}  (data: {settings.data_dir})")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True)
    return 0


def cmd_create_user(args: argparse.Namespace, settings: Settings) -> int:
    password = getpass.getpass("Password (10+ characters): ")
    if getpass.getpass("Password again: ") != password:
        print("The passwords do not match.")
        return 1
    conn = _connect(settings)
    try:
        with transaction(conn):
            create_user(conn, args.username, args.name, args.role, password)
            audit(conn, "command line", "created account", f"{args.username} ({args.role})")
    except AccountError as e:
        print(e)
        return 1
    finally:
        conn.close()
    print(f"Created {args.role} account “{args.username}”.")
    return 0


def link_for_csv(path: Path) -> str:
    """comments_<code>.csv was saved from https://www.facebook.com/share/p/<code>/."""
    match = SHARE_CODE.match(path.stem)
    if not match:
        raise ValueError(
            f"{path.name}: cannot tell which post this is from its name; pass --url with the post link"
        )
    return f"https://www.facebook.com/share/p/{match.group(1)}/"


def cmd_import_csv(args: argparse.Namespace, settings: Settings) -> int:
    if args.url and len(args.files) != 1:
        print("--url can only be used with a single file.")
        return 1
    conn = _connect(settings)
    failed = 0
    try:
        for path in args.files:
            try:
                link = args.url or link_for_csv(path)
                with transaction(conn):
                    result = import_csv(conn, path, link)
                    audit(conn, "command line", "imported CSV", f"{path.name}: {result.new} new comments")
            except (OSError, ValueError) as e:
                print(f"  skipped {path}: {e}")
                failed += 1
                continue
            extra = ""
            if result.dropped:
                extra += f", {result.dropped} from another post skipped"
            if result.merged_into:
                extra += " (same post as an earlier file; merged)"
            print(f"  {path.name}: {result.new} new, {result.updated} already stored{extra}")
    finally:
        conn.close()
    return 1 if failed else 0


def cmd_create_agent_token(args: argparse.Namespace, settings: Settings) -> int:
    token = new_token()
    conn = _connect(settings)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO agent_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
                (args.name, token_hash(token), now()),
            )
            audit(conn, "command line", "created agent token", args.name)
    finally:
        conn.close()
    print(f"Agent token for “{args.name}” (shown once, keep it private):\n\n{token}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server", description="LGU Comment Monitor admin commands")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the website")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    user = sub.add_parser("create-user", help="create an account (asks for the password)")
    user.add_argument("username")
    user.add_argument("--name", required=True, help="the person's name as shown in the site")
    user.add_argument("--role", required=True, choices=ROLES)
    user.set_defaults(func=cmd_create_user)

    imp = sub.add_parser("import-csv", help="import comments CSVs saved by main.py")
    imp.add_argument("files", nargs="+", type=Path)
    imp.add_argument("--url", help="the post link, when the file name is not comments_<code>.csv")
    imp.set_defaults(func=cmd_import_csv)

    tok = sub.add_parser("create-agent-token", help="create a token for the laptop agent")
    tok.add_argument("name", nargs="?", default="Admin laptop")
    tok.set_defaults(func=cmd_create_agent_token)

    args = parser.parse_args(argv)
    return args.func(args, Settings.from_env())


if __name__ == "__main__":
    sys.exit(main())
