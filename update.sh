#!/bin/bash
set -e

INSTALL_DIR=/opt/aismixer

echo "[+] Updating Python source files in $INSTALL_DIR"
sudo cp aismixer.py aismixer_secure.py meta_cleaner.py dedup.py forwarder.py assembler.py meta_writer.py $INSTALL_DIR

echo "[+] Restarting aismixer service"
sudo systemctl restart aismixer

echo "[+] Update complete"
