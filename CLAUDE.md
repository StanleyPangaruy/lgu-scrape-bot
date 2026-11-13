# CLAUDE.md

## Project Overview
Automated Facebook comment extraction tool using Python and Playwright. Designed to handle Facebook's dynamic SPA architecture, infinite scrolling comment trees, session persistence, and anti-bot mitigations.

## Tech Stack & Core Libraries
- **Language**: Python 3.11+
- **Automation Framework**: `playwright` (Chromium engine)
- **Session / Storage**: Persistent browser profile in `./sessions/` (override with `--session-dir`), `json` / `sqlite3` for comment exports
- **Async Runtime**: Standard library `asyncio`

## Setup

```bash
# Install dependencies
pip install -e .
python -m playwright install chromium   # `python -m` works even if Python's Scripts dir isn't on PATH

# Alternative via uv
uv venv
uv pip install playwright
uv run playwright install chromium
```

## Common Commands

```bash
# 1. First run / Auth setup (headed browser to save credentials)
python main.py --login

# 2. Extract comments from a post
python main.py --url "https://www.facebook.com/permalink.php?story_fbid=..." --output ./output.csv

# 3. Run with specific limits
python main.py --url "<POST_URL>" --max-comments 500

# 4. Shared posts of a post + the comments under each share
python main.py --url "<POST_URL>" --shares --output ./shares.csv

# 5. Screenshot selected comments (CSV with an `id` column, e.g. a filtered scrape CSV)
python main.py --url "<POST_URL>" --screenshot ./positive.csv   # -> screenshots/positive/

# 6. LGU Comment Monitor website (FastAPI + Jinja2, SQLite in data/monitor.db)
python -m server create-user admin --name "Admin" --role admin
python -m server import-csv comments_*.csv
python -m server compare-supportive positive_*.csv [--label]   # AI accuracy check
python -m server backup | cleanup [--yes] | restore FILE --yes
python -m server serve                     # http://127.0.0.1:8000
python -m agent --server http://127.0.0.1:8000 --token <agent token>

# 7. Tests
python -m pytest
```

