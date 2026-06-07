#!/bin/bash
set -e

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CONFIG_DIR=/etc/aismixer
KEYS_DIR="$CONFIG_DIR/keys"
PRIVATE_KEY="$KEYS_DIR/aismixer_private.pem"
PUBLIC_KEY="$KEYS_DIR/aismixer_public.pem"
AUTHORIZED_KEYS_FILE="$CONFIG_DIR/authorized_keys.yaml"

echo "[+] Checking dependencies..."

for pkg in python3-setproctitle python3-yaml python3-cryptography; do
	if dpkg -s "$pkg" >/dev/null 2>&1; then
		echo "  - $pkg is installed"
	else
		echo "  - Missing: $pkg"
		echo "    Run: sudo apt install $pkg"
		exit 1
	fi
done

echo "[+] Installing aismixer to $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo cp *.py "$INSTALL_DIR"/
sudo mkdir -p "$INSTALL_DIR/core"
sudo cp -R core/. "$INSTALL_DIR/core"/
sudo mkdir -p "$INSTALL_DIR/tools"
sudo cp -R tools/. "$INSTALL_DIR/tools"/

echo "[+] Installing config to $CONFIG_DIR"
sudo mkdir -p "$CONFIG_DIR"
sudo cp config.yaml udp_alias_map.yaml "$CONFIG_DIR"

echo "[+] Preparing server keys in $KEYS_DIR"
sudo mkdir -p "$KEYS_DIR"
sudo chmod 700 "$KEYS_DIR"
private_key_exists=false
public_key_exists=false
if sudo test -e "$PRIVATE_KEY" || sudo test -L "$PRIVATE_KEY"; then
	private_key_exists=true
fi
if sudo test -e "$PUBLIC_KEY" || sudo test -L "$PUBLIC_KEY"; then
	public_key_exists=true
fi

if [ "$private_key_exists" = true ] && [ "$public_key_exists" = true ]; then
	echo "  - Existing server key pair found; preserving it"
elif [ "$private_key_exists" = false ] && [ "$public_key_exists" = false ]; then
	echo "  - No server keys found; generating a new key pair"
	sudo python3 "$INSTALL_DIR/tools/aismixer_keys.py" server --keys-dir "$KEYS_DIR"
else
	echo "[!] Incomplete server key pair in $KEYS_DIR; refusing to overwrite operator keys" >&2
	if [ "$private_key_exists" = false ]; then
		echo "    Missing: $PRIVATE_KEY" >&2
	else
		echo "    Missing: $PUBLIC_KEY" >&2
	fi
	echo "    Restore the missing key mate, or run the key tool manually with --force:" >&2
	echo "    sudo python3 \"$INSTALL_DIR/tools/aismixer_keys.py\" server --keys-dir \"$KEYS_DIR\" --force" >&2
	exit 1
fi

echo "[+] Preparing authorized client keys"
if sudo test -e "$AUTHORIZED_KEYS_FILE" || sudo test -L "$AUTHORIZED_KEYS_FILE"; then
	echo "  - Existing $AUTHORIZED_KEYS_FILE found; preserving it"
else
	printf '%s\n' 'authorized_clients: []' | sudo tee "$AUTHORIZED_KEYS_FILE" >/dev/null
	echo "  - Created $AUTHORIZED_KEYS_FILE"
fi

echo "[+] Creating systemd unit"
sudo tee $SYSTEMD_UNIT >/dev/null <<EOF
[Unit]
Description=AIS Mixer Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 aismixer.py
Restart=always
SyslogIdentifier=aismixer

[Install]
WantedBy=multi-user.target
EOF

echo "[+] Reloading systemd and enabling service"
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable aismixer
echo "[+] Done. You can now start the service with: sudo systemctl start aismixer"
