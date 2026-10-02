# lgu-scrape-bot

Extracts comments (and replies) from a Facebook post and saves them to CSV.

It opens a real Chromium browser, logs in with your saved Facebook session, switches the post to **All comments** (or **Newest** when a post has no "All comments" option), expands every "View more comments" / reply thread, and captures the comment data from Facebook's network responses.

## Requirements

- **Python 3.11+**: check with `python --version`
- **Git**
- A Facebook account that can view the posts you want to scrape
- A screen. The browser always opens in a visible window; see [Linux servers](#linux-servers-no-display) if there's no display.

## Setup on a new computer

### 1. Clone the repo

```bash
git clone https://github.com/StanleyPangaruy/lgu-scrape-bot.git
cd lgu-scrape-bot
```

### 2. Create a virtual environment (recommended)

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activate script, run this once, then try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Activate the venv each time you open a new terminal to use the scraper.

### 3. Install dependencies and the browser

```bash
pip install -e .
python -m playwright install chromium
```

On Linux, use `python -m playwright install --with-deps chromium` to also install the system libraries Chromium needs.

### 4. Log in to Facebook (once per computer)

```bash
python main.py --login
```

A browser window opens. Log in (and complete 2FA if asked). When you can see your News Feed, go back to the terminal and press **Enter**. You should see `Session saved to sessions`.

The login is stored in the `sessions/` folder on that computer only. It is **not** in Git, so every new computer needs its own `--login`. Don't copy or share the `sessions/` folder: it holds your Facebook login cookies.

## Usage

```bash
# All comments from a post -> output.csv
python main.py --url "<POST_URL>"

# Choose the file name (use a different name per post; an existing file is overwritten)
python main.py --url "<POST_URL>" --output ./comments_post1.csv

# Stop after 500 comments (handy for testing or very large posts)
python main.py --url "<POST_URL>" --output ./comments_post1.csv --max-comments 500
```

Keep the browser window open while it runs. Progress prints after each round, for example `120 comments captured (+14, 3 expanders clicked)`. It stops when no new comments load for 4 rounds in a row, or when `--max-comments` is reached.

| Option | Default | Description |
| --- | --- | --- |
| `--login` | | Open a browser to log in and save the session |
| `--url` | | Facebook post URL to scrape |
| `--output` | `output.csv` | Output file. `.csv`, `.json`, or `.db`/`.sqlite` |
| `--max-comments` | no limit | Stop after this many comments (replies count too) |
| `--shares` | | Scrape the post's shares and the comments under each share instead (see below) |
| `--max-shares` | no limit | With `--shares`: stop after this many shares |
| `--session-dir` | `sessions/` | Where the browser login is stored |

## Shared posts

```bash
python main.py --url "<POST_URL>" --shares --output ./shares_post1.csv
```

Opens the post's **⋯** menu → **Share history** (or the **N shares** counter if that menu item isn't there), scrolls through the list to collect every share (who shared it and what they wrote), then opens each share to read its caption and collects the comments under it, the same way as a normal run. A share is saved if it has a caption **or** comments; shares with neither are skipped. `--max-comments` applies to each share separately.

Only shares your account can see are listed (Facebook hides shares made privately or to friends only).

Output columns are the shared post (`share_id`, `share_author_name`, `share_text`, `share_url`, `share_comment_count`, …) followed by the comment columns from the table below, prefixed with `comment_`. There is one row per comment, plus one row for each share without comments. `.json` nests comments under each share; `.db` writes a `shares` table and a `share_comments` table linked by `share_id`.

## Output (CSV)

One row per comment; opens directly in Excel or Google Sheets (Filipino text and emoji display correctly).

| Column | Meaning |
| --- | --- |
| `id` | Comment ID |
| `legacy_fbid` | Numeric comment ID |
| `parent_id` | For replies: the `id` of the comment being replied to (empty for top-level) |
| `depth` | 0 = top-level comment, 1+ = reply |
| `author_id`, `author_name`, `author_url` | Commenter |
| `text` | Comment text |
| `created_time`, `created_iso` | When it was posted (Unix seconds / ISO 8601 UTC) |
| `reaction_count` | Total reactions |
| `reply_count` | Number of replies |
| `url` | Link to the comment |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `playwright : The term 'playwright' is not recognized` | Use `python -m playwright install chromium` instead of `playwright install chromium` |
| `No saved Facebook session. Run python main.py --login first.` | Run `python main.py --login` on this computer |
| `Comment sort dropdown not found` / `Neither "All comments" nor "Newest" is offered` | Facebook's language must be **English** (Settings → Language). Otherwise it scrapes the default "Most relevant" comments only |
| Facebook asks for a checkpoint / security check | Run `python main.py --login`, complete the check in the browser, then retry |
| 0 comments saved | Make sure the URL opens the post itself, and that your account can see it. Facebook may have changed its data format; open an issue with the terminal output |
| `ModuleNotFoundError: No module named 'playwright'` | Activate the venv (step 2) or rerun `pip install -e .` |

## Linux servers (no display)

The scraper never runs headless (Facebook flags headless browsers). On a machine without a screen, run it under a virtual display:

```bash
sudo apt install xvfb
xvfb-run python main.py --url "<POST_URL>"
```

The `--login` step needs a real screen to type into, so do it on a desktop or over remote desktop/VNC.
