# APIDistributor

A self-hosted, folder-driven video distribution system for faceless YouTube channels.
Drop videos into per-channel folders → they auto-upload to YouTube on a schedule
you control. Each channel has its own caption template, hashtags, and posting cadence.

Built on top of [Postiz](https://postiz.com) (open-source social media scheduler)
with a custom Python watcher and a small web UI.

## Why this exists

If you run multiple faceless YouTube channels and produce videos in batches, the
existing tools either cost $30–100/month per seat (Buffer, Hootsuite, Postiz Cloud)
or require you to manually upload each batch. APIDistributor:

- Watches a folder per channel for new videos
- Uploads to Postiz, schedules in your channel's slots
- Fires `type=now` posts to Postiz at slot times (no Temporal pre-scheduling, so
  if your machine sleeps for a day, missed slots get rebalanced — no
  shadowban-bait burst when you wake the machine)
- Free if self-hosted; videos and metadata stay on your computer

## Architecture

```
[Your factory output folder]
         │
         ▼ (file appears)
   [watcher.py]  ── uploads file to ──▶ [Postiz container] ── auths via OAuth ──▶ [YouTube]
         │
         └── stores intended slot in _state.json
         │
         ▼ (slot time arrives)
   [watcher.py] ─── type=now post ───▶ [Postiz container] ── uploads to ────▶ [YouTube]
```

Postiz handles the OAuth dance, file storage, and the actual YouTube API call.
The watcher handles scheduling, burst protection, caption templating, and the
folder-driven workflow.

## Quick start

Prerequisites:
- macOS (Linux works too with minor tweaks; Windows untested)
- [Docker Desktop](https://docker.com/products/docker-desktop) installed
- A Google Cloud project with YouTube APIs enabled (see
  [setup guide](https://docs.postiz.com/providers/youtube))

```bash
git clone https://github.com/<your-username>/APIDistributor.git
cd APIDistributor
cp .env.example .env
# Edit .env: paste your Google OAuth credentials and a random JWT_SECRET

docker compose up -d
# Wait ~60s for Postiz to come up at http://localhost:4007

# In Postiz: register, connect a YouTube channel, then go to
# Settings → Developers → Public API → create an API key
# Paste the API key into .env (POSTIZ_API_KEY)

./start-watcher.command
# Opens a Terminal showing live logs. The web UI is at http://localhost:5050.
```

## Project layout

```
APIDistributor/
├── docker-compose.yaml          # Postiz + Postgres + Redis + Temporal stack
├── .env                         # Secrets — gitignored
├── .env.example                 # Template
├── start-watcher.command        # Double-click to launch watcher + UI
├── dynamicconfig/               # Temporal config (mostly empty)
├── channels/
│   ├── _example/                # Template — copy this for new channels
│   │   ├── config.yaml
│   │   ├── inbox/
│   │   └── posted/
│   └── <your channel name>/     # Created via the UI or by copying _example
└── watcher/
    ├── watcher.py               # The scheduler
    ├── ui.py                    # Flask web UI
    └── requirements.txt
```

## Per-channel `config.yaml`

```yaml
integration_name: "MyYouTubeChannel"     # case-insensitive match to Postiz
source_folder: "/Users/you/factory/out"  # where to watch for new videos
move_after_post: true                    # move into posted/ once scheduled
catch_up_window_minutes: 30              # tolerance for late firing

schedule:
  times: ["08:00", "11:00", "14:00", "17:00", "20:00"]
  days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
  timezone: "America/New_York"

youtube:
  title_template: "{smart_title}"        # {filename} also supported
  description: |
    Subscribe for more!
    #shorts
  privacy: "public"                      # public | unlisted | private
  made_for_kids: "no"
  tags: [shorts, viral]

# Optional one-off: force the very first new video to land at this exact time.
# Ignored once any video has been scheduled, so safe to leave in the file.
force_first_slot: "2026-05-03T05:30:00-04:00"
```

### Per-video sidecar metadata (optional)

Drop a `<videoname>.json` file next to a video to override defaults for that
single upload:

```json
{
  "title": "Custom YouTube title",
  "description": "Custom description with #hashtags",
  "tags": ["custom", "tags"]
}
```

## How burst protection works

The watcher schedules videos in its own `_state.json`. It does **not**
pre-schedule them in Postiz. At each slot's time, the watcher fires a
`type=now` post to Postiz, which immediately uploads to YouTube.

If the watcher (or Docker, or your machine) is offline when a slot is due:

- Within `catch_up_window_minutes` of the missed time → fire on resume
  (small lateness, looks normal).
- Beyond that window → push the video to the next free future slot.

Result: if your machine is off for 3 days, **no burst of 15 missed uploads**
when it boots. The schedule slides forward naturally.

## Web UI (localhost:5050)

- Dashboard: every channel with queued / fired / overdue counts
- Add Channel: form-based (no YAML editing)
- Channel detail: full schedule + inline config editor
- Restart Docker stack: one click, no terminal

## Limitations

- Single user / single machine. No remote management.
- YouTube only on the watcher side (Postiz supports more, but the watcher
  pipeline targets YouTube settings; adding TikTok/Instagram support is
  ~50 lines per platform).
- Postiz Public API is rate-limited to 30 req/hour by default; we bump it
  via env (`API_LIMIT`). At ~2 requests per video, default limit caps at
  ~15 videos/hour processed.

## License

MIT. See LICENSE.

## Acknowledgements

- [Postiz](https://github.com/gitroomhq/postiz-app) — does the heavy lifting
  on the OAuth + posting side
- Built with Claude Code
