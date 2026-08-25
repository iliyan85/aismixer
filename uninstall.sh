#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SCRIPT_NAME=$(basename -- "$SCRIPT_DIR/${BASH_SOURCE[0]##*/}")

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CLI_WRAPPER=/usr/local/bin/aismixerctl
CONFIG_DIR=/etc/aismixer

PURGE_CONFIG=false
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

echo "[+] Stopping and disabling systemd service"
if run_as_root systemctl is-active --quiet aismixer; then
	run_as_root systemctl stop aismixer
else
	echo "  - aismixer service is not active"
fi
if run_as_root systemctl is-enabled --quiet aismixer; then
	run_as_root systemctl disable aismixer
else
	echo "  - aismixer service is not enabled"
fi

echo "[+] Removing installed files"
run_as_root rm -rf -- "$INSTALL_DIR"
run_as_root rm -f -- "$SYSTEMD_UNIT"
run_as_root rm -f -- "$CLI_WRAPPER"

if [ "$PURGE_CONFIG" = true ]; then
	echo "[!] Purging $CONFIG_DIR, including operator configs and keys"
	run_as_root rm -rf -- "$CONFIG_DIR"
else
	echo "[+] Preserving operator configs and keys in $CONFIG_DIR"
	echo "    Remove them manually or re-run this script with --purge-config"
fi

echo "[+] Reloading systemd"
run_as_root systemctl daemon-reload

echo "[+] Uninstall complete"
