#!/bin/bash
set -e

INSTALL_DIR=/opt/aismixer
SYSTEMD_UNIT=/etc/systemd/system/aismixer.service
CONFIG_DIR=/etc/aismixer

echo "[+] Checking dependencies..."

for pkg in python3-setproctitle python3-yaml; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        echo "  - $pkg is installed"
    else
        echo "  - Missing: $pkg"
        echo "    Run: sudo apt install $pkg"
        exit 1
    fi
done

echo "[+] Installing aismixer to $INSTALL_DIR"
sudo mkdir -p $INSTALL_DIR
sudo cp aismixer.py meta_cleaner.py dedup.py forwarder.py assembler.py meta_writer.py $INSTALL_DIR

echo "[+] Installing config to $CONFIG_DIR"
sudo mkdir -p $CONFIG_DIR
sudo cp config.yaml $CONFIG_DIR

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
