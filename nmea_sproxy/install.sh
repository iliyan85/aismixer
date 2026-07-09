#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

INSTALL_DIR=/opt/nmea_sproxy
TOOLS_DIR="$INSTALL_DIR/tools"
CORE_DIR="$INSTALL_DIR/core"
CONFIG_DIR=/etc/nmea_sproxy
CONFIG_FILE="$CONFIG_DIR/config.yaml"
INSTANCES_DIR="$CONFIG_DIR/instances"
KEYS_DIR="$CONFIG_DIR/keys"
PRIVATE_KEY="$KEYS_DIR/station_private.pem"
PUBLIC_KEY="$KEYS_DIR/station_public.pem"
REMOTE_PUBLIC_KEY="$KEYS_DIR/aismixer_public.pem"
SYSTEMD_DIR=/etc/systemd/system
SINGLETON_UNIT="$SYSTEMD_DIR/nmea_sproxy.service"
TEMPLATE_UNIT="$SYSTEMD_DIR/nmea_sproxy@.service"
KEY_TOOL="$TOOLS_DIR/aismixer_keys.py"

if (( EUID == 0 )); then
	AS_ROOT=()
	ROOT_CMD_PREFIX=""
elif command -v sudo >/dev/null 2>&1; then
	AS_ROOT=(sudo)
	ROOT_CMD_PREFIX="sudo "
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

path_exists() {
	run_as_root test -e "$1" || run_as_root test -L "$1"
}

echo "[+] Checking dependencies"
for pkg in python3-setproctitle python3-yaml python3-cryptography python3-serial; do
	if dpkg -s "$pkg" >/dev/null 2>&1; then
		echo "  - $pkg is installed"
	else
		echo "[!] Missing dependency: $pkg" >&2
		echo "    Run: ${ROOT_CMD_PREFIX}apt install $pkg" >&2
		exit 1
	fi
done

echo "[+] Installing secure proxy runtime to $INSTALL_DIR"
run_as_root install -d -m 0755 "$INSTALL_DIR" "$TOOLS_DIR" "$CORE_DIR"
run_as_root install -m 0755 "$SCRIPT_DIR/nmea_sproxy.py" "$INSTALL_DIR/nmea_sproxy.py"
run_as_root install -m 0644 "$SCRIPT_DIR/input_adapters.py" "$INSTALL_DIR/input_adapters.py"
run_as_root install -m 0644 "$SCRIPT_DIR/output_adapters.py" "$INSTALL_DIR/output_adapters.py"
run_as_root install -m 0644 "$SCRIPT_DIR/meta_cleaner.py" "$INSTALL_DIR/meta_cleaner.py"
run_as_root install -m 0644 "$REPO_ROOT/core/network_policy.py" "$CORE_DIR/network_policy.py"
run_as_root install -m 0755 "$REPO_ROOT/tools/aismixer_keys.py" "$KEY_TOOL"

echo "[+] Preparing configuration layout"
run_as_root install -d -m 0755 "$CONFIG_DIR" "$INSTANCES_DIR"
run_as_root install -d -m 0700 "$KEYS_DIR"
if path_exists "$CONFIG_FILE"; then
	echo "  - Preserving existing singleton config: $CONFIG_FILE"
else
	run_as_root install -m 0644 "$SCRIPT_DIR/config.system.yaml" "$CONFIG_FILE"
	echo "  - Installed singleton config: $CONFIG_FILE"
fi
echo "  - Instance configs are operator-created under: $INSTANCES_DIR"

echo "[+] Preparing station keys"
if path_exists "$PRIVATE_KEY"; then
	echo "  - Preserving station private key and checking public key"
	run_as_root python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR" --repair-public
elif path_exists "$PUBLIC_KEY"; then
	echo "[!] Found $PUBLIC_KEY without $PRIVATE_KEY" >&2
	echo "    Refusing to generate or overwrite station private-key material." >&2
	exit 1
else
	echo "  - No station keys found; generating a new station key pair"
	run_as_root python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR"
fi

if ! path_exists "$REMOTE_PUBLIC_KEY"; then
	echo "[!] Missing AISMixer public key: $REMOTE_PUBLIC_KEY" >&2
	echo "    Copy the trusted server public key there before starting a proxy." >&2
fi

echo "[+] Installing singleton and template systemd units"
run_as_root install -m 0644 "$SCRIPT_DIR/nmea_sproxy.service" "$SINGLETON_UNIT"
run_as_root install -m 0644 "$SCRIPT_DIR/nmea_sproxy@.service" "$TEMPLATE_UNIT"

echo "[+] Reloading systemd and enabling only the singleton service"
run_as_root systemctl daemon-reload
run_as_root systemctl enable nmea_sproxy.service

echo "[+] Install complete; no services were started"
echo "    Singleton: ${ROOT_CMD_PREFIX}systemctl start nmea_sproxy.service"
echo "    Template example: ${ROOT_CMD_PREFIX}systemctl enable --now nmea_sproxy@boat.service"
