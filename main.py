import asyncio
import os
import json
from torrent_search import torrent_search_api
from telegram import Bot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

# فایل ذخیره لینک‌های ارسال‌شده
SENT_FILE = "sent_links.json"

def load_sent():
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_sent(sent_set):
    with open(SENT_FILE, "w") as f:
        json.dump(list(sent_set), f)

async def main():
    bot = Bot(token=BOT_TOKEN)
    sent_links = load_sent()
    
    # لیست کوئری‌ها - اینجا موضوعات مختلف رو اضافه کنید
    queries = [
        "movie 2024",
        "series 2024",
        "anime",
    ]
    
    new_count = 0
    
    for query in queries:
        try:
            results = await torrent_search_api.search_torrents(query, max_items=5)
            
            if not results:
                print(f"چیزی برای '{query}' پیدا نشد.")
                continue

            for torrent in results:
                magnet = getattr(torrent, 'magnet_link', None)
                if not magnet:
                    continue
                
                # اگه قبلاً فرستادیم، رد کن
                if magnet in sent_links:
                    print(f"تکراری، رد شد: {torrent.filename}")
                    continue
                
                caption = f"🎬 {torrent.filename}\n💾 {torrent.size} | 👤 {torrent.seeders} SE"
                
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"{caption}\n\n`{magnet}`",
                    parse_mode='Markdown'
                )
                
                sent_links.add(magnet)
                new_count += 1
                print(f"ارسال شد: {torrent.filename}")
                
                # کمی صبر بین ارسال‌ها که تلگرام بلاک نکنه
                await asyncio.sleep(2)
                
        except Exception as e:
            print(f"خطا در کوئری '{query}': {e}")
    
    save_sent(sent_links)
    print(f"\n✅ مجموع {new_count} لینک جدید ارسال شد.")

if __name__ == "__main__":
    asyncio.run(main())
