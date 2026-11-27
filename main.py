"""Torrent -> Telegram channel poster, designed for GitHub Actions.

Strategy (per run)
------------------
1. Load persistent state: sent hashes (append-only log) + pending queue.
2. Collect candidates:
   - the "new releases" listings (EZTV, SubsPlease, YTS, Nyaa, TPB, UIndex, 1337x)
   - a rotating slice of a large search catalog (so the long tail is covered
     over several runs instead of hammering the same 10 queries forever)
3. Merge, dedupe by normalized info-hash, drop anything already sent.
4. Sort the whole queue newest-first (global, not per query).
5. Send until the time budget is exhausted, at a rate that stays under
   Telegram's per-chat flood limit (~20 messages/minute).
6. Persist state after every send; periodically push it to the `state` branch.

Notes
-----
* The search library exposes no pagination, so "every torrent ever" is not
  reachable through it. This bot maximizes coverage by rotating queries and
  always consuming the newest feeds first.
* `sent.txt` is append-only on purpose: git stores tiny deltas instead of a
  full rewritten blob on every run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from telegram import Bot
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from torrent_search.wrapper import TorrentSearchApi

# --------------------------------------------------------------------------
# Configuration (environment first, safe defaults second)
# --------------------------------------------------------------------------

LOG = logging.getLogger("bot")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


BOT_TOKEN = _env("BOT_TOKEN", "")
_raw_channel = _env("CHANNEL_ID", "")
# Numeric channel ids (e.g. -1001234567890) must be int; @usernames stay str.
CHANNEL_ID: int | str | None = (
    int(_raw_channel)
    if re.fullmatch(r"-?\d+", _raw_channel)
    else (_raw_channel or None)
)

STATE_DIR = Path(_env("STATE_DIR", "state")).resolve()
SENT_FILE = STATE_DIR / "sent.txt"
QUEUE_FILE = STATE_DIR / "queue.json"
META_FILE = STATE_DIR / "meta.json"
# Old single-file state from previous versions; imported once, then ignored.
LEGACY_SENT_FILE = Path(_env("LEGACY_SENT_FILE", "sent_links.json"))

RUN_BUDGET = _env_float("RUN_BUDGET_SECONDS", 1500)      # 25 min total
SEARCH_BUDGET = _env_float("SEARCH_BUDGET_SECONDS", 300)  # max 5 min searching
SEND_INTERVAL = _env_float("SEND_INTERVAL_SECONDS", 3.2)  # ~18.7 msg/min
SEND_JITTER = _env_float("SEND_JITTER_SECONDS", 0.5)
POPULAR_PER_SOURCE = _env_int("POPULAR_PER_SOURCE", 100)
SEARCHES_PER_RUN = _env_int("SEARCHES_PER_RUN", 4)
MAX_QUEUE = _env_int("MAX_QUEUE", 20000)
MAX_ATTEMPTS = _env_int("MAX_ATTEMPTS", 4)
QUEUE_SAVE_INTERVAL = _env_float("QUEUE_SAVE_SECONDS", 60)
COMMIT_EVERY = _env_float("COMMIT_EVERY_SECONDS", 600)
MAX_MESSAGE_CHARS = 4000
MAGNET_SAFE_CHARS = 3800

QUALITIES = ("2160p", "1080p", "720p", "480p")

ANIME_MARKERS = (
    "[subsplease]", "[ember]", "anime time", "erai-raws", "horriblesubs",
    "naruto", "one piece", "dragon ball", "kimetsu", "jujutsu kaisen",
    "shippuden", "attack on titan", "shingeki", "demon slayer", "chainsaw man",
    "my hero academia", "boku no hero", "sword art online", "tokyo ghoul",
    "death note", "fullmetal alchemist", "steins gate", "code geass",
)

SERIES_RE = re.compile(
    r"(?i)\bS\d{1,2}(?:[ ._-]?E\d{1,3})?\b"
    r"|\bseason[ ._-]?\d{1,2}\b"
    r"|\bcomplete\b"
    r"|\b\d{1,2}x\d{1,3}\b"
)

# A year must not be part of a resolution like 1920x1080 or a codec number.
YEAR_RE = re.compile(r"(?<!\d)(19[5-9]\d|20\d{2})(?!\d)(?!\s*[xX]\s*\d)")

GENRES = (
    "action", "adventure", "animation", "biography", "comedy", "crime",
    "documentary", "drama", "family", "fantasy", "history", "horror",
    "music", "mystery", "romance", "sci-fi", "sport", "thriller", "war",
    "western",
)
LANGUAGES = (
    "hindi", "korean", "turkish", "spanish", "french", "japanese", "arabic",
    "persian", "italian", "german", "russian", "chinese", "thai", "portuguese",
)
EXTRAS = (
    "bluray", "web-dl", "webrip", "hdcam", "remux", "x265", "hevc", "dubbed",
    "subbed", "multi audio", "complete series", "season pack", "imax", "4k",
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def build_catalog() -> list[str]:
    """Search queries rotated across runs to cover the catalog over time."""
    year = datetime.now(timezone.utc).year
    queries: list[str] = []
    for y in (year, year - 1, year - 2):
        queries += [f"{y} 2160p", f"{y} 1080p", f"{y} 720p", f"{y} series", f"{y} anime"]
    for genre in GENRES:
        queries += [genre, f"{genre} 1080p"]
    for lang in LANGUAGES:
        queries += [f"{lang} movie", f"{lang} series"]
    for extra in EXTRAS:
        queries += [f"{extra} 1080p"]
    return queries


def normalize_hash(value: str | None) -> str | None:
    """Normalize a BTIH hash so hex and base32 spellings dedupe together."""
    if not value:
        return None
    value = value.strip()
    if len(value) == 32:  # base32 -> hex
        try:
            return base64.b32decode(value.upper()).hex()
        except Exception:  # noqa: BLE001 - malformed hash
            return value.lower()
    return value.lower()


def extract_hash(magnet: str | None) -> str | None:
    if not magnet:
        return None
    match = re.search(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet)
    return normalize_hash(match.group(1)) if match else None


def parse_dt(value: str | None) -> datetime | None:
    """Parse the library's date field (date-only or ISO with offset)."""
    if not value:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def sort_key(item: dict) -> tuple:
    """Newest first, then healthiest. Undated items go last."""
    dt = parse_dt(item.get("d"))
    return (dt is not None, dt.timestamp() if dt else 0.0, int(item.get("se", 0)))


