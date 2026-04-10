"""
Debitum mentions monitor.

Polls Reddit, YouTube (new videos + comments on matched videos), and a
Google Alerts RSS feed, then posts any new hits to a Slack Incoming Webhook.

Designed to run on GitHub Actions every 30 minutes. State is persisted in
`seen.json` which the workflow commits back to the repo so we don't
re-notify on the same item.

Environment variables (set as GitHub Actions secrets):
    SLACK_WEBHOOK_URL     Slack Incoming Webhook URL (required)
    YOUTUBE_API_KEY       YouTube Data API v3 key (required)
    GOOGLE_ALERTS_RSS     RSS URL from a Google Alert (optional but recommended)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser  # type: ignore
import requests

# ---------- config ----------

KEYWORDS = [
    "debitum",
    "debitum investments",
    "debitum network",
    "debitum platform",
    "debitum p2p",
]
# Compiled regex: case-insensitive, word-boundary match on "debitum"
KEYWORD_RE = re.compile(r"\bdebitum\b", re.IGNORECASE)

# Channels to monitor for comment-only mentions. Every recent video from these
# channels gets added to the comment watchlist, even if the video title/
# description never mentions Debitum. This is how we catch cases like
# "Monthly portfolio update" videos where Debitum only shows up in comments.
#
# Add channel IDs here (the `UC...` string — find it by opening a channel page
# and looking at the URL, or view-source and search for "channelId").
# You can also add via the YOUTUBE_CHANNELS env var as a comma-separated list.
DEFAULT_CHANNELS: list[str] = [
    # e.g. "UCxxxxxxxxxxxxxxxxxxxx",
]
CHANNELS_PER_RUN_VIDEOS = 10  # how many recent uploads per channel to watch

SEEN_FILE = Path(__file__).parent / "seen.json"
MAX_SEEN_PER_SOURCE = 2000  # cap state file size

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
GOOGLE_ALERTS_RSS = os.environ.get("GOOGLE_ALERTS_RSS", "").strip()

USER_AGENT = "debitum-monitor/1.0 (github actions)"


# ---------- state ----------

def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "reddit": [],
        "youtube_video": [],
        "youtube_comment": [],
        "alerts": [],
        "watched_videos": [],  # rolling list of every video we've ever matched
    }


def save_seen(seen: dict) -> None:
    # Trim each source to most recent N ids
    for key in list(seen.keys()):
        if isinstance(seen[key], list) and len(seen[key]) > MAX_SEEN_PER_SOURCE:
            seen[key] = seen[key][-MAX_SEEN_PER_SOURCE:]
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def matches(text: str) -> bool:
    return bool(text and KEYWORD_RE.search(text))


# ---------- sources ----------

def check_reddit(seen: list) -> list[dict]:
    """Reddit search RSS — no auth required."""
    hits = []
    url = "https://www.reddit.com/search.rss?q=debitum&sort=new&restrict_sr=off"
    try:
        feed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as e:
        print(f"[reddit] error: {e}", file=sys.stderr)
        return hits
    for entry in feed.entries:
        entry_id = entry.get("id") or entry.get("link")
        if not entry_id or entry_id in seen:
            continue
        title = entry.get("title", "")
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
        if not (matches(title) or matches(summary)):
            continue
        hits.append({
            "source": "Reddit",
            "title": title,
            "url": entry.get("link", ""),
            "snippet": summary[:300],
            "id": entry_id,
            "seen_key": "reddit",
        })
    return hits


def check_google_alerts(seen: list) -> list[dict]:
    """Supports multiple RSS feed URLs separated by commas in GOOGLE_ALERTS_RSS."""
    if not GOOGLE_ALERTS_RSS:
        return []
    hits = []
    # Support comma-separated list of RSS URLs (e.g. general + YouTube-specific alert)
    feed_urls = [u.strip() for u in GOOGLE_ALERTS_RSS.split(",") if u.strip()]
    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url, agent=USER_AGENT)
        except Exception as e:
            print(f"[alerts] error on {feed_url}: {e}", file=sys.stderr)
            continue
        for entry in feed.entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen:
                continue
            title = re.sub(r"<[^>]+>", " ", entry.get("title", ""))
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            # Google Alerts URLs are wrapped — unwrap if possible
            link = entry.get("link", "")
            m = re.search(r"url=([^&]+)", link)
            if m:
                from urllib.parse import unquote
                link = unquote(m.group(1))
            hits.append({
                "source": "Google Alerts",
                "title": title,
                "url": link,
                "snippet": summary[:300],
                "id": entry_id,
                "seen_key": "alerts",
            })
    return hits


def yt_api(endpoint: str, params: dict) -> dict:
    params = {**params, "key": YOUTUBE_API_KEY}
    r = requests.get(
        f"https://www.googleapis.com/youtube/v3/{endpoint}",
        params=params,
        timeout=30,
        headers={"User-Agent": USER_AGENT},
    )
    r.raise_for_status()
    return r.json()


def check_youtube_videos(seen_videos: list) -> tuple[list[dict], list[str]]:
    """
    Returns (hits, matched_video_ids).
    matched_video_ids is fed into the comment watcher so we auto-watch
    comments on any video whose title/description matches.

    Strategy for maximum coverage:
    1. Run multiple search queries (different keywords) to cast a wider net
    2. Search by both "date" and "relevance" order — relevance catches
       description-only mentions that date ordering often misses
    3. Use maxResults=50 (API max) for each query
    4. Fetch FULL video metadata (videos.list) for every candidate, because
       the search API truncates descriptions to ~160 chars — a description-
       only mention like "Jetzt auf Debitum investieren" buried 500 chars
       deep would be invisible in the truncated snippet
    """
    if not YOUTUBE_API_KEY:
        return [], []

    # Step 1: collect candidate video IDs from multiple search passes
    candidate_ids: set[str] = set()
    search_queries = ["debitum", "debitum investments", "debitum network"]
    search_orders = ["date", "relevance"]

    for query in search_queries:
        for order in search_orders:
            try:
                data = yt_api("search", {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "order": order,
                    "maxResults": 50,
                })
            except Exception as e:
                print(f"[youtube search q={query} order={order}] error: {e}",
                      file=sys.stderr)
                continue
            for item in data.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid:
                    candidate_ids.add(vid)

    print(f"  YouTube search returned {len(candidate_ids)} unique candidate videos")

    if not candidate_ids:
        return [], []

    # Step 2: fetch FULL metadata for all candidates (title + full description)
    # videos.list supports up to 50 IDs per call and returns the COMPLETE
    # description, unlike search.list which truncates it
    hits: list[dict] = []
    matched_ids: list[str] = []

    for i in range(0, len(candidate_ids), 50):
        batch = list(candidate_ids)[i : i + 50]
        try:
            data = yt_api("videos", {
                "part": "snippet",
                "id": ",".join(batch),
            })
        except Exception as e:
            print(f"[youtube videos detail] error: {e}", file=sys.stderr)
            continue
        for item in data.get("items", []):
            vid = item.get("id")
            if not vid:
                continue
            snippet = item.get("snippet", {})
            title = snippet.get("title", "")
            desc = snippet.get("description", "")
            # Check the FULL title + description for keyword match
            if not (matches(title) or matches(desc)):
                continue
            matched_ids.append(vid)
            if vid in seen_videos:
                continue
            hits.append({
                "source": "YouTube video",
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "snippet": f"{snippet.get('channelTitle', '')} — {desc[:250]}",
                "id": vid,
                "seen_key": "youtube_video",
            })

    print(f"  {len(matched_ids)} videos confirmed mentioning Debitum (full description check)")
    return hits, matched_ids


def get_channel_recent_videos(channel_ids: list[str]) -> list[str]:
    """
    For each channel ID, return the most recent upload video IDs.
    Uses the channel's uploads playlist (1 API unit per channel + 1 per playlist page).
    """
    if not YOUTUBE_API_KEY or not channel_ids:
        return []
    video_ids: list[str] = []
    try:
        # Step 1: get uploads playlist ID for each channel (batch up to 50)
        data = yt_api("channels", {
            "part": "contentDetails",
            "id": ",".join(channel_ids[:50]),
        })
    except Exception as e:
        print(f"[yt channels] error: {e}", file=sys.stderr)
        return video_ids

    uploads_playlists = []
    for item in data.get("items", []):
        pid = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if pid:
            uploads_playlists.append(pid)

    # Step 2: get recent videos from each uploads playlist
    for pid in uploads_playlists:
        try:
            pdata = yt_api("playlistItems", {
                "part": "contentDetails",
                "playlistId": pid,
                "maxResults": CHANNELS_PER_RUN_VIDEOS,
            })
        except Exception as e:
            print(f"[yt playlist {pid}] error: {e}", file=sys.stderr)
            continue
        for item in pdata.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
    return video_ids


def check_videos_metadata_for_mentions(
    video_ids: list[str], seen_videos: list
) -> list[dict]:
    """
    Fetch full metadata (title + description) for a list of video IDs and
    return hits for any whose title/description matches. Used for channel-
    watched videos so we also surface description-only mentions as top-level
    notifications, not just comment hits.
    """
    if not YOUTUBE_API_KEY or not video_ids:
        return []
    hits: list[dict] = []
    # videos.list supports up to 50 IDs per call
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            data = yt_api("videos", {
                "part": "snippet",
                "id": ",".join(batch),
            })
        except Exception as e:
            print(f"[yt videos meta] error: {e}", file=sys.stderr)
            continue
        for item in data.get("items", []):
            vid = item.get("id")
            if not vid or vid in seen_videos:
                continue
            snippet = item.get("snippet", {})
            title = snippet.get("title", "")
            desc = snippet.get("description", "")
            if not (matches(title) or matches(desc)):
                continue
            hits.append({
                "source": "YouTube video",
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "snippet": f"{snippet.get('channelTitle', '')} — {desc[:250]}",
                "id": vid,
                "seen_key": "youtube_video",
            })
    return hits


def check_youtube_comments(video_ids: list[str], seen_comments: list) -> list[dict]:
    if not YOUTUBE_API_KEY or not video_ids:
        return []
    hits: list[dict] = []
    for vid in video_ids:
        try:
            data = yt_api("commentThreads", {
                "part": "snippet",
                "videoId": vid,
                "maxResults": 50,
                "order": "time",
                "textFormat": "plainText",
            })
        except requests.HTTPError as e:
            # Comments may be disabled on some videos — skip
            if e.response is not None and e.response.status_code in (403, 404):
                continue
            print(f"[yt comments {vid}] error: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"[yt comments {vid}] error: {e}", file=sys.stderr)
            continue
        for item in data.get("items", []):
            cid = item.get("id")
            if not cid or cid in seen_comments:
                continue
            top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            text = top.get("textDisplay", "")
            author = top.get("authorDisplayName", "")
            # Only notify if the comment itself mentions debitum
            # (video-level mentions are covered by the video feed)
            if not matches(text):
                # Still mark as seen so we don't re-check every run
                hits_ignore_id = cid
                seen_comments.append(hits_ignore_id)
                continue
            hits.append({
                "source": "YouTube comment",
                "title": f"{author} commented",
                "url": f"https://www.youtube.com/watch?v={vid}&lc={cid}",
                "snippet": text[:300],
                "id": cid,
                "seen_key": "youtube_comment",
            })
    return hits


# ---------- slack ----------

def post_to_slack(hit: dict) -> None:
    if not SLACK_WEBHOOK_URL:
        print(f"[slack dry-run] {hit['source']}: {hit['title']}")
        return
    emoji = {
        "Reddit": ":red_circle:",
        "YouTube video": ":tv:",
        "YouTube comment": ":speech_balloon:",
        "Google Alerts": ":bell:",
    }.get(hit["source"], ":mag:")
    text = (
        f"{emoji} *{hit['source']}* — <{hit['url']}|{hit['title']}>\n"
        f"> {hit['snippet']}"
    )
    payload = {"text": text, "unfurl_links": True}
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[slack] error posting hit {hit['id']}: {e}", file=sys.stderr)


# ---------- main ----------

def main() -> int:
    seen = load_seen()
    all_hits: list[dict] = []

    print("Checking Reddit...")
    all_hits += check_reddit(seen["reddit"])

    print("Checking Google Alerts...")
    all_hits += check_google_alerts(seen["alerts"])

    print("Checking YouTube videos (keyword search)...")
    yt_hits, matched_video_ids = check_youtube_videos(seen["youtube_video"])
    all_hits += yt_hits

    # --- Channel watching: pull recent uploads from every configured channel ---
    env_channels = [c.strip() for c in os.environ.get("YOUTUBE_CHANNELS", "").split(",") if c.strip()]
    all_channels = list({*DEFAULT_CHANNELS, *env_channels})
    channel_video_ids: list[str] = []
    if all_channels:
        print(f"Fetching recent uploads from {len(all_channels)} channel(s)...")
        channel_video_ids = get_channel_recent_videos(all_channels)
        # Also check their title/description for Debitum mentions (description-only case)
        meta_hits = check_videos_metadata_for_mentions(channel_video_ids, seen["youtube_video"])
        all_hits += meta_hits
        for h in meta_hits:
            matched_video_ids.append(h["id"])

    # Merge matched videos AND all channel videos into the persistent watchlist.
    # Channel videos go on the watchlist even if their title/description never
    # mentioned Debitum — that's how we catch comment-only mentions.
    watchlist = set(seen.get("watched_videos", []))
    watchlist.update(matched_video_ids)
    watchlist.update(channel_video_ids)
    # Cap to 500 most recent to keep API quota sane
    seen["watched_videos"] = list(watchlist)[-500:]

    print(f"Checking YouTube comments on {len(seen['watched_videos'])} watched videos...")
    all_hits += check_youtube_comments(seen["watched_videos"], seen["youtube_comment"])

    print(f"Found {len(all_hits)} new hit(s)")

    for hit in all_hits:
        post_to_slack(hit)
        seen[hit["seen_key"]].append(hit["id"])
        time.sleep(0.3)  # gentle pacing on slack

    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
