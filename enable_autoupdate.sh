#!/usr/bin/env bash
set -euo pipefail

RUN_USER="$(whoami)"
APP_DIR="/home/${RUN_USER}/sticker_slicer_bot"
REPO_URL="${1:-https://github.com/Artist2005/sticker_slicer_bot.git}"
REPO_DIR="/home/${RUN_USER}/sticker_slicer_repo"
SERVICE_NAME="sticker-slicer"
UPDATER_NAME="sticker-slicer-autoupdate"

echo "[1/3] Preparing Git repository for auto-updates (${REPO_URL})..."
sudo apt install -y git rsync >/dev/null 2>&1 || true

if [ ! -d "${REPO_DIR}/.git" ]; then
    git clone --depth 1 --branch main "${REPO_URL}" "${REPO_DIR}"
else
    git -C "${REPO_DIR}" remote set-url origin "${REPO_URL}"
    git -C "${REPO_DIR}" fetch origin main
    git -C "${REPO_DIR}" reset --hard origin/main
fi

echo "[2/3] Creating sync script ${APP_DIR}/sync_from_git.sh..."
mkdir -p "${APP_DIR}"
cat > "${APP_DIR}/sync_from_git.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR}"
APP_DIR="${APP_DIR}"
SERVICE_NAME="${SERVICE_NAME}"

cd "\${REPO_DIR}"
git fetch origin main --quiet

LOCAL_HASH=\$(git rev-parse HEAD)
REMOTE_HASH=\$(git rev-parse origin/main)

if [ "\${LOCAL_HASH}" = "\${REMOTE_HASH}" ]; then
    exit 0
fi

if [ -f "\${REPO_DIR}/sticker_slicer_bot/bot.py" ]; then
    SUBPATH="sticker_slicer_bot/"
    SRC_DIR="\${REPO_DIR}/sticker_slicer_bot/"
    REQ_PATH="sticker_slicer_bot/requirements.txt"
else
    SUBPATH="."
    SRC_DIR="\${REPO_DIR}/"
    REQ_PATH="requirements.txt"
fi

CHANGED=\$(git diff --name-only "\${LOCAL_HASH}" "\${REMOTE_HASH}" -- "\${SUBPATH}" || true)
REQ_CHANGED=\$(git diff --name-only "\${LOCAL_HASH}" "\${REMOTE_HASH}" -- "\${REQ_PATH}" || true)

git reset --hard origin/main --quiet

if [ -n "\${CHANGED}" ]; then
    rsync -a --exclude='.git/' --exclude='.env' --exclude='*.db*' --exclude='temp_sessions/' --exclude='venv/' --exclude='__pycache__/' --exclude='*.tar.gz' "\${SRC_DIR}" "\${APP_DIR}/"
    if [ -n "\${REQ_CHANGED}" ] && [ -x "\${APP_DIR}/venv/bin/pip" ]; then
        "\${APP_DIR}/venv/bin/pip" install -q -r "\${APP_DIR}/requirements.txt"
    fi
    sudo systemctl restart "\${SERVICE_NAME}"
    echo "Updated \${SERVICE_NAME} to commit \${REMOTE_HASH}"
fi
EOF

chmod +x "${APP_DIR}/sync_from_git.sh"

echo "[3/3] Installing systemd timer (${UPDATER_NAME}.timer - checks every 30s)..."
sudo tee "/etc/systemd/system/${UPDATER_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Auto-update Sticker Slicer Bot from GitHub
After=network-online.target

[Service]
Type=oneshot
User=${RUN_USER}
ExecStart=${APP_DIR}/sync_from_git.sh
EOF

sudo tee "/etc/systemd/system/${UPDATER_NAME}.timer" > /dev/null <<EOF
[Unit]
Description=Check GitHub every 30 seconds for Sticker Slicer Bot updates

[Timer]
OnBootSec=20s
OnUnitActiveSec=30s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

echo "${RUN_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl restart ${SERVICE_NAME}" | sudo tee "/etc/sudoers.d/${SERVICE_NAME}-restart" > /dev/null
sudo chmod 440 "/etc/sudoers.d/${SERVICE_NAME}-restart"

sudo systemctl daemon-reload
sudo systemctl enable --now "${UPDATER_NAME}.timer"

echo "Auto-update enabled! Whenever you run deploy.bat on your PC, the VM will update and restart ${SERVICE_NAME} within 30 seconds."
