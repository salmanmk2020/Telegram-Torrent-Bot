import asyncio
import os
from torrent_search import torrent_search_api
from telegram import Bot

# تنظیمات از Secrets می‌خوانیم (امن‌تر است)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

async def main():
    bot = Bot(token=BOT_TOKEN)
    
    # یک کوئری جستجو (می‌توانید بعداً تغییر دهید)
    query = "movie 2024" 
    
    try:
        # جستجو در منابع (این کتابخانه خودش از چندین سایت می‌گردد)
        results = await torrent_search_api.search_torrents(query, max_items=5)
        
        if not results:
            print("چیزی پیدا نشد.")
            return

        for torrent in results:
            # ساخت متن پیام
            caption = f"🎬 {torrent.filename}\n💾 {torrent.size} | 👤 {torrent.seeders} SE"
            
            # لینک مگنت (اگر موجود باشد)
            magnet = getattr(torrent, 'magnet_link', None)
            
            if magnet:
                # ارسال به کانال
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"{caption}\n\n{magnet}",
                    parse_mode='Markdown'
                )
                print(f"ارسال شد: {torrent.filename}")
            else:
                print(f"لینک مگنت نداشت: {torrent.filename}")
                
    except Exception as e:
        print(f"خطا: {e}")

if name == "main":
    asyncio.run(main())
