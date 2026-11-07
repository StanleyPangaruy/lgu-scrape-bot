#!/usr/bin/env bash
# Set up the LGU Comment Monitor on a fresh Ubuntu 24.04 server (AWS Lightsail).
#
#   sudo bash setup.sh
#
# Safe to run again: it keeps the database, the settings file and the address.
# Afterwards the website runs as the "lcm" service behind Caddy (automatic HTTPS).
set -euo pipefail

REPO="${LCM_REPO:-https://github.com/StanleyPangaruy/lgu-scrape-bot.git}"
APP=/opt/lcm
DATA=/var/lib/lcm
ENV_FILE=/etc/lcm/env

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo:  sudo bash $0"
  exit 1
fi

echo "== 1/6 System updates and packages (a few minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq python3 python3-venv python3-pip git curl sqlite3 unattended-upgrades
if ! apt-get install -yq caddy; then
  # Not in this Ubuntu's archive: use Caddy's own package repository.
  apt-get install -yq debian-keyring debian-archive-keyring apt-transport-https gnupg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -yq caddy
fi

# A small swap file so installing packages never runs out of memory on a 1 GB server.
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 2/6 Website code"
id lcm >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin lcm
mkdir -p "$DATA/backups" /etc/lcm
chown -R lcm:lcm "$DATA"
chmod 750 "$DATA"
if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only
else
  git clone "$REPO" "$APP"
fi
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$APP[dev]"

echo "== 3/6 Checking the code on this server (tests)"
(cd "$APP" && "$APP/venv/bin/python" -m pytest -q -p no:cacheprovider)

echo "== 4/6 Settings"
if [ ! -f "$ENV_FILE" ]; then
  IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
  ADDRESS="${IP//./-}.sslip.io"
  echo "The Anthropic API key turns on AI sentiment. Paste it here (it is not shown);"
  echo "press Enter to skip and add it later to $ENV_FILE."
  read -r -s -p "API key: " KEY < /dev/tty || KEY=""
  echo
  umask 077
  cat > "$ENV_FILE" <<EOF
# LGU Comment Monitor settings. After changing: sudo systemctl restart lcm
LCM_ADDRESS=$ADDRESS
LCM_DATA_DIR=$DATA
LCM_SECURE_COOKIES=1
ANTHROPIC_API_KEY=$KEY
EOF
  umask 022
  chown root:lcm "$ENV_FILE"
  chmod 640 "$ENV_FILE"
fi
ADDRESS="$(grep '^LCM_ADDRESS=' "$ENV_FILE" | cut -d= -f2)"

# `sudo lcm <command>` runs `python -m server <command>` as the service user with its settings.
cat > /usr/local/bin/lcm <<'EOF'
#!/usr/bin/env bash
# Run an LGU Comment Monitor admin command:  sudo lcm create-user | backup | restore | cleanup ...
cd /var/lib/lcm
exec sudo -u lcm bash -c 'set -a; . /etc/lcm/env; set +a; exec /opt/lcm/venv/bin/python -m server "$@"' lcm "$@"
EOF
chmod 755 /usr/local/bin/lcm

echo "== 5/6 Website service"
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
ExecStart=/opt/lcm/venv/bin/python -m server serve --host 127.0.0.1 --port 8000
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

echo "== 6/6 HTTPS (Caddy)"
cat > /etc/caddy/Caddyfile <<EOF
$ADDRESS {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}
EOF
systemctl enable caddy >/dev/null
systemctl restart caddy

sleep 3
if systemctl is-active --quiet lcm; then
  echo
  echo "Done. The website is at:  https://$ADDRESS"
  echo "(The HTTPS certificate can take a minute on the first visit.)"
  echo
  echo "Next:"
  echo "  - Bring your data:  upload a backup, then  sudo bash $APP/deploy/restore.sh ~/monitor-....db"
  echo "  - Or start empty:   sudo lcm create-user admin --name \"Your Name\" --role admin"
else
  echo "The website did not start. See:  sudo journalctl -u lcm -n 50"
  exit 1
fi
