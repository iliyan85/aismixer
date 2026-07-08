#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CONFIG_DIR=/etc/aismixer
KEYS_DIR="$CONFIG_DIR/keys"
PRIVATE_KEY="$KEYS_DIR/aismixer_private.pem"
PUBLIC_KEY="$KEYS_DIR/aismixer_public.pem"
AUTHORIZED_KEYS_FILE="$CONFIG_DIR/authorized_keys.yaml"

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

require_source_file() {
	if [ ! -f "$1" ]; then
		echo "[!] Missing required source file: $1" >&2
		exit 1
	fi
}

require_source_dir() {
	if [ ! -d "$1" ]; then
		echo "[!] Missing required source directory: $1" >&2
		exit 1
	fi
}

preflight_source_layout() {
	require_source_file "$SCRIPT_DIR/aismixer.py"
	require_source_dir "$SCRIPT_DIR/core"
	require_source_dir "$SCRIPT_DIR/tools"
	require_source_file "$SCRIPT_DIR/config.yaml"
	require_source_file "$SCRIPT_DIR/udp_alias_map.yaml"
	find "$SCRIPT_DIR" -maxdepth 1 -type f -name '*.py' -print0 >/dev/null
	find "$SCRIPT_DIR/core" -type f -print0 >/dev/null
	find "$SCRIPT_DIR/tools" -type f -print0 >/dev/null
}

install_top_level_python() {
	local source_path target_path

	find "$SCRIPT_DIR" -maxdepth 1 -type f -name '*.py' -print0 |
		while IFS= read -r -d '' source_path; do
			target_path="$INSTALL_DIR/$(basename -- "$source_path")"
			run_as_root install -m 0644 "$source_path" "$target_path"
		done
}

install_tree() {
	local source_dir=$1
	local dest_dir=$2
	local file_mode=$3
	local source_path relative_path target_path target_dir

	run_as_root install -d -m 0755 "$dest_dir"
	find "$source_dir" -type f -print0 |
		while IFS= read -r -d '' source_path; do
			relative_path=${source_path#"$source_dir"/}
			target_path="$dest_dir/$relative_path"
			target_dir=$(dirname -- "$target_path")
			run_as_root install -d -m 0755 "$target_dir"
			run_as_root install -m "$file_mode" "$source_path" "$target_path"
		done
}

preflight_source_layout

echo "[+] Checking dependencies..."

for pkg in python3-setproctitle python3-yaml python3-cryptography; do
	if dpkg -s "$pkg" >/dev/null 2>&1; then
		echo "  - $pkg is installed"
	else
		echo "  - Missing: $pkg"
		echo "    Run: ${ROOT_CMD_PREFIX}apt install $pkg"
		exit 1
	fi
done

echo "[+] Installing aismixer to $INSTALL_DIR"
run_as_root install -d -m 0755 "$INSTALL_DIR"
install_top_level_python
install_tree "$SCRIPT_DIR/core" "$INSTALL_DIR/core" 0644
install_tree "$SCRIPT_DIR/tools" "$INSTALL_DIR/tools" 0755

echo "[+] Installing config to $CONFIG_DIR"
run_as_root install -d -m 0755 "$CONFIG_DIR"
for config_name in config.yaml udp_alias_map.yaml; do
	source_path="$SCRIPT_DIR/$config_name"
	dest_path="$CONFIG_DIR/$config_name"
	if path_exists "$dest_path"; then
		echo "  - Existing $dest_path found; preserving it"
	else
		run_as_root install -m 0644 "$source_path" "$dest_path"
		echo "  - Installed $dest_path"
	fi
done

echo "[+] Preparing server keys in $KEYS_DIR"
run_as_root install -d -m 0700 "$KEYS_DIR"
private_key_exists=false
public_key_exists=false
if path_exists "$PRIVATE_KEY"; then
	private_key_exists=true
fi
if path_exists "$PUBLIC_KEY"; then
	public_key_exists=true
fi

if [ "$private_key_exists" = true ] && [ "$public_key_exists" = true ]; then
	echo "  - Existing server key pair found; preserving it"
elif [ "$private_key_exists" = false ] && [ "$public_key_exists" = false ]; then
	echo "  - No server keys found; generating a new key pair"
	run_as_root python3 "$INSTALL_DIR/tools/aismixer_keys.py" server --keys-dir "$KEYS_DIR"
else
	echo "[!] Incomplete server key pair in $KEYS_DIR; refusing to overwrite operator keys" >&2
	if [ "$private_key_exists" = false ]; then
		echo "    Missing: $PRIVATE_KEY" >&2
	else
		echo "    Missing: $PUBLIC_KEY" >&2
	fi
	echo "    Restore the missing key mate, or run the key tool manually with --force:" >&2
	echo "    ${ROOT_CMD_PREFIX}python3 \"$INSTALL_DIR/tools/aismixer_keys.py\" server --keys-dir \"$KEYS_DIR\" --force" >&2
	exit 1
fi

echo "[+] Preparing authorized client keys"
if path_exists "$AUTHORIZED_KEYS_FILE"; then
	echo "  - Existing $AUTHORIZED_KEYS_FILE found; preserving it"
else
	printf '%s\n' 'authorized_clients: []' | run_as_root tee "$AUTHORIZED_KEYS_FILE" >/dev/null
	run_as_root chmod 0644 "$AUTHORIZED_KEYS_FILE"
	echo "  - Created $AUTHORIZED_KEYS_FILE"
fi

echo "[+] Creating systemd unit"
run_as_root tee "$SYSTEMD_UNIT" >/dev/null <<EOF
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
run_as_root chmod 0644 "$SYSTEMD_UNIT"

echo "[+] Reloading systemd and enabling service"
run_as_root systemctl daemon-reload
run_as_root systemctl enable aismixer
echo "[+] Done. You can now start the service with: ${ROOT_CMD_PREFIX}systemctl start aismixer"
