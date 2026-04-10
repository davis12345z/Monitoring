# Debitum mentions monitor

Runs every 30 minutes on GitHub Actions (free). Posts new mentions of
"Debitum" to Slack. Sources: Reddit, YouTube videos, YouTube comments on
any video ever matched, and Google Alerts (news/blogs/web).

## How YouTube monitoring works

The script catches Debitum mentions in three different places on YouTube:

**1. Videos where title/description mentions Debitum** — caught by the keyword
search (`q=debitum`) every run. Posted to Slack as "YouTube video".

**2. Comments on those videos** — any matched video is added to a persistent
watchlist in `seen.json`, and comments are polled every 30 min forever.

**3. Videos where Debitum is ONLY mentioned in comments (or only spoken in
the video itself)** — YouTube's API doesn't let us search comment text
globally, so these are invisible to keyword search. The workaround: we
monitor specific **channels** (P2P reviewers, passive income YouTubers, etc.)
and automatically add every recent video from those channels to the comment
watchlist — even if the video's title/description never says "Debitum". A
video called "My Monthly Portfolio Update" from a watched channel will get
its comments polled, so any Debitum comment mention is caught.

### How to add channels to watch

Find a channel's ID (the `UC...` string):
- Option A: open the channel page → right-click → View page source → search
  for `"channelId":"UC...` and copy it
- Option B: paste the channel URL into <https://commentpicker.com/youtube-channel-id.php>

Then either:
- **Easier:** add a GitHub secret `YOUTUBE_CHANNELS` with comma-separated
  IDs: `UCxxxx,UCyyyy,UCzzzz`. No code change needed; update anytime in
  GitHub settings.
- **Or:** edit the `DEFAULT_CHANNELS` list at the top of `monitor.py` and
  commit.

Good channels to seed with (search YouTube yourself and verify):
- "P2P Empire", "Passive Income YT", "Revenue Land", "Blog des P2P", and
  similar P2P investing review channels. Start with 5–10, you can always add more.

The script pulls the 10 most recent uploads per channel each run (≈1 API
unit per channel) and adds them to the comment watchlist automatically.

## Setup (one-time, ~15 min)

### 1. Create a Slack Incoming Webhook
- Go to <https://api.slack.com/apps> → Create New App → From scratch
- Pick your workspace, name it "Debitum Monitor"
- Features → Incoming Webhooks → toggle ON → Add New Webhook to Workspace
- Pick the channel (e.g. `#debitum-mentions`) → Allow
- Copy the webhook URL (starts with `https://hooks.slack.com/services/...`)

### 2. Get a YouTube Data API v3 key
- Go to <https://console.cloud.google.com/>
- Create a new project "debitum-monitor"
- APIs & Services → Library → search "YouTube Data API v3" → Enable
- APIs & Services → Credentials → Create Credentials → API key
- Copy the key

### 3. Create Google Alerts (optional but recommended)

Create TWO alerts for maximum coverage:

**Alert 1 — General web (news, blogs, forums):**
- Go to <https://www.google.com/alerts>
- Search: `"Debitum"`
- Click "Show options": How often → As-it-happens, Sources → Automatic,
  Language → Any, Region → Any, How many → All results, Deliver to → **RSS feed**
- Click Create Alert

**Alert 2 — YouTube-specific (catches description-only mentions Google indexes):**
- Same page, create a new alert
- Search: `"Debitum" site:youtube.com`
- Same options as above, Deliver to → **RSS feed**
- This catches YouTube videos where "Debitum" appears anywhere Google can
  see (title, description, even auto-captions) — a safety net for videos
  the YouTube API search misses

On the alerts dashboard, click the RSS icon next to each alert and copy
both URLs. Combine them into the `GOOGLE_ALERTS_RSS` secret separated by
a comma: `https://google.com/alerts/feeds/xxx,https://google.com/alerts/feeds/yyy`

### 4. Push this folder to a new GitHub repo
```bash
cd "Debitum Investments monitoring"
git init
git add .
git commit -m "initial"
# create an empty repo on github.com first, then:
git remote add origin git@github.com:YOUR_USERNAME/debitum-monitor.git
git branch -M main
git push -u origin main
```

### 5. Add secrets
On GitHub: your repo → Settings → Secrets and variables → Actions → New repository secret.
Add three secrets:
- `SLACK_WEBHOOK_URL` — from step 1
- `YOUTUBE_API_KEY` — from step 2
- `GOOGLE_ALERTS_RSS` — from step 3 (leave blank if you skipped it)
- `YOUTUBE_CHANNELS` — comma-separated channel IDs to watch for
  comment-only mentions (optional, see "How YouTube monitoring works" above)

### 6. Turn on Actions
- Repo → Actions tab → enable workflows if prompted
- Click "debitum-monitor" → "Run workflow" to test immediately
- After that it runs automatically every 30 minutes

## Files
- `monitor.py` — the script
- `.github/workflows/monitor.yml` — the schedule + runner
- `seen.json` — state (auto-updated by the bot, don't edit)
- `README.md` — this file

## Tuning
- Change cadence: edit the cron in `.github/workflows/monitor.yml`
- Change keywords: edit `KEYWORD_RE` in `monitor.py`
- Add more sources later: each source is its own `check_*` function

## Cost
$0. GitHub Actions gives 2000 free minutes/month on private repos (unlimited
on public). Each run is ~30 seconds → ~1500 min/month at 30-min cadence, so
make the repo **public** or increase cadence to hourly if you go over.

YouTube API free quota is 10,000 units/day — this script uses ~1000/day.
