# lgu-scrape-bot

Two parts:

- **LGU Comment Monitor**: a website where staff and the mayor read the comments on the Municipality's Facebook posts: keyword counts, top commenters, most-reacted comments, Excel exports. Posts are collected by an **agent** on the admin's laptop. See [LGU Comment Monitor](#lgu-comment-monitor-website--agent).
- **The scraper** (`main.py`): extracts comments (and replies) from one Facebook post to CSV. It opens a real Chromium browser, logs in with your saved Facebook session, switches the post to **All comments** (or **Newest** when a post has no "All comments" option), expands every "View more comments" / reply thread, and captures the comment data from Facebook's network responses.

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
| `--screenshot` | | Screenshot the comments listed in a CSV instead of scraping (see below) |
| `--screenshot-dir` | `screenshots/<CSV name>/` | With `--screenshot`: where to save the images |
| `--session-dir` | `sessions/` | Where the browser login is stored |

## Shared posts

```bash
python main.py --url "<POST_URL>" --shares --output ./shares_post1.csv
```

Opens the post's **⋯** menu → **Share history** (or the **N shares** counter if that menu item isn't there), scrolls through the list to collect every share (who shared it and what they wrote), then opens each share to read its caption and collects the comments under it, the same way as a normal run. A share is saved if it has a caption **or** comments; shares with neither are skipped. `--max-comments` applies to each share separately.

Only shares your account can see are listed (Facebook hides shares made privately or to friends only).

Output columns are the shared post (`share_id`, `share_author_name`, `share_text`, `share_url`, `share_comment_count`, …) followed by the comment columns from the table below, prefixed with `comment_`. There is one row per comment, plus one row for each share without comments. `.json` nests comments under each share; `.db` writes a `shares` table and a `share_comments` table linked by `share_id`.

## Comment screenshots

```bash
# 1. Scrape the post
python main.py --url "<POST_URL>" --output ./comments_post1.csv

# 2. Make a CSV of just the comments you want (e.g. copy comments_post1.csv to
#    positive_post1.csv and delete the other rows). Only the `id` column is required.

# 3. Screenshot them
python main.py --url "<POST_URL>" --screenshot ./positive_post1.csv
```

Opens the post, loads comments and replies until every listed comment is on screen (it stops early once they all are), and saves each one as its own cropped PNG: name, profile photo, full text (expands "See more"), and reactions. Images go to `screenshots/positive_post1/`, named `001_<author>_<comment id>.png`, with an `index.csv` listing each comment and whether it was captured. Comments that can't be found (deleted, hidden, or no longer loading) are listed at the end of the run.

The `screenshots/` folder is gitignored: the images show commenters' names and photos.

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

## LGU Comment Monitor (website + agent)

```text
Admin laptop: agent ──uploads──►  Website (server)  ◄── staff, mayor, admin (browser)
  Facebook login + scraper           accounts, database, keywords, exports
```

Only the agent touches Facebook. The website stores everything in `data/monitor.db` (gitignored).

### Start the website

```bash
# First time only: create the admin account (asks for a password, 10+ characters)
python -m server create-user admin --name "Your Name" --role admin

# Optional: bring in comments scraped earlier with main.py
python -m server import-csv comments_*.csv

# Run it, then open http://127.0.0.1:8000
python -m server serve
```

`serve` listens on this computer only. To let others on the network reach it, use `--host 0.0.0.0` (for the internet, put it behind HTTPS and set `LCM_SECURE_COOKIES=1`). Create the other accounts in **Settings** (roles: Admin, Staff, Mayor).

| Role | Can do |
| --- | --- |
| Admin | Everything: add, re-check and delete posts, keywords, accounts, agent tokens, jobs, backups |
| Staff | View all pages, export to Excel, add, edit and delete keywords, correct AI labels |
| Mayor | View all pages, export to Excel |

### The mayor's view link (no sign-in)

**Settings → View-only links → Create view link** makes a private link. Opening it shows Overview, Posts, People and Keywords, with Excel exports, without a username or password. Nothing can be changed through it: no keyword editing, adding posts, or correcting labels. Ask the mayor to bookmark the link itself; it keeps working until you turn it off in the same place, which also signs out every device that opened it.

