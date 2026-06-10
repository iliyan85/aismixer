#!/bin/bash
set -euo pipefail

INSTALL_DIR=/opt/nmea_sproxy
CONFIG_DIR=/etc/nmea_sproxy
SYSTEMD_DIR=/etc/systemd/system
PURGE_CONFIG=false

if [ "$#" -gt 1 ]; then
	echo "Usage: $0 [--purge-config]" >&2
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
		echo "Usage: $0 [--purge-config]" >&2
		exit 2
		;;
esac

echo "[+] Stopping proxy services"
sudo systemctl stop nmea_sproxy.service >/dev/null 2>&1 || true
sudo systemctl stop 'nmea_sproxy@*.service' >/dev/null 2>&1 || true

echo "[+] Disabling singleton service"
sudo systemctl disable nmea_sproxy.service >/dev/null 2>&1 || true

echo "[+] Disabling enabled template instances"
while read -r unit _; do
	if [ -n "$unit" ]; then
		sudo systemctl disable "$unit"
	fi
done < <(systemctl list-unit-files 'nmea_sproxy@*.service' \
	--state=enabled --no-legend --no-pager || true)

echo "[+] Removing secure proxy runtime and systemd units"
sudo rm -rf "$INSTALL_DIR"
sudo rm -f "$SYSTEMD_DIR/nmea_sproxy.service" "$SYSTEMD_DIR/nmea_sproxy@.service"

if [ "$PURGE_CONFIG" = true ]; then
	echo "[!] Purging $CONFIG_DIR, including operator configs and keys"
	sudo rm -rf "$CONFIG_DIR"
else
	echo "[+] Preserving operator configs and keys in $CONFIG_DIR"
	echo "    Re-run with --purge-config only when that data should be deleted."
fi

sudo systemctl daemon-reload
echo "[+] Uninstall complete"
