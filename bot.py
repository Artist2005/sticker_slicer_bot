import asyncio
import html
import io
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    InputSticker,
    Update,
)
from telegram.constants import ParseMode, StickerFormat, StickerType
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import StickerDB
from slicer import (
    SliceConfig,
    SliceResult,
    build_tiles_zip,
    max_tiles_for_pack,
    slice_image,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sticker_slicer_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DEFAULT_EMOJI = os.getenv("DEFAULT_EMOJI", "🧩").strip() or "🧩"
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2")))

SESSION_DIR = os.path.join(BASE_DIR, "temp_sessions")
DB_PATH = os.path.join(BASE_DIR, "stickers.db")
os.makedirs(SESSION_DIR, exist_ok=True)

db = StickerDB(DB_PATH, default_emoji=DEFAULT_EMOJI)
work_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
user_upload_locks: Dict[int, asyncio.Lock] = {}

SESSION_TTL_SECONDS = 2 * 3600
_last_cleanup_ts: float = 0.0

GRID_PRESETS: List[Tuple[int, int, str]] = [
    (0, 0, "Auto"),
    (2, 2, "2×2"),
    (3, 3, "3×3"),
    (4, 3, "4×3"),
    (4, 4, "4×4"),
    (4, 5, "4×5"),
    (5, 5, "5×5"),
]

FIT_LABELS = {
    "crop": "Fill (Crop)",
    "pad": "Fit (Pad)",
    "stretch": "Stretch",
}

STYLE_LABELS = {
    "seamless": "Seamless",
    "rounded": "Rounded",
    "border": "White Border",
}


def cleanup_old_sessions() -> None:
    global _last_cleanup_ts
    now = time.time()
    if (now - _last_cleanup_ts) < 600:
        return
    _last_cleanup_ts = now
    try:
        for fname in os.listdir(SESSION_DIR):
            fpath = os.path.join(SESSION_DIR, fname)
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > SESSION_TTL_SECONDS:
                try:
                    os.remove(fpath)
                except OSError:
                    pass
    except OSError:
        pass


def get_user_lock(user_id: int) -> asyncio.Lock:
    if len(user_upload_locks) > 5000:
        idle = [uid for uid, lk in user_upload_locks.items() if not lk.locked()]
        for uid in idle[:2500]:
            user_upload_locks.pop(uid, None)
    lock = user_upload_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_upload_locks[user_id] = lock
    return lock


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    if cp in (0x200D, 0xFE0F, 0x20E3):
        return True
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2300 <= cp <= 0x23FF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x1F1E6 <= cp <= 0x1F1FF
    )


def sanitize_emoji_list(raw: str, fallback: str = "🧩") -> List[str]:
    cleaned = "".join(ch for ch in raw.strip() if _is_emoji_char(ch))
    if not cleaned:
        return [fallback]
    clusters: List[str] = []
    cur = ""
    for ch in cleaned:
        cp = ord(ch)
        if cp in (0xFE0F, 0x20E3) or (0x1F3FB <= cp <= 0x1F3FF):
            if cur:
                cur += ch
        elif cp == 0x200D:
            if cur:
                cur += ch
        else:
            if cur and not cur.endswith("\u200d"):
                clusters.append(cur)
                cur = ch
            else:
                cur += ch
    if cur:
        clusters.append(cur.rstrip("\u200d"))
    valid = [c for c in clusters if c]
    return valid[:5] if valid else [fallback]


def generate_pack_short_name(user_id: int, bot_username: str) -> str:
    clean_bot = re.sub(r"[^a-zA-Z0-9_]", "", bot_username)
    suffix = f"_by_{clean_bot}"
    max_prefix = max(8, 64 - len(suffix))
    token = secrets.token_hex(4)
    prefix = f"s{user_id}_{token}"
    prefix = re.sub(r"_+", "_", prefix).strip("_")
    if len(prefix) > max_prefix:
        prefix = f"s_{token}"[:max_prefix].rstrip("_")
    return f"{prefix}{suffix}"


def build_default_title(user_name: str, pack_type: str, result: SliceResult) -> str:
    safe_user = (user_name or "My").strip()[:24]
    if pack_type == "custom_emoji":
        kind = "Emoji"
    else:
        kind = "Puzzle" if result.slice_mode == "grid" else "Stickers"
    if result.slice_mode == "grid":
        title = f"{safe_user} • {kind} {result.cols}x{result.rows}"
    else:
        title = f"{safe_user} • {kind} ({result.count})"
    return title[:64]


