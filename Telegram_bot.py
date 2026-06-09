#!/usr/bin/env python3
"""
Telegram Duplicate Link Detector Bot
- Stores links for 3 days (auto-expires on 4th day)
- Detects duplicate URLs (ignores names)
- Uses GitHub Gist for persistent storage
- Extracts links and optional names from messages
- Only the bot OWNER can use this bot
"""

import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN   = os.environ.get("GTHUB_TOKEN")
GIST_ID        = os.environ.get("GIST_ID")
OWNER_ID       = int(os.environ.get("OWNER_ID", "0"))
GIST_FILENAME  = "link_store.json"
KEEP_DAYS      = 3

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

def validate_config():
    missing = []
    if not TELEGRAM_TOKEN: missing.append("TELEGRAM_TOKEN")
    if not GITHUB_TOKEN:   missing.append("GITHUB_TOKEN")
    if not GIST_ID:        missing.append("GIST_ID")
    if OWNER_ID == 0:      missing.append("OWNER_ID")
    if missing:
        raise ValueError(f"❌ Missing environment variables: {', '.join(missing)}")

# ─── OWNER-ONLY GUARD ──────────────────────────────────────────────────────────
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

# ─── URL EXTRACTION ─────────────────────────────────────────────────────────────
URL_RE = re.compile(
    r"https?://[^\s\)\]\>,\"\']+",
    re.IGNORECASE,
)

# নাম হিসেবে skip করার pattern
SKIP_LINE_RE = re.compile(
    r"^(TG\s*[-–]|👤|@|\d+[.)]\s*|link\s*dropped|লিংক|url\s*[:\-]?$)",
    re.IGNORECASE,
)

def normalize_url(url: str) -> str:
    """trailing punctuation সরাবে কিন্তু query params রাখবে"""
    return re.sub(r"[.,!?;)\]]+$", "", url)

def extract_links(text: str) -> list[dict]:
    """
    যেকোনো ফরম্যাট থেকে লিংক + নাম বের করে।
    নাম না পেলে খালি string রাখে।
    """
    found: dict[str, str] = {}  # url → name
    lines = text.splitlines()

    for i, line in enumerate(lines):
        line_stripped = line.strip()
        urls_in_line = URL_RE.findall(line_stripped)
        if not urls_in_line:
            continue

        for raw_url in urls_in_line:
            url = normalize_url(raw_url)
            if url in found:
                continue  # ✅ একই রানে duplicate skip

            # একই লাইনে URL-এর আগে কিছু আছে কিনা দেখো
            before = line_stripped[:line_stripped.index(raw_url)].strip().rstrip(":-–—।").strip()

            if before and not SKIP_LINE_RE.match(before):
                found[url] = before
                continue

            # আগের সর্বোচ্চ ৪টি লাইন থেকে নাম খোঁজো
            name = ""
            for j in range(i - 1, max(i - 5, -1), -1):
                prev = lines[j].strip()
                if not prev:
                    continue
                if SKIP_LINE_RE.match(prev):
                    continue
                # অন্য URL থাকলে stop
                if URL_RE.search(prev):
                    break
                candidate = prev.rstrip(":").strip()
                if candidate:
                    name = candidate
                    break

            found[url] = name

    # Fallback: কোনো URL মিস হলে ধরো
    for m in URL_RE.finditer(text):
        url = normalize_url(m.group(0))
        if url not in found:
            found[url] = ""

    return [{"url": u, "name": n} for u, n in found.items()]


# ─── GIST STORAGE ───────────────────────────────────────────────────────────────
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


# ─── LINK STORE LOGIC ───────────────────────────────────────────────────────────
def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def expire_old_entries(store: dict) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    return {
        date_key: links
        for date_key, links in store.items()
        if datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=timezone.utc) >= cutoff
    }

def all_saved_urls(store: dict) -> set[str]:
    """Gist-এ সেভ থাকা সব URL একটা set-এ রিটার্ন করে"""
    urls = set()
    for links in store.values():
        for item in links:
            urls.add(item["url"])
    return urls

async def process_links(new_items: list[dict]) -> tuple[list[dict], list[dict]]:
    store = await gist_read()
    store = expire_old_entries(store)
    saved_urls = all_saved_urls(store)  # ✅ Gist থেকে আগের সব URL

    unique_items    = []
    duplicate_items = []

    for item in new_items:
        if item["url"] in saved_urls:
            # ✅ আগে থেকে Gist-এ আছে → duplicate
            duplicate_items.append(item)
        else:
            unique_items.append(item)
            saved_urls.add(item["url"])  # ✅ এই রানে আর দ্বিতীয়বার সেভ হবে না

    today = today_str()
    if unique_items:
        store.setdefault(today, []).extend(unique_items)
        await gist_write(store)

    return unique_items, duplicate_items


# ─── TELEGRAM HANDLERS ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    await update.message.reply_text(
        "👋 আমি ডুপ্লিকেট লিংক ডিটেক্টর বট!\n\n"
        "যেকোনো ফরম্যাটে লিংক পাঠান। আমি ৩ দিনের মধ্যে দেওয়া লিংকগুলোর সাথে মিলিয়ে দেখব।\n\n"
        "📌 কমান্ড:\n"
        "/start - এই বার্তা\n"
        "/stats - কতটি লিংক সেভ আছে\n"
        "/clear - সব লিংক মুছে ফেলুন"
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
    await gist_write({})
    await update.message.reply_text("✅ সব লিংক মুছে ফেলা হয়েছে।")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    text = update.message.text or update.message.caption or ""
    if not text:
        return

    new_items = extract_links(text)
    if not new_items:
        return

    unique, dupes = await process_links(new_items)

    lines = []

    if dupes:
        lines.append(f"🔁 *ডুপ্লিকেট লিংক ({len(dupes)} টি):*")
        for item in dupes:
            label = item["name"] if item["name"] else item["url"]
            lines.append(f"  • {label}")

    if unique:
        lines.append(f"\n✅ *নতুন লিংক সেভ ({len(unique)} টি):*")
        for item in unique:
            label = item["name"] if item["name"] else item["url"]
            lines.append(f"  • {label}")

    if lines:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )


# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    validate_config()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    log.info("Bot starting… Owner ID: %s", OWNER_ID)
    app.run_polling()

if __name__ == "__main__":
    main()
