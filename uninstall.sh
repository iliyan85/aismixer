#!/bin/bash
set -e

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CONFIG_DIR=/etc/aismixer

echo "[+] Stopping and disabling systemd service"
sudo systemctl stop aismixer || true
sudo systemctl disable aismixer || true

echo "[+] Removing installed files"
sudo rm -rf $INSTALL_DIR
sudo rm -f $SYSTEMD_UNIT
sudo rm -rf $CONFIG_DIR

echo "[+] Reloading systemd"
sudo systemctl daemon-reexec
sudo systemctl daemon-reload

echo "[+] Uninstall complete"
