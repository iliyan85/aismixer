#!/bin/bash
set -e

INSTALL_DIR=/opt/aismixer

echo "[+] Updating Python source files in $INSTALL_DIR"
sudo cp *.py $INSTALL_DIR
sudo mkdir -p $INSTALL_DIR/core
sudo cp core/*.py $INSTALL_DIR/core

echo "[+] Restarting aismixer service"
sudo systemctl restart aismixer

echo "[+] Update complete"