def prefs_to_config(prefs: Dict[str, Any]) -> SliceConfig:
    return SliceConfig(
        slice_mode=prefs.get("slice_mode", "grid"),
        cols=int(prefs.get("cols", 0)),
        rows=int(prefs.get("rows", 0)),
        fit_mode=prefs.get("fit_mode", "crop"),
        tile_style=prefs.get("tile_style", "seamless"),
        pack_type=prefs.get("pack_type", "regular"),
    )


def format_studio_caption(prefs: Dict[str, Any], result: SliceResult, user_name: str) -> str:
    pack_type = prefs.get("pack_type", "regular")
    pack_label = (
        "Emoji Pack (100×100 px)"
        if pack_type == "custom_emoji"
        else "Sticker Pack (512×512 px)"
    )
    custom_title = prefs.get("custom_title", "").strip()
    shown_title = custom_title if custom_title else build_default_title(user_name, pack_type, result)
    emoji = prefs.get("emoji", DEFAULT_EMOJI)

    if result.slice_mode == "grid":
        auto_tag = " (Auto)" if (prefs.get("cols", 0) == 0 or prefs.get("rows", 0) == 0) else ""
        mode_line = f"▦ Grid: <b>{result.cols}×{result.rows}{auto_tag}</b> ({result.count} tiles)"
        fit_line = f"🖼 Fit: <b>{FIT_LABELS.get(prefs.get('fit_mode', 'crop'), 'Fill (Crop)')}</b>\n"
    else:
        mode_line = f"🔍 Mode: <b>Separate Objects</b> (detected: <b>{result.count}</b>)"
        fit_line = ""

    style_name = STYLE_LABELS.get(prefs.get("tile_style", "seamless"), "Seamless")
    lines = [
        "✂️ <b>Slice Preview</b>\n",
        f"{mode_line}",
        f"{fit_line}🎨 Tile style: <b>{style_name}</b>",
        f"📦 Format: <b>{pack_label}</b>",
        f"🏷 Pack title: <b>{html.escape(shown_title)}</b>",
        f"😀 Emoji: {html.escape(emoji)}\n",
        "Adjust options below or tap <b>Create Pack</b>.",
    ]
    return "\n".join(lines)


def build_studio_keyboard(
    sid: str, prefs: Dict[str, Any], result: SliceResult, has_existing_packs: bool
) -> InlineKeyboardMarkup:
    slice_mode = prefs.get("slice_mode", "grid")
    cols_pref = int(prefs.get("cols", 0))
    rows_pref = int(prefs.get("rows", 0))
    fit_mode = prefs.get("fit_mode", "crop")
    tile_style = prefs.get("tile_style", "seamless")
    pack_type = prefs.get("pack_type", "regular")
    emoji = prefs.get("emoji", DEFAULT_EMOJI)

    rows_kb: List[List[InlineKeyboardButton]] = []

    rows_kb.append(
        [
            InlineKeyboardButton(
                f"{'✅ ' if slice_mode == 'grid' else ''}▦ Grid (Puzzle)",
                callback_data=f"sm|{sid}|grid",
            ),
            InlineKeyboardButton(
                f"{'✅ ' if slice_mode == 'objects' else ''}🔍 Separate Objects",
                callback_data=f"sm|{sid}|objects",
            ),
        ]
    )

    if slice_mode == "grid":
        preset_btns_1: List[InlineKeyboardButton] = []
        preset_btns_2: List[InlineKeyboardButton] = []
        matched_preset = False
        for idx, (gc, gr, label) in enumerate(GRID_PRESETS):
            selected = cols_pref == gc and rows_pref == gr
            if selected:
                matched_preset = True
            btn = InlineKeyboardButton(
                f"{'✅ ' if selected else ''}{label}",
                callback_data=f"gr|{sid}|{gc}x{gr}",
            )
            if idx < 4:
                preset_btns_1.append(btn)
            else:
                preset_btns_2.append(btn)

        custom_label = (
            f"✅ {cols_pref}×{rows_pref}"
            if (not matched_preset and cols_pref > 0 and rows_pref > 0)
            else "✏️ Custom NxM"
        )
        preset_btns_2.append(
            InlineKeyboardButton(custom_label, callback_data=f"ask|{sid}|grid")
        )
        rows_kb.append(preset_btns_1)
        rows_kb.append(preset_btns_2)

        rows_kb.append(
            [
                InlineKeyboardButton(
                    f"{'✅ ' if fit_mode == k else ''}{lbl}",
                    callback_data=f"ft|{sid}|{k}",
                )
                for k, lbl in FIT_LABELS.items()
            ]
        )

    rows_kb.append(
        [
            InlineKeyboardButton(
                f"{'✅ ' if tile_style == k else ''}{lbl}",
                callback_data=f"st|{sid}|{k}",
            )
            for k, lbl in STYLE_LABELS.items()
        ]
    )

    rows_kb.append(
        [
            InlineKeyboardButton(
                f"{'✅ ' if pack_type == 'regular' else ''}Sticker Pack 512px",
                callback_data=f"pt|{sid}|regular",
            ),
            InlineKeyboardButton(
                f"{'✅ ' if pack_type == 'custom_emoji' else ''}Emoji Pack 100px",
                callback_data=f"pt|{sid}|custom_emoji",
            ),
        ]
    )

    rows_kb.append(
        [
            InlineKeyboardButton("🏷 Set Title", callback_data=f"ask|{sid}|title"),
            InlineKeyboardButton(f"😀 Emoji: {emoji}", callback_data=f"ask|{sid}|emoji"),
        ]
    )

    action_title = (
        f"🚀 Create Emoji Pack ({result.count} pcs)"
        if pack_type == "custom_emoji"
        else f"🚀 Create Sticker Pack ({result.count} pcs)"
    )
    rows_kb.append(
        [InlineKeyboardButton(action_title, callback_data=f"mk|{sid}")]
    )

    extra_row: List[InlineKeyboardButton] = []
    if has_existing_packs:
        extra_row.append(
            InlineKeyboardButton("📂 Add to Existing Pack", callback_data=f"ap|{sid}")
        )
    extra_row.append(
        InlineKeyboardButton("📥 Download ZIP", callback_data=f"zp|{sid}")
    )
    rows_kb.append(extra_row)

    return InlineKeyboardMarkup(rows_kb)


