import asyncio
import os
import json
import re
from torrent_search import torrent_search_api
from telegram import Bot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

SENT_FILE = "sent_links.json"


def load_sent():
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_sent(sent_set):
    with open(SENT_FILE, "w") as f:
        json.dump(list(sent_set), f)


def extract_hash(magnet):
    if not magnet:
        return None
    match = re.search(r'urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', magnet)
    return match.group(1).lower() if match else None


async def main():
    bot = Bot(token=BOT_TOKEN)
    sent_hashes = load_sent()
    print(f"📂 لینک‌های قبلی: {len(sent_hashes)}")

    queries = [
        "movie 2024",
        "series 2024",
        "anime",
    ]

    new_count = 0
    skipped = 0

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

                h = extract_hash(magnet)
                if not h:
                    print(f"هش پیدا نشد: {torrent.filename}")
                    continue

                if h in sent_hashes:
                    print(f"⏭️ تکراری، رد شد: {torrent.filename}")
                    skipped += 1
                    continue

                caption = f"🎬 {torrent.filename}\n💾 {torrent.size} | 👤 {torrent.seeders} SE"

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
    print(f"\n📊 نتیجه: {new_count} جدید ارسال شد، {skipped} تکراری رد شد.")


if __name__ == "__main__":
    asyncio.run(main())
