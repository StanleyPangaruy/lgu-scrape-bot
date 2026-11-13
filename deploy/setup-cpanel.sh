#!/usr/bin/env bash
# Set up the LGU Comment Monitor on a cPanel/WHM server (AlmaLinux 9, GoDaddy VPS)
# that also hosts other websites. See deploy/cpanel.md for the whole move.
#
#   sudo bash setup-cpanel.sh CPANEL_USER [ADDRESS]
#
#   CPANEL_USER  the cPanel account that owns the subdomain (WHM → List Accounts)
#   ADDRESS      the subdomain, created first in that cPanel account
#                (default: monitor.livelopez.gov.ph)
#
# The website runs as the "lcm" service on 127.0.0.1 only; cPanel's Apache forwards
# just that subdomain to it. Nothing else on the server is changed: no system
# upgrade, no change to other sites, and if Apache rejects the new settings they
# are removed again before Apache restarts.
#
# Safe to run again: it keeps the database, the settings file and the port.
set -euo pipefail

REPO="${LCM_REPO:-https://github.com/StanleyPangaruy/lgu-scrape-bot.git}"
APP=/opt/lcm
DATA=/var/lib/lcm
ENV_FILE=/etc/lcm/env
PYTHON=python3.11
CPUSER="${1:-}"
ADDRESS="${2:-monitor.livelopez.gov.ph}"

