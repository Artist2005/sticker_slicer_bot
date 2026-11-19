# Step-by-step VM deployment guide

`sticker_slicer_bot` is self-contained. It uses its own SQLite database (`stickers.db`), an isolated Python `venv`, and a dedicated systemd service (`sticker-slicer.service`), so it runs safely alongside `tg_bot` and `seller_studio_bot` on the same Linux VM.

## Step 1. Create a bot in BotFather

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Enter a display name (for example, `Sticker Slicer & Puzzle Maker`) and a username ending in `bot`.
3. Copy the `BOT_TOKEN`.

## Step 2. Upload to your Linux VM (Ubuntu / Debian)

### Option A: Upload `sticker_slicer_bot.tar.gz` (GCP SSH browser or SCP)

1. Upload `sticker_slicer_bot.tar.gz` to your VM home directory (in Google Cloud SSH browser window, click **Upload file** and pick `sticker_slicer_bot/sticker_slicer_bot.tar.gz`, or use `scp` from PowerShell):

```powershell
scp c:\Users\PinchukRO\Desktop\TGbot\sticker_slicer_bot\sticker_slicer_bot.tar.gz USER@VM_IP:~/
```

2. On the VM, extract the archive into `~/sticker_slicer_bot`:

```bash
mkdir -p ~/sticker_slicer_bot
tar -xzf ~/sticker_slicer_bot.tar.gz -C ~/sticker_slicer_bot
cd ~/sticker_slicer_bot
```

### Option B: Pull via Git

If your repository is cloned on the VM, run `deploy.bat` locally to push changes, then pull on the VM:

```bash
cd ~/TGbot
git pull
cd sticker_slicer_bot
```

## Step 3. Configure `.env` and start the systemd service

1. Create `.env` and paste your `BOT_TOKEN`:

```bash
cp .env.example .env
nano .env
```

Set:

```env
BOT_TOKEN=1234567890:your_token_from_botfather
DEFAULT_EMOJI=🧩
MAX_CONCURRENT_JOBS=1
```

Save and exit (`Ctrl + O`, `Enter`, `Ctrl + X`).

2. Run the setup script and start the service:

```bash
sed -i 's/\r$//' setup_vm.sh
chmod +x setup_vm.sh
./setup_vm.sh
sudo systemctl restart sticker-slicer
sudo systemctl status sticker-slicer
```

## Useful service commands on the VM

- Check service status: `sudo systemctl status sticker-slicer`
- Watch live logs: `sudo journalctl -u sticker-slicer -f`
- Restart bot: `sudo systemctl restart sticker-slicer`
- Stop bot: `sudo systemctl stop sticker-slicer`
