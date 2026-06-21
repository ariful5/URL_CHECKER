#!/usr/bin/env python3
"""
Telegram Duplicate Link Detector Bot
- Stores links for 3 days (auto-expires on 4th day)
- Detects duplicate URLs (ignores names)
- Uses GitHub Gist for persistent storage
- Extracts links and the associated name/username from messages
- Only the bot OWNER can use this bot
- Auto-shuts down after MAX_RUNTIME_SECONDS (for CI job runtime limits)
"""

import os
import re
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

# ─── CONFIG ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN   = os.environ.get("GTHUB_TOKEN")
GIST_ID        = os.environ.get("GIST_ID")
OWNER_ID       = int(os.environ.get("OWNER_ID", "0"))
GIST_FILENAME  = "link_store.json"
KEEP_DAYS      = 3

# Auto-shutdown before GitHub Actions' 6-hour job limit, so the job exits
# cleanly and is reported as a success.
MAX_RUNTIME_SECONDS = 5 * 3600 + 59 * 60  # 5h 59m = 21540 seconds

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

# Lines that are clearly NOT a name/username line and should be skipped
# when scanning upward for a name (post headers, "Link dropped:", bullets,
# numbering, the bot banner, the literal TG- line is handled separately).
SKIP_LINE_RE = re.compile(
    r"^(post\s*#?\d+|#\d+|👤|link\s*dropped\s*:?$|লিংক|url\s*[:\-]?$|"
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\s]*$)",
    re.IGNORECASE,
)

TG_LINE_RE = re.compile(r"^TG\s*[-–]\s*(@\S+)", re.IGNORECASE)
USERNAME_LABEL_RE = re.compile(r"^username\s*[:：]\s*(@?\S+)", re.IGNORECASE)
NAME_LABEL_RE = re.compile(r"^name\s*[:：]\s*(.+)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = re.sub(r"[.,!?;)\]]+$", "", url)
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def clean_name_line(line: str) -> str:
    """Strip a trailing colon and leading bullet/number markers from a candidate name line."""
    line = line.strip()
    line = re.sub(r"^[•\-\*\u2022]\s*", "", line)
    line = re.sub(r"^\d+[.)]\s*", "", line)
    line = line.rstrip(":：").strip()
    return line


def pick_name_for_line(prev_line: str) -> str | None:
    """
    Given a single non-empty line immediately preceding (or near) a URL line,
    decide if it's usable as a name, and if so return the cleaned name.
    Returns None if this line should be skipped (not a name candidate).
    """
    raw = prev_line.strip()
    if not raw:
        return None

    # "TG - @username" -> use @username
    tg_match = TG_LINE_RE.match(raw)
    if tg_match:
        return tg_match.group(1)

    # "Username: @handle" -> use @handle (preferred over a "Name:" line above it)
    username_match = USERNAME_LABEL_RE.match(raw)
    if username_match:
        return username_match.group(1)

    # "Name: Some Person" -> use "Some Person"
    name_match = NAME_LABEL_RE.match(raw)
    if name_match:
        return name_match.group(1).strip()

    # Skip post headers, emoji-only lines, "Link dropped:", numbering like #171
    if SKIP_LINE_RE.match(raw):
        return None

    # A line like "@Ishan67262969 MadarM49564" -> take the @username token
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

            # Text before the URL on the same line (e.g. "Name: <url>")
            idx = line_stripped.find(raw_url)
            before = line_stripped[:idx].strip().rstrip(":：-–—।").strip()
            before = re.sub(r"^[•\-\*\u2022]\s*", "", before)

            name = ""
            if before:
                name = before
            else:
                # Scan upward for the nearest usable name line, skipping
                # blank lines, headers, "Link dropped:", emoji banners, etc.
                for j in range(i - 1, max(i - 6, -1), -1):
                    prev = lines[j].strip()
                    if not prev:
                        continue
                    if URL_RE.search(prev):
                        break  # hit a previous URL block, stop searching
                    candidate = pick_name_for_line(prev)
                    if candidate is None:
                        continue
                    name = candidate
                    break

            found[url] = name

    return [{"url": u, "name": n} for u, n in found.items()]


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

    store = expire_old_entries(store)
    saved_urls = all_saved_urls(store)

    unique_items = []
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


def format_item(item: dict) -> str:
    name = item["name"]
    url = item["url"]
    if name:
        return f"  • {name} — {url}"
    return f"  • {url}"


TELEGRAM_MAX_LEN = 4096


async def send_long_message(update: Update, lines: list[str]):
    """
    Telegram caps a single message at 4096 characters. When the link list
    is long, split it into multiple messages instead of sending one giant
    blob that Telegram would otherwise silently reject.
    """
    chunks = []
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > TELEGRAM_MAX_LEN:
            if current:
                chunks.append(current)
            # Single line itself longer than the limit (very rare) - hard split it
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    log.info("Bot starting… Owner ID: %s", OWNER_ID)
    schedule_self_shutdown(MAX_RUNTIME_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
