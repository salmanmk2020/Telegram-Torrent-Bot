import os
import re
import json
import html
import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import feedparser
import requests

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("telegram-torrent-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
DATABASE = Path("seen.json")
MAX_SEEN = 20000

# Put only RSS feeds whose operators permit redistribution of the linked works.
# Separate multiple feed URLs with semicolons or new lines.
RSS_FEEDS = [
    x.strip()
    for x in re.split(r"[;\r\n]+", os.getenv("RSS_FEEDS", ""))
    if x.strip()
]

ANIME_RE = re.compile(
    r"\b(anime|ani\-me|fansub|fansubs|raws?|nyaa)\b|"
    r"\[(?:subsplease|erai-raws|horriblesubs|judas|EMBER|ASW)\]",
    re.IGNORECASE,
)

TV_RE = re.compile(r"\bS\d{1,2}\s*E\d{1,3}\b|\bSeason[ ._-]*\d+\b", re.I)
YEAR_RE = re.compile(r"\b(?:19\d{2}|20\d{2})\b")
ANIMATION_RE = re.compile(r"\b(animated|animation|cartoon)\b", re.I)


def load_seen():
    if not DATABASE.exists():
        return {}
    try:
        return json.loads(DATABASE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("seen.json unreadable; starting with an empty database")
        return {}


def save_seen(seen):
    # Keep the file bounded so it does not grow forever.
    if len(seen) > MAX_SEEN:
        seen = dict(list(seen.items())[-MAX_SEEN:])
    DATABASE.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def entry_text(entry):
    parts = [
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("description", ""),
    ]
    for content in entry.get("content", []):
        parts.append(content.get("value", ""))
    return html.unescape(" ".join(str(p) for p in parts))


def extract_link(entry):
    """Return a magnet URI or a direct .torrent URL exposed by the RSS item."""
    text = entry_text(entry)

    magnet = re.search(
        r"magnet:\?xt=urn:btih:[^\"'<>\s&]+(?:&[^\"'<>\s]*)?",
        text,
        re.IGNORECASE,
    )
    if magnet:
        return html.unescape(magnet.group(0)).rstrip(".,);]")

    # RSS enclosure is the usual place for a .torrent download.
    for item in entry.get("enclosures", []):
        href = item.get("href") or item.get("url")
        if href and (".torrent" in href.lower()
                     or "bittorrent" in item.get("type", "").lower()):
            return href

    # Some feeds expose enclosure links only in the generic links array.
    for item in entry.get("links", []):
        href = item.get("href", "")
        rel = item.get("rel", "")
        typ = item.get("type", "").lower()
        if href.startswith("magnet:"):
            return href
        if href and (".torrent" in href.lower()
                     or rel == "enclosure"
                     or "bittorrent" in typ):
            return href

    # Some feeds put the magnet or torrent URL directly in the item link.
    link = entry.get("link", "")
    if link.startswith("magnet:") or ".torrent" in link.lower():
        return link

    return None


def is_allowed_title(title):
    """Conservative title filter; feed sources should already be film/TV-focused."""
    if not title or ANIME_RE.search(title):
        return False
    return bool(TV_RE.search(title) or YEAR_RE.search(title)
                or ANIMATION_RE.search(title))


def readable_title(original):
    # First line: readable variant. Second line remains the exact RSS title.
    value = html.unescape(original)
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def item_key(title, link):
    # Prefer the infohash for magnets, so tracker variations don't create duplicates.
    if link.lower().startswith("magnet:"):
        parsed = parse_qs(urlparse(link).query)
        xt = parsed.get("xt", [""])[0]
        if xt:
            return hashlib.sha256(xt.lower().encode()).hexdigest()
    return hashlib.sha256((title + "\n" + link).encode()).hexdigest()


def send_to_telegram(original, link):
    message = f"{readable_title(original)}\n\n{original}\n{link}"
    if len(message) > 4096:
        message = message[:4090] + "…"

    endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        endpoint,
        data={"chat_id": CHANNEL_ID, "text": message, "disable_web_page_preview": True},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected message: {payload}")


def main():
    if not RSS_FEEDS:
        raise SystemExit(
            "RSS_FEEDS is empty. Add one or more RSS URLs for sources "
            "that permit redistribution of the linked works."
        )

    seen = load_seen()
    added = 0

    for feed_url in RSS_FEEDS:
        log.info("Checking feed: %s", feed_url)
        try:
            feed = feedparser.parse(feed_url)
            if feed.bozo:
                log.warning("Feed parser warning for %s: %s",
                            feed_url, getattr(feed, "bozo_exception", "unknown"))

            # Oldest-first means a backlog is published in chronological order.
            for entry in reversed(feed.entries):
                original = html.unescape(str(entry.get("title", "")).strip())
                if not is_allowed_title(original):
                    continue

                link = extract_link(entry)
                if not link:
                    continue

                key = item_key(original, link)
                if key in seen:
                    continue

                try:
                    send_to_telegram(original, link)
                except Exception:
                    log.exception("Failed to publish: %s", original)
                    continue

                seen[key] = True
                added += 1
                log.info("Published: %s", original)

        except Exception:
            log.exception("Could not process feed: %s", feed_url)

    save_seen(seen)
    log.info("Finished. Newly published items: %d", added)


if __name__ == "__main__":
    main()
