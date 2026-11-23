import asyncio
import os
import json
import re
from datetime import datetime
from torrent_search import torrent_search_api
from telegram import Bot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

SENT_FILE = "sent_links.json"

# کیفیت‌هایی که می‌خواهیم پشتیبانی شوند
QUALITY_FILTERS = ["1080p", "720p", "2160p", "480p"]

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
    """بررسی اینکه آیا نام فایل شامل یکی از کیفیت‌های مورد نظر است یا نه"""
    if not filename:
        return False
    name_lower = filename.lower()
    return any(q in name_lower for q in QUALITY_FILTERS)

def parse_date(date_val):
    """تبدیل تاریخ به فرمت قابل مقایسه (برای مرتب‌سازی)"""
    if not date_val:
        return datetime.min
    try:
        # تلاش برای تبدیل رایج‌ترین فرمت‌ها
        if isinstance(date_val, str):
            # فرمت ISO یا فرمت‌های تاریخ معمولی
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(date_val.split('.')[0], fmt)
                except ValueError:
                    continue
        # اگر از قبل datetime باشد
        if isinstance(date_val, datetime):
            return date_val
    except Exception:
        pass
    return datetime.min


async def main():
    bot = Bot(token=BOT_TOKEN)
    sent_hashes = load_sent()
    print(f"📂 لینک‌های قبلی: {len(sent_hashes)}")

    # کوئری‌های متمرکز روی کیفیت و محتوای جدید
    queries = [
        "1080p movie",
        "1080p series",
        "720p movie",
        "720p series",
        "2160p movie",
        "1080p anime",
        "1080p documentary",
        "1080p K-drama",
    ]

    new_count = 0
    skipped = 0
    total_found = 0

    for query in queries:
        try:
            results = await torrent_search_api.search_torrents(query, max_items=15)

            if not results:
                print(f"چیزی برای '{query}' پیدا نشد.")
                continue

            # ۱. فیلتر بر اساس کیفیت (اختیاری اما توصیه‌شده)
            quality_results = [t for t in results if is_quality_match(getattr(t, 'filename', ''))]

            # ۲. مرتب‌سازی بر اساس تاریخ (جدیدترین‌ها اول)
            sorted_results = sorted(
                quality_results,
                key=lambda t: parse_date(getattr(t, 'date', None)),
                reverse=True
            )

            total_found += len(sorted_results)
            print(f"🔍 برای '{query}': {len(results)} نتیجه کلی، {len(sorted_results)} نتیجه با کیفیت مجاز و مرتب‌شده بر اساس تاریخ.")

            for torrent in sorted_results:
                magnet = getattr(torrent, 'magnet_link', None)
                if not magnet:
                    continue

                h = extract_hash(magnet)
                if not h:
                    continue

                if h in sent_hashes:
                    skipped += 1
                    continue

                caption = (
                    f"🎬 {torrent.filename}\n"
                    f"💾 {torrent.size} | 👤 {torrent.seeders} SE | 📅 {getattr(torrent, 'date', 'N/A')}"
                )

                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"{caption}\n\n{magnet}"
                )

                sent_hashes.add(h)
                new_count += 1
                print(f"✅ ارسال شد: {torrent.filename}")

                await asyncio.sleep(2)

        except Exception as e:
            print(f"خطا در کوئری '{query}': {e}")

    save_sent(sent_hashes)
    print(f"\n📊 نتیجه نهایی: {new_count} لینک جدید ارسال شد، {skipped} تکراری رد شد (از مجموع {total_found} نتیجه پردازش‌شده).")


if __name__ == "__main__":
    asyncio.run(main())
