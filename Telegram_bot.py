#!/usr/bin/env python3
"""
Telegram Duplicate Link Detector Bot
- Stores links for 3 days (day = 6 AM BD time to next 6 AM BD time)
- Detects duplicate URLs (ignores names)
- Uses GitHub Gist for persistent storage
- Extracts links and the associated name/username from messages
- Only the bot OWNER can use this bot
- Auto-shuts down after MAX_RUNTIME_SECONDS (for CI job runtime limits)
- /clear has a 10-minute undo button for safety
"""

import os
import re
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import httpx

# ─── CONFIG ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN   = os.environ.get("GTHUB_TOKEN")
GIST_ID        = os.environ.get("GIST_ID")
OWNER_ID       = int(os.environ.get("OWNER_ID", "0"))
GIST_FILENAME  = "link_store.json"
KEEP_DAYS      = 3
BD_UTC_OFFSET  = 6          # Bangladesh = UTC+6
DAY_START_HOUR = 6          # নতুন দিন শুরু হয় সকাল ৬টায় (BD time)
UNDO_SECONDS   = 600        # Undo বাটন ১০ মিনিট কাজ করবে

# Auto-shutdown before GitHub Actions' 6-hour job limit
MAX_RUNTIME_SECONDS = 5 * 3600 + 59 * 60  # 5h 59m

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── undo store: { message_id: {"backup": store_dict, "expires": datetime} }
_undo_store: dict[int, dict] = {}


def validate_config():
    missing = []
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_TOKEN")
    if not GITHUB_TOKEN:   missing.append("GITHUB_TOKEN")
    if not GIST_ID:        missing.append("GIST_ID")
    if OWNER_ID == 0:      missing.append("OWNER_ID")
    if missing:
        raise ValueError(f"❌ Missing environment variables: {', '.join(missing)}")


# ─── SELF-SHUTDOWN TIMER ────────────────────────────────────────────────
def schedule_self_shutdown(seconds: int):
    def _shutdown():
        log.info("⏰ Max runtime (%s sec) reached — shutting down.", seconds)
        os._exit(0)
    timer = threading.Timer(seconds, _shutdown)
    timer.daemon = True
    timer.start()
    return timer


# ─── OWNER-ONLY GUARD ───────────────────────────────────────────────────
def is_owner(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


async def owner_only(update: Update) -> bool:
    if not is_owner(update):
        log.warning(
            "Unauthorized access attempt by user_id=%s username=%s",
            update.effective_user.id,
            update.effective_user.username,
        )
        return False
    return True


# ─── URL EXTRACTION ──────────────────────────────────────────────────────
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?x\.com/[^\s\)\]\>,\"\']+"
    r"|(?:https?://)?(?:www\.)?twitter\.com/[^\s\)\]\>,\"\']+"
    r"|https?://[^\s\)\]\>,\"\']+",
    re.IGNORECASE,
)

SKIP_LINE_RE = re.compile(
    r"^(post\s*#?\d+|#\d+|👤|link\s*dropped\s*:?$|লিংক|url\s*[:\-]?$|"
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\s]*$)",
    re.IGNORECASE,
)

TG_LINE_RE        = re.compile(r"^TG\s*[-–]\s*(@\S+)", re.IGNORECASE)
USERNAME_LABEL_RE = re.compile(r"^username\s*[:：]\s*(@?\S+)", re.IGNORECASE)
NAME_LABEL_RE     = re.compile(r"^name\s*[:：]\s*(.+)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = re.sub(r"[.,!?;)\]]+$", "", url)
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def clean_name_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[•\-\*\u2022]\s*", "", line)
    line = re.sub(r"^\d+[.)]\s*", "", line)
    line = line.rstrip(":：").strip()
    return line


def pick_name_for_line(prev_line: str) -> str | None:
    raw = prev_line.strip()
    if not raw:
        return None

    tg_match = TG_LINE_RE.match(raw)
    if tg_match:
        return tg_match.group(1)

    username_match = USERNAME_LABEL_RE.match(raw)
    if username_match:
        return username_match.group(1)

    name_match = NAME_LABEL_RE.match(raw)
    if name_match:
        return name_match.group(1).strip()

    if SKIP_LINE_RE.match(raw):
        return None

    at_match = re.match(r"^(@\S+)", raw)
    if at_match:
        return at_match.group(1)

    cleaned = clean_name_line(raw)
    return cleaned if cleaned else None


