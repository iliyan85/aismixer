#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

INSTALL_DIR=/opt/nmea_sproxy
TOOLS_DIR="$INSTALL_DIR/tools"
SYSTEMD_DIR=/etc/systemd/system

echo "[+] Updating secure proxy runtime in $INSTALL_DIR"
sudo install -d -m 0755 "$INSTALL_DIR" "$TOOLS_DIR"
sudo install -m 0755 "$SCRIPT_DIR/nmea_sproxy.py" "$INSTALL_DIR/nmea_sproxy.py"
sudo install -m 0644 "$SCRIPT_DIR/meta_cleaner.py" "$INSTALL_DIR/meta_cleaner.py"
sudo install -m 0755 "$REPO_ROOT/tools/aismixer_keys.py" "$TOOLS_DIR/aismixer_keys.py"

echo "[+] Updating systemd unit files"
sudo install -m 0644 "$SCRIPT_DIR/nmea_sproxy.service" "$SYSTEMD_DIR/nmea_sproxy.service"
sudo install -m 0644 "$SCRIPT_DIR/nmea_sproxy@.service" "$SYSTEMD_DIR/nmea_sproxy@.service"
sudo systemctl daemon-reload

echo "[+] Update complete"
echo "    /etc/nmea_sproxy configs and keys were not changed."
echo "    Restart the singleton or selected template instances when ready."
