# Moving the website to the GoDaddy VPS (cPanel/WHM + Cloudflare)

The website moves from AWS Lightsail to the GoDaddy VPS that hosts livelopez.gov.ph
(AlmaLinux 9, WHM/cPanel), at **https://monitor.livelopez.gov.ph**, behind Cloudflare.

How it fits on that server:

```text
Visitor / laptop agent ──HTTPS──► Cloudflare ──HTTPS──► Apache (cPanel) ──► website on 127.0.0.1:8765
                                                         │
                                                         └─ livelopez.gov.ph and other sites: unchanged
```

- The website runs as its own service (`lcm`) under its own system user, reachable only from the
  server itself. Apache forwards **only** `monitor.livelopez.gov.ph` to it.
- Code in `/opt/lcm`, data in `/var/lib/lcm` (database and backups), settings in `/etc/lcm/env`.
- The domain's DNS, the main website and email are not touched. One DNS record is added.

You need: WHM root access (WHM → **Terminal**, or SSH as root), the cPanel account name that owns
livelopez.gov.ph (WHM → **List Accounts**), and the Cloudflare dashboard.

---

## 1. Back up first

WHM → **Backup** → make sure a recent backup of the livelopez.gov.ph account exists (or WHM →
**Transfers** → **Backup a single account**). Nothing below should touch that site, but a government
website deserves a fresh backup before any server change.

## 2. Create the subdomain (cPanel)

WHM → **List Accounts** → the livelopez.gov.ph account → **cPanel** →
**Domains** → **Create A New Domain**:

- Domain: `monitor.livelopez.gov.ph`
- Untick "Share document root" if it is offered (the folder stays empty; the website does not use it).

## 3. Make the HTTPS certificate (Cloudflare)

Cloudflare → livelopez.gov.ph → **SSL/TLS** → **Origin Server** → **Create Certificate**:

- Let Cloudflare generate the private key (RSA), hostnames: `monitor.livelopez.gov.ph` only,
  validity 15 years → **Create**.
- Keep the page open: the **private key is shown only once**.

## 4. Install the certificate (cPanel)

Same cPanel account → **Security** → **SSL/TLS** → **Manage SSL sites** (Install and Manage SSL
for your site):

- Domain: `monitor.livelopez.gov.ph`
- Certificate (CRT): paste the **Origin Certificate** from step 3
- Private Key (KEY): paste the **Private Key** from step 3
- **Install Certificate**. (cPanel warns that the certificate is not trusted by browsers: that is
  expected, only Cloudflare connects to the server with it.)

## 5. Install the website (WHM Terminal, as root)

```bash
git clone https://github.com/StanleyPangaruy/lgu-scrape-bot.git /opt/lcm
sudo bash /opt/lcm/deploy/setup-cpanel.sh CPANEL_USER
```

Replace `CPANEL_USER` with the account name from step 2. The script:

1. checks the server: cPanel account, subdomain, certificate, Apache modules, Apache's current settings;
2. installs Python 3.11 (no system upgrade, nothing else changes);
3. installs the website in `/opt/lcm` and **runs the tests on the server**;
4. asks for the **Anthropic API key** (paste it; it is not shown; Enter skips it);
5. starts the `lcm` service on `127.0.0.1:8765` (the next free port if that one is taken);
6. adds the Apache forwarding for this subdomain only, checks Apache's settings, and **removes it
   again without restarting Apache if Apache rejects it**;
7. checks `https://monitor.livelopez.gov.ph/login` on the server itself.

It ends with `Done. ... answers (HTTP 200)`. Send the last lines to whoever is helping you if not.

> If step 1 says a module is off: WHM → **EasyApache 4** → **Customize** → **Apache Modules** →
> turn on `mod_proxy`, `mod_proxy_http`, `mod_headers`, `mod_rewrite` → **Provision**, then run the
> script again. It is safe to run again at any time.

## 6. Cloudflare settings

Cloudflare → livelopez.gov.ph:

1. **DNS** → **Records** → **Add record**: Type `A`, Name `monitor`, IPv4 `184.168.121.22`,
   Proxy status **Proxied** (orange cloud) → **Save**.
2. **SSL/TLS** → **Overview**: note the current mode.
   - If it is already **Full (strict)**, skip to 3.
   - Otherwise **do not change it** (the main site depends on it). Instead: **Rules** →
     **Configuration Rules** → **Create rule** → name `Monitor SSL` → *Hostname* *equals*
     `monitor.livelopez.gov.ph` → **SSL** → **Full (strict)** → **Deploy**.