def get_session_path(user_id: int, sid: str) -> str:
    safe_sid = re.sub(r"[^a-zA-Z0-9]", "", sid)
    return os.path.join(SESSION_DIR, f"u{user_id}_{safe_sid}.img")


async def run_slice_async(raw_bytes: bytes, cfg: SliceConfig) -> SliceResult:
    async with work_semaphore:
        return await asyncio.to_thread(slice_image, raw_bytes, cfg)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    context.user_data.pop("awaiting", None)
    _ = db.get_prefs(update.effective_user.id)

    text = (
        "👋 <b>Welcome! Send me any image and I will slice it into a Telegram sticker pack or custom emoji pack.</b>\n\n"
        "<b>How it works:</b>\n"
        "1. Send an image as a photo or as an uncompressed file.\n"
        "2. Configure your cut on the live preview:\n"
        "   • <b>▦ Grid (Puzzle)</b> slices the picture into even tiles (Auto 4-column sticker panel width, 2×2, 3×3, 4×4, or any custom NxM grid).\n"
        "   • <b>🔍 Separate Objects</b> detects individual drawings or stickers on a transparent or solid background and turns each piece into its own sticker.\n"
        "3. Tap <b>Create Sticker Pack</b> to publish the pack straight to Telegram.\n\n"
        "Commands:\n"
        "/mypacks — manage your created packs\n"
        "/help — tips for grid alignment and best quality"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    text = (
        "📖 <b>Slicing Tips</b>\n\n"
        "• <b>Which grid size aligns inside Telegram's sticker picker?</b>\n"
        "Telegram displays <b>4 stickers per row</b> in the standard sticker panel. "
        "Using <b>Auto</b> (4 columns) or presets like <b>4×3, 4×4, 4×5</b> keeps the puzzle aligned inside the picker.\n\n"
        "• <b>Sticker Pack vs. Emoji Pack:</b>\n"
        "Sticker packs use 512×512 px tiles (up to 120 per pack). "
        "Emoji packs use 100×100 px tiles (up to 200 per pack) and can be arranged inside text messages. "
        "When you create an emoji grid pack, the bot also posts the assembled mosaic message ready to copy.\n\n"
        "• <b>Separate Objects mode:</b>\n"
        "Send a sheet containing multiple characters, icons, or doodles on a transparent PNG or solid background. "
        "The bot isolates each piece and can add a die-cut white border.\n\n"
        "• <b>Best image quality:</b>\n"
        "Send your picture as a <b>File (Document)</b> so Telegram does not compress it before slicing."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_mypacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    uid = update.effective_user.id
    packs = db.list_packs(uid, limit=20)
    if not packs:
        await update.effective_message.reply_text(
            "You have not created any packs yet. Send an image to build your first sticker pack!"
        )
        return

    lines = ["📦 <b>Your Created Packs:</b>\n"]
    kb_rows: List[List[InlineKeyboardButton]] = []
    for idx, p in enumerate(packs, start=1):
        is_emoji = p["pack_type"] == "custom_emoji"
        url = (
            f"https://t.me/addemoji/{p['pack_name']}"
            if is_emoji
            else f"https://t.me/addstickers/{p['pack_name']}"
        )
        badge = "✨ Emoji" if is_emoji else "🧩 Stickers"
        title_esc = html.escape(p["pack_title"])
        lines.append(
            f"{idx}. {badge}: <a href=\"{url}\">{title_esc}</a> ({p['stickers_count']} pcs)"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(f"🔗 {p['pack_title'][:22]}", url=url),
                InlineKeyboardButton("🗑 Delete", callback_data=f"delp|{p['id']}"),
            ]
        )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def handle_incoming_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    context.user_data.pop("awaiting", None)
    cleanup_old_sessions()

    file_id: Optional[str] = None
    file_size: int = 0

    if msg.photo:
        best = msg.photo[-1]
        file_id = best.file_id
        file_size = best.file_size or 0
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        file_id = msg.document.file_id
        file_size = msg.document.file_size or 0
    elif msg.sticker and not msg.sticker.is_animated and not msg.sticker.is_video:
        file_id = msg.sticker.file_id
        file_size = msg.sticker.file_size or 0

    if not file_id:
        await msg.reply_text(
            "Please send a static image (JPG, PNG, or WEBP) as a photo or file."
        )
        return

    if file_size > 20 * 1024 * 1024:
        await msg.reply_text(
            "This file is larger than 20 MB. Please send a smaller image."
        )
        return

    status_msg = await msg.reply_text("⏳ Downloading image and rendering slice preview...")

    try:
        tg_file = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        raw_bytes = buf.getvalue()

        sid = secrets.token_hex(4)
        spath = get_session_path(user.id, sid)
        with open(spath, "wb") as f:
            f.write(raw_bytes)

        prefs = db.get_prefs(user.id)
        cfg = prefs_to_config(prefs)
        result = await run_slice_async(raw_bytes, cfg)

        existing = db.list_packs(user.id, pack_type=prefs.get("pack_type", "regular"), limit=1)
        caption = format_studio_caption(prefs, result, user.first_name)
        kb = build_studio_keyboard(sid, prefs, result, has_existing_packs=bool(existing))

        await status_msg.delete()
        sent = await msg.reply_photo(
            photo=result.preview_png,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        context.user_data["last_studio"] = {
            "sid": sid,
            "chat_id": sent.chat_id,
            "message_id": sent.message_id,
        }
    except Exception as exc:
        logger.exception("Failed to process incoming image: %s", exc)
        await status_msg.edit_text(
            "❌ Could not read this image. Make sure it is a valid PNG, JPG, or WEBP file."
        )


async def refresh_studio_message(
    chat_id: int,
    message_id: int,
    user_id: int,
    user_name: str,
    sid: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> Optional[SliceResult]:
    spath = get_session_path(user_id, sid)
    if not os.path.isfile(spath):
        return None

    with open(spath, "rb") as f:
        raw_bytes = f.read()

    prefs = db.get_prefs(user_id)
    cfg = prefs_to_config(prefs)
    result = await run_slice_async(raw_bytes, cfg)
    existing = db.list_packs(user_id, pack_type=prefs.get("pack_type", "regular"), limit=1)

    caption = format_studio_caption(prefs, result, user_name)
    kb = build_studio_keyboard(sid, prefs, result, has_existing_packs=bool(existing))

    media = InputMediaPhoto(
        media=result.preview_png,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=media,
            reply_markup=kb,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            logger.warning("edit_message_media warning: %s", exc)
    return result


async def handle_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return

    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        await msg.reply_text(
            "Send me an image (as a photo or file) to slice it into a sticker pack! 🧩"
        )
        return

    kind = awaiting.get("kind")
    sid = awaiting.get("sid", "")
    studio_chat_id = awaiting.get("chat_id", msg.chat_id)
    studio_msg_id = awaiting.get("message_id")
    raw_text = msg.text.strip()

    if kind == "grid":
        nums = re.findall(r"\d+", raw_text)
        if len(nums) != 2:
            await msg.reply_text(
                "Enter two numbers (columns and rows), for example: <code>4x6</code> or <code>5 3</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        cols, rows = int(nums[0]), int(nums[1])
        prefs = db.get_prefs(user.id)
        limit = max_tiles_for_pack(prefs.get("pack_type", "regular"))
        if cols < 1 or rows < 1 or cols > 20 or rows > 20:
            await msg.reply_text("Columns and rows must each be between 1 and 20.")
            return
        if cols * rows > limit:
            await msg.reply_text(
                f"Too many tiles ({cols * rows}). Telegram allows up to {limit} items for this pack type."
            )
            return
        db.update_prefs(user.id, slice_mode="grid", cols=cols, rows=rows)
        context.user_data.pop("awaiting", None)
        confirm = await msg.reply_text(f"✅ Grid set to {cols}×{rows}. Updating preview...")
        if studio_msg_id:
            await refresh_studio_message(
                studio_chat_id, studio_msg_id, user.id, user.first_name, sid, context
            )
        try:
            await confirm.delete()
        except TelegramError:
            pass
        return

    if kind == "title":
        clean_title = raw_text[:64].strip()
        if clean_title == "-":
            clean_title = ""
        db.update_prefs(user.id, custom_title=clean_title)
        context.user_data.pop("awaiting", None)
        confirm = await msg.reply_text("✅ Pack title updated.")
        if studio_msg_id:
            await refresh_studio_message(
                studio_chat_id, studio_msg_id, user.id, user.first_name, sid, context
            )
        try:
            await confirm.delete()
        except TelegramError:
            pass
        return

    if kind == "emoji":
        emojis = sanitize_emoji_list(raw_text, fallback="")
        if not emojis or emojis == [""]:
            await msg.reply_text(
                "No standard emoji detected. Send a regular emoji such as 🧩, 🔥, or 🐱."
            )
            return
        chosen = "".join(emojis[:3])
        db.update_prefs(user.id, emoji=chosen)
        context.user_data.pop("awaiting", None)
        confirm = await msg.reply_text(f"✅ Sticker emoji updated to {chosen}")
        if studio_msg_id:
            await refresh_studio_message(
                studio_chat_id, studio_msg_id, user.id, user.first_name, sid, context
            )
        try:
            await confirm.delete()
        except TelegramError:
            pass
        return


async def upload_new_pack(
    query_msg_chat_id: int,
    user_id: int,
    user_name: str,
    sid: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    spath = get_session_path(user_id, sid)
    if not os.path.isfile(spath):
        await context.bot.send_message(
            query_msg_chat_id,
            "⚠️ This image session has expired. Please send the image again.",
        )
        return

    progress_msg = await context.bot.send_message(
        query_msg_chat_id,
        "⏳ Slicing high-resolution tiles...",
    )

    try:
        with open(spath, "rb") as f:
            raw_bytes = f.read()

        prefs = db.get_prefs(user_id)
        cfg = prefs_to_config(prefs)
        result = await run_slice_async(raw_bytes, cfg)

        if not result.tiles_webp:
            await progress_msg.edit_text("❌ Could not extract any tiles from this image.")
            return

        bot_me = await context.bot.get_me()
        pack_name = generate_pack_short_name(user_id, bot_me.username or "bot")
        pack_type = prefs.get("pack_type", "regular")
        custom_title = prefs.get("custom_title", "").strip()
        pack_title = (
            custom_title[:64]
            if custom_title
            else build_default_title(user_name, pack_type, result)
        )
        emoji_list = sanitize_emoji_list(prefs.get("emoji", DEFAULT_EMOJI), DEFAULT_EMOJI)

        input_stickers: List[InputSticker] = [
            InputSticker(
                sticker=InputFile(tile_bytes, filename=f"tile_{idx:03d}.webp"),
                emoji_list=emoji_list,
                format=StickerFormat.STATIC,
            )
            for idx, tile_bytes in enumerate(result.tiles_webp, start=1)
        ]

        tg_sticker_type = (
            StickerType.CUSTOM_EMOJI
            if pack_type == "custom_emoji"
            else StickerType.REGULAR
        )

        first_batch = input_stickers[:50]
        rest_batch = input_stickers[50:]

        await progress_msg.edit_text(
            f"☁️ Uploading to Telegram ({min(len(input_stickers), 50)}/{len(input_stickers)} tiles)..."
        )

        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=pack_name,
            title=pack_title,
            stickers=first_batch,
            sticker_type=tg_sticker_type,
            read_timeout=90,
            write_timeout=90,
            connect_timeout=30,
        )

        for idx, st in enumerate(rest_batch, start=51):
            if idx == 51 or idx % 10 == 0 or idx == len(input_stickers):
                try:
                    await progress_msg.edit_text(
                        f"☁️ Uploading remaining tiles ({idx}/{len(input_stickers)})..."
                    )
                except TelegramError:
                    pass
            while True:
                try:
                    await context.bot.add_sticker_to_set(
                        user_id=user_id,
                        name=pack_name,
                        sticker=st,
                        read_timeout=60,
                        write_timeout=60,
                    )
                    break
                except RetryAfter as flood:
                    await asyncio.sleep(float(flood.retry_after) + 1.0)

        db.add_pack(
            user_id=user_id,
            pack_name=pack_name,
            pack_title=pack_title,
            pack_type=pack_type,
            stickers_count=len(input_stickers),
        )

        is_emoji = pack_type == "custom_emoji"
        pack_url = (
            f"https://t.me/addemoji/{pack_name}"
            if is_emoji
            else f"https://t.me/addstickers/{pack_name}"
        )

        sticker_set = await context.bot.get_sticker_set(pack_name)
        await progress_msg.delete()

        done_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Add Pack to Telegram",
                        url=pack_url,
                    )
                ]
            ]
        )

        summary = (
            f"✅ <b>Pack \"{html.escape(pack_title)}\" is ready!</b>\n\n"
            f"• Total tiles: <b>{len(input_stickers)}</b>\n"
            f"• Direct link: {pack_url}"
        )
        await context.bot.send_message(
            query_msg_chat_id,
            summary,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=done_kb,
        )

        if sticker_set.stickers:
            if is_emoji and result.slice_mode == "grid" and result.cols > 0:
                fallback_em = html.escape(emoji_list[0])
                mosaic_rows: List[str] = []
                idx_s = 0
                for _r in range(result.rows):
                    row_parts: List[str] = []
                    for _c in range(result.cols):
                        if idx_s < len(sticker_set.stickers):
                            ce_id = sticker_set.stickers[idx_s].custom_emoji_id
                            if ce_id:
                                row_parts.append(
                                    f'<tg-emoji emoji-id="{ce_id}">{fallback_em}</tg-emoji>'
                                )
                        idx_s += 1
                    if row_parts:
                        mosaic_rows.append("".join(row_parts))
                mosaic_html = "\n".join(mosaic_rows)
                if mosaic_html and len(mosaic_html) <= 4000:
                    try:
                        await context.bot.send_message(
                            query_msg_chat_id,
                            mosaic_html,
                            parse_mode=ParseMode.HTML,
                        )
                    except TelegramError:
                        pass
            elif not is_emoji:
                try:
                    await context.bot.send_sticker(
                        query_msg_chat_id,
                        sticker=sticker_set.stickers[0].file_id,
                    )
                except TelegramError:
                    pass

    except BadRequest as exc:
        err_text = str(exc)
        logger.error("Telegram BadRequest while creating pack: %s", err_text)
        if "Peer_id_invalid" in err_text or "user not found" in err_text.lower():
            msg_out = "❌ Please send /start in a direct message with the bot first so Telegram allows creating sticker packs for your account."
        else:
            msg_out = f"❌ Telegram API error: {html.escape(err_text)}"
        await progress_msg.edit_text(msg_out, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("Error while creating sticker pack: %s", exc)
        await progress_msg.edit_text(
            "❌ Failed to upload the sticker pack. Please try again in a few seconds."
        )


async def append_to_existing_pack(
    chat_id: int,
    user_id: int,
    sid: str,
    pack_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    pack = db.get_pack(pack_id, user_id)
    if not pack:
        await context.bot.send_message(chat_id, "⚠️ Selected pack was not found.")
        return

    spath = get_session_path(user_id, sid)
    if not os.path.isfile(spath):
        await context.bot.send_message(
            chat_id, "⚠️ This image session has expired. Please send the image again."
        )
        return

    progress_msg = await context.bot.send_message(
        chat_id, f"⏳ Slicing tiles to append to \"{pack['pack_title']}\"..."
    )

    try:
        with open(spath, "rb") as f:
            raw_bytes = f.read()

        prefs = db.get_prefs(user_id)
        cfg = prefs_to_config(prefs)
        cfg.pack_type = pack["pack_type"]
        result = await run_slice_async(raw_bytes, cfg)

        sticker_set = await context.bot.get_sticker_set(pack["pack_name"])
        limit = max_tiles_for_pack(pack["pack_type"])
        current_count = len(sticker_set.stickers)
        free_slots = limit - current_count

        if free_slots <= 0:
            await progress_msg.edit_text(
                f"⚠️ This pack is already full ({limit} items). Please create a new pack."
            )
            return

        tiles_to_add = result.tiles_webp[:free_slots]
        emoji_list = sanitize_emoji_list(prefs.get("emoji", DEFAULT_EMOJI), DEFAULT_EMOJI)

        for idx, tile_bytes in enumerate(tiles_to_add, start=1):
            if idx == 1 or idx % 8 == 0 or idx == len(tiles_to_add):
                try:
                    await progress_msg.edit_text(
                        f"☁️ Adding tiles to pack ({idx}/{len(tiles_to_add)})..."
                    )
                except TelegramError:
                    pass
            inp = InputSticker(
                sticker=InputFile(tile_bytes, filename=f"add_{idx:03d}.webp"),
                emoji_list=emoji_list,
                format=StickerFormat.STATIC,
            )
            while True:
                try:
                    await context.bot.add_sticker_to_set(
                        user_id=user_id,
                        name=pack["pack_name"],
                        sticker=inp,
                        read_timeout=60,
                        write_timeout=60,
                    )
                    break
                except RetryAfter as flood:
                    await asyncio.sleep(float(flood.retry_after) + 1.0)

        new_total = current_count + len(tiles_to_add)
        db.add_pack(
            user_id=user_id,
            pack_name=pack["pack_name"],
            pack_title=pack["pack_title"],
            pack_type=pack["pack_type"],
            stickers_count=new_total,
        )

        is_emoji = pack["pack_type"] == "custom_emoji"
        pack_url = (
            f"https://t.me/addemoji/{pack['pack_name']}"
            if is_emoji
            else f"https://t.me/addstickers/{pack['pack_name']}"
        )
        await progress_msg.edit_text(
            f"✅ Added <b>{len(tiles_to_add)} tiles</b> to <a href=\"{pack_url}\">{html.escape(pack['pack_title'])}</a> "
            f"(total in pack: {new_total}).",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔗 Open Pack", url=pack_url)]]
            ),
        )
    except Exception as exc:
        logger.exception("Error appending to sticker pack: %s", exc)
        await progress_msg.edit_text(
            f"❌ Failed to append stickers: {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    parts = query.data.split("|")
    action = parts[0]

    if action == "cancel_ask":
        context.user_data.pop("awaiting", None)
        await query.answer("Cancelled")
        if query.message:
            try:
                await query.message.delete()
            except TelegramError:
                pass
        return

    if action == "delp" and len(parts) == 2:
        pack_id = int(parts[1])
        pack = db.get_pack(pack_id, user.id)
        if not pack:
            await query.answer("Pack already deleted", show_alert=True)
            return
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, delete permanently",
                        callback_data=f"delp_ok|{pack_id}",
                    )
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="delp_no")],
            ]
        )
        await query.answer()
        if query.message:
            await query.message.reply_text(
                f"Permanently delete sticker pack <b>\"{html.escape(pack['pack_title'])}\"</b> from Telegram?",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        return

    if action == "delp_no":
        await query.answer("Cancelled")
        if query.message:
            try:
                await query.message.delete()
            except TelegramError:
                pass
        return

    if action == "delp_ok" and len(parts) == 2:
        pack_id = int(parts[1])
        pack = db.get_pack(pack_id, user.id)
        if not pack:
            await query.answer("Pack not found", show_alert=True)
            return
        try:
            await context.bot.delete_sticker_set(pack["pack_name"])
        except BadRequest as exc:
            if "Stickerset_invalid" not in str(exc):
                await query.answer(f"Error: {exc}", show_alert=True)
                return
        db.delete_pack_record(pack_id, user.id)
        await query.answer("Pack deleted!")
        if query.message:
            await query.message.edit_text(
                f"🗑 Pack <b>\"{html.escape(pack['pack_title'])}\"</b> has been deleted.",
                parse_mode=ParseMode.HTML,
            )
        return

    if len(parts) < 2 or not query.message:
        await query.answer()
        return

    sid = parts[1]
    spath = get_session_path(user.id, sid)
    if not os.path.isfile(spath):
        await query.answer(
            "This image session has expired. Please send the image again.",
            show_alert=True,
        )
        return

    if action in ("sm", "gr", "ft", "st", "pt") and len(parts) >= 3:
        val = parts[2]
        if action == "sm" and val in ("grid", "objects"):
            db.update_prefs(user.id, slice_mode=val)
        elif action == "gr":
            c_str, r_str = val.split("x", 1)
            db.update_prefs(
                user.id, slice_mode="grid", cols=int(c_str), rows=int(r_str)
            )
        elif action == "ft" and val in FIT_LABELS:
            db.update_prefs(user.id, fit_mode=val)
        elif action == "st" and val in STYLE_LABELS:
            db.update_prefs(user.id, tile_style=val)
        elif action == "pt" and val in ("regular", "custom_emoji"):
            db.update_prefs(user.id, pack_type=val)

        await query.answer("Updating preview...")
        await refresh_studio_message(
            query.message.chat_id,
            query.message.message_id,
            user.id,
            user.first_name,
            sid,
            context,
        )
        return

    if action == "ask" and len(parts) == 3:
        ask_kind = parts[2]
        context.user_data["awaiting"] = {
            "kind": ask_kind,
            "sid": sid,
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
        }
        cancel_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_ask")]]
        )
        await query.answer()
        if ask_kind == "grid":
            prompt = (
                "✏️ Send your custom grid size as <b>Columns×Rows</b>.\n"
                "Examples: <code>4x6</code>, <code>3x5</code>, or <code>8x4</code>."
            )
        elif ask_kind == "title":
            prompt = (
                "🏷 Send a custom pack title (up to 64 characters).\n"
                "Send a hyphen <code>-</code> to reset to the automatic title."
            )
        else:
            prompt = "😀 Send the emoji to associate with your stickers (for example: 🧩 or 🔥)."

        await query.message.reply_text(
            prompt, parse_mode=ParseMode.HTML, reply_markup=cancel_kb
        )
        return

    if action == "mk":
        ulock = get_user_lock(user.id)
        if ulock.locked():
            await query.answer("Your previous pack is still uploading, please wait...", show_alert=True)
            return
        await query.answer("Creating pack...")
        async with ulock:
            await upload_new_pack(
                query.message.chat_id, user.id, user.first_name, sid, context
            )
        return

    if action == "ap":
        prefs = db.get_prefs(user.id)
        packs = db.list_packs(
            user.id, pack_type=prefs.get("pack_type", "regular"), limit=10
        )
        if not packs:
            await query.answer(
                "You have no existing packs of this type yet. Tap Create Pack first.",
                show_alert=True,
            )
            return
        rows_p = [
            [
                InlineKeyboardButton(
                    f"📂 {p['pack_title'][:34]} ({p['stickers_count']} pcs)",
                    callback_data=f"apsel|{sid}|{p['id']}",
                )
            ]
            for p in packs
        ]
        rows_p.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_ask")]
        )
        await query.answer()
        await query.message.reply_text(
            "Select the pack you want to append these tiles to:",
            reply_markup=InlineKeyboardMarkup(rows_p),
        )
        return

    if action == "apsel" and len(parts) == 3:
        ulock = get_user_lock(user.id)
        if ulock.locked():
            await query.answer("An upload is already in progress, please wait...", show_alert=True)
            return
        pack_id = int(parts[2])
        await query.answer("Appending tiles...")
        try:
            await query.message.delete()
        except TelegramError:
            pass
        async with ulock:
            await append_to_existing_pack(
                query.message.chat_id, user.id, sid, pack_id, context
            )
        return

    if action == "zp":
        await query.answer("Building ZIP archive...")
        with open(spath, "rb") as f:
            raw_bytes = f.read()
        prefs = db.get_prefs(user.id)
        cfg = prefs_to_config(prefs)
        result = await run_slice_async(raw_bytes, cfg)
        zip_bytes = await asyncio.to_thread(
            build_tiles_zip, result.tiles_webp, result.cols
        )
        fname = (
            f"tiles_{result.cols}x{result.rows}.zip"
            if result.slice_mode == "grid"
            else f"stickers_{result.count}.zip"
        )
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=InputFile(zip_bytes, filename=fname),
            caption=f"📥 Sliced tiles ({result.count} pcs, WEBP {result.tile_px}×{result.tile_px} px).",
        )
        return


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("start", "Start the bot and view instructions"),
        BotCommand("mypacks", "List and manage your created packs"),
        BotCommand("help", "Tips for grid sizing and image quality"),
    ]
    await app.bot.set_my_commands(commands)
    me = await app.bot.get_me()
    logger.info("Bot started as @%s (id=%s)", me.username, me.id)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_telegram_bot_token_here":
        raise RuntimeError(
            "BOT_TOKEN is not set! Copy .env.example to .env and paste your token from @BotFather."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(32)
        .connection_pool_size(64)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("mypacks", cmd_mypacks))
    app.add_handler(CallbackQueryHandler(handle_callback))

    image_filter = (
        filters.PHOTO
        | filters.Document.IMAGE
        | (filters.Sticker.ALL & ~filters.Sticker.ANIMATED & ~filters.Sticker.VIDEO)
    )
    app.add_handler(MessageHandler(image_filter, handle_incoming_image))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)
    )

    logger.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
