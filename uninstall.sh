#!/bin/bash
set -e

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CONFIG_DIR=/etc/aismixer

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

echo "[+] Stopping and disabling systemd service"
if sudo systemctl is-active --quiet aismixer; then
	sudo systemctl stop aismixer
else
	echo "  - aismixer service is not active"
fi
if sudo systemctl is-enabled --quiet aismixer; then
	sudo systemctl disable aismixer
else
	echo "  - aismixer service is not enabled"
fi

echo "[+] Removing installed files"
sudo rm -rf "$INSTALL_DIR"
sudo rm -f "$SYSTEMD_UNIT"

if [ "$PURGE_CONFIG" = true ]; then
	echo "[!] Purging $CONFIG_DIR, including operator configs and keys"
	sudo rm -rf "$CONFIG_DIR"
else
	echo "[+] Preserving operator configs and keys in $CONFIG_DIR"
	echo "    Remove them manually or re-run this script with --purge-config"
fi

echo "[+] Reloading systemd"
sudo systemctl daemon-reload

echo "[+] Uninstall complete"
