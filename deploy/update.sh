#!/usr/bin/env bash
# Install the newest code from GitHub and restart the website (data and settings are kept):
#
#   sudo bash /opt/lcm/deploy/update.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo:  sudo bash $0"
  exit 1
fi

lcm backup
git -C /opt/lcm pull --ff-only
/opt/lcm/venv/bin/pip install -q "/opt/lcm[dev]"
(cd /opt/lcm && /opt/lcm/venv/bin/python -m pytest -q -p no:cacheprovider)
systemctl restart lcm
sleep 3
systemctl is-active --quiet lcm && echo "Updated and running." || { echo "Did not start: sudo journalctl -u lcm -n 50"; exit 1; }
