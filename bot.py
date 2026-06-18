import sys
sys.modules['telegram.ext._updater'] = None

"""
Telegram Advertising Manager Bot
- Supports text, photo, video (max 5 sec), and GIF ads.
- Target channel can be set dynamically via /setchannel.
- All messages include signature: Created by @Veronica_adbot
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
import aiosqlite

# -------------------- Configuration --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DEFAULT_CHANNEL = os.getenv("TARGET_CHANNEL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN must be set in .env")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID must be set in .env")

SIGNATURE = "\n\nCreated by @Veronica_adbot"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_PATH = "ads.db"

# -------------------- Database --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS advertisements (
                ad_id TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                media_file_id TEXT,
                caption TEXT,
                schedule_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        if DEFAULT_CHANNEL:
            await db.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES ('target_channel', ?)",
                (DEFAULT_CHANNEL,)
            )
        await db.commit()

async def get_config(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_config(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()

async def add_ad(ad_id: str, media_type: str, media_file_id: Optional[str], caption: Optional[str], schedule_time: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO advertisements (ad_id, media_type, media_file_id, caption, schedule_time, status) VALUES (?, ?, ?, ?, ?, 'pending')",
            (ad_id, media_type, media_file_id, caption, schedule_time)
        )
        await db.commit()

async def get_pending_ads():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ad_id, media_type, media_file_id, caption, schedule_time FROM advertisements WHERE status = 'pending' ORDER BY schedule_time"
        ) as cursor:
            return await cursor.fetchall()

async def get_all_ads(limit=20, offset=0):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ad_id, media_type, caption, schedule_time, status FROM advertisements ORDER BY schedule_time LIMIT ? OFFSET ?",
            (limit, offset)
        ) as cursor:
            return await cursor.fetchall()

async def delete_ad(ad_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM advertisements WHERE ad_id = ? AND status = 'pending'", (ad_id,))
        await db.commit()
        return db.total_changes > 0

async def mark_ad_posted(ad_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE advertisements SET status = 'posted' WHERE ad_id = ?", (ad_id,))
        await db.commit()

async def mark_ad_failed(ad_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE advertisements SET status = 'failed' WHERE ad_id = ?", (ad_id,))
        await db.commit()

# -------------------- Posting Helper --------------------
async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, media_type: str, media_file_id: str, caption: str):
    target = await get_config("target_channel")
    if not target:
        raise ValueError("Target channel not set. Use /setchannel to configure.")
    bot = context.bot
    if caption is None:
        caption = ""
    caption += SIGNATURE

    if media_type == "text":
        await bot.send_message(chat_id=target, text=caption, parse_mode=ParseMode.HTML)
    elif media_type == "photo":
        await bot.send_photo(chat_id=target, photo=media_file_id, caption=caption, parse_mode=ParseMode.HTML)
    elif media_type == "video":
        await bot.send_video(chat_id=target, video=media_file_id, caption=caption, parse_mode=ParseMode.HTML)
    elif media_type == "animation":
        await bot.send_animation(chat_id=target, animation=media_file_id, caption=caption, parse_mode=ParseMode.HTML)
    else:
        raise ValueError(f"Unsupported media_type: {media_type}")

async def post_ad(ad_id: str, media_type: str, media_file_id: Optional[str], caption: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    try:
        if media_type == "text" and not caption:
            caption = "(no content)"
        await post_to_channel(context, media_type, media_file_id or "", caption or "")
        await mark_ad_posted(ad_id)
        logger.info(f"Ad {ad_id} posted successfully")
    except Exception as e:
        logger.exception(f"Failed to post ad {ad_id}: {e}")
        await mark_ad_failed(ad_id)

# -------------------- Scheduler --------------------
async def schedule_pending_ads(scheduler: AsyncIOScheduler, context: ContextTypes.DEFAULT_TYPE):
    pending = await get_pending_ads()
    now = datetime.utcnow()
    for ad_id, media_type, media_file_id, caption, schedule_time_str in pending:
        schedule_time = datetime.fromisoformat(schedule_time_str)
        if schedule_time <= now:
            logger.warning(f"Ad {ad_id} schedule time {schedule_time} is in the past. Skipping.")
            await mark_ad_failed(ad_id)
            continue
        if not scheduler.get_job(ad_id):
            scheduler.add_job(
                post_ad,
                trigger=DateTrigger(run_date=schedule_time),
                args=[ad_id, media_type, media_file_id, caption, context],
                id=ad_id,
                misfire_grace_time=3600
            )
            logger.info(f"Scheduled ad {ad_id} for {schedule_time}")

# -------------------- Admin-only decorator --------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, context)
    return wrapper

# -------------------- Handlers --------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_config("target_channel") or "not set"
    await update.message.reply_text(
        f"📢 *Advertising Manager Bot*\n\n"
        f"**Target channel:** `{target}`\n\n"
        "**Commands:**\n"
        "/setchannel – set target channel (reply to a message from that channel)\n"
        "/createad – create a scheduled ad\n"
        "/postnow – instantly post replied message\n"
        "/schedulepost – schedule replied message\n"
        "/listads – list all ads\n"
        "/deletead – delete pending ad\n"
        "/status – bot status\n"
        "/help – show this help\n\n"
        f"{SIGNATURE}",
        parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

@admin_only
async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text(
            "❌ Reply to any message from the channel you want to set as target.\n"
            "Alternatively, provide the chat ID or @username: `/setchannel @my_channel`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    chat = replied.chat
    chat_id = chat.id
    if chat.type in ["channel", "group", "supergroup"]:
        await set_config("target_channel", str(chat_id))
        await update.message.reply_text(f"✅ Target channel set to `{chat_id}`\n{SIGNATURE}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ The replied message must be from a channel, group, or supergroup.")

@admin_only
async def create_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text(
                "❌ Usage:\n"
                "`/createad 2025-12-31 23:59:59 Your text`\n"
                "Or reply to a photo/video/GIF with:\n"
                "`/createad 2025-12-31 23:59:59 Caption`\n\n"
                "Time must be in **UTC**.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        date_str = args[0]
        time_str = args[1]
        schedule_str = f"{date_str} {time_str}"
        try:
            schedule_time = datetime.fromisoformat(schedule_str)
            if schedule_time <= datetime.utcnow():
                await update.message.reply_text("❌ Schedule time must be in the future.")
                return
        except ValueError:
            await update.message.reply_text("❌ Invalid datetime. Use: `YYYY-MM-DD HH:MM:SS` (UTC)", parse_mode=ParseMode.MARKDOWN)
            return

        caption = " ".join(args[2:]) if len(args) > 2 else None
        replied = update.message.reply_to_message
        media_type = "text"
        media_file_id = None

        if replied:
            if replied.text:
                media_type = "text"
                caption = replied.text if not caption else caption
            elif replied.photo:
                media_type = "photo"
                media_file_id = replied.photo[-1].file_id
                if not caption:
                    caption = replied.caption
            elif replied.video:
                if replied.video.duration > 5:
                    await update.message.reply_text("❌ Video duration must be ≤ 5 seconds.")
                    return
                media_type = "video"
                media_file_id = replied.video.file_id
                if not caption:
                    caption = replied.caption
            elif replied.animation:
                media_type = "animation"
                media_file_id = replied.animation.file_id
                if not caption:
                    caption = replied.caption
            else:
                await update.message.reply_text("❌ Unsupported media type. Use text, photo, video (≤5s), or GIF.")
                return
        else:
            if not caption:
                await update.message.reply_text("❌ Please provide ad text or reply to a media message.")
                return
            media_type = "text"

        ad_id = f"ad_{int(schedule_time.timestamp())}_{hash(caption or '') % 10000}"
        await add_ad(ad_id, media_type, media_file_id, caption, schedule_time.isoformat())

        scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
        scheduler.add_job(
            post_ad,
            trigger=DateTrigger(run_date=schedule_time),
            args=[ad_id, media_type, media_file_id, caption, context],
            id=ad_id,
            misfire_grace_time=3600
        )

        media_emoji = {"text": "📝", "photo": "🖼️", "video": "🎬", "animation": "🎞️"}
        await update.message.reply_text(
            f"✅ Ad created!\n{media_emoji.get(media_type, '📄')} Type: {media_type}\n"
            f"🆔 ID: `{ad_id}`\n🕒 Scheduled: {schedule_time} UTC\n\n{SIGNATURE}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.exception("Error in create_ad")
        await update.message.reply_text(f"❌ Failed to create ad: {e}")

@admin_only
async def postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text("❌ Reply to a message you want to post.")
        return

    try:
        if replied.text:
            await post_to_channel(context, "text", None, replied.text)
        elif replied.photo:
            await post_to_channel(context, "photo", replied.photo[-1].file_id, replied.caption or "")
        elif replied.video:
            if replied.video.duration > 5:
                await update.message.reply_text("❌ Video longer than 5 seconds cannot be posted.")
                return
            await post_to_channel(context, "video", replied.video.file_id, replied.caption or "")
        elif replied.animation:
            await post_to_channel(context, "animation", replied.animation.file_id, replied.caption or "")
        else:
            await update.message.reply_text("❌ Unsupported message type.")
            return

        await update.message.reply_text(f"✅ Posted to channel!\n{SIGNATURE}")
    except Exception as e:
        logger.exception("Error in postnow")
        await update.message.reply_text(f"❌ Failed to post: {e}")

@admin_only
async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text("❌ Reply to a message to schedule it.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/schedulepost 2025-12-31 23:59:59`", parse_mode=ParseMode.MARKDOWN)
        return

    date_str = context.args[0]
    time_str = context.args[1]
    schedule_str = f"{date_str} {time_str}"
    try:
        schedule_time = datetime.fromisoformat(schedule_str)
        if schedule_time <= datetime.utcnow():
            await update.message.reply_text("❌ Schedule time must be in the future.")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid datetime. Use: `YYYY-MM-DD HH:MM:SS` (UTC)", parse_mode=ParseMode.MARKDOWN)
        return

    if replied.text:
        media_type = "text"
        media_file_id = None
        caption = replied.text
    elif replied.photo:
        media_type = "photo"
        media_file_id = replied.photo[-1].file_id
        caption = replied.caption
    elif replied.video:
        if replied.video.duration > 5:
            await update.message.reply_text("❌ Video duration must be ≤ 5 seconds.")
            return
        media_type = "video"
        media_file_id = replied.video.file_id
        caption = replied.caption
    elif replied.animation:
        media_type = "animation"
        media_file_id = replied.animation.file_id
        caption = replied.caption
    else:
        await update.message.reply_text("❌ Unsupported message type.")
        return

    ad_id = f"ad_{int(schedule_time.timestamp())}_{hash(caption or '') % 10000}"
    await add_ad(ad_id, media_type, media_file_id, caption, schedule_time.isoformat())

    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    scheduler.add_job(
        post_ad,
        trigger=DateTrigger(run_date=schedule_time),
        args=[ad_id, media_type, media_file_id, caption, context],
        id=ad_id,
        misfire_grace_time=3600
    )

    await update.message.reply_text(
        f"✅ Scheduled for {schedule_time} UTC\n🆔 ID: `{ad_id}`\n\n{SIGNATURE}",
        parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def list_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        page = 1
        if context.args and context.args[0].isdigit():
            page = int(context.args[0])
        limit = 10
        offset = (page - 1) * limit

        ads = await get_all_ads(limit, offset)
        if not ads:
            await update.message.reply_text("📭 No ads found.")
            return

        msg = f"*Ads – Page {page}*\n\n"
        for ad_id, media_type, caption, schedule_time, status in ads:
            short_cap = (caption or "(no caption)")[:40] + "..." if caption and len(caption) > 40 else (caption or "(no caption)")
            msg += f"• `{ad_id}`\n  🧷 {media_type} | {short_cap}\n  🕒 {schedule_time}\n  🏷 {status}\n\n"

        await update.message.reply_text(msg + f"\n{SIGNATURE}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.exception("Error in list_ads")
        await update.message.reply_text(f"❌ Failed to list ads: {e}")

@admin_only
async def delete_ad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/deletead <ad_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    ad_id = context.args[0]
    deleted = await delete_ad(ad_id)
    if deleted:
        scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
        if scheduler.get_job(ad_id):
            scheduler.remove_job(ad_id)
        await update.message.reply_text(f"✅ Ad `{ad_id}` deleted.\n\n{SIGNATURE}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ Ad `{ad_id}` not found or already posted.\n\n{SIGNATURE}", parse_mode=ParseMode.MARKDOWN)

@admin_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scheduler: AsyncIOScheduler = context.bot_data["scheduler"]
    jobs = scheduler.get_jobs()
    target = await get_config("target_channel") or "not set"
    await update.message.reply_text(
        f"🤖 Bot is running.\n"
        f"📅 Scheduled ads: {len(jobs)}\n"
        f"📢 Target channel: `{target}`\n"
        f"👑 Admin ID: `{ADMIN_ID}`\n"
        f"⏱️ Video limit: 5 seconds\n\n{SIGNATURE}",
        parse_mode=ParseMode.MARKDOWN
    )

# -------------------- Main --------------------
async def post_startup(application: Application):
    await init_db()
    context = ContextTypes.DEFAULT_TYPE()
    context.bot = application.bot
    context.bot_data = application.bot_data
    scheduler = application.bot_data["scheduler"]
    await schedule_pending_ads(scheduler, context)
    logger.info("Startup complete – pending ads loaded.")

def main():
    scheduler = AsyncIOScheduler(timezone="UTC")
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["scheduler"] = scheduler

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setchannel", set_channel))
    app.add_handler(CommandHandler("createad", create_ad))
    app.add_handler(CommandHandler("postnow", postnow))
    app.add_handler(CommandHandler("schedulepost", schedule_post))
    app.add_handler(CommandHandler("listads", list_ads))
    app.add_handler(CommandHandler("deletead", delete_ad_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    async def startup():
        await post_startup(app)
        scheduler.start()

    loop = asyncio.get_event_loop()
    loop.create_task(startup())
    app.run_polling()

# ---- Added for Render Web Service ----
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn
import threading

async def healthcheck(request):
    return JSONResponse({"status": "ok"})

app_routes = [
    Route("/healthcheck", healthcheck),
]
starlette_app = Starlette(routes=app_routes)

def run_bot():
    main()

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    uvicorn.run(starlette_app, host="0.0.0.0", port=10000)
