#!/bin/bash
set -euo pipefail

INSTALL_DIR=/opt/nmea_sproxy
CONFIG_DIR=/etc/nmea_sproxy
SYSTEMD_DIR=/etc/systemd/system
PURGE_CONFIG=false
SCRIPT_NAME=$(basename -- "${BASH_SOURCE[0]}")

if [ "$#" -gt 1 ]; then
	echo "Usage: $SCRIPT_NAME [--purge-config]" >&2
	exit 2
fi

case "${1:-}" in
	"")
		;;
	--purge-config)
		PURGE_CONFIG=true
		;;
	*)
		echo "Unknown option: $1" >&2
		echo "Usage: $SCRIPT_NAME [--purge-config]" >&2
		exit 2
		;;
esac

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

echo "[+] Stopping proxy services"
run_as_root systemctl stop nmea_sproxy.service >/dev/null 2>&1 || true
run_as_root systemctl stop 'nmea_sproxy@*.service' >/dev/null 2>&1 || true

echo "[+] Disabling singleton service"
run_as_root systemctl disable nmea_sproxy.service >/dev/null 2>&1 || true

echo "[+] Disabling enabled template instances"
while read -r unit _; do
	if [ -n "$unit" ]; then
		run_as_root systemctl disable "$unit"
	fi
done < <(run_as_root systemctl list-unit-files 'nmea_sproxy@*.service' \
	--state=enabled --no-legend --no-pager || true)

echo "[+] Removing secure proxy runtime and systemd units"
run_as_root rm -rf -- "$INSTALL_DIR"
run_as_root rm -f -- "$SYSTEMD_DIR/nmea_sproxy.service" "$SYSTEMD_DIR/nmea_sproxy@.service"

if [ "$PURGE_CONFIG" = true ]; then
	echo "[!] Purging $CONFIG_DIR, including operator configs and keys"
	run_as_root rm -rf -- "$CONFIG_DIR"
else
	echo "[+] Preserving operator configs and keys in $CONFIG_DIR"
	echo "    Re-run with --purge-config only when that data should be deleted."
fi

run_as_root systemctl daemon-reload
echo "[+] Uninstall complete"