3. **Caching** → **Cache Rules** → **Create rule** → name `Monitor no cache` → *Hostname* *equals*
   `monitor.livelopez.gov.ph` → **Bypass cache** → **Deploy**.
   (The website also tells every proxy not to keep pages; this rule makes sure no
   "cache everything" rule for the main site can ever store residents' comments.)
4. **Security** → **Bots**: if **Bot Fight Mode** is on, the laptop agent may be blocked when it
   uploads (it is a program, not a browser). Test the agent in step 8; if its uploads fail with 403,
   see *Troubleshooting*.

Open **https://monitor.livelopez.gov.ph** in a browser: the sign-in page should load with a valid
padlock (DNS can take a few minutes).

## 7. Move the data from Lightsail

1. On the laptop: stop the agent (**Ctrl+C**).
2. On Lightsail (browser SSH):
   ```bash
   sudo lcm backup
   sudo systemctl stop lcm
   ls -t /var/lib/lcm/backups | head -1
   ```
   The website on Lightsail is now stopped, so nothing new is saved there.
3. Copy that file to the GoDaddy server. The simplest way is through your laptop:
   - Lightsail → the instance → **Connect** → download the default SSH key, then on the laptop:
     ```bash
     scp -i LightsailDefaultKey.pem ubuntu@<lightsail-ip>:/var/lib/lcm/backups/monitor-....db .
     ```
     (If permission is denied, first run on Lightsail:
     `sudo cp /var/lib/lcm/backups/monitor-....db /home/ubuntu/ && sudo chown ubuntu /home/ubuntu/monitor-....db`
     and copy from `/home/ubuntu/` instead.)
   - Upload it to the GoDaddy server: with root SSH, `scp monitor-....db root@184.168.121.22:/root/`.
     Otherwise use the account's cPanel → **File Manager** → **Upload** (it lands in
     `/home/CPANEL_USER/`), then in WHM Terminal: `mv /home/CPANEL_USER/monitor-....db /root/`.
4. On the GoDaddy server:
   ```bash
   sudo bash /opt/lcm/deploy/restore.sh /root/monitor-....db
   ```
   It stops the website, backs up the (empty) current database, loads the file and starts again.
   Accounts, posts, comments, AI labels, corrections and agent tokens all come along: nobody needs a
   new password or token.
5. Delete the copies on the laptop and in `/root/` once the site works: they contain residents' comments.

## 8. Check, then switch the agent

1. Sign in at https://monitor.livelopez.gov.ph, open a post, export it to Excel.
2. On the laptop, start the agent with the new address:
   ```bash
   .\.venv\Scripts\python -m agent --server https://monitor.livelopez.gov.ph --token YOUR_TOKEN
   ```
   (If you saved `LCM_SERVER` as an environment variable, change it to the new address.)
3. Click **Re-check** on one post and watch it collect and upload.
4. **Settings → View-only links**: create a new link for the mayor and send it (the old links
   pointed at the Lightsail address).

## 9. After a week

If everything works, delete the Lightsail instance (Lightsail → the instance → **Delete**) and its
snapshots. Until then it is the fallback: `sudo systemctl start lcm` there brings the old site back.

---

## Day to day

| Task | Command (WHM Terminal) |
| --- | --- |
| Install the newest code from GitHub | `sudo bash /opt/lcm/deploy/update.sh` |
| Back up now | `sudo lcm backup` (also **Settings → Back up now**) |
| Load a backup | `sudo bash /opt/lcm/deploy/restore.sh FILE` |
| Change settings (API key, …) | `nano /etc/lcm/env`, then `systemctl restart lcm` |
| See what happened | `journalctl -u lcm -n 100` |

Automatic backups are made monthly in `/var/lib/lcm/backups` (newest 12 kept). They are on the same
server: also keep the VPS's own backups (WHM → **Backup**) turned on, or copy a backup off the server
now and then.

## Troubleshooting

| What you see | Likely cause and fix |
| --- | --- |
| Cloudflare **526** "Invalid SSL certificate" | The origin certificate is not installed for the subdomain (step 4), or SSL is "Full (strict)" without it. |
| Cloudflare **521/522** | Apache is not answering: WHM → **Restart Services** → HTTP Server (Apache). |
| Cloudflare **502** / "Bad gateway" from Apache | The website service is down: `systemctl status lcm` and `journalctl -u lcm -n 50`. |
| **403** page from Apache (not the website's own pages) | Usually ModSecurity blocking a form or an agent upload. WHM → **ModSecurity Tools** → **Hits List** shows the rule. To turn it off for this subdomain only, add the line `<IfModule security2_module>SecRuleEngine Off</IfModule>` to `/etc/apache2/conf.d/userdata/ssl/2_4/CPANEL_USER/monitor.livelopez.gov.ph/lcm.conf`, then run `/usr/local/cpanel/scripts/rebuildhttpdconf && /usr/local/cpanel/scripts/restartsrv_httpd`. The website has its own sign-in, CSRF checks and upload limits. |
| Agent: uploads fail with **403** or a Cloudflare challenge page | Bot Fight Mode (free plan) cannot be skipped for one address. Either turn it off (Security → Bots), or add a second DNS record `monitor-api` → `184.168.121.22` with **DNS only** (grey cloud), a second domain/certificate for it in cPanel, and point the agent at that. |
| The main site misbehaves after step 5 | Remove the forwarding: `rm -f /etc/apache2/conf.d/userdata/*/2_4/CPANEL_USER/monitor.livelopez.gov.ph/lcm.conf`, then `/usr/local/cpanel/scripts/rebuildhttpdconf && /usr/local/cpanel/scripts/restartsrv_httpd`. The files only apply to the monitor subdomain, so this is unlikely. |

To remove the website from the server entirely: remove the forwarding as above,
`systemctl disable --now lcm`, delete the subdomain in cPanel and the DNS record in Cloudflare. The
data stays in `/var/lib/lcm` until you delete that folder.
