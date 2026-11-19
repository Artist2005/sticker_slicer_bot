#!/usr/bin/env bash
set -euo pipefail

RUN_USER="$(whoami)"
APP_DIR="/home/${RUN_USER}/sticker_slicer_bot"
SERVICE_NAME="sticker-slicer"

echo "[1/4] Installing required system packages..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv

echo "[2/4] Preparing application directory ${APP_DIR}..."
mkdir -p "${APP_DIR}"
if [ "$(pwd)" != "${APP_DIR}" ]; then
    cp -r bot.py slicer.py db.py requirements.txt .env.example "${APP_DIR}/"
    if [ -f .env ]; then
        cp .env "${APP_DIR}/.env"
    fi
fi

cd "${APP_DIR}"

echo "[3/4] Creating isolated Python virtual environment..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
fi

echo "[4/4] Registering systemd service (${SERVICE_NAME}.service)..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Telegram Sticker Slicer Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=5
Nice=10
CPUQuota=70%
MemoryHigh=300M
MemoryMax=450M
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo "Setup finished! Service ${SERVICE_NAME} is registered."