def extract_links(text: str) -> list[dict]:
    found: dict[str, str] = {}
    lines = text.splitlines()

    for i, line in enumerate(lines):
        line_stripped = line.strip()
        urls_in_line = URL_RE.findall(line_stripped)
        if not urls_in_line:
            continue

        for raw_url in urls_in_line:
            url = normalize_url(raw_url)
            if url in found:
                continue

            idx    = line_stripped.find(raw_url)
            before = line_stripped[:idx].strip().rstrip(":：-–—।").strip()
            before = re.sub(r"^[•\-\*\u2022]\s*", "", before)

            name = ""
            if before:
                name = before
            else:
                for j in range(i - 1, max(i - 6, -1), -1):
                    prev = lines[j].strip()
                    if not prev:
                        continue
                    if URL_RE.search(prev):
                        break
                    candidate = pick_name_for_line(prev)
                    if candidate is None:
                        continue
                    name = candidate
                    break

            found[url] = name

    return [{"url": u, "name": n} for u, n in found.items()]


# ─── BD-TIME DAY HELPERS ─────────────────────────────────────────────────
def bd_now() -> datetime:
    """বর্তমান সময় BD timezone এ (UTC+6)।"""
    return datetime.now(timezone.utc) + timedelta(hours=BD_UTC_OFFSET)


def today_str() -> str:
    """
    'আজকের' date key ফেরত দেয়।
    সকাল ৬টার আগে হলে আগের দিনকে আজ ধরা হয়।
    """
    now_bd = bd_now()
    if now_bd.hour < DAY_START_HOUR:
        now_bd -= timedelta(days=1)
    return now_bd.strftime("%Y-%m-%d")


def expire_old_entries(store: dict) -> dict:
    """
    শুধু শেষ KEEP_DAYS দিনের entries রাখে।
    date শুধু date এর সাথে তুলনা হয় — time নয়।
    """
    today_date  = datetime.strptime(today_str(), "%Y-%m-%d").date()
    cutoff_date = today_date - timedelta(days=KEEP_DAYS - 1)
    return {
        date_key: links
        for date_key, links in store.items()
        if datetime.strptime(date_key, "%Y-%m-%d").date() >= cutoff_date
    }


# ─── GIST STORAGE ─────────────────────────────────────────────────────────
def gist_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


async def gist_read() -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=gist_headers(),
            timeout=10,
        )
    if r.status_code != 200:
        log.error("Gist read failed: %s", r.text)
        return {}
    raw = r.json()["files"].get(GIST_FILENAME, {}).get("content", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def gist_write(data: dict) -> bool:
    payload = {
        "files": {
            GIST_FILENAME: {"content": json.dumps(data, ensure_ascii=False, indent=2)}
        }
    }
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=gist_headers(),
            json=payload,
            timeout=10,
        )
    if r.status_code not in (200, 201):
        log.error("Gist write failed: %s", r.text)
        return False
    return True


# ─── LINK STORE LOGIC ─────────────────────────────────────────────────────
def all_saved_urls(store: dict) -> set[str]:
    urls = set()
    for links in store.values():
        for item in links:
            urls.add(item["url"])
    return urls


async def process_links(new_items: list[dict]) -> tuple[list[dict], list[dict]]:
    try:
        store = await gist_read()
    except Exception:
        log.exception("gist_read failed")
        store = {}

    store      = expire_old_entries(store)
    saved_urls = all_saved_urls(store)

    unique_items    = []
    duplicate_items = []

    for item in new_items:
        if item["url"] in saved_urls:
            duplicate_items.append(item)
        else:
            unique_items.append(item)
            saved_urls.add(item["url"])

    today = today_str()
    if unique_items:
        store.setdefault(today, []).extend(unique_items)
        try:
            await gist_write(store)
        except Exception:
            log.exception("gist_write failed")

    return unique_items, duplicate_items


