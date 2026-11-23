# Telegram Movie / TV RSS Publisher

Publishes RSS entries to a Telegram channel in this format:

Readable title

Exact original RSS title
Magnet or direct `.torrent` URL

## GitHub repository setup

1. Keep the repository **Private**.
2. Add repository Actions secrets:
   - `BOT_TOKEN`: Telegram bot token from BotFather.
   - `CHANNEL_ID`: channel username such as `@your_channel`, or numeric channel ID.
3. Add repository Actions variable:
   - `RSS_FEEDS`: one or more RSS feed URLs separated by semicolons or new lines.
4. The bot must be an administrator in the channel and have permission to post.
5. Ensure the workflow is on the repository's default branch.

## RSS source requirements

Only add feeds from sources that authorize redistribution of the linked works.
Each feed item should expose a magnet URI or a direct `.torrent` enclosure/link.
The bot does not crawl websites or bypass access controls.

The title filter accepts entries containing a year, a TV episode/season marker,
or an animation keyword. It excludes entries whose title contains common anime
markers. Title-only classification is imperfect; use film/TV-focused feeds.

## Schedule

GitHub Actions checks every 5 minutes.
GitHub's minimum scheduled interval is 5 minutes, but scheduled jobs can be delayed.

## Important

- Never commit your bot token.
- `seen.json` stores published-item keys to prevent duplicates.
- If Telegram rejects a message, that item is not marked as published.