def pick_year(filename: str) -> int | None:
    current = datetime.now(timezone.utc).year
    years = [
        int(m.group(1))
        for m in YEAR_RE.finditer(filename or "")
        if 1950 <= int(m.group(1)) <= current + 1
    ]
    return max(years) if years else None


def hashtags(item: dict) -> str:
    name = (item.get("t") or "").lower()
    query = (item.get("q") or "").lower()
    tags: list[str] = []

    if "k-drama" in query or "kdrama" in query or "korean" in query:
        tags.append("#KDrama")
    elif "documentary" in query or "documentary" in name:
        tags.append("#Documentary")
    elif "anime" in query or any(marker in name for marker in ANIME_MARKERS):
        tags.append("#Anime")
    elif SERIES_RE.search(name) or "series" in query or "season" in query:
        tags.append("#Series")
    else:
        tags.append("#Movie")

    for quality in QUALITIES:
        if quality in name or quality in query:
            tags.append(f"#{quality}")
            break

    year = pick_year(item.get("t") or "")
    if year:
        tags.append(f"#{year}")

    return " ".join(tags)


def slim_magnet(magnet: str, budget: int) -> str:
    """Trim the tracker list so the whole message fits Telegram's 4096 limit."""
    if len(magnet) <= budget:
        return magnet

    pairs = parse_qsl(urlsplit(magnet).query, keep_blank_values=True)
    xt = next((v for k, v in pairs if k.lower() == "xt"), None)
    if not xt:
        return magnet[:budget]

    result = "magnet:?xt=" + quote(xt, safe=":")
    name = next((v for k, v in pairs if k.lower() == "dn"), None)
    if name:
        piece = "&dn=" + quote(name[:120], safe="")
        if len(result) + len(piece) <= budget:
            result += piece

    for key, value in pairs:
        if key.lower() != "tr":
            continue
        piece = "&tr=" + quote(value, safe="")
        if len(result) + len(piece) > budget:
            break
        result += piece

    return result


