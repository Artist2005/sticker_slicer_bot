# Sticker Slicer Bot for Telegram

Telegram bot that slices images into regular sticker packs (`512x512` WEBP) or custom emoji packs (`100x100` WEBP) with a live visual preview.

## Features

- Grid puzzle slicing: automatic 4-column layout matching Telegram's sticker panel width, presets (`2x2`, `3x3`, `4x3`, `4x4`, `4x5`, `5x5`), and custom `NxM` dimensions.
- Separate object extraction: detects individual drawings or stickers on transparent or solid-color sheets, removes uniform backgrounds, and centers each piece on its own tile.
- Aspect ratio fit modes: center fill (`crop`), full image with transparent margins (`pad`), and exact resize (`stretch`).
- Tile styles: seamless puzzle edges, rounded cards, and white die-cut borders.
- Sticker and custom emoji packs: creates static sticker packs (up to 120 tiles) and custom emoji packs (up to 200 tiles, including a ready-to-copy assembled mosaic message).
- Pack management: list and delete created packs via `/mypacks`, append new sliced images to existing packs, and download all tiles as a `.zip` archive.

## Quick local run (Windows)

1. Create a new bot via `@BotFather` with `/newbot` and copy the token.
2. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`:

```powershell
cd sticker_slicer_bot
Copy-Item .env.example .env
```

3. Run with your virtual environment:

```powershell
..\.venv\Scripts\python.exe bot.py
```

## VM deployment (Ubuntu / Debian)

See `INSTRUCTION.md` for full step-by-step Linux VM setup using `sticker_slicer_bot.tar.gz` and `setup_vm.sh`.

## Project structure

- `bot.py` — Telegram Bot API handlers, live preview keyboard, pack upload, and `/mypacks` management.
- `slicer.py` — grid slicing, connected-component object extraction, preview rendering, tile styling, and WEBP encoding under 512 KB.
- `db.py` — SQLite storage for user settings and created packs.
- `setup_vm.sh` — automated Ubuntu/Debian installer and `systemd` service setup (`sticker-slicer.service`).
