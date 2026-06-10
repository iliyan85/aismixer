#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

INSTALL_DIR=/opt/nmea_sproxy
TOOLS_DIR="$INSTALL_DIR/tools"
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

path_exists() {
	sudo test -e "$1" || sudo test -L "$1"
}

echo "[+] Checking dependencies"
for pkg in python3-setproctitle python3-yaml python3-cryptography; do
	if dpkg -s "$pkg" >/dev/null 2>&1; then
		echo "  - $pkg is installed"
	else
		echo "[!] Missing dependency: $pkg" >&2
		echo "    Run: sudo apt install $pkg" >&2
		exit 1
	fi
done

echo "[+] Installing secure proxy runtime to $INSTALL_DIR"
sudo install -d -m 0755 "$INSTALL_DIR" "$TOOLS_DIR"
sudo install -m 0755 "$SCRIPT_DIR/nmea_sproxy.py" "$INSTALL_DIR/nmea_sproxy.py"
sudo install -m 0644 "$SCRIPT_DIR/meta_cleaner.py" "$INSTALL_DIR/meta_cleaner.py"
sudo install -m 0755 "$REPO_ROOT/tools/aismixer_keys.py" "$KEY_TOOL"

echo "[+] Preparing configuration layout"
sudo install -d -m 0755 "$CONFIG_DIR" "$INSTANCES_DIR"
sudo install -d -m 0700 "$KEYS_DIR"
if path_exists "$CONFIG_FILE"; then
	echo "  - Preserving existing singleton config: $CONFIG_FILE"
else
	sudo install -m 0644 "$SCRIPT_DIR/config.yaml" "$CONFIG_FILE"
	echo "  - Installed singleton config: $CONFIG_FILE"
fi
echo "  - Instance configs are operator-created under: $INSTANCES_DIR"

echo "[+] Preparing station keys"
if path_exists "$PRIVATE_KEY"; then
	echo "  - Preserving station private key and checking public key"
	sudo python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR" --repair-public
elif path_exists "$PUBLIC_KEY"; then
	echo "[!] Found $PUBLIC_KEY without $PRIVATE_KEY" >&2
	echo "    Refusing to generate or overwrite station private-key material." >&2
	exit 1
else
	echo "  - No station keys found; generating a new station key pair"
	sudo python3 "$KEY_TOOL" station --keys-dir "$KEYS_DIR"
fi

if ! path_exists "$REMOTE_PUBLIC_KEY"; then
	echo "[!] Missing AISMixer public key: $REMOTE_PUBLIC_KEY" >&2
	echo "    Copy the trusted server public key there before starting a proxy." >&2
fi

echo "[+] Installing singleton and template systemd units"
sudo install -m 0644 "$SCRIPT_DIR/nmea_sproxy.service" "$SINGLETON_UNIT"
sudo install -m 0644 "$SCRIPT_DIR/nmea_sproxy@.service" "$TEMPLATE_UNIT"

echo "[+] Reloading systemd and enabling only the singleton service"
sudo systemctl daemon-reload
sudo systemctl enable nmea_sproxy.service

echo "[+] Install complete; no services were started"
echo "    Singleton: sudo systemctl start nmea_sproxy.service"
echo "    Template example: sudo systemctl enable --now nmea_sproxy@boat.service"