def build_text(item: dict) -> str:
    date = (item.get("d") or "N/A")[:10]
    caption = (
        f"🎬 {item.get('t') or 'N/A'}\n"
        f"💾 {item.get('s') or 'N/A'} | 👤 {item.get('se', 0)} SE | 📅 {date}\n"
        f"📡 {item.get('src') or 'N/A'}\n\n"
        f"{hashtags(item)}"
    )
    magnet = slim_magnet(item["m"], min(MAGNET_SAFE_CHARS, MAX_MESSAGE_CHARS - len(caption) - 4))
    return f"{caption}\n\n{magnet}"


# --------------------------------------------------------------------------
# Persistent state
# --------------------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.sent: set[str] = set()
        self.queue: dict[str, dict] = {}
        self.cursor: int = 0

    # -- load ---------------------------------------------------------
    def load(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        if SENT_FILE.exists():
            with SENT_FILE.open("r", encoding="utf-8", errors="ignore") as handle:
                self.sent = {line.strip() for line in handle if line.strip()}
        elif LEGACY_SENT_FILE.exists():
            self._import_legacy(LEGACY_SENT_FILE)

        if QUEUE_FILE.exists():
            try:
                for item in json.loads(QUEUE_FILE.read_text(encoding="utf-8")):
                    key = item.get("h")
                    if key and key not in self.sent:
                        self.queue[key] = item
            except (OSError, json.JSONDecodeError) as exc:
                LOG.error("queue.json قابل خواندن نبود (%s) - از صفر شروع می‌شود", exc)

        if META_FILE.exists():
            try:
                meta = json.loads(META_FILE.read_text(encoding="utf-8"))
                self.cursor = int(meta.get("cursor", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                self.cursor = 0

    # -- mutations ----------------------------------------------------
    def _import_legacy(self, path: Path) -> None:
        """One-time migration from the old sent_links.json layout."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("مهاجرت از %s ناموفق: %s", path.name, exc)
            return
        if not isinstance(data, list):
            return
        for raw in data:
            key = normalize_hash(str(raw))
            if key:
                self.sent.add(key)
        if self.sent:
            SENT_FILE.write_text(
                "\n".join(sorted(self.sent)) + "\n", encoding="utf-8"
            )
        LOG.info("مهاجرت: %d هش از %s وارد شد", len(self.sent), path.name)

    def add(self, torrent, query: str) -> bool:
        magnet = getattr(torrent, "magnet_link", None)
        if not magnet:
            return False
        key = normalize_hash(getattr(torrent, "info_hash", None) or extract_hash(magnet))
        if not key or key in self.sent or key in self.queue:
            return False

        self.queue[key] = {
            "h": key,
            "t": " ".join(str(getattr(torrent, "filename", "") or "").split())[:300],
            "s": str(getattr(torrent, "size", "") or "N/A"),
            "se": int(getattr(torrent, "seeders", 0) or 0),
            "d": str(getattr(torrent, "date", "") or ""),
            "m": magnet,
            "src": str(getattr(torrent, "source", "") or ""),
            "q": query,
            "a": 0,
        }
        return True

    def mark_sent(self, key: str) -> None:
        self.queue.pop(key, None)
        if key in self.sent:
            return
        self.sent.add(key)
        with SENT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(key + "\n")

    def prune(self) -> int:
        if len(self.queue) <= MAX_QUEUE:
            return 0
        kept = sorted(self.queue.values(), key=sort_key, reverse=True)[:MAX_QUEUE]
        dropped = len(self.queue) - len(kept)
        self.queue = {item["h"]: item for item in kept}
        return dropped

    # -- save ---------------------------------------------------------
    def _write_json(self, path: Path, payload) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def save_queue(self) -> None:
        ordered = sorted(self.queue.values(), key=sort_key, reverse=True)
        self._write_json(QUEUE_FILE, ordered)

    def save_meta(self) -> None:
        self._write_json(
            META_FILE,
            {
                "cursor": self.cursor,
                "sent": len(self.sent),
                "queue": len(self.queue),
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )


def git_commit_state(message: str) -> None:
    """Amend the single commit on the `state` branch and force-push it."""
    repo = _env("STATE_GIT_DIR", "")
    if not repo or not (Path(repo) / ".git").exists():
        return

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, check=False
        )

    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    if git("commit", "-q", "--amend", "-m", message).returncode != 0:
        git("commit", "-q", "-m", message)
    push = git("push", "-q", "--force", "origin", "HEAD:state")
    if push.returncode != 0:
        LOG.warning("push وضعیت ناموفق بود: %s", push.stderr.strip())


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


class AbortRun(Exception):
    """Raised when continuing makes no sense (bot removed, channel deleted)."""


async def send_one(bot: Bot, text: str) -> str:
    """Return 'sent', 'skip' (permanently invalid) or 'uncertain' (unknown)."""
    errors = 0
    floods = 0
    while errors < MAX_ATTEMPTS and floods < 10:
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                disable_web_page_preview=True,
            )
            return "sent"
        except RetryAfter as exc:
            # Flood waits are not counted as failures: Telegram told us to wait.
            floods += 1
            wait = exc.retry_after
            seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            seconds = max(seconds, 1.0) + 1.0
            LOG.warning("Flood control: %.0f ثانیه صبر...", seconds)
            await asyncio.sleep(seconds)
        except TimedOut:
            errors += 1
            LOG.warning("Timeout در ارسال (تلاش %d/%d)", errors, MAX_ATTEMPTS)
            await asyncio.sleep(2 * errors)
        except (Forbidden, ChatMigrated) as exc:
            raise AbortRun(str(exc)) from exc
        except BadRequest as exc:
            # Message too long / invalid markup: retrying will never work.
            LOG.error("BadRequest، پیام رد شد: %s", exc)
            return "skip"
        except NetworkError as exc:
            errors += 1
            LOG.warning("خطای شبکه (تلاش %d/%d): %s", errors, MAX_ATTEMPTS, exc)
            await asyncio.sleep(2 * errors)

    LOG.error("ارسال پس از %d تلاش ناموفق ماند", MAX_ATTEMPTS)
    return "uncertain"


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


async def collect(api: TorrentSearchApi, state: State, deadline: float) -> int:
    added = 0

    try:
        popular = await asyncio.wait_for(
            api.popular_torrents(per_source=POPULAR_PER_SOURCE, refresh=True),
            timeout=120,
        )
        fresh = sum(1 for torrent in popular if state.add(torrent, "popular"))
        added += fresh
        LOG.info("لیست‌های تازه: %d آیتم، %d جدید", len(popular), fresh)
    except Exception as exc:  # noqa: BLE001 - a dead source must not kill the run
        LOG.warning("دریافت لیست‌های تازه ناموفق بود: %s", exc)

    catalog = build_catalog()
    count = min(SEARCHES_PER_RUN, len(catalog))
    for _ in range(count):
        if time.monotonic() >= deadline:
            LOG.info("بودجهٔ جستجو تمام شد")
            break
        query = catalog[state.cursor % len(catalog)]
        state.cursor = (state.cursor + 1) % len(catalog)
        try:
            results = await asyncio.wait_for(
                api.search_torrents(query, max_items=None),
                timeout=90,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("جستجوی '%s' ناموفق: %s", query, exc)
            continue
        fresh = sum(1 for torrent in results if state.add(torrent, query))
        added += fresh
        LOG.info("جستجو '%s': %d نتیجه، %d جدید", query, len(results), fresh)

    return added


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    # Windows consoles default to a legacy code page and choke on emoji.
    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not BOT_TOKEN:
        raise SystemExit("❌ متغیر محیطی BOT_TOKEN تنظیم نشده است")
    if CHANNEL_ID is None:
        raise SystemExit("❌ متغیر محیطی CHANNEL_ID تنظیم نشده است")

    state = State()
    state.load()
    LOG.info(
        "state: %d هش ارسال‌شده، %d آیتم در صف، cursor=%d",
        len(state.sent),
        len(state.queue),
        state.cursor,
    )

    stop = asyncio.Event()
    with suppress(NotImplementedError, RuntimeError):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    started = time.monotonic()
    deadline = started + RUN_BUDGET
    search_deadline = min(deadline, started + SEARCH_BUDGET)

    api = TorrentSearchApi()
    added = await collect(api, state, search_deadline)
    LOG.info("%d آیتم جدید به صف اضافه شد", added)

    dropped = state.prune()
    if dropped:
        LOG.warning("صف از سقف %d گذشت؛ %d آیتم قدیمی‌تر حذف شد", MAX_QUEUE, dropped)
    state.save_queue()

    ordered = sorted(state.queue.values(), key=sort_key, reverse=True)
    LOG.info("آمادهٔ ارسال: %d آیتم (بودجه %.0f ثانیه)", len(ordered), deadline - time.monotonic())

    sent = skipped = failed = 0
    last_queue_save = last_commit = time.monotonic()

    if args.limit is not None:
        ordered = ordered[: args.limit]

    if args.dry_run:
        for item in ordered[:10]:
            print("-" * 60)
            print(build_text(item))
        LOG.info("dry-run: %d آیتم چاپ شد، هیچ پیامی ارسال نشد", min(len(ordered), 10))
        return 0

    async with Bot(token=BOT_TOKEN) as bot:
        try:
            for item in ordered:
                if stop.is_set():
                    LOG.info("سیگنال توقف دریافت شد")
                    break
                if time.monotonic() >= deadline:
                    LOG.info("بودجهٔ زمانی اجرا تمام شد")
                    break

                key = item["h"]
                if key in state.sent:
                    continue

                status = await send_one(bot, build_text(item))

                if status == "sent":
                    state.mark_sent(key)
                    sent += 1
                    LOG.info("✅ %s", item.get("t", "")[:90])
                elif status == "skip":
                    state.mark_sent(key)
                    skipped += 1
                else:  # uncertain: assume delivered to avoid duplicates, then stop
                    state.mark_sent(key)
                    failed += 1
                    LOG.warning("⚠️ نامشخص، فرض بر ارسال: %s", item.get("t", "")[:90])
                    break

                now = time.monotonic()
                if now - last_queue_save >= QUEUE_SAVE_INTERVAL:
                    state.save_queue()
                    last_queue_save = now
                if now - last_commit >= COMMIT_EVERY:
                    state.save_meta()
                    git_commit_state(f"state {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
                    last_commit = now

                if time.monotonic() < deadline:
                    await asyncio.sleep(SEND_INTERVAL + random.uniform(0, SEND_JITTER))
        except AbortRun as exc:
            LOG.error("اجرا متوقف شد (دسترسی به کانال): %s", exc)

    state.save_queue()
    state.save_meta()
    git_commit_state(f"state {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")

    LOG.info(
        "پایان: %d ارسال، %d رد‌شده، %d نامشخص | صف: %d | کل ارسال‌شده: %d",
        sent,
        skipped,
        failed,
        len(state.queue),
        len(state.sent),
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Torrent -> Telegram channel bot")
    parser.add_argument("--dry-run", action="store_true", help="چاپ پیام‌ها بدون ارسال")
    parser.add_argument(
        "--limit",
        type=int,
        default=_env_int("LIMIT", 0) or None,
        help="حداکثر تعداد ارسال در این اجرا",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