# ─── TELEGRAM HANDLERS ────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    await update.message.reply_text(
        "👋 আমি ডুপ্লিকেট লিংক ডিটেক্টর বট!\n\n"
        "যেকোনো ফরম্যাটে লিংক পাঠান। আমি ৩ দিনের মধ্যে দেওয়া লিংকগুলোর সাথে মিলিয়ে দেখব।\n\n"
        "📌 কমান্ড:\n"
        "/start - এই বার্তা\n"
        "/stats - কতটি লিংক সেভ আছে\n"
        "/clear - সব লিংক মুছে ফেলুন (১০ মিনিট undo সুযোগ)"
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    store = await gist_read()
    store = expire_old_entries(store)
    total = sum(len(v) for v in store.values())
    lines = [f"📊 সংরক্ষিত লিংক: {total} টি (শেষ ৩ দিন)\n"]
    for date_key in sorted(store.keys()):
        lines.append(f"  {date_key}: {len(store[date_key])} টি")
    await update.message.reply_text("\n".join(lines))


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    # ক্লিয়ার করার আগে backup নিয়ে রাখি
    backup = await gist_read()
    await gist_write({})

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=UNDO_SECONDS)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Undo / ফিরিয়ে আনুন", callback_data="undo_clear")]
    ])

    sent = await update.message.reply_text(
        "✅ সব লিংক মুছে ফেলা হয়েছে।\n"
        f"⏳ ১০ মিনিটের মধ্যে Undo করতে পারবেন।",
        reply_markup=keyboard,
    )

    # backup রাখি message id দিয়ে
    _undo_store[sent.message_id] = {
        "backup":  backup,
        "expires": expires_at,
    }

    # ১০ মিনিট পর বাটন সরিয়ে দেব
    async def remove_button():
        import asyncio
        await asyncio.sleep(UNDO_SECONDS)
        try:
            await sent.edit_reply_markup(reply_markup=None)
            await sent.edit_text(
                "✅ সব লিংক মুছে ফেলা হয়েছে।\n"
                "⌛ Undo এর সময় শেষ হয়ে গেছে।"
            )
        except Exception:
            pass
        _undo_store.pop(sent.message_id, None)

    import asyncio
    asyncio.create_task(remove_button())


async def callback_undo_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        await query.answer("❌ শুধু মালিক এটা করতে পারবেন।", show_alert=True)
        return

    msg_id = query.message.message_id
    entry  = _undo_store.get(msg_id)

    if not entry:
        await query.edit_message_text("⌛ Undo এর সময় শেষ হয়ে গেছে।")
        return

    if datetime.now(timezone.utc) > entry["expires"]:
        await query.edit_message_text("⌛ Undo এর সময় শেষ হয়ে গেছে।")
        _undo_store.pop(msg_id, None)
        return

    # backup ফিরিয়ে দিই
    ok = await gist_write(entry["backup"])
    _undo_store.pop(msg_id, None)

    if ok:
        total = sum(len(v) for v in entry["backup"].values())
        await query.edit_message_text(
            f"✅ সফলভাবে ফিরিয়ে আনা হয়েছে!\n"
            f"📊 মোট {total} টি লিংক পুনরুদ্ধার হয়েছে।"
        )
    else:
        await query.edit_message_text("❌ Undo ব্যর্থ হয়েছে — Gist write error।")


def format_item(item: dict) -> str:
    name = item["name"]
    url  = item["url"]
    if name:
        return f"  • {name} — {url}"
    return f"  • {url}"


TELEGRAM_MAX_LEN = 4096


async def send_long_message(update: Update, lines: list[str]):
    chunks  = []
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > TELEGRAM_MAX_LEN:
            if current:
                chunks.append(current)
            if len(line) > TELEGRAM_MAX_LEN:
                for k in range(0, len(line), TELEGRAM_MAX_LEN):
                    chunks.append(line[k:k + TELEGRAM_MAX_LEN])
                current = ""
            else:
                current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    for chunk in chunks:
        await update.message.reply_text(
            chunk,
            parse_mode=None,
            disable_web_page_preview=True,
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    text = update.message.text or update.message.caption or ""
    if not text:
        return

    new_items = extract_links(text)
    log.info("Extracted %d links: %s", len(new_items), new_items)

    if not new_items:
        return

    unique, dupes = await process_links(new_items)
    log.info("unique=%d dupes=%d", len(unique), len(dupes))

    lines = []

    if dupes:
        lines.append(f"🔁 ডুপ্লিকেট লিংক ({len(dupes)} টি):")
        for item in dupes:
            lines.append(format_item(item))

    if unique:
        lines.append(f"\n✅ নতুন লিংক সেভ ({len(unique)} টি):")
        for item in unique:
            lines.append(format_item(item))

    if lines:
        await send_long_message(update, lines)
    else:
        log.warning("No lines to send despite %d items", len(new_items))
        await update.message.reply_text("⚠️ কিছু প্রসেস হয়নি — log চেক করুন।")


# ─── MAIN ──────────────────────────────────────────────────────────────
def main():
    validate_config()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_undo_clear, pattern="^undo_clear$"))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    log.info("Bot starting… Owner ID: %s", OWNER_ID)
    schedule_self_shutdown(MAX_RUNTIME_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
        
