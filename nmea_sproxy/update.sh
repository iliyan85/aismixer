#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

INSTALL_DIR=/opt/nmea_sproxy
TOOLS_DIR="$INSTALL_DIR/tools"
SYSTEMD_DIR=/etc/systemd/system

if (( EUID == 0 )); then
	AS_ROOT=()
elif command -v sudo >/dev/null 2>&1; then
	AS_ROOT=(sudo)
else
	echo "[!] This script must be run as root or by a user with sudo installed." >&2
	exit 1
fi

run_as_root() {
	if ((${#AS_ROOT[@]})); then
		"${AS_ROOT[@]}" "$@"
	else
		"$@"
	fi
}

echo "[+] Updating secure proxy runtime in $INSTALL_DIR"
run_as_root install -d -m 0755 "$INSTALL_DIR" "$TOOLS_DIR"
run_as_root install -m 0755 "$SCRIPT_DIR/nmea_sproxy.py" "$INSTALL_DIR/nmea_sproxy.py"
run_as_root install -m 0644 "$SCRIPT_DIR/meta_cleaner.py" "$INSTALL_DIR/meta_cleaner.py"
run_as_root install -m 0755 "$REPO_ROOT/tools/aismixer_keys.py" "$TOOLS_DIR/aismixer_keys.py"

echo "[+] Updating systemd unit files"
run_as_root install -m 0644 "$SCRIPT_DIR/nmea_sproxy.service" "$SYSTEMD_DIR/nmea_sproxy.service"
run_as_root install -m 0644 "$SCRIPT_DIR/nmea_sproxy@.service" "$SYSTEMD_DIR/nmea_sproxy@.service"
run_as_root systemctl daemon-reload

echo "[+] Update complete"
echo "    /etc/nmea_sproxy configs and keys were not changed."
echo "    Restart the singleton or selected template instances when ready."
