# APIDistributor

A folder-driven YouTube distribution system for faceless channels. Drop videos
into per-channel folders → they auto-upload to YouTube on a schedule you
control. Each channel has its own caption template, hashtags, and posting
cadence.

Built on top of [Post Bridge](https://www.post-bridge.com) (cloud social
scheduler) with a custom Python watcher and a small web UI.

## Why this exists

If you run multiple faceless YouTube channels and produce videos in batches:

- Buffer / Hootsuite / Postbridge alone are great for posting, but lack
  *folder-driven* automation — you still have to drag every video into their UI.
- DIY YouTube Data API integration hits Google's verification gates: uploads get
  forced to "Private" mode, OAuth refresh tokens expire weekly, and TikTok
  requires a separate multi-week app review.

APIDistributor delegates the OAuth/posting layer to Post Bridge (their app is
already verified by Google/TikTok/Meta) and handles everything *upstream* of
that — folder watching, caption templating, scheduling, burst protection,
multi-channel orchestration.

## Architecture

```
[Your factory output folder]
         │
         ▼ (file appears)
   [watcher.py]  ── 2-step upload ──▶  [Post Bridge]  ──▶ [YouTube]
         │
         └── stores intended slot in _state.json
         │
         ▼ (slot time arrives)
   [watcher.py] ─── type=now post ───▶ [Post Bridge]  ──▶ [YouTube]
```

- **Post Bridge** does the OAuth dance, file storage, and the actual YouTube
  API call (with its verified Google app, so uploads stay public and tokens
  don't expire on you).
- **Watcher** is the scheduler: folder-watching, caption templating, slot
  picking, burst protection, multi-channel.
- **UI** at `localhost:5050` shows status, lets you add channels, edit
  schedules, reschedule individual videos, and fire on-demand.

## Quick start

Prerequisites:
- macOS (Linux works too with minor tweaks; Windows untested)
- Python 3.9+ (macOS ships with this)
- A [Post Bridge](https://www.post-bridge.com) account on Creator or Pro plan
  with the **$5/mo API add-on enabled**

```bash
git clone https://github.com/<your-username>/APIDistributor.git
cd APIDistributor
cp .env.example .env

# Edit .env: paste your Post Bridge API key from
# https://www.post-bridge.com/dashboard/api-keys
```

```bash
./start-watcher.command
```

That double-clickable script launches the watcher + UI in one Terminal window.
First run installs Python deps in a local venv (~30 seconds). The web UI is at
http://localhost:5050.

## Project layout

```
APIDistributor/
├── .env                         # Secrets — gitignored
├── .env.example                 # Template
├── start-watcher.command        # Double-click to launch watcher + UI
├── channels/
│   ├── _example/                # Template — copy this for new channels
│   │   ├── config.yaml
│   │   ├── inbox/
│   │   └── posted/
│   └── <your channel>/          # Created via the UI or by copying _example
└── watcher/
    ├── watcher.py               # The scheduler
    ├── ui.py                    # Flask web UI
    └── requirements.txt
```

## Per-channel `config.yaml`

```yaml
# Post Bridge channel (the username from /v1/social-accounts).
social_account: "MyChannelName"

# Where the watcher looks for new videos. Empty = use this folder's inbox/.
source_folder: "/Users/you/factory/out"

# After scheduling, move the file into ./posted/.
move_after_post: true

# How late can a slot fire before we reschedule it?
catch_up_window_minutes: 30

schedule:
  times: ["08:00", "11:00", "14:00", "17:00", "20:00"]
  days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
  timezone: "America/New_York"

youtube:
  title_template: "{smart_title}"        # {filename} also works

  # First line of every description — visible above the fold on the watch page,
  # so it acts like a pinned comment without needing one.
  pinned_message: |
    👇 Earn money making videos like this 👇

  description: |
    Subscribe for more!
    #shorts

# Optional one-off: force the very first new video to land at this exact time.
# Ignored once any video is in state.json. Safe to leave in.
force_first_slot: "2026-05-04T08:00:00-04:00"
```

### Per-video sidecar metadata (optional)

Drop a `<videoname>.json` next to a video to override defaults for that
upload:

```json
{
  "title": "Custom YouTube title (2-100 chars)",
  "description": "Per-video description (replaces config's `description`)"
}
```

The `pinned_message` is still prepended automatically.

## How burst protection works

The watcher schedules videos in its own `_state.json`. It does **not**
pre-schedule them in Post Bridge. At each slot's time, the watcher fires a
`type=now` post to Post Bridge, which immediately uploads to YouTube.

If the watcher (or your machine) is offline when a slot is due:

- Within `catch_up_window_minutes` of the missed time → fire on resume
  (small lateness, looks normal).
- Beyond that window → push the video to the next free future slot.

Result: if your machine is off for 3 days, **no burst of 15 missed uploads**
when it boots. The schedule slides forward naturally.

## Web UI (localhost:5050)

- Dashboard: every channel with queued / fired / overdue counts
- Add Channel: form-based (no YAML editing)
- Channel detail: full schedule, video list, per-video actions:
  - **Fire now** — bump a queued video to fire on the next 30s cycle
  - **Reschedule** — datetime picker
  - **Delete** — remove from queue (file in posted/ stays)
- Inline `config.yaml` editor with YAML validation

## What's *not* exposed via Post Bridge's API

A few things their API doesn't surface (these are handled in their UI as
account-level defaults):

- Per-video YouTube **tags**. Workaround: hashtags in the caption — YouTube
  indexes them.
- Per-video **privacy** (public/unlisted/private). Set the default in Post
  Bridge's per-account settings.
- Per-video **made for kids**. Same — set the channel default in Post Bridge.
- **Pinning a comment** is impossible via any API (YouTube limitation, not
  Post Bridge's). Use `pinned_message` to put your CTA at the top of the
  description; it's visible above the fold and serves the same purpose.

## License

MIT. See LICENSE.

## Acknowledgements

- [Post Bridge](https://www.post-bridge.com) — does the OAuth + posting layer
  with verified social apps so we don't have to.
- Built with Claude Code.
