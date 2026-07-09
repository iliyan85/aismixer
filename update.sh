#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CLI_WRAPPER=/usr/local/bin/aismixerctl
CLI_WRAPPER_DIR=/usr/local/bin

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
	require_source_file "$SCRIPT_DIR/aismixerctl.py"
	require_source_file "$SCRIPT_DIR/aismixer.service"
	require_source_dir "$SCRIPT_DIR/bin"
	require_source_file "$SCRIPT_DIR/bin/aismixerctl"
	require_source_dir "$SCRIPT_DIR/core"
	require_source_dir "$SCRIPT_DIR/tools"
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

echo "[+] Updating Python source files in $INSTALL_DIR"
run_as_root install -d -m 0755 "$INSTALL_DIR"
install_top_level_python
install_tree "$SCRIPT_DIR/core" "$INSTALL_DIR/core" 0644
install_tree "$SCRIPT_DIR/tools" "$INSTALL_DIR/tools" 0755

echo "[+] Updating systemd unit and aismixerctl command"
run_as_root install -m 0644 "$SCRIPT_DIR/aismixer.service" "$SYSTEMD_UNIT"
run_as_root install -d -m 0755 "$CLI_WRAPPER_DIR"
run_as_root install -m 0755 "$SCRIPT_DIR/bin/aismixerctl" "$CLI_WRAPPER"

echo "[+] Reloading systemd"
run_as_root systemctl daemon-reload

echo "[+] Restarting aismixer service"
run_as_root systemctl restart aismixer

echo "[+] Update complete"
