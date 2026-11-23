import os
import re
import json
import hashlib
import feedparser
import requests
from pathlib import Path

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]

RSS_FEEDS = os.environ.get("RSS_FEEDS", "").split(";")

DATABASE = Path("seen.json")

if DATABASE.exists():
    seen = json.loads(DATABASE.read_text())
else:
    seen = {}

def get_link(entry):
    text = str(entry.get("link", ""))

    for key in ("summary", "description"):
        text += " " + str(entry.get(key, ""))

    match = re.search(
        r'magnet:\?xt=urn:btih:[^\s"<&]+',
        text,
        re.I
    )

    if match:
        return match.group(0).rstrip(".,)")

    for link in entry.get("links", []):
        href = link.get("href", "")
        if ".torrent" in href.lower():
            return href

    return None


def is_movie_or_series(title):
    return bool(
        re.search(
            r'\b(19\d{2}|20\d{2})\b|S\d{1,2}E\d{1,2}|Season\s*\d+',
            title,
            re.I
        )
    )


def readable_name(title):
    name = re.sub(r'\.(?=[A-Za-z0-9])', ' ', title)
    name = name.replace("_", " ")
    name = re.sub(r'\s+', ' ', name)
    name = re.sub(r'-[A-Za-z0-9]+$', '', name)
    return name.strip()


def publish(title, original, link):
    message = (
        f"{readable_name(title)}\n\n"
        f"{original}\n"
        f"{link}"
    )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHANNEL_ID,
            "text": message
        },
        timeout=30
    )

    response.raise_for_status()


for feed_url in RSS_FEEDS:
    if not feed_url.strip():
        continue

    try:
        feed = feedparser.parse(feed_url.strip())

        for entry in reversed(feed.entries):

            original = entry.get("title", "").strip()

            if not original:
                continue

            if not is_movie_or_series(original):
                continue

            link = get_link(entry)

            if not link:
                continue

            item_id = hashlib.sha256(
                link.encode()
            ).hexdigest()

            if item_id in seen:
                continue

            publish(original, original, link)

            seen[item_id] = True

            print("Published:", original)

    except Exception as error:
        print("Feed error:", error)


DATABASE.write_text(
    json.dumps(seen, ensure_ascii=False, indent=2)
)