- Send the link privately: anyone who has it can read residents' comments.
- The link uses the address you opened the site with. While the website runs only on this computer (`127.0.0.1`), the link works only here; it works for the mayor once the site is reachable from the mayor's device (on the office network with `--host 0.0.0.0`, or online).

### Laptop agent

1. In the website: **Settings → Laptop agent → Create agent token**, and copy the token (shown once).
2. On the laptop that is logged in to Facebook (`python main.py --login` done):

```bash
python -m agent --server http://127.0.0.1:8000 --token <TOKEN>
```

Or set `LCM_SERVER` and `LCM_AGENT_TOKEN` and run `python -m agent`. The agent checks the website for queued posts every ~15 seconds, collects them in a visible browser window, and uploads the comments. Leave it running; Ctrl+C stops it (a job in progress goes back to the queue). If Facebook logs out, the agent puts the job back, stops, and asks you to run `python main.py --login`.

To collect a post, the admin pastes its link on the **Posts** page; **Re-check** on a post collects it again (new comments are added, reaction counts refreshed; nothing is deleted). Two links to the same Facebook post are merged into one.

### Keywords

The site starts with a list of local topics (colorum, gasolina, sidecar, prangkisa, …). Admin and staff can add, edit and delete keywords on the **Keywords** page. Matching ignores capitals, accents and repeated letters (`COLORUMMM` = `colorum`), counts listed spellings as the same word (`kolorum`, `culorom`), and matches whole words only (`gas` does not match `gastos`). A spelling ending in `*` also matches longer words (`pahirap*` → `pahirapan`).

### AI sentiment (Claude)

After each collection the website labels every new comment **Supportive**, **Against**, **Neutral** or **Question**, with a one-line reason. It then writes a short summary per post and a 30-day brief for the **Overview** page. It uses Claude Haiku: about 1,000 comments cost a few US cents. **Settings → AI sentiment** shows progress and this month's cost.

- *Supportive* means backing the LGU's measure ("pabor po", "nararapat lang na ipatupad"). It still counts if the comment objects to one part of the measure. General thanks, advice, requests and sarcasm are not supportive.
- Staff and the admin can correct any label (the **Correct…** menu under a comment). A correction overrides the AI, survives re-analysis, and updates the post summary.
- The API key is read from the `ANTHROPIC_API_KEY` environment variable on the website computer. Without it, nothing is analyzed and the rest of the site works normally. **Never put the key in a file in this repo.**

Set the key once on Windows (PowerShell), then open a **new** terminal and start the website:

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "<your key>", "User")
```

On Linux, add `export ANTHROPIC_API_KEY=...` to the service's environment (not to the repo).

Accuracy check: compare the AI's "supportive" with comments you picked by hand (`positive_<code>.csv` next to its `comments_<code>.csv`). `--label` first labels just those posts, which costs a few cents:

```bash
python -m server compare-supportive positive_*.csv --label
```

### Keeping data for one year, and backups

- **Adding posts:** only the admin adds and re-checks posts. Staff send the admin the Facebook link.
- **One year:** comments posted more than a year ago are deleted automatically, checked every hour. Their replies, labels and corrections are deleted with them, and a post goes once it has no comments left. The admin can delete a post sooner with **Delete post** on its page. Settings → **Data and backups** shows the oldest comment and what the next cleanup will remove.
- **Backups:** the website saves a copy of the database every month (30 days after the newest one) and keeps the newest 12 (a year) in `data/backups`. **Back up now** in Settings makes one immediately.

A backup on the same disk is lost if the computer dies. Point backups at a folder that is copied elsewhere, such as OneDrive or a USB drive (PowerShell, then open a new terminal and restart the website):

```powershell
[Environment]::SetEnvironmentVariable("LCM_BACKUP_DIR", "$env:USERPROFILE\OneDrive\LCM backups", "User")
```

```bash
python -m server backup                       # save a copy now
python -m server cleanup                      # show what is past one year (add --yes to delete now)
# Restore: stop the website (Ctrl+C) first. The current database is backed up before it is replaced.
python -m server restore data/backups/monitor-2026-11-07-020000.db --yes
```

### Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LCM_DATA_DIR` | `data` | Where the database lives |
| `LCM_SECURE_COOKIES` | `0` | `1` when served over HTTPS (required on the internet) |
| `LCM_UTC_OFFSET` | `8` | Hours from UTC for displayed times (Philippines = 8) |
| `ANTHROPIC_API_KEY` | (none) | Claude API key; without it comments are not analyzed |
| `LCM_SENTIMENT_MODEL` | `claude-haiku-5-5` | Claude model for labels and summaries |
| `LCM_SENTIMENT_EFFORT` | `low` | Effort setting (`low`, `medium`, `high`) |
| `LCM_AI_WORKER` | `1` | `0` turns the background analysis off |
| `LCM_RETENTION_DAYS` | `365` | Delete comments older than this; `0` keeps everything |
| `LCM_BACKUP_DIR` | `data/backups` | Where monthly backups go |
| `LCM_BACKUP_KEEP` | `12` | How many backups to keep |
| `LCM_MAINTENANCE` | `1` | `0` turns off the hourly cleanup and monthly backup |

