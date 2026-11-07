#!/usr/bin/env bash
# Load a backup (for example one made on the admin's computer) into the server:
#
#   sudo bash /opt/lcm/deploy/restore.sh ~/monitor-2026-11-07-141500.db
#
# The website is stopped, the current database is backed up, the file is loaded,
# and the website starts again.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo:  sudo bash $0 FILE"
  exit 1
fi
if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: sudo bash $0 path/to/monitor-....db"
  exit 1
fi

incoming=/var/lib/lcm/incoming.db
install -o lcm -g lcm -m 640 "$1" "$incoming"
systemctl stop lcm
status=0
lcm restore "$incoming" --yes || status=$?
rm -f "$incoming"
systemctl start lcm
if [ "$status" -eq 0 ]; then
  echo "Loaded. The website is running again with that data."
else
  echo "Not loaded (see the message above). The website is running with its previous data."
fi
exit "$status"
