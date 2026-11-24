import asyncio
import os
import json
import re
import random
from datetime import datetime
from torrent_search import torrent_search_api
from telegram import Bot
from telegram.error import RetryAfter, TimedOut

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

SENT_FILE = "sent_links.json"

QUALITY_FILTERS = ["1080p", "720p", "2160p", "480p"]
MAX_PER_RUN = 40              # سقف پیام در هر اجرا (فقط برای جلوگیری از Flood)
MAX_ITEMS_PER_QUERY = 10      # نتیجه در هر کوئری
DELAY_BETWEEN = (4, 8)        # تأخیر تصادفی بین ۴ تا ۸ ثانیه


def load_sent():
    if not os.path.exists(SENT_FILE):
        return set()
    with open(SENT_FILE, "r") as f:
        return set(json.load(f))


def save_sent(sent_set):
    with open(SENT_FILE, "w") as f:
        json.dump(list(sent_set), f)


def extract_hash(magnet):
    if not magnet:
        return None
    match = re.search(r'urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', magnet)
    return match.group(1).lower() if match else None


def is_quality_match(filename):
    if not filename:
        return False
    name_lower = filename.lower()
    return any(q in name_lower for q in QUALITY_FILTERS)


def parse_date(date_val):
    if not date_val:
        return datetime.min
    try:
        if isinstance(date_val, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(date_val.split('.')[0], fmt)
                except ValueError:
                    continue
        if isinstance(date_val, datetime):
            return date_val
    except Exception:
        pass
    return datetime.min


# ---- تشخیص هشتگ ----

ANIME_MARKERS = [
    "[subsplease]", "[ember]", "anime time", "erai-raws", "horriblesubs",
    "naruto", "one piece", "dragon ball", "kimetsu", "jujutsu kaisen",
    "shippuden", "attack on titan", "shingeki", "demon slayer", "chainsaw man",
    "my hero academia", "boku no hero", "sword art online", "tokyo ghoul",
    "death note", "fullmetal alchemist", "steins gate", "code geass",
]


def get_hashtags(query, filename):
    q = (query or "").lower()
    f = (filename or "").lower()
    tags = []

    # ۱. نوع محتوا
    if "k-drama" in q or "kdrama" in q:
        tags.append("#KDrama")
    elif "documentary" in q:
        tags.append("#Documentary")
    elif "anime" in q or any(m in f for m in ANIME_MARKERS):
        tags.append("#Anime")
    elif (re.search(r's\d{1,2}\s*[ex]\d{1,3}', f)
          or "complete series" in f
          or re.search(r'season\s*\d', f)
          or "series" in q):
        tags.append("#Series")
    else:
        tags.append("#Movie")

    # ۲. کیفیت
    for quality in ["2160p", "1080p", "720p", "480p"]:
        if quality in f or quality in q:
            tags.append(f"#{quality}")
            break

    # ۳. سال
    year_match = re.search(r'(19\d{2}|20\d{2})', filename or "")
    if year_match:
        tags.append(f"#{year_match.group(1)}")

    return " ".join(tags)


# ---- ارسال با مدیریت Flood ----

async def send_with_retry(bot, chat_id, text, max_retries=3):
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except RetryAfter as e:
            wait = int(e.retry_after) + 2
            print(f"⏳ Flood control، {wait} ثانیه صبر...")
            await asyncio.sleep(wait)
        except TimedOut:
            print(f"⏱️ Timeout، تلاش {attempt + 1}/{max_retries}...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ خطای ارسال: {e}")
            return False
    return False


# ---- main ----

async def main():
    bot = Bot(token=BOT_TOKEN)
    sent_hashes = load_sent()
    print(f"📂 لینک‌های قبلی: {len(sent_hashes)}")

    queries = [
        "Complete Series",

    ]

    new_count = 0
    skipped = 0

    for query in queries:
        if new_count >= MAX_PER_RUN:
            print(f"🛑 سقف {MAX_PER_RUN} پیام در این اجرا. بقیه اجرای بعد.")
            break

        try:
            results = await torrent_search_api.search_torrents(
                query, max_items=MAX_ITEMS_PER_QUERY
            )
        except Exception as e:
            print(f"خطا در جستجوی '{query}': {e}")
            continue

        if not results:
            continue

        quality_results = [
            t for t in results if is_quality_match(getattr(t, 'filename', ''))
        ]
        sorted_results = sorted(
            quality_results,
            key=lambda t: parse_date(getattr(t, 'date', None)),
            reverse=True,
        )

        print(f"🔍 '{query}': {len(sorted_results)} نتیجه")

        for torrent in sorted_results:
            if new_count >= MAX_PER_RUN:
                break

            magnet = getattr(torrent, 'magnet_link', None)
            if not magnet:
                continue

            h = extract_hash(magnet)
            if not h or h in sent_hashes:
                skipped += 1
                continue

            hashtags = get_hashtags(query, torrent.filename)
            caption = (
                f"🎬 {torrent.filename}\n"
                f"💾 {torrent.size} | 👤 {torrent.seeders} SE | "
                f"📅 {getattr(torrent, 'date', 'N/A')}\n\n"
                f"{hashtags}"
            )

            success = await send_with_retry(
                bot, CHANNEL_ID, f"{caption}\n\n{magnet}"
            )

            if success:
                sent_hashes.add(h)
                new_count += 1
                print(f"✅ ارسال شد: {torrent.filename}")
                await asyncio.sleep(random.uniform(*DELAY_BETWEEN))
            else:
                print(f"⚠️ ارسال ناموفق: {torrent.filename}")

    save_sent(sent_hashes)
    print(f"\n📊 نتیجه: {new_count} ارسال، {skipped} تکراری.")


if __name__ == "__main__":
    asyncio.run(main())
