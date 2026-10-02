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
```

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
│   └── actions.py       # Scroll, expander clicks, sort dropdown toggles
├── storage/
│   ├── __init__.py
│   └── exporter.py      # Format captured data to JSON/CSV/SQLite
└── sessions/            # Persistent browser context data (gitignored)