### Tests

```bash
pip install -e ".[dev]"
python -m pytest
```

## Putting the website online on a cPanel server (GoDaddy VPS + Cloudflare)

To run it on a WHM/cPanel server (AlmaLinux 9) that also hosts other websites, at a subdomain such as
`monitor.livelopez.gov.ph` behind Cloudflare, follow **[deploy/cpanel.md](deploy/cpanel.md)**. It
covers the subdomain, the Cloudflare origin certificate, `deploy/setup-cpanel.sh`, Cloudflare rules
and moving the data from Lightsail. Every page is sent with `Cache-Control: no-store`, so Cloudflare
never stores residents' comments.

## Putting the website online (AWS Lightsail)

The website runs on a small Ubuntu server; the scraper agent stays on the admin laptop. `deploy/setup.sh` installs everything: the website as the `lcm` service (starts on boot, restarts if it stops), and Caddy in front for HTTPS. The address is a free `<server-ip>.sslip.io` name until you get a real domain.

1. **Create the server** at lightsail.aws.amazon.com: *Create instance* → region **Singapore**, *Linux/Unix* → *OS Only* → **Ubuntu 24.04 LTS**, the 1 GB (or larger) plan, name it `lcm`.
2. **Give it a fixed address**: *Networking* → *Create static IP* → attach it to `lcm`. Do this before step 4: the website address is made from this IP.
3. **Open HTTPS**: on the instance, *Networking* → *IPv4 Firewall* → *Add rule* → **HTTPS**. Keep SSH and HTTP (HTTP is needed to get the certificate).
4. **Install**: click *Connect using SSH* and run the following. It takes about 5 minutes, runs the tests on the server, and asks for the Anthropic API key (typed there, not shown):

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/StanleyPangaruy/lgu-scrape-bot/main/deploy/setup.sh
   sudo bash setup.sh
   ```

5. **Bring the existing data** (no re-collecting, no paying for labels again): on the admin computer run `python -m server backup`, then in the SSH window use the gear menu → *Upload file* to upload that `monitor-….db`, and:

   ```bash
   sudo bash /opt/lcm/deploy/restore.sh ~/monitor-2026-11-07-141500.db
   ```

   Accounts, passwords, keywords, labels and the agent token all come along. To start empty instead: `sudo lcm create-user admin --name "Your Name" --role admin`.
6. **Point the agent at the server** on the admin laptop: `python -m agent --server https://<address> --token <TOKEN>`. Stop using the copy on the admin computer, so the data lives in one place.
7. **Mayor's link**: create a new one in Settings → View-only links (the old one had the local address).
8. **Off-server copies**: in Lightsail, *Snapshots* → turn on **automatic snapshots** (a few cents per GB a month). The website's own monthly backups stay on the server's disk.

Day to day on the server:

| Task | Command |
| --- | --- |
| Install the newest code from GitHub | `sudo bash /opt/lcm/deploy/update.sh` |
| Admin commands | `sudo lcm backup`, `sudo lcm cleanup`, `sudo lcm create-user …` |
| Change settings (API key, …) | `sudo nano /etc/lcm/env`, then `sudo systemctl restart lcm` |
| See what happened | `sudo journalctl -u lcm -n 100` |

## Linux servers (no display)

The scraper never runs headless (Facebook flags headless browsers). On a machine without a screen, run it under a virtual display:

```bash
sudo apt install xvfb
xvfb-run python main.py --url "<POST_URL>"
```

The `--login` step needs a real screen to type into, so do it on a desktop or over remote desktop/VNC.