fail() { echo; echo "Stopped: $*"; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it as root:  sudo bash $0 CPANEL_USER [ADDRESS]"
  exit 1
fi

echo "== 1/7 Checking this server"
[ -n "$CPUSER" ] || fail "give the cPanel account name:  sudo bash $0 CPANEL_USER [ADDRESS]"
[ -d /usr/local/cpanel ] || fail "this is not a cPanel server (use deploy/setup.sh on a plain Ubuntu server)."
command -v dnf >/dev/null || fail "dnf not found; this script is for AlmaLinux/RHEL 9."
[ -f "/var/cpanel/users/$CPUSER" ] || fail "no cPanel account named \"$CPUSER\" (WHM → List Accounts shows the names)."
[ -f "/var/cpanel/userdata/$CPUSER/$ADDRESS" ] || fail "$ADDRESS is not a domain of \"$CPUSER\" yet.
  In cPanel (as $CPUSER) → Domains → Create A New Domain → $ADDRESS, then run this again."
MODULES="$(/usr/sbin/httpd -M 2>/dev/null || true)"
for module in proxy_module proxy_http_module headers_module rewrite_module; do
  grep -qw "$module" <<<"$MODULES" \
    || fail "Apache module $module is off. Turn it on in WHM → EasyApache 4 → Customize → Apache Modules (mod_${module%_module}), then run this again."
done
/usr/sbin/httpd -t >/dev/null 2>&1 \
  || fail "Apache's current settings already have a problem (httpd -t). Fix that first; nothing was changed."
[ -f "/var/cpanel/userdata/$CPUSER/${ADDRESS}_SSL" ] \
  || fail "$ADDRESS has no HTTPS certificate in cPanel yet. Install the Cloudflare origin
  certificate first (deploy/cpanel.md, step 4: cPanel → SSL/TLS → Manage SSL sites), then run this again."
if command -v getenforce >/dev/null && [ "$(getenforce)" = "Enforcing" ]; then
  echo "SELinux is enforcing: allowing Apache to forward to the website."
  setsebool -P httpd_can_network_connect 1
fi
echo "OK: cPanel account $CPUSER, address $ADDRESS"

echo "== 2/7 Python $PYTHON and tools"
dnf install -y -q "$PYTHON" "$PYTHON-pip" git sqlite >/dev/null

echo "== 3/7 Website code"
id lcm >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /sbin/nologin lcm
mkdir -p "$DATA/backups" /etc/lcm
chown -R lcm:lcm "$DATA"
chmod 750 "$DATA"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only
else
  git clone -q "$REPO" "$APP"
fi
[ -x "$APP/venv/bin/python" ] || "$PYTHON" -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$APP[dev]"

echo "== 4/7 Checking the code on this server (tests)"
(cd "$APP" && "$APP/venv/bin/python" -m pytest -q -p no:cacheprovider)

echo "== 5/7 Settings"
if [ ! -f "$ENV_FILE" ]; then
  PORT=8765
  while [ -n "$(ss -Hltn "sport = :$PORT")" ]; do PORT=$((PORT + 1)); done
  echo "The Anthropic API key turns on AI sentiment. Paste it here (it is not shown);"
  echo "press Enter to skip and add it later to $ENV_FILE."
  read -r -s -p "API key: " KEY < /dev/tty || KEY=""
  echo
  umask 077
  cat > "$ENV_FILE" <<EOF
LCM_ADDRESS=$ADDRESS
LCM_PORT=$PORT
LCM_DATA_DIR=$DATA
LCM_SECURE_COOKIES=1
ANTHROPIC_API_KEY=$KEY
EOF
  umask 022
  chown root:lcm "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi
PORT="$(grep '^LCM_PORT=' "$ENV_FILE" | cut -d= -f2)"
[ -n "$PORT" ] || fail "LCM_PORT is missing from $ENV_FILE."

cat > /usr/local/bin/lcm <<'EOF'
#!/usr/bin/env bash
# Run a website admin command as the service user:  sudo lcm backup
cd /var/lib/lcm
exec sudo -u lcm bash -c 'set -a; . /etc/lcm/env; set +a; exec /opt/lcm/venv/bin/python -m server "$@"' lcm "$@"
EOF
chmod 755 /usr/local/bin/lcm

echo "== 6/7 Website service (127.0.0.1:$PORT)"
cat > /etc/systemd/system/lcm.service <<'EOF'
[Unit]
Description=LGU Comment Monitor
After=network-online.target
Wants=network-online.target

[Service]
User=lcm
Group=lcm
EnvironmentFile=/etc/lcm/env
WorkingDirectory=/var/lib/lcm
ExecStart=/opt/lcm/venv/bin/python -m server serve --host 127.0.0.1 --port ${LCM_PORT}
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/lcm

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable lcm >/dev/null
systemctl restart lcm
sleep 3
systemctl is-active --quiet lcm || fail "the website did not start. See:  journalctl -u lcm -n 50"

echo "== 7/7 Apache: forward $ADDRESS to the website"
SSL_DIR="/etc/apache2/conf.d/userdata/ssl/2_4/$CPUSER/$ADDRESS"
STD_DIR="/etc/apache2/conf.d/userdata/std/2_4/$CPUSER/$ADDRESS"
mkdir -p "$SSL_DIR" "$STD_DIR"
cat > "$SSL_DIR/lcm.conf" <<EOF
# LGU Comment Monitor (written by deploy/setup-cpanel.sh): this subdomain only.
ProxyPreserveHost On
ProxyTimeout 120
ProxyPass "/.well-known/" "!"
ProxyPass "/" "http://127.0.0.1:$PORT/"
ProxyPassReverse "/" "http://127.0.0.1:$PORT/"
RequestHeader set X-Forwarded-Proto "https"
EOF
cat > "$STD_DIR/lcm.conf" <<EOF
# LGU Comment Monitor (written by deploy/setup-cpanel.sh): plain http goes to https.
RewriteEngine On
RewriteCond %{REQUEST_URI} !^/\.well-known/
RewriteRule ^/?(.*)$ https://$ADDRESS/\$1 [R=301,L]
EOF
/usr/local/cpanel/scripts/rebuildhttpdconf >/dev/null
if ! /usr/sbin/httpd -t >/dev/null 2>&1; then
  /usr/sbin/httpd -t || true
  rm -f "$SSL_DIR/lcm.conf" "$STD_DIR/lcm.conf"
  /usr/local/cpanel/scripts/rebuildhttpdconf >/dev/null
  fail "Apache rejected the new settings (above). They were removed again; Apache was not restarted and the other sites are unchanged."
fi
/usr/local/cpanel/scripts/restartsrv_httpd >/dev/null

sleep 2
code="$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$ADDRESS:443:127.0.0.1" "https://$ADDRESS/login" || true)"
plain="$(curl -s -o /dev/null -w '%{http_code}' --resolve "$ADDRESS:80:127.0.0.1" "http://$ADDRESS/login" || true)"
echo
if [ "$code" = "200" ]; then
  echo "Done. On this server, https://$ADDRESS/login answers (HTTP 200); plain http gives $plain (301 = sent to https)."
else
  echo "The website runs, but https://$ADDRESS/login through Apache gave HTTP $code."
  echo "Check:  journalctl -u lcm -n 50   and   tail -n 50 /etc/apache2/logs/error_log"
  echo "An Apache 403 here is often ModSecurity: see \"Troubleshooting\" in deploy/cpanel.md."
fi
echo
echo "Next (deploy/cpanel.md):"
echo "  - Cloudflare: DNS record, origin certificate (install it in cPanel for $ADDRESS), SSL and cache rules"
echo "  - Bring your data:  upload a backup, then  sudo bash $APP/deploy/restore.sh ~/monitor-....db"
echo "  - Updates later:    sudo bash $APP/deploy/update.sh"
