#!/usr/bin/env python3
"""
Telegram Duplicate Link Detector Bot
- Stores links for 3 days (auto-expires on 4th day)
- Detects duplicate URLs (ignores names)
- Uses GitHub Gist for persistent storage
- Extracts links and optional names from messages
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN",   "YOUR_GITHUB_TOKEN")
GIST_ID        = os.environ.get("GIST_ID",        "YOUR_GIST_ID")   # create once, reuse
GIST_FILENAME  = "link_store.json"
KEEP_DAYS      = 3

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── URL EXTRACTION ─────────────────────────────────────────────────────────────
URL_RE = re.compile(
    r"https?://[^\s\)\]\>,\"\']+",
    re.IGNORECASE,
)

# Patterns like:  নাম: লিংক  |  Name - link  |  1. Name link
NAME_BEFORE_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+[.\)]\s*)?([^\n\-:।]+?)\s*[-:।]\s*(https?://\S+)",
    re.MULTILINE,
)
NAME_AFTER_RE = re.compile(
    r"(https?://\S+)\s*[-:]\s*([^\n]+)",
)

def normalize_url(url: str) -> str:
    """Strip trailing punctuation that may have been captured."""
    return url.rstrip(".,!?;)")

def extract_links(text: str) -> list[dict]:
    """
    Returns list of {"url": ..., "name": ...} dicts.
    Name is empty string if not found.
    """
    found: dict[str, str] = {}  # url -> name

    # Try name-before-link pattern first
    for m in NAME_BEFORE_RE.finditer(text):
        name = m.group(1).strip()
        url  = normalize_url(m.group(2).strip())
        found[url] = name

    # Try name-after-link pattern
    for m in NAME_AFTER_RE.finditer(text):
        url  = normalize_url(m.group(1).strip())
        name = m.group(2).strip()
        if url not in found:
            found[url] = name

    # Catch any remaining bare URLs
    for m in URL_RE.finditer(text):
        url = normalize_url(m.group(0))
        if url not in found:
            found[url] = ""

    return [{"url": u, "name": n} for u, n in found.items()]


# ─── GIST STORAGE ───────────────────────────────────────────────────────────────
GIST_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

async def gist_read() -> dict:
    """Read JSON store from Gist. Returns {} on error."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=GIST_HEADERS,
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
    """Write JSON store to Gist."""
    payload = {
        "files": {
            GIST_FILENAME: {"content": json.dumps(data, ensure_ascii=False, indent=2)}
        }
    }
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=GIST_HEADERS,
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
    """Remove entries older than KEEP_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    return {
        date_key: links
        for date_key, links in store.items()
        if datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=timezone.utc) >= cutoff
    }

def all_saved_urls(store: dict) -> set[str]:
    urls = set()
    for links in store.values():
        for item in links:
            urls.add(item["url"])
    return urls

async def process_links(new_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Compares new_items against stored links.
    Returns (unique_items, duplicate_items).
    Saves unique items to store.
    """
    store = await gist_read()
    store = expire_old_entries(store)
    saved_urls = all_saved_urls(store)

    unique_items    = []
    duplicate_items = []

    for item in new_items:
        if item["url"] in saved_urls:
            duplicate_items.append(item)
        else:
            unique_items.append(item)
            saved_urls.add(item["url"])   # prevent intra-batch duplicates

    # Append unique items under today's date
    today = today_str()
    if unique_items:
        store.setdefault(today, []).extend(unique_items)
        await gist_write(store)

    return unique_items, duplicate_items


# ─── TELEGRAM HANDLERS ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 আমি ডুপ্লিকেট লিংক ডিটেক্টর বট!\n\n"
        "যেকোনো বার্তায় লিংক পাঠান। আমি ৩ দিনের মধ্যে দেওয়া লিংকগুলোর সাথে মিলিয়ে দেখব।\n\n"
        "📌 কমান্ড:\n"
        "/start - এই বার্তা\n"
        "/stats - কতটি লিংক সেভ আছে\n"
        "/clear - সব লিংক মুছে ফেলুন"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store = await gist_read()
    store = expire_old_entries(store)
    total = sum(len(v) for v in store.values())
    lines = [f"📊 সংরক্ষিত লিংক: {total} টি (শেষ ৩ দিন)\n"]
    for date_key in sorted(store.keys()):
        lines.append(f"  {date_key}: {len(store[date_key])} টি")
    await update.message.reply_text("\n".join(lines))

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await gist_write({})
    await update.message.reply_text("✅ সব লিংক মুছে ফেলা হয়েছে।")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    if not text:
        return

    new_items = extract_links(text)
    if not new_items:
        return  # No links found, ignore silently

    unique, dupes = await process_links(new_items)

    lines = []

    if dupes:
        lines.append(f"🔁 *ডুপ্লিকেট লিংক পাওয়া গেছে ({len(dupes)} টি):*")
        for item in dupes:
            if item["name"]:
                lines.append(f"  • {item['name']}: {item['url']}")
            else:
                lines.append(f"  • {item['url']}")

    if unique:
        lines.append(f"\n✅ *নতুন লিংক সেভ হয়েছে ({len(unique)} টি):*")
        for item in unique:
            if item["name"]:
                lines.append(f"  • {item['name']}: {item['url']}")
            else:
                lines.append(f"  • {item['url']}")

    if lines:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )


# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    log.info("Bot starting…")
    app.run_polling()

if __name__ == "__main__":
    main()
      