### LGU Comment Monitor rules
- Only `agent/` (on the admin laptop) runs the scraper; the server never touches Facebook.
- Comments are keyed by Facebook's comment id; collecting again upserts (never deletes). Posts merge by Facebook post id.
- A comment id decodes to `comment:<post id>_<comment id>`; ingest drops comments whose post id differs (feed stories on the page).
- Facebook ids must be all digits; anything else (Excel's `1.00003E+14`) is stored as unknown.
- Every state-changing form checks the session CSRF token; roles: admin (all), staff (view + export + manage keywords), mayor (view + export).
- `keyword_hits` is a cache of keyword matches: refresh it (`refresh_hits`) whenever comments or keywords change.
- AI sentiment (`server/ai.py` prompts and API calls, `server/analysis.py` background worker) uses `claude-haiku-5-5` (effort `low`; `LCM_SENTIMENT_MODEL`/`LCM_SENTIMENT_EFFORT`). Labels: supportive / against / neutral / question. "Supportive" = backs the LGU measure; general thanks, advice, requests and sarcasm are not.
- The key comes only from `ANTHROPIC_API_KEY` in the environment; never ask for it or write it to a file. Real API calls cost money: tests use a fake client, and running `compare-supportive --label` needs the user's go-ahead.
- Read labels through the `comment_labels` view (a staff correction overrides the AI). An edited comment loses its AI label and is labeled again; corrections are kept. Every API call is recorded in `api_usage` (Settings shows the monthly cost).
- Only the admin adds posts (staff send the admin links; there is no request queue). Retention: comments older than `LCM_RETENTION_DAYS` (365) by their Facebook date are deleted hourly by `server/maintenance.py`, replies go with their parent comment, and posts with no comments left are removed. Never delete a post that has a queued or running job.
- View-only links (`view_links`, `/view/<token>`): each link has its own hidden mayor-role user (excluded from the Accounts list); revoking deactivates it and deletes its sessions. Only the token's hash is stored; the link is shown once.
- Server (Lightsail): code in /opt/lcm, data in /var/lib/lcm, settings in /etc/lcm/env (root:lcm 640, holds the API key), service `lcm`, Caddy for HTTPS; `sudo lcm <command>` runs `python -m server` as the service user.
- Backups: monthly (every 30 days, counted from the newest backup) SQLite backup-API copies in `LCM_BACKUP_DIR` (default `data/backups`, newest 12 kept); `restore` needs the website stopped and backs up the current database first.

Output format is picked from the `--output` extension: `.csv` (default `output.csv`, UTF-8 with BOM for Excel), `.json`, or `.db`/`.sqlite` (upserts by comment `id`).

---

## Architecture & Guiding Principles

### 1. Interception Over DOM Parsing (Primary Strategy)
- **Do not rely on CSS class names**: Meta obfuscates classes dynamically (e.g., `.x11i5rnm`). Class selectors are brittle and will break across deployments.
- **Listen to GraphQL network traffic**: Hook into `page.on("response")` for requests targeting `https://www.facebook.com/api/graphql/`.
- Parse JSON payloads directly to extract comment text, author metadata, creation timestamps, and reaction counts.
- Fall back to ARIA role selectors (e.g., `role="article"`, text matching on "View more comments") only to simulate user interactions that trigger new network requests.

### 2. Session Management & Stealth
- Use `browser_type.launch_persistent_context()` with a dedicated `user_data_dir` to store cookies, local storage, and session tokens across runs.
- **First-run manual auth flow**: Provide a flag (`--login`) that launches a headed browser to allow manual login and 2FA approval.
- Avoid raw automation fingerprints:
  - Disable automation flags via args: `--disable-blink-features=AutomationControlled`.
  - Never run headless: always launch with `headless=False` (use a virtual display such as `xvfb` on servers). Do not add a `--headless` flag.
  - Implement randomized human-like delays (jitter) between clicks and scroll operations (e.g., `random.uniform(1.2, 3.8)`).

### 3. Comment Traversal Logic
- Identify the comment sorting mode on page load. By default, Facebook sorts by "Most relevant". Script must interact with the dropdown to switch to "All comments" to capture hidden or low-engagement replies.
- Reels and videos (`/share/v/…` lands on `/reel/<video id>`): comments live in a side panel opened by the "Comment, N comments" button, and the wheel must stay over that panel (over the video it jumps to the next reel). Their comments are keyed by the story's `post_id`, not the video id; `video_story_info()` reads it.
- Recursively trigger expanding elements:
  - "View more comments" (parent-level pagination).
  - "X replies" or "View previous replies" (child/nested thread pagination).
- Maintain an in-memory `Set[comment_id]` to deduplicate entries during long extraction sessions.

---

## Project Structure & File Layout

```text
lgu-scrape-bot/
├── CLAUDE.md
├── .gitignore
├── pyproject.toml
├── main.py              # CLI entry point (parse URLs, trigger scraper)
├── scraper/
│   ├── __init__.py
│   ├── browser.py       # Playwright browser context & anti-detection setup
│   ├── interceptor.py   # Network listener & GraphQL payload parser
│   ├── actions.py       # Scroll, expander clicks, sort dropdown toggles
│   ├── collect.py       # collect_comments(): open post, sort, expand, intercept
│   ├── post_info.py     # The post's own details (page, caption, date); comment → post id
│   └── screenshots.py   # Match comments to their [role=article] by comment_id link; element PNGs
├── storage/
│   ├── __init__.py
│   └── exporter.py      # Format captured data to JSON/CSV/SQLite
├── server/              # LGU Comment Monitor website
│   ├── app.py           # FastAPI app, security headers, error pages
│   ├── web.py           # Pages and forms
│   ├── api.py           # Agent API (Bearer agent token)
│   ├── auth.py          # Accounts, sessions, roles, CSRF
│   ├── db.py            # SQLite schema migrations
│   ├── ingest.py        # Save scrapes, merge posts, import CSVs
│   ├── jobs.py          # Job queue (queued → running → done/failed)
│   ├── keywords.py      # Spelling-tolerant keyword matching + highlight
│   ├── ai.py            # Claude prompts, structured output, cost per call
│   ├── analysis.py      # Background worker: label comments, post summaries, 30-day brief
│   ├── accuracy.py      # compare-supportive: AI labels vs hand-picked comments
│   ├── maintenance.py   # 1-year retention, post deletion, monthly backups, restore
│   ├── queries.py       # Read queries for the pages
│   ├── excel.py         # .xlsx exports (formula-injection safe)
│   ├── templates/, static/
│   └── cli.py           # python -m server …
├── agent/runner.py      # python -m agent: claim job → scrape → upload
├── deploy/             # setup.sh (Ubuntu + Caddy, Lightsail); setup-cpanel.sh + cpanel.md (AlmaLinux cPanel VPS: Apache forwards one subdomain to 127.0.0.1:LCM_PORT, Cloudflare in front); update.sh, restore.sh for both
├── tests/               # pytest
├── data/                # monitor.db (gitignored: residents' comments)
├── screenshots/         # Comment screenshots (gitignored: names and photos)
└── sessions/            # Persistent browser context data (gitignored)
## Agent skills

### Issue tracker

Issues live in GitHub Issues on StanleyPangaruy/lgu-scrape-bot (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

The five default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root (created only when needed). See `docs/agents/domain.md`.
