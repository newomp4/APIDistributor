#!/usr/bin/env python3
"""
APIDistributor web UI — runs alongside watcher.py in the same process.

http://localhost:5050  →  Dashboard, channels, variants, per-video actions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from flask import (
    Flask, abort, jsonify, redirect, render_template_string, request, url_for,
    Response,
)

import variants as variants_lib
import watcher as watcher_lib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = PROJECT_ROOT / "channels"

app = Flask(__name__)


# -------------------- helpers --------------------


def api_url() -> str:
    return os.environ.get("POSTBRIDGE_API_URL", "https://api.post-bridge.com/v1").rstrip("/")


def api_key() -> str:
    return os.environ.get("POSTBRIDGE_API_KEY", "")


def fetch_integrations() -> list[dict]:
    """{id, platform, username} for connected Post Bridge accounts."""
    try:
        r = requests.get(
            f"{api_url()}/social-accounts",
            headers={"Authorization": f"Bearer {api_key()}"},
            timeout=5,
        )
        r.raise_for_status()
        payload = r.json()
        return payload.get("data", payload) if isinstance(payload, dict) else payload
    except Exception:
        return []


def channel_dirs() -> list[Path]:
    if not CHANNELS_DIR.exists():
        return []
    return sorted(
        p for p in CHANNELS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "config.yaml").exists()
    )


def load_channel(name: str) -> tuple[Path, dict, dict]:
    channel_dir = CHANNELS_DIR / name
    if not channel_dir.exists() or not (channel_dir / "config.yaml").exists():
        abort(404)
    config = yaml.safe_load((channel_dir / "config.yaml").read_text()) or {}
    state_file = channel_dir / "_state.json"
    state = {"videos": []}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            pass
    return channel_dir, config, state


def write_config(channel_dir: Path, config: dict) -> None:
    (channel_dir / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, default_flow_style=False, allow_unicode=True)
    )


def write_state(channel_dir: Path, state: dict) -> None:
    (channel_dir / "_state.json").write_text(json.dumps(state, indent=2))


def today_strip_html(state: dict, config: dict) -> str:
    """Horizontal time-of-day strip showing today's videos as colored dots,
    with a marker for current time. 0..24 hours mapped to 0..100% width."""
    tz = ZoneInfo(config.get("schedule", {}).get("timezone", "UTC"))
    now = datetime.now(tz)
    today = now.date()
    now_pct = (now.hour * 60 + now.minute) / (24 * 60) * 100

    markers = []
    for v in state.get("videos", []):
        try:
            slot = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00")).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if slot.date() != today:
            continue
        pct = (slot.hour * 60 + slot.minute) / (24 * 60) * 100
        if v.get("published_url"):
            cls = "posted"
        elif v.get("publish_failed"):
            cls = "failed"
        elif v.get("fired"):
            cls = "fired"
        else:
            cls = "queued"
        title = html_escape(v.get("title", v.get("filename", "?"))[:80])
        time_str = slot.strftime("%-I:%M %p")
        markers.append(
            f'<div class="slot-marker {cls}" style="left:{pct:.2f}%;" title="{time_str}: {title}"></div>'
        )
    grid_lines = ""
    for h in (0, 6, 12, 18):
        x = h / 24 * 100
        label_str = f"{h:02d}h" if h > 0 else "12am"
        if h == 12:
            label_str = "noon"
        elif h == 6:
            label_str = "6am"
        elif h == 18:
            label_str = "6pm"
        grid_lines += f'<div class="grid-line" style="left:{x:.2f}%;"></div>'
        grid_lines += f'<div class="grid-label" style="left:{x:.2f}%;">{label_str}</div>'

    return (
        f'<div class="today-strip">'
        f'{grid_lines}'
        f'<div class="now-line" style="left:{now_pct:.2f}%;"></div>'
        f'<div class="now-label" style="left:{now_pct:.2f}%;">now</div>'
        f'{"".join(markers)}'
        f'</div>'
    )


def fired_histogram_html(state: dict, config: dict, days: int = 7) -> str:
    """Tiny bar chart of fired-count per day for the last N days."""
    tz = ZoneInfo(config.get("schedule", {}).get("timezone", "UTC"))
    today = datetime.now(tz).date()
    counts: dict = {today - timedelta(days=i): 0 for i in range(days - 1, -1, -1)}
    for v in state.get("videos", []):
        if not v.get("fired"):
            continue
        ts = v.get("fired_at") or v.get("scheduled_for")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date()
        except Exception:
            continue
        if d in counts:
            counts[d] += 1
    max_count = max(counts.values()) or 1
    bars = ""
    for d, n in counts.items():
        h_pct = (n / max_count) * 100 if max_count else 0
        cls = "has" if n > 0 else ""
        if d == today:
            cls = (cls + " today").strip()
        title = f"{d.strftime('%a %b %-d')}: {n} fired"
        bars += f'<div class="bar {cls}" style="height:{max(h_pct,4):.0f}%;" title="{title}"></div>'
    return f'<div class="histo">{bars}</div>'


def channel_health(stats: dict, config: dict, state: dict, pb_ok: bool) -> tuple[str, str]:
    """Returns (css_class, label) for an at-a-glance status pill."""
    if not pb_ok:
        return "problem", "PB offline"
    failed = sum(1 for v in state.get("videos", []) if v.get("publish_failed"))
    if failed:
        return "problem", f"{failed} failed"
    if stats["overdue"] > 0:
        return "attention", f"{stats['overdue']} overdue"
    buffer_target = int(config.get("media_buffer_size", 0) or 0)
    if buffer_target > 0:
        buffer_uploaded = sum(
            1 for v in state.get("videos", [])
            if not v.get("fired") and not v.get("published_url")
            and (v.get("media") or {}).get("id")
        )
        unfired = sum(
            1 for v in state.get("videos", [])
            if not v.get("fired") and not v.get("published_url")
        )
        if buffer_uploaded < min(buffer_target, unfired) * 0.5 and unfired > 0:
            return "attention", "buffer low"
    if stats["queued"] == 0:
        return "attention", "queue empty"
    return "healthy", "healthy"


def channel_stats(state: dict, config: dict) -> dict:
    tz = ZoneInfo(config.get("schedule", {}).get("timezone", "UTC"))
    now = datetime.now(tz)
    queued = [v for v in state.get("videos", []) if not v.get("fired")]
    fired = [v for v in state.get("videos", []) if v.get("fired")]
    published = [v for v in state.get("videos", []) if v.get("published_url")]
    overdue = 0
    next_slot = None
    for v in queued:
        try:
            slot = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00")).astimezone(tz)
        except (KeyError, ValueError):
            continue
        if slot < now:
            overdue += 1
        elif next_slot is None or slot < next_slot:
            next_slot = slot

    last_posted_at = None
    last_posted_url = None
    for v in fired:
        ts = v.get("fired_at") or v.get("scheduled_for")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if last_posted_at is None or dt > last_posted_at:
            last_posted_at = dt
            last_posted_url = v.get("published_url")

    return {
        "queued": len(queued),
        "fired": len(fired),
        "published": len(published),
        "overdue": overdue,
        "next_slot": next_slot.strftime("%a %b %d · %-I:%M %p") if next_slot else None,
        "last_posted_iso": (last_posted_at.isoformat() if last_posted_at else None),
        "last_posted_url": last_posted_url,
    }


CHANNEL_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def safe_name(name: str) -> bool:
    return bool(CHANNEL_NAME_RE.match(name))


def html_escape(s) -> str:
    if s is None:
        return ""
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
    )


# -------------------- styles + base layout --------------------


CSS = """
.slot-dot { display:inline-block; width:10px; height:10px; border-radius:50%; }
.slot-dot.active { background: var(--accent); box-shadow: 0 0 0 1px rgba(122,162,255,0.30); }
.slot-dot.inactive { background: transparent; border: 1px solid var(--border-strong); }
tr.next-up td { background: rgba(122,162,255,0.06); border-left: 2px solid var(--accent); }
tr.next-up td:first-child::before {
  content: "▶ NEXT UP"; display: inline-block; font-size: 9px; font-weight: 700;
  letter-spacing: 0.08em; color: var(--accent); background: rgba(122,162,255,0.15);
  padding: 2px 6px; border-radius: 4px; margin-right: 8px; vertical-align: 2px;
}

/* Today's timeline strip */
.today-strip { position: relative; height: 38px; margin-top: 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.today-strip .grid-line { position: absolute; top: 0; bottom: 0; width: 1px; background: rgba(255,255,255,0.04); }
.today-strip .grid-label { position: absolute; bottom: 2px; font-size: 9px; color: var(--text-3); transform: translateX(-50%); font-family: ui-monospace, monospace; }
.today-strip .now-line { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); box-shadow: 0 0 8px rgba(122,162,255,0.6); z-index: 5; }
.today-strip .now-label { position: absolute; top: 2px; font-size: 9px; color: var(--accent); transform: translateX(-50%); font-weight: 700; z-index: 6; }
.today-strip .slot-marker { position: absolute; top: 8px; width: 14px; height: 14px; border-radius: 50%; transform: translateX(-50%); border: 2px solid var(--bg); cursor: help; transition: transform 0.15s; }
.today-strip .slot-marker:hover { transform: translateX(-50%) scale(1.4); z-index: 10; }
.today-strip .slot-marker.posted   { background: var(--ok); }
.today-strip .slot-marker.fired    { background: var(--accent); }
.today-strip .slot-marker.queued   { background: var(--warn); }
.today-strip .slot-marker.failed   { background: var(--err); }

/* Health badges */
.health-pill { display:inline-flex; align-items:center; gap:6px; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 999px; letter-spacing: 0.01em; }
.health-pill::before { content:""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.health-pill.healthy { background: rgba(52,211,153,0.10); color: var(--ok); border: 1px solid rgba(52,211,153,0.25); }
.health-pill.attention { background: rgba(251,191,36,0.10); color: var(--warn); border: 1px solid rgba(251,191,36,0.30); }
.health-pill.problem { background: rgba(248,113,113,0.10); color: var(--err); border: 1px solid rgba(248,113,113,0.30); }

/* Progress bars */
.progress-bar { height: 6px; background: var(--bg); border-radius: 3px; overflow: hidden; border: 1px solid var(--border); }
.progress-bar .fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); transition: width 0.3s; }
.progress-bar .fill.full { background: linear-gradient(90deg, var(--ok), #4ade80); }

/* Mini histogram for last-7-days fired */
.histo { display: flex; align-items: flex-end; gap: 3px; height: 28px; margin-top: 6px; }
.histo .bar { flex: 1; background: var(--bg-3); border-radius: 2px 2px 0 0; min-height: 2px; transition: background 0.2s; }
.histo .bar.has { background: var(--accent); }
.histo .bar.today { background: var(--accent-2); box-shadow: 0 0 0 1px rgba(167,139,250,0.40); }

/* Hero stats — big numbers on Performance page */
.hero-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap: 12px; margin-bottom: 24px; }
.hero-card { background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px 20px; transition: border-color var(--transition); }
.hero-card:hover { border-color: var(--border-strong); }
.hero-card .label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
.hero-card .value { font-size: 28px; font-weight: 700; margin-top: 6px; letter-spacing: -0.02em; line-height: 1; }
.hero-card .sub { font-size: 11px; color: var(--text-3); margin-top: 6px; }

/* Section header with subtle icon support */
h2 .h2-icon { display: inline-block; opacity: 0.6; margin-right: 6px; font-size: 14px; vertical-align: 1px; }

:root {
  --bg: #0a0b0f;
  --bg-2: #14161d;
  --bg-3: #1c1f2a;
  --border: #262a36;
  --border-strong: #353a4a;
  --text: #e6e8ee;
  --text-2: #9ba1b0;
  --text-3: #5f6577;
  --accent: #7aa2ff;
  --accent-2: #a78bfa;
  --ok: #34d399;
  --warn: #fbbf24;
  --err: #f87171;
  --shadow: 0 1px 0 rgba(255,255,255,0.04), 0 8px 24px rgba(0,0,0,0.35);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "SF Pro Text", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.nav {
  position: sticky; top: 0; z-index: 50;
  background: rgba(10,11,15,0.85);
  backdrop-filter: saturate(150%) blur(8px);
  border-bottom: 1px solid var(--border);
}
.nav-inner {
  max-width: 1200px; margin: 0 auto;
  padding: 14px 24px;
  display: flex; gap: 18px; align-items: center;
}
.brand { font-weight: 700; font-size: 16px; letter-spacing: -0.01em; color: var(--text); text-decoration: none; }
.brand .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background: var(--accent); margin-right: 8px; vertical-align:middle; }
.nav a { color: var(--text-2); text-decoration: none; font-weight: 500; font-size: 13px; padding: 6px 10px; border-radius: 6px; }
.nav a:hover, .nav a.active { color: var(--text); background: var(--bg-2); }
.nav-spacer { flex: 1; }
.nav .meta { color: var(--text-3); font-size: 12px; font-family: ui-monospace, monospace; }
.container { max-width: 1200px; margin: 0 auto; padding: 32px 24px 64px; }

h1 { font-size: 28px; font-weight: 700; margin: 0 0 4px 0; letter-spacing: -0.02em; }
h2 { font-size: 17px; font-weight: 600; margin: 28px 0 12px 0; letter-spacing: -0.01em; color: var(--text); }
h3 { font-size: 15px; font-weight: 600; margin: 0; }
.subhead { color: var(--text-2); font-size: 14px; margin: 0 0 24px 0; }
.muted { color: var(--text-2); }
.muted-2 { color: var(--text-3); font-size: 12px; }
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: var(--space-4); }
.card {
  background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; transition: border-color var(--transition), transform var(--transition), box-shadow var(--transition);
  box-shadow: var(--shadow-1);
}
.card:hover { border-color: var(--border-strong); }
.card-link { color: inherit; text-decoration: none; display:block; }
.card-link:hover { text-decoration: none; }
.card-link:hover .card { transform: translateY(-1px); box-shadow: var(--shadow-2); border-color: var(--border-strong); }
.card-row { display: flex; align-items: center; gap: 10px; }
.card-row + .card-row { margin-top: 8px; }

.pill { display:inline-flex; align-items:center; gap:6px; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 500; background: var(--bg-3); color: var(--text-2); border: 1px solid var(--border); }
.pill.ok   { background: rgba(52,211,153,0.10); color: var(--ok);   border-color: rgba(52,211,153,0.25); }
.pill.warn { background: rgba(251,191,36,0.10); color: var(--warn); border-color: rgba(251,191,36,0.30); }
.pill.err  { background: rgba(248,113,113,0.10); color: var(--err); border-color: rgba(248,113,113,0.25); }
.pill .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: 0.9; }

.stat { display: inline-flex; align-items: baseline; gap: 6px; margin-right: 14px; font-size: 13px; color: var(--text-2); }
.stat strong { color: var(--text); font-weight: 600; font-size: 16px; }
.kbd { font-family: ui-monospace, monospace; font-size: 11px; background: var(--bg-3); border: 1px solid var(--border-strong); padding: 1px 5px; border-radius: 4px; color: var(--text-2); }

table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border); }
td { padding: 12px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
tbody tr:hover { background: rgba(255,255,255,0.02); }

form .field { margin: 14px 0; }
form label { display: block; font-size: 12px; color: var(--text-2); margin-bottom: 6px; font-weight: 500; }
form input[type=text], form input[type=number], form input[type=datetime-local], form select, form textarea {
  width: 100%; padding: 9px 12px; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px; font-size: 14px; font-family: inherit;
  transition: border-color 0.15s, background 0.15s;
}
form input:focus, form select:focus, form textarea:focus { outline: none; border-color: var(--accent); background: var(--bg-2); }
form textarea { min-height: 90px; font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; }
.btn, button {
  padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px;
  border: 1px solid transparent;
  transition: background var(--transition), border-color var(--transition), color var(--transition), transform 0.06s ease;
  background: var(--accent); color: #0a0b0f; font-family: inherit;
}
.btn:hover, button:hover { background: #93b6ff; }
.btn:active, button:active { transform: scale(0.98); }
.btn.subtle, button.subtle { background: var(--bg-3); color: var(--text); border-color: var(--border); }
.btn.subtle:hover, button.subtle:hover { background: #232737; border-color: var(--border-strong); }
.btn.ghost, button.ghost { background: transparent; color: var(--text-2); border-color: var(--border); }
.btn.ghost:hover, button.ghost:hover { color: var(--text); border-color: var(--border-strong); background: var(--bg-2); }
.btn.ghost.active, .btn.tiny.ghost.active { color: var(--text); background: var(--bg-3); border-color: var(--border-strong); }
.btn.danger, button.danger { background: rgba(248,113,113,0.10); color: var(--err); border: 1px solid rgba(248,113,113,0.30); }
.btn.danger:hover, button.danger:hover { background: rgba(248,113,113,0.20); }
.btn.tiny, button.tiny { padding: 4px 9px; font-size: 12px; font-weight: 500; }
.btn:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.row.spread { justify-content: space-between; }
.divider { height: 1px; background: var(--border); margin: 20px 0; }

details summary { cursor: pointer; user-select: none; color: var(--accent); font-size: 13px; }
details summary::-webkit-details-marker { display: none; }
details[open] summary::after { content: " ▴"; }
details:not([open]) summary::after { content: " ▾"; }
details > div { margin-top: 10px; padding: 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; }

pre { background: var(--bg); border: 1px solid var(--border); padding: 14px; border-radius: 6px; overflow-x: auto; font-size: 12px; line-height: 1.5; color: var(--text); white-space: pre-wrap; }
code { background: var(--bg-3); padding: 1px 6px; border-radius: 4px; font-size: 12px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.empty { padding: 48px 20px; text-align: center; color: var(--text-2); border: 1px dashed var(--border); border-radius: var(--radius); background: var(--bg-2); }
.empty h3 { color: var(--text); margin-bottom: 6px; font-size: 16px; }
.empty .icon { font-size: 32px; opacity: 0.5; display: block; margin-bottom: 12px; }

footer.site-footer { padding: 32px 24px 24px; max-width: 1200px; margin: 0 auto; border-top: 1px solid var(--border); margin-top: 64px; display: flex; gap: 24px; align-items: center; flex-wrap: wrap; color: var(--text-3); font-size: 12px; }
footer.site-footer .nav-spacer { flex: 1; }
footer.site-footer a { color: var(--text-2); }
footer.site-footer a:hover { color: var(--text); }

/* Slightly tighter input padding on number inputs */
form input[type=number] { font-variant-numeric: tabular-nums; }
form select { appearance: none; background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%239ba1b0'><path d='M4 6l4 4 4-4'/></svg>"); background-repeat: no-repeat; background-position: right 10px center; padding-right: 32px; }

table { font-variant-numeric: tabular-nums; }

/* When a section is heavy (raw YAML editor), let it collapse */
details.advanced > summary { padding: 10px 12px; background: var(--bg-2); border: 1px solid var(--border); border-radius: 8px; }
details.advanced > div { margin-top: 8px; padding: 0; background: transparent; border: 0; }

/* Page enter animation — very subtle */
main.container > h1 { animation: fadeIn 0.25s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
"""

JS = """
(function() {
  // Live refresh of dashboard cards every 5s.
  async function refreshDashboard() {
    try {
      const r = await fetch('/api/status');
      if (!r.ok) return;
      const data = await r.json();
      data.forEach(c => {
        const card = document.querySelector('[data-channel="' + c.name + '"]');
        if (!card) return;
        const set = (sel, txt) => { const el = card.querySelector(sel); if (el) el.textContent = txt; };
        set('[data-stat=queued]', c.queued);
        set('[data-stat=fired]', c.fired);
        set('[data-stat=overdue]', c.overdue);
        set('[data-stat=next]', c.next_slot || '— nothing queued —');
        const pill = card.querySelector('[data-stat=overdue-pill]');
        if (pill) {
          pill.classList.remove('ok','warn');
          pill.classList.add(c.overdue === 0 ? 'ok' : 'warn');
        }
      });
    } catch(e) {}
  }
  if (document.querySelector('[data-channel]')) setInterval(refreshDashboard, 5000);

  // Filter videos in the channel detail table by title.
  const search = document.querySelector('[data-search-videos]');
  if (search) {
    search.addEventListener('input', () => {
      const q = search.value.toLowerCase();
      document.querySelectorAll('[data-video-row]').forEach(row => {
        row.style.display = row.dataset.title.toLowerCase().includes(q) ? '' : 'none';
      });
    });
  }

  // Live-update relative timestamps every 30s.
  function fmtRel(ms) {
    const abs = Math.abs(ms);
    const m = Math.round(abs / 60000);
    if (m < 1)   return ms < 0 ? 'just now' : 'in <1 min';
    if (m < 60)  return ms < 0 ? m + 'm ago' : 'in ' + m + 'm';
    const h = Math.round(m / 60);
    if (h < 48)  return ms < 0 ? h + 'h ago' : 'in ' + h + 'h';
    const d = Math.round(h / 24);
    return ms < 0 ? d + 'd ago' : 'in ' + d + 'd';
  }
  function refreshRel() {
    document.querySelectorAll('[data-when]').forEach(el => {
      const t = parseInt(el.dataset.when, 10);
      if (!t) return;
      el.textContent = fmtRel(Date.now() - t);
    });
  }
  refreshRel();
  setInterval(refreshRel, 30000);

  // Click-to-copy for URLs.
  document.querySelectorAll('[data-copy]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        const orig = btn.textContent;
        btn.textContent = 'Copied ✓';
        setTimeout(() => btn.textContent = orig, 1200);
      } catch (err) {}
    });
  });

  // Loading state on form submits — disables the button + shows an ellipsis
  // so it's clear the action is in flight.
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', () => {
      const btn = form.querySelector('button[type=submit]');
      if (!btn || btn.dataset.skipLoading === '1') return;
      btn.disabled = true;
      btn.dataset.origLabel = btn.textContent;
      btn.textContent = btn.textContent.trim().replace(/[⏵▶↻↑↓+×]\\s*/, '') + ' …';
      btn.style.opacity = '0.65';
    });
  });

  // Auto-dismiss flash messages after 4s with a fade.
  document.querySelectorAll('.flash-pill').forEach(p => {
    setTimeout(() => { p.style.transition = 'opacity 0.4s'; p.style.opacity = '0'; }, 4000);
    setTimeout(() => p.remove(), 4500);
  });
})();
"""

BASE = """
<!doctype html>
<html><head>
<title>{{ title }} — APIDistributor</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{{ css|safe }}</style>
</head><body>
<header class="nav">
  <div class="nav-inner">
    <a class="brand" href="{{ url_for('dashboard') }}"><span class="dot"></span>APIDistributor</a>
    <a href="{{ url_for('dashboard') }}" {% if active=='dashboard' %}class="active"{% endif %}>Channels</a>
    {% if channels %}
    <select onchange="if(this.value)location.href='/channel/'+this.value" style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px;">
      <option value="">Jump to channel…</option>
      {% for c in channels %}<option value="{{ c }}" {% if active_channel==c %}selected{% endif %}>{{ c }}</option>{% endfor %}
    </select>
    {% endif %}
    <a href="{{ url_for('add_channel') }}" {% if active=='add' %}class="active"{% endif %}>+ Add</a>
    <span class="nav-spacer"></span>
    {% if flash %}<span class="pill ok flash-pill"><span class="dot"></span>{{ flash }}</span>{% endif %}
    <span class="meta">{{ api }}</span>
  </div>
</header>
<main class="container">
{{ body|safe }}
</main>
<footer class="site-footer">
  <span>APIDistributor · folder-driven YouTube distribution</span>
  <span class="nav-spacer"></span>
  <a href="https://github.com/newomp4/APIDistributor" target="_blank" rel="noopener">github.com/newomp4/APIDistributor</a>
</footer>
<script>{{ js|safe }}</script>
</body></html>
"""


def render(title: str, body: str, active: str = "", active_channel: str = "") -> str:
    return render_template_string(
        BASE, title=title, body=body, css=CSS, js=JS, api=api_url(),
        active=active, active_channel=active_channel,
        channels=[c.name for c in channel_dirs()],
        flash=request.args.get("flash", ""),
    )


# -------------------- dashboard --------------------


@app.route("/")
def dashboard():
    cards: list[dict] = []
    for chan in channel_dirs():
        config = yaml.safe_load((chan / "config.yaml").read_text()) or {}
        state = {"videos": []}
        sf = chan / "_state.json"
        if sf.exists():
            try:
                state = json.loads(sf.read_text())
            except json.JSONDecodeError:
                pass
        stats = channel_stats(state, config)
        sched = config.get("schedule", {})
        cards.append({
            "name": chan.name,
            "integration": config.get("social_account") or config.get("integration_name") or "?",
            "times": ", ".join(sched.get("times", [])) or "(none)",
            **stats,
        })

    integrations = fetch_integrations()
    integ_count = len(integrations)
    pb_ok = integ_count > 0
    cards_html = ""
    for c in cards:
        # Load full state/config so we can compute health + histogram
        chan_dir = CHANNELS_DIR / c["name"]
        config_full = yaml.safe_load((chan_dir / "config.yaml").read_text()) or {}
        state_full = {"videos": []}
        sf = chan_dir / "_state.json"
        if sf.exists():
            try:
                state_full = json.loads(sf.read_text())
            except json.JSONDecodeError:
                pass
        stats_full = channel_stats(state_full, config_full)
        health_cls, health_lbl = channel_health(stats_full, config_full, state_full, pb_ok)

        last_posted_html = ""
        if c.get("last_posted_iso"):
            try:
                ts_ms = int(datetime.fromisoformat(c["last_posted_iso"]).timestamp() * 1000)
                last_posted_html = (
                    f'<div class="muted-2" style="margin-top:6px;">'
                    f'Last posted: <span data-when="{ts_ms}">…</span>'
                    f'</div>'
                )
            except Exception:
                pass
        else:
            last_posted_html = '<div class="muted-2" style="margin-top:6px;">Last posted: <em>never</em></div>'

        histo_html = fired_histogram_html(state_full, config_full)

        cards_html += f"""
        <a class="card-link" href="/channel/{c['name']}"><div class="card" data-channel="{c['name']}">
          <div class="card-row spread"><h3>{html_escape(c['name'])}</h3>
            <span class="health-pill {health_cls}">{html_escape(health_lbl)}</span>
          </div>
          <div class="muted-2" style="margin-top:4px;">{html_escape(c['integration'])} · {html_escape(c['times'])}</div>
          <div class="card-row" style="margin-top:14px;">
            <span class="stat"><strong data-stat="queued">{c['queued']}</strong> queued</span>
            <span class="stat"><strong data-stat="fired">{c['fired']}</strong> fired</span>
            <span class="stat"><strong data-stat="overdue">{c['overdue']}</strong> overdue</span>
          </div>
          <div class="muted-2" style="margin-top:10px;">Next: <span data-stat="next">{html_escape(c['next_slot'] or '— nothing queued —')}</span></div>
          {last_posted_html}
          <div class="muted-2" style="margin-top:10px;">Last 7 days</div>
          {histo_html}
        </div></a>
        """

    if not cards:
        cards_html = """
        <div class="empty">
          <span class="icon">📡</span>
          <h3>No channels yet</h3>
          <div>Wire up your first channel to start distributing.</div>
          <div style="margin-top:14px;"><a class="btn" href="/add">+ Add channel</a></div>
        </div>"""

    pb_status_html = (
        f'<span class="pill ok" style="margin-right:8px;"><span class="dot"></span>Post Bridge connected · {integ_count} accounts</span>'
        if pb_ok else
        '<span class="pill err" style="margin-right:8px;"><span class="dot"></span>Post Bridge unreachable — check your API key</span>'
    )
    body = f"""
    <h1>Channels</h1>
    <p class="subhead">{pb_status_html}auto-refresh every 5s</p>
    <div class="grid">{cards_html}</div>
    """
    return render("Channels", body, active="dashboard")


# -------------------- channel detail --------------------


@app.route("/channel/<name>")
def channel_detail(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, config, state = load_channel(name)
    stats = channel_stats(state, config)
    sched = config.get("schedule", {})
    yt = config.get("youtube", {})
    tz = ZoneInfo(sched.get("timezone", "UTC"))

    indexed = list(enumerate(state.get("videos", [])))
    indexed.sort(key=lambda iv: iv[1].get("scheduled_for", ""))

    filt = request.args.get("filter", "all")
    if filt == "queued":
        indexed = [(i, v) for i, v in indexed if not v.get("fired")]
    elif filt == "fired":
        indexed = [(i, v) for i, v in indexed if v.get("fired")]

    # Identify the "next up" video — earliest unfired with a future slot.
    now_utc = datetime.now(timezone.utc)
    next_up_idx = None
    for orig_idx, v in indexed:
        if v.get("fired") or v.get("published_url"):
            continue
        try:
            slot_dt = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00"))
        except Exception:
            continue
        if slot_dt > now_utc:
            next_up_idx = orig_idx
            break

    rows_html = ""
    for orig_idx, v in indexed:
        is_published = bool(v.get("published_url"))
        is_failed = bool(v.get("publish_failed"))
        is_prescheduled = bool(v.get("prescheduled"))
        is_submitted = bool(v.get("fired"))
        if is_published:
            status_pill = '<span class="pill ok"><span class="dot"></span>posted</span>'
        elif is_failed:
            status_pill = '<span class="pill err"><span class="dot"></span>failed</span>'
        elif is_prescheduled:
            status_pill = '<span class="pill ok"><span class="dot"></span>queued in PB</span>'
        elif is_submitted:
            status_pill = '<span class="pill ok"><span class="dot"></span>sent</span>'
        else:
            status_pill = '<span class="pill warn"><span class="dot"></span>queued</span>'

        try:
            slot_dt = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00"))
            slot_local = slot_dt.astimezone(tz)
            slot_str = slot_local.strftime("%a %b %-d · %-I:%M %p")
            slot_rel_ms = int(slot_dt.timestamp() * 1000)
            slot_input = slot_local.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            slot_str = v.get("scheduled_for", "")
            slot_rel_ms = 0
            slot_input = ""
        raw_title = v.get("title", v["filename"])
        title_html = html_escape(raw_title)
        meta_bits = []
        if v.get("published_url"):
            url = v["published_url"]
            meta_bits.append(
                f'<a href="{html_escape(url)}" target="_blank" rel="noopener" title="Open on YouTube">▶ {html_escape(url.split("v=")[-1][:11])}</a>'
                f' <a href="#" data-copy="{html_escape(url)}" class="muted-2" style="margin-left:4px;">copy</a>'
            )
        if v.get("publish_failed"):
            meta_bits.append(f'<span class="muted-2" title="{html_escape(str(v["publish_failed"])[:200])}">publish error</span>')
        if v.get("post_id"):
            meta_bits.append(f'<span class="mono muted-2">PB {html_escape(v["post_id"][:8])}…</span>')
        vu = v.get("variants_used") or {}
        if vu:
            tag = ", ".join(f"{k.replace('_index','')}#{vv}" for k, vv in vu.items())
            meta_bits.append(f'<span class="muted-2">variant: {html_escape(tag)}</span>')
        meta_line = (' · '.join(meta_bits))
        if meta_line:
            title_html += f'<div class="muted-2" style="margin-top:4px;">{meta_line}</div>'

        # Relative-time hint, JS keeps it fresh.
        rel_html = (
            f'<div class="muted-2" data-when="{slot_rel_ms}" style="margin-top:2px;"></div>'
            if slot_rel_ms else ""
        )

        if is_published:
            actions = '<span class="muted-2">—</span>'
        else:
            actions = (
                f'<form method="post" action="/channel/{name}/video/{orig_idx}/fire-now" style="display:inline;">'
                f'<button class="subtle tiny" type="submit" title="Cancel any pre-schedule and fire on the next watcher cycle (~30s)">Fire now</button></form> '
                f'<details style="display:inline-block; margin-left:6px;">'
                f'<summary>Reschedule</summary>'
                f'<div><form method="post" action="/channel/{name}/video/{orig_idx}/reschedule" class="row">'
                f'<input name="when" type="datetime-local" value="{slot_input}" required>'
                f'<button class="subtle tiny" type="submit">Save</button></form></div>'
                f'</details> '
                f'<form method="post" action="/channel/{name}/video/{orig_idx}/delete" '
                f'onsubmit="return confirm(\'Remove from queue? File in posted/ stays on disk; Post Bridge schedule is cancelled.\');" style="display:inline;">'
                f'<button class="danger tiny" type="submit">Delete</button></form>'
            )
        row_class = "next-up" if orig_idx == next_up_idx else ""
        rows_html += (
            f'<tr data-video-row data-title="{html_escape(raw_title)}"'
            f'{f" class=\"{row_class}\"" if row_class else ""}>'
            f'<td>{title_html}</td>'
            f'<td>{slot_str}{rel_html}</td>'
            f'<td>{status_pill}</td>'
            f'<td>{actions}</td>'
            '</tr>'
        )

    search_html = (
        '<input data-search-videos type="text" placeholder="Filter by title…" '
        'style="max-width:280px; margin-bottom:0;">'
    )
    table_html = (
        f'<table><thead><tr><th>Video</th><th>Slot</th><th>Status</th><th></th></tr></thead><tbody>{rows_html}</tbody></table>'
        if rows_html else
        '<div class="empty"><span class="icon">📭</span><h3>Nothing here yet</h3><div>Drop a video into the source folder — the watcher picks it up within 30s.</div></div>'
    )

    desc_count = len(yt.get("description_variants") or [])
    pinned_count = len(yt.get("pinned_message_variants") or [])
    title_var_count = len(yt.get("title_template_variants") or [])
    variants_summary = f'{desc_count} desc / {pinned_count} pinned / {title_var_count} title'

    pill_overdue = "ok" if stats['overdue'] == 0 else "warn"

    warmup_cfg = config.get("warmup") or {}
    if warmup_cfg.get("ramp"):
        today = datetime.now(tz).date()
        age = watcher_lib.channel_age_days(state, today, tz)
        per_day = watcher_lib.per_day_for(config, age)
        total = len(sched.get("times", []))
        # Visual: filled circles for active slots, hollow for inactive
        dots = ''.join(
            '<span class="slot-dot active"></span>' if i < per_day
            else '<span class="slot-dot inactive"></span>'
            for i in range(total)
        )
        warmup_html = (
            f'<div><div class="muted-2">Warmup · day {age}</div>'
            f'<div style="margin-top:4px; display:flex; gap:5px; align-items:center;">'
            f'{dots} <span class="muted-2" style="margin-left:6px;">{per_day}/{total}</span></div></div>'
        )
    else:
        warmup_html = ''

    # Buffer status: how many unfired videos have media uploaded vs target
    buffer_target = int(config.get("media_buffer_size", 0) or 0)
    buffer_uploaded = sum(
        1 for v in state.get("videos", [])
        if not v.get("fired") and not v.get("published_url")
        and (v.get("media") or {}).get("id")
    )
    buffer_remaining_local = sum(
        1 for v in state.get("videos", [])
        if not v.get("fired") and not v.get("published_url")
        and not (v.get("media") or {}).get("id")
    )
    buffer_html = ""
    if buffer_target > 0:
        pct = min(100, int((buffer_uploaded / buffer_target) * 100))
        full_cls = " full" if buffer_uploaded >= buffer_target else ""
        buffer_html = (
            f'<div><div class="muted-2">PB media buffer</div>'
            f'<div><strong>{buffer_uploaded}/{buffer_target}</strong>'
            f'{f" · {buffer_remaining_local} local" if buffer_remaining_local else ""}</div>'
            f'<div class="progress-bar" style="margin-top:6px; width:160px;">'
            f'<div class="fill{full_cls}" style="width:{pct}%;"></div></div></div>'
        )
    else:
        buffer_html = (
            f'<div><div class="muted-2">PB media buffer</div>'
            f'<div><strong>{buffer_uploaded}</strong>'
            f'{f" · {buffer_remaining_local} local" if buffer_remaining_local else ""}</div></div>'
        )

    body = f"""
    <a href="/" class="muted">← All channels</a>
    <h1 style="margin-top:10px;">{html_escape(name)}</h1>
    <p class="subhead">
      <span class="muted">Post Bridge:</span> <strong>{html_escape(config.get('social_account') or config.get('integration_name','?'))}</strong>
      &nbsp;·&nbsp; <span class="muted">Source:</span> <code>{html_escape(config.get('source_folder') or '(channel inbox/)')}</code>
    </p>

    <div class="row">
      <span class="stat"><strong>{stats['queued']}</strong> queued</span>
      <span class="stat"><strong>{stats['fired']}</strong> fired</span>
      <span class="pill {pill_overdue}"><span class="dot"></span>{stats['overdue']} overdue</span>
      <span style="margin-left:14px;" class="muted">Next: <strong>{stats['next_slot'] or '—'}</strong></span>
      <span class="nav-spacer"></span>
      <a class="btn ghost" href="/channel/{name}/performance">📊 Performance</a>
      <a class="btn ghost" href="/channel/{name}/calendar">📅 Calendar</a>
      <a class="btn ghost" href="/channel/{name}/variants">Variants ({variants_summary})</a>
    </div>

    <div class="row" style="margin-top:14px; gap:8px; flex-wrap:wrap;">
      <form method="post" action="/channel/{name}/open/source" style="display:inline;"><button class="subtle tiny" type="submit" title="Open the folder where the watcher looks for new videos">📁 Source folder</button></form>
      <form method="post" action="/channel/{name}/open/posted" style="display:inline;"><button class="subtle tiny" type="submit" title="Files that have been uploaded to Post Bridge but not yet published">📁 Posted</button></form>
      <form method="post" action="/channel/{name}/open/archive" style="display:inline;"><button class="subtle tiny" type="submit" title="Files moved here after YouTube confirms publish (when cleanup_after_publish is 'archive')">📁 Archive</button></form>
      <form method="post" action="/channel/{name}/open/channel" style="display:inline;"><button class="subtle tiny" type="submit" title="The channel's project folder (config.yaml lives here)">📁 Channel folder</button></form>
    </div>

    <h2><span class="h2-icon">🗓</span>Schedule</h2>
    <div class="card">
      <div class="row" style="gap:36px; flex-wrap:wrap;">
        <div><div class="muted-2">Times</div><div><strong>{', '.join(sched.get('times', [])) or '(none)'}</strong></div></div>
        <div><div class="muted-2">Days</div><div><strong>{' '.join(sched.get('days', [])) or '(all)'}</strong></div></div>
        <div><div class="muted-2">Timezone</div><div><strong>{sched.get('timezone','UTC')}</strong></div></div>
        <div><div class="muted-2">Jitter</div><div><strong>{('±' + str(sched.get('jitter_minutes', 0)) + ' min') if sched.get('jitter_minutes', 0) else 'off'}</strong></div></div>
        <div><div class="muted-2">Catch-up</div><div><strong>{config.get('catch_up_window_minutes', 30)} min</strong></div></div>
        <div><div class="muted-2">Pre-schedule</div><div><strong>{config.get('prescheduling_window_hours', 8)}h ahead</strong></div></div>
        {buffer_html}
        {warmup_html}
      </div>
      <div style="margin-top:18px;"><div class="muted-2" style="margin-bottom:4px;">Today's plan</div>{today_strip_html(state, config)}</div>
    </div>

    <h2 style="margin-top:28px;"><span class="h2-icon">⚡</span>Quick actions</h2>
    <div class="card">
      <form method="post" action="/channel/{name}/bonus-today" class="row" style="align-items:flex-end; gap:14px;">
        <div style="flex:1; min-width:280px;">
          <label style="display:block; font-size:12px; color:var(--text-2); margin-bottom:6px; font-weight:500;">Add bonus posts today</label>
          <input name="times" type="text" placeholder="21:00, 22:30, 23:45" required>
          <div class="muted-2" style="margin-top:4px;">Comma-separated times today (24-hour). Pulls the next queued videos forward to those times.</div>
        </div>
        <button type="submit" title="Pull queued videos forward to extra slots today">Pull videos to today</button>
      </form>
      <div class="divider"></div>
      <div class="row" style="gap:8px; flex-wrap:wrap;">
        <form method="post" action="/channel/{name}/fill-today" style="display:inline;">
          <button class="subtle" type="submit"
                  title="For each empty slot today (per your schedule), pull the latest-scheduled queued video forward to fill it.">↑ Fill today's empty slots</button>
        </form>
        <form method="post" action="/channel/{name}/reschedule-all"
              onsubmit="return confirm('Repack ALL queued videos onto the current schedule, starting from now? Useful after editing times/days in config. Already-published videos stay put.');"
              style="display:inline;">
          <button class="subtle" type="submit"
                  title="Re-distribute every queued video evenly across the current schedule, starting from now. Use after editing the times-per-day or days settings.">↻ Reschedule all queued</button>
        </form>
      </div>
    </div>

    <div class="row spread" style="margin-top:28px; align-items:baseline;">
      <h2 style="margin:0;">Videos ({len(indexed)})</h2>
      <div class="row">
        {search_html}
        <a class="btn tiny ghost{(' active' if filt=='all' else '')}" href="?filter=all">All</a>
        <a class="btn tiny ghost{(' active' if filt=='queued' else '')}" href="?filter=queued">Queued</a>
        <a class="btn tiny ghost{(' active' if filt=='fired' else '')}" href="?filter=fired">Fired</a>
      </div>
    </div>
    {table_html}

    <details class="advanced" style="margin-top:32px;">
      <summary><span class="muted">Advanced — </span>Edit raw config.yaml</summary>
      <div>
        <form method="post" action="/channel/{name}/config" style="margin-top:12px;">
          <textarea name="config_yaml" rows="18">{html_escape(yaml.safe_dump(config, sort_keys=False, default_flow_style=False, allow_unicode=True))}</textarea>
          <div class="row" style="margin-top:10px;">
            <button type="submit">Save config</button>
            <span class="nav-spacer"></span>
            <form method="post" action="/channel/{name}/delete" onsubmit="return confirm('Archive this channel folder? Videos in posted/ stay on disk; the folder moves to .deleted_channels/.');" style="margin:0;">
              <button class="danger" type="submit">Delete channel folder</button>
            </form>
          </div>
        </form>
      </div>
    </details>
    """
    return render(name, body, active_channel=name)


@app.route("/channel/<name>/config", methods=["POST"])
def update_config(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, _, _ = load_channel(name)
    raw = request.form.get("config_yaml", "")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return render("Error", f'<h1>Invalid YAML</h1><pre>{html_escape(e)}</pre><a href="/channel/{name}">Back</a>'), 400
    if not isinstance(parsed, dict):
        return render("Error", f'<h1>Config must be a YAML object</h1><a href="/channel/{name}">Back</a>'), 400
    (channel_dir / "config.yaml").write_text(raw)
    return redirect(url_for("channel_detail", name=name))


@app.route("/channel/<name>/delete", methods=["POST"])
def delete_channel(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, _, _ = load_channel(name)
    archive = CHANNELS_DIR.parent / ".deleted_channels" / f"{name}_{int(datetime.now().timestamp())}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    channel_dir.rename(archive)
    return redirect(url_for("dashboard"))


# -------------------- variants page --------------------


@app.route("/channel/<name>/variants")
def variants_page(name: str):
    if not safe_name(name):
        abort(400)
    _, config, _ = load_channel(name)
    yt = config.get("youtube", {})

    current = {
        "description_variants": yt.get("description_variants") or [],
        "pinned_message_variants": yt.get("pinned_message_variants") or [],
        "title_template_variants": yt.get("title_template_variants") or [],
    }
    md_now = variants_lib.serialize_markdown(current)
    ai_prompt = variants_lib.make_ai_prompt(config, name)

    sections_html = ""
    for key, items in current.items():
        display = variants_lib.DISPLAY_NAMES[key]
        if not items:
            sections_html += f'<div class="card"><h3>{display}</h3><div class="muted-2" style="margin-top:6px;">— no variants — using single-value fallback from config</div></div>'
            continue
        bullet_list = "".join(
            f'<li style="margin:6px 0;">{html_escape(v[:200])}{"…" if len(v) > 200 else ""}</li>'
            for v in items
        )
        sections_html += f'<div class="card"><h3>{display} <span class="muted-2">({len(items)})</span></h3><ul style="padding-left:20px; margin-top:8px;">{bullet_list}</ul></div>'

    body = f"""
    <a href="/channel/{name}" class="muted">← {html_escape(name)}</a>
    <h1 style="margin-top:10px;">Variants</h1>
    <p class="subhead">When variants are present for a field, the watcher picks one at random per video. Single-value <code>description</code>/<code>pinned_message</code> fields are used as fallback when no variants exist.</p>

    <h2>Current variants</h2>
    <div class="grid">{sections_html}</div>

    <h2>Generate more with AI</h2>
    <div class="card">
      <p class="muted" style="margin-top:0;">Copy the prompt below into ChatGPT or Claude. The model will return a markdown file you paste into the import box further down.</p>
      <details>
        <summary>Show AI prompt</summary>
        <div>
          <textarea readonly rows="22" onclick="this.select()">{html_escape(ai_prompt)}</textarea>
          <div class="row" style="margin-top:8px;">
            <button class="subtle" type="button" onclick="navigator.clipboard.writeText(this.parentElement.previousElementSibling.value); this.textContent='Copied ✓'; setTimeout(()=>this.textContent='Copy to clipboard', 1500);">Copy to clipboard</button>
          </div>
        </div>
      </details>
    </div>

    <h2>Import variants</h2>
    <div class="card">
      <p class="muted" style="margin-top:0;">Paste the markdown the AI generated. The format the AI uses (sections separated by <code># Heading</code>, variants separated by <code>---</code>) is what we parse. Existing variants are <strong>replaced</strong> per section.</p>
      <form method="post" action="/channel/{name}/variants/import">
        <textarea name="markdown" rows="14" placeholder="# Descriptions&#10;---&#10;Variant 1...&#10;---&#10;Variant 2..."></textarea>
        <div class="row" style="margin-top:8px;">
          <button type="submit">Import & save to config.yaml</button>
        </div>
      </form>
    </div>

    <h2>Export current variants</h2>
    <div class="card">
      <p class="muted" style="margin-top:0;">Download the current variants as a markdown file (handy for sharing with the AI for a refresh round).</p>
      <details>
        <summary>Show current variants as markdown</summary>
        <div>
          <textarea readonly rows="14" onclick="this.select()">{html_escape(md_now)}</textarea>
        </div>
      </details>
      <div class="row" style="margin-top:10px;">
        <a class="btn subtle" href="/channel/{name}/variants/export">Download .md</a>
      </div>
    </div>
    """
    return render(f"{name} — Variants", body, active_channel=name)


@app.route("/channel/<name>/variants/import", methods=["POST"])
def variants_import(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, config, _ = load_channel(name)
    md_text = request.form.get("markdown", "")
    parsed = variants_lib.parse_markdown(md_text)
    if not parsed:
        return render(
            "Error",
            f'<h1>Nothing recognized</h1><p>I couldn\'t find any of the supported section headings (Descriptions / Pinned Messages / Titles). '
            f'<a href="/channel/{name}/variants">Back</a></p>',
        ), 400
    new_config = variants_lib.merge_into_config(config, parsed)
    write_config(channel_dir, new_config)
    return redirect(url_for("variants_page", name=name))


@app.route("/channel/<name>/variants/export")
def variants_export(name: str):
    if not safe_name(name):
        abort(400)
    _, config, _ = load_channel(name)
    yt = config.get("youtube", {})
    current = {
        "description_variants": yt.get("description_variants") or [],
        "pinned_message_variants": yt.get("pinned_message_variants") or [],
        "title_template_variants": yt.get("title_template_variants") or [],
    }
    md = variants_lib.serialize_markdown(current)
    return Response(
        md,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{name}-variants.md"'},
    )


# -------------------- per-video actions --------------------


@app.route("/channel/<name>/video/<int:idx>/reschedule", methods=["POST"])
def reschedule_video(name: str, idx: int):
    if not safe_name(name):
        abort(400)
    channel_dir, _, state = load_channel(name)
    when = request.form.get("when", "").strip()
    if not when or idx >= len(state.get("videos", [])):
        abort(400)
    s = watcher_lib.load_settings()
    target = state["videos"][idx]
    old_slot = target.get("scheduled_for")
    watcher_lib.unschedule_video(s, target)
    target["scheduled_for"] = when

    flash_msg = "Rescheduled"
    if request.form.get("backfill", "1") == "1" and old_slot:
        try:
            new_dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
            old_dt = datetime.fromisoformat(old_slot.replace("Z", "+00:00"))
            moved_earlier = new_dt < old_dt
        except Exception:
            moved_earlier = False
        if moved_earlier:
            moved = watcher_lib.backfill_vacated_slot(state, old_slot, idx, s)
            if moved:
                flash_msg = f"Rescheduled · backfilled with '{moved.get('title','?')[:40]}'"
    write_state(channel_dir, state)
    return redirect(url_for("channel_detail", name=name, flash=flash_msg))


@app.route("/channel/<name>/video/<int:idx>/delete", methods=["POST"])
def delete_video(name: str, idx: int):
    """Remove a video from the queue. Cancels Post Bridge pre-scheduled post
    if any. The source file in posted/ stays on disk."""
    if not safe_name(name):
        abort(400)
    channel_dir, _, state = load_channel(name)
    if idx < len(state.get("videos", [])):
        s = watcher_lib.load_settings()
        watcher_lib.unschedule_video(s, state["videos"][idx])
        state["videos"].pop(idx)
        write_state(channel_dir, state)
    return redirect(url_for("channel_detail", name=name, flash="Removed from queue"))


@app.route("/channel/<name>/video/<int:idx>/fire-now", methods=["POST"])
def fire_now(name: str, idx: int):
    if not safe_name(name):
        abort(400)
    channel_dir, _, state = load_channel(name)
    if idx >= len(state.get("videos", [])):
        abort(400)
    s = watcher_lib.load_settings()
    target = state["videos"][idx]
    original_slot = target.get("scheduled_for")
    watcher_lib.unschedule_video(s, target)
    target["scheduled_for"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    flash_msg = "Will fire on next watcher cycle"
    # Backfill the vacated slot with the latest-scheduled video so daily
    # cadence stays the same. Default ON; pass backfill=0 to leave a gap.
    if request.form.get("backfill", "1") == "1" and original_slot:
        moved = watcher_lib.backfill_vacated_slot(state, original_slot, idx, s)
        if moved:
            flash_msg = f"Firing now · backfilled with '{moved.get('title','?')[:40]}'"
    write_state(channel_dir, state)
    return redirect(url_for("channel_detail", name=name, flash=flash_msg))


@app.route("/channel/<name>/performance")
def channel_performance(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, config, state = load_channel(name)
    tz = ZoneInfo(config.get("schedule", {}).get("timezone", "UTC"))

    published = [v for v in state.get("videos", []) if v.get("published_url")]
    with_stats = [v for v in published if v.get("analytics", {}).get("view_count") is not None]

    total_views = sum(v.get("analytics", {}).get("view_count", 0) for v in with_stats)
    total_likes = sum(v.get("analytics", {}).get("like_count", 0) for v in with_stats)
    total_comments = sum(v.get("analytics", {}).get("comment_count", 0) for v in with_stats)
    avg_views = (total_views / len(with_stats)) if with_stats else 0

    last_sync = state.get("last_analytics_sync_at")
    last_sync_ms = 0
    if last_sync:
        try:
            last_sync_ms = int(datetime.fromisoformat(last_sync.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            pass

    sort_by = request.args.get("sort", "views")
    metric_key = {
        "views": "view_count", "likes": "like_count",
        "comments": "comment_count", "shares": "share_count",
    }.get(sort_by, "view_count")

    sorted_posts = sorted(
        with_stats,
        key=lambda v: v.get("analytics", {}).get(metric_key, 0),
        reverse=True,
    )

    rows_top = ""
    for rank, v in enumerate(sorted_posts[:25], 1):
        a = v.get("analytics", {})
        try:
            fired_dt = datetime.fromisoformat((v.get("fired_at") or v["scheduled_for"]).replace("Z", "+00:00")).astimezone(tz)
            fired_str = fired_dt.strftime("%b %-d · %-I:%M %p")
        except Exception:
            fired_str = "?"
        url = v.get("published_url") or "#"
        rows_top += (
            f'<tr><td class="muted-2">{rank}</td>'
            f'<td><a href="{html_escape(url)}" target="_blank" rel="noopener">{html_escape(v.get("title", v["filename"])[:80])}</a></td>'
            f'<td>{a.get("view_count", 0):,}</td>'
            f'<td>{a.get("like_count", 0):,}</td>'
            f'<td>{a.get("comment_count", 0):,}</td>'
            f'<td>{a.get("share_count", 0):,}</td>'
            f'<td class="muted-2">{fired_str}</td></tr>'
        )

    # ---- Variant A/B aggregation ----
    yt = config.get("youtube", {})
    desc_variants = yt.get("description_variants") or []
    pinned_variants = yt.get("pinned_message_variants") or []

    def variant_breakdown(key: str, choices: list[str]) -> list[dict]:
        groups: dict[int, list[int]] = {}
        for v in with_stats:
            idx = (v.get("variants_used") or {}).get(key)
            if idx is None:
                continue
            groups.setdefault(int(idx), []).append(v.get("analytics", {}).get("view_count", 0))
        out = []
        for idx, views_list in groups.items():
            n = len(views_list)
            avg = sum(views_list) / n
            label = (choices[idx][:80] + "…") if 0 <= idx < len(choices) and len(choices[idx]) > 80 else (choices[idx] if 0 <= idx < len(choices) else f"index {idx}")
            out.append({"idx": idx, "label": label, "n": n, "avg": avg, "total": sum(views_list)})
        out.sort(key=lambda r: r["avg"], reverse=True)
        return out

    desc_rows = variant_breakdown("description_variant_index", desc_variants)
    pinned_rows = variant_breakdown("pinned_variant_index", pinned_variants)

    def render_variant_table(name: str, rows: list[dict]) -> str:
        if not rows:
            return f'<div class="muted-2">No A/B data yet for {name}. Need at least 2 posts using different variants with view stats.</div>'
        max_avg = max((r["avg"] for r in rows), default=1) or 1
        out = f'<table><thead><tr><th>Rank</th><th>{name}</th><th>n</th><th>Avg views</th><th>Total</th><th></th></tr></thead><tbody>'
        for rank, r in enumerate(rows, 1):
            bar_pct = (r["avg"] / max_avg) * 100
            badge = (
                '<span class="pill ok" style="margin-left:6px;">★ winner</span>' if rank == 1 and len(rows) > 1
                else ('<span class="pill warn" style="margin-left:6px;">underperformer</span>' if rank == len(rows) and len(rows) > 1 else '')
            )
            out += (
                f'<tr><td class="muted-2">#{rank}</td>'
                f'<td><span class="muted-2 mono">#{r["idx"]}</span> {html_escape(r["label"])}{badge}</td>'
                f'<td>{r["n"]}</td><td><strong>{r["avg"]:,.0f}</strong></td><td>{r["total"]:,}</td>'
                f'<td><div class="progress-bar" style="width:120px;"><div class="fill" style="width:{bar_pct:.0f}%;"></div></div></td>'
                '</tr>'
            )
        out += '</tbody></table>'
        return out

    # ---- Best time aggregation ----
    hour_buckets: dict[int, list[int]] = {h: [] for h in range(24)}
    for v in with_stats:
        try:
            dt = datetime.fromisoformat((v.get("fired_at") or v["scheduled_for"]).replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        hour_buckets[dt.hour].append(v.get("analytics", {}).get("view_count", 0))
    hour_avgs = {h: (sum(v) / len(v) if v else 0) for h, v in hour_buckets.items()}
    max_hour_avg = max(hour_avgs.values()) or 1

    hours_html = ""
    for h in range(24):
        v = hour_avgs[h]
        n = len(hour_buckets[h])
        if n == 0:
            bar_class, height = "", 4
        else:
            bar_class = "has"
            height = max(8, int((v / max_hour_avg) * 100))
        hours_html += (
            f'<div class="hour-col" title="{h:02d}:00 — {n} posts, avg {v:,.0f} views">'
            f'<div class="bar {bar_class}" style="height:{height}%;"></div>'
            f'<div class="hour-label">{h:02d}</div></div>'
        )
    best_hours = sorted(hour_avgs.items(), key=lambda kv: kv[1], reverse=True)
    best_hours_with_data = [(h, avg) for h, avg in best_hours if len(hour_buckets[h]) > 0][:3]

    best_hours_summary = (
        " · ".join(f"<strong>{h:02d}:00</strong> ({avg:,.0f})" for h, avg in best_hours_with_data)
        if best_hours_with_data else "Need more data"
    )

    perf_styles = """
    <style>
      .hour-grid { display: flex; align-items: flex-end; gap: 4px; height: 110px; padding: 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
      .hour-col { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
      .hour-col .bar { width: 100%; background: var(--bg-3); border-radius: 2px 2px 0 0; min-height: 4px; transition: background 0.2s; }
      .hour-col .bar.has { background: var(--accent); }
      .hour-col .hour-label { font-size: 9px; color: var(--text-3); margin-top: 4px; font-family: ui-monospace, monospace; }
    </style>
    """

    body = f"""
    {perf_styles}
    <a href="/channel/{name}" class="muted">← {html_escape(name)}</a>
    <div class="row spread" style="margin-top:10px; align-items:center;">
      <h1 style="margin:0;">Performance</h1>
      <form method="post" action="/channel/{name}/performance/sync" style="margin:0;">
        <button class="subtle" type="submit" title="Pull fresh stats from Post Bridge (rate-limited on their side)">↻ Sync now</button>
      </form>
    </div>
    <p class="subhead">{len(with_stats)} of {len(published)} published videos have analytics{f' · last sync <span data-when="{last_sync_ms}">…</span>' if last_sync_ms else ''}</p>

    <div class="hero-grid">
      <div class="hero-card"><div class="label">Total views</div><div class="value">{total_views:,}</div><div class="sub">across {len(with_stats)} posts</div></div>
      <div class="hero-card"><div class="label">Total likes</div><div class="value">{total_likes:,}</div><div class="sub">{(total_likes/total_views*100) if total_views else 0:.1f}% engagement</div></div>
      <div class="hero-card"><div class="label">Comments</div><div class="value">{total_comments:,}</div><div class="sub">{(total_comments/total_views*100) if total_views else 0:.2f}% rate</div></div>
      <div class="hero-card"><div class="label">Avg views / post</div><div class="value">{avg_views:,.0f}</div><div class="sub">across all published</div></div>
    </div>

    <h2><span class="h2-icon">📈</span>Top performers</h2>
    <div class="row" style="margin-bottom:8px;">
      <a class="btn tiny ghost{(' active' if sort_by=='views' else '')}" href="?sort=views">By views</a>
      <a class="btn tiny ghost{(' active' if sort_by=='likes' else '')}" href="?sort=likes">By likes</a>
      <a class="btn tiny ghost{(' active' if sort_by=='comments' else '')}" href="?sort=comments">By comments</a>
      <a class="btn tiny ghost{(' active' if sort_by=='shares' else '')}" href="?sort=shares">By shares</a>
    </div>
    {f'<table><thead><tr><th>#</th><th>Title</th><th>Views</th><th>Likes</th><th>Comments</th><th>Shares</th><th>Posted</th></tr></thead><tbody>{rows_top}</tbody></table>' if rows_top else '<div class="empty">No analytics yet. Click <strong>Sync now</strong> above (Post Bridge usually takes a few hours after publish to surface the first numbers).</div>'}

    <h2 style="margin-top:32px;"><span class="h2-icon">🧪</span>A/B test — Description variants</h2>
    {render_variant_table('Description', desc_rows)}

    <h2 style="margin-top:24px;"><span class="h2-icon">📌</span>A/B test — Pinned message variants</h2>
    {render_variant_table('Pinned message', pinned_rows)}

    <h2 style="margin-top:32px;"><span class="h2-icon">⏰</span>Best posting times</h2>
    <p class="subhead">Top hours by average views: {best_hours_summary}</p>
    <div class="hour-grid">{hours_html}</div>
    """
    return render(f"{name} — Performance", body, active_channel=name)


@app.route("/channel/<name>/performance/sync", methods=["POST"])
def performance_sync(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, _, state = load_channel(name)
    s = watcher_lib.load_settings()
    n = watcher_lib.refresh_channel_analytics(s, state, name, do_sync=True)
    state["last_analytics_sync_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state["last_analytics_full_sync_at"] = state["last_analytics_sync_at"]
    write_state(channel_dir, state)
    msg = f"Synced · {n} post{'s' if n != 1 else ''} updated"
    return redirect(url_for("channel_performance", name=name, flash=msg))


@app.route("/channel/<name>/calendar")
def channel_calendar(name: str):
    if not safe_name(name):
        abort(400)
    _, config, state = load_channel(name)
    sched = config.get("schedule", {})
    tz = ZoneInfo(sched.get("timezone", "UTC"))
    days_ahead = int(request.args.get("days", "14"))
    today = datetime.now(tz).date()

    # Bucket every video by local date
    buckets: dict = {}
    for v in state.get("videos", []):
        try:
            slot = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00")).astimezone(tz)
        except (KeyError, ValueError):
            continue
        buckets.setdefault(slot.date(), []).append((slot, v))

    days_html = ""
    for offset in range(days_ahead):
        day = today + timedelta(days=offset)
        day_videos = sorted(buckets.get(day, []), key=lambda iv: iv[0])
        date_str = day.strftime("%a · %b %-d")
        if offset == 0:
            date_str = f"Today · {day.strftime('%b %-d')}"
        elif offset == 1:
            date_str = f"Tomorrow · {day.strftime('%b %-d')}"

        rows_html = ""
        if not day_videos:
            rows_html = '<div class="cal-empty">— no posts scheduled —</div>'
        else:
            for slot, v in day_videos:
                if v.get("published_url"):
                    pill = f'<a class="pill ok" href="{html_escape(v["published_url"])}" target="_blank" rel="noopener" style="text-decoration:none;"><span class="dot"></span>posted ▶</a>'
                elif v.get("publish_failed"):
                    pill = '<span class="pill err"><span class="dot"></span>failed</span>'
                elif v.get("prescheduled"):
                    pill = '<span class="pill ok"><span class="dot"></span>queued in PB</span>'
                elif v.get("fired"):
                    pill = '<span class="pill ok"><span class="dot"></span>sent</span>'
                else:
                    pill = '<span class="pill warn"><span class="dot"></span>queued</span>'
                rows_html += f"""
                <div class="cal-row">
                  <div class="cal-time">{slot.strftime('%-I:%M %p')}</div>
                  <div class="cal-title">{html_escape(v.get('title', v.get('filename','?')))}</div>
                  <div>{pill}</div>
                </div>
                """
        days_html += f"""
        <div class="cal-day">
          <div class="cal-day-head">{date_str}</div>
          {rows_html}
        </div>
        """

    cal_css = """
    <style>
      .cal-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
      .cal-day { background: var(--bg-2); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
      .cal-day-head { font-weight: 600; font-size: 13px; margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid var(--border); color: var(--text); }
      .cal-row { display: grid; grid-template-columns: 64px 1fr auto; gap: 10px; align-items: center; padding: 6px 0; font-size: 13px; }
      .cal-row + .cal-row { border-top: 1px solid var(--border); }
      .cal-time { color: var(--text-2); font-family: ui-monospace, monospace; font-size: 12px; }
      .cal-title { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .cal-empty { color: var(--text-3); font-size: 12px; padding: 6px 0; }
    </style>
    """
    body = f"""
    {cal_css}
    <a href="/channel/{name}" class="muted">← {html_escape(name)}</a>
    <div class="row spread" style="margin-top:10px;">
      <h1 style="margin:0;">Calendar</h1>
      <div class="row">
        <a class="btn tiny ghost{(' active' if days_ahead==7 else '')}" href="?days=7">7 days</a>
        <a class="btn tiny ghost{(' active' if days_ahead==14 else '')}" href="?days=14">14 days</a>
        <a class="btn tiny ghost{(' active' if days_ahead==30 else '')}" href="?days=30">30 days</a>
      </div>
    </div>
    <p class="subhead">Times shown in <strong>{sched.get('timezone','UTC')}</strong>. <strong>Queued in PB</strong> = already pre-scheduled in Post Bridge — fires from their cloud even if your Mac is off.</p>
    <div class="cal-grid">{days_html}</div>
    """
    return render(f"{name} — Calendar", body, active_channel=name)


@app.route("/channel/<name>/reschedule-all", methods=["POST"])
def reschedule_all(name: str):
    """Repack every queued/pre-scheduled video onto the current config's schedule,
    starting from now. Cancels Post Bridge pre-scheduled posts so the watcher
    re-submits them at the new times."""
    if not safe_name(name):
        abort(400)
    channel_dir, config, state = load_channel(name)
    s = watcher_lib.load_settings()
    n = watcher_lib.reschedule_all_queued(state, config, s=s)
    if n:
        write_state(channel_dir, state)
    return redirect(url_for("channel_detail", name=name, flash=f"Rescheduled {n} videos"))


@app.route("/channel/<name>/fill-today", methods=["POST"])
def fill_today(name: str):
    """Pull queued videos forward to fill any empty slots today (per the
    config's configured times). Useful when fire-now or deletes left holes
    before the auto-backfill feature was added."""
    if not safe_name(name):
        abort(400)
    channel_dir, config, state = load_channel(name)
    s = watcher_lib.load_settings()
    n = watcher_lib.fill_todays_remaining_slots(state, config, s)
    if n:
        write_state(channel_dir, state)
    msg = (
        f"Filled {n} empty slot{'s' if n != 1 else ''} today" if n
        else "Today's slots are already full (or all past)"
    )
    return redirect(url_for("channel_detail", name=name, flash=msg))


@app.route("/channel/<name>/bonus-today", methods=["POST"])
def bonus_today(name: str):
    """Pull the earliest queued videos forward to extra slots today.
    Form fields: `times` (comma-separated HH:MM)."""
    if not safe_name(name):
        abort(400)
    channel_dir, config, state = load_channel(name)
    raw = request.form.get("times", "")
    times = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]
    if not times:
        return redirect(url_for("channel_detail", name=name, flash="No times provided"))
    s = watcher_lib.load_settings()
    n = watcher_lib.add_bonus_slots_today(state, config, times, s=s)
    if n:
        write_state(channel_dir, state)
    return redirect(url_for(
        "channel_detail", name=name,
        flash=f"Pulled {n} video{'s' if n != 1 else ''} to today" if n else "No videos to pull forward",
    ))


# -------------------- add channel --------------------


@app.route("/add")
def add_channel():
    integrations = fetch_integrations()
    options_html = "".join(
        f'<option value="{html_escape(i["username"])}" data-platform="{html_escape(i["platform"])}">{html_escape(i["username"])} ({html_escape(i["platform"])})</option>'
        for i in integrations
    )
    if not options_html:
        options_html = '<option value="">⚠️ No accounts found — connect a channel in Post Bridge first</option>'

    existing = [c.name for c in channel_dirs()]
    template_options = ''.join(f'<option value="{html_escape(c)}">{html_escape(c)}</option>' for c in existing)

    days_checkboxes = "".join(
        f'<label class="chk"><input type="checkbox" name="days" value="{d}" checked>{d}</label>'
        for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    )

    body = f"""
    <h1>Add a channel</h1>
    <p class="subhead">Creates a new folder under <code>channels/</code> with a fresh <code>config.yaml</code>. Pre-fills as much as possible.</p>

    <form method="post" action="/add" class="card" style="max-width:760px;" id="addform">

      {f'<div class="field"><label>Copy settings from existing channel <span class="muted-2">(optional — uses their schedule / description / pinned message)</span></label><select id="tpl-channel"><option value="">— start fresh —</option>{template_options}</select></div>' if template_options else ''}

      <div class="field">
        <label>Post Bridge account</label>
        <select name="social_account" id="acct-select" required>
          <option value="">— pick one —</option>
          {options_html}
        </select>
      </div>

      <div class="field">
        <label>Folder name <span class="muted-2">(auto-filled from account; lowercase letters/digits/underscores)</span></label>
        <input name="name" id="folder-name" type="text" placeholder="my_new_channel" required pattern="[a-z0-9_]{{1,40}}">
      </div>

      <div class="field">
        <label>Source folder <span class="muted-2">(absolute path. Empty = use this channel's <code>inbox/</code>. Folder is auto-created if it doesn't exist.)</span></label>
        <input name="source_folder" id="source-folder" type="text" placeholder="/Users/owen/myfactory/output">
      </div>

      <div class="field">
        <label>Schedule presets</label>
        <div class="row" style="gap:6px;">
          <button class="subtle tiny" type="button" data-times="09:00, 18:00">2 / day</button>
          <button class="subtle tiny" type="button" data-times="08:00, 13:00, 18:00">3 / day</button>
          <button class="subtle tiny" type="button" data-times="08:00, 11:00, 14:00, 17:00, 20:00">5 / day</button>
          <button class="subtle tiny" type="button" data-times="08:00, 10:30, 13:00, 15:30, 18:00, 20:30">6 / day</button>
          <button class="subtle tiny" type="button" data-times="07:00, 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00">8 / day</button>
        </div>
      </div>
      <div class="field">
        <label>Times <span class="muted-2">(24-hour, comma-separated, channel timezone)</span></label>
        <input name="times" id="sched-times" type="text" value="09:00, 18:00">
      </div>

      <div class="field">
        <label>Days of week</label>
        <div class="row" style="gap:8px; flex-wrap:wrap;">{days_checkboxes}</div>
        <div class="row" style="gap:6px; margin-top:6px;">
          <button class="subtle tiny" type="button" data-days="Mon Tue Wed Thu Fri Sat Sun">Every day</button>
          <button class="subtle tiny" type="button" data-days="Mon Tue Wed Thu Fri">Weekdays</button>
          <button class="subtle tiny" type="button" data-days="Sat Sun">Weekends</button>
        </div>
      </div>

      <div class="field">
        <label>Pre-uploaded buffer <span class="muted-2">(how many of the next-up videos to upload to Post Bridge in advance — the rest stay local on your Mac until they get close to firing)</span></label>
        <input name="media_buffer_size" type="number" min="0" max="200" value="10" style="max-width:140px;">
      </div>

      <div class="field">
        <label>Time jitter <span class="muted-2">(randomize each upload ±N min around its base time — looks more human, less robotic to YouTube)</span></label>
        <div class="row" style="gap:6px;">
          <button class="subtle tiny" type="button" data-jitter="0">Off</button>
          <button class="subtle tiny" type="button" data-jitter="15">±15 min</button>
          <button class="subtle tiny" type="button" data-jitter="30">±30 min</button>
          <button class="subtle tiny" type="button" data-jitter="60">±60 min</button>
        </div>
        <input name="jitter_minutes" id="jitter" type="number" min="0" max="720" value="30" style="margin-top:6px; max-width:120px;">
      </div>

      <div class="field">
        <label>Warmup ramp <span class="muted-2">(new channels: start slow, ramp to full schedule. Looks like organic growth.)</span></label>
        <div class="row" style="gap:6px;">
          <button class="subtle tiny" type="button" data-warmup="off">Off</button>
          <button class="subtle tiny" type="button" data-warmup="slow">Slow (14 days)</button>
          <button class="subtle tiny" type="button" data-warmup="medium">Medium (7 days)</button>
          <button class="subtle tiny" type="button" data-warmup="aggressive">Aggressive (3 days)</button>
        </div>
        <input name="warmup_preset" id="warmup-preset" type="hidden" value="off">
        <div id="warmup-summary" class="muted-2" style="margin-top:6px;">No warmup — full schedule from day 1.</div>
      </div>

      <div class="field">
        <label>Timezone <span class="muted-2">(IANA format)</span></label>
        <input name="timezone" id="tz" type="text" value="America/New_York" list="tz-list">
        <datalist id="tz-list">
          <option value="America/New_York">
          <option value="America/Chicago">
          <option value="America/Denver">
          <option value="America/Los_Angeles">
          <option value="Europe/London">
          <option value="Europe/Berlin">
          <option value="Asia/Tokyo">
          <option value="Australia/Sydney">
          <option value="UTC">
        </datalist>
      </div>

      <div class="field">
        <label>YouTube title template <span class="muted-2">(<code>{{smart_title}}</code> = filename-derived title)</span></label>
        <input name="title_template" id="title-tpl" type="text" value="{{smart_title}}">
      </div>

      <div class="field">
        <label>Pinned message <span class="muted-2">(first line of every description, visible above the fold — substitute for a pinned comment)</span></label>
        <input name="pinned_message" id="pinned" type="text" placeholder="👇 Earn money like this 👇">
      </div>

      <div class="field">
        <label>Description</label>
        <textarea name="description" id="desc" placeholder="Subscribe for more!"></textarea>
      </div>

      <div class="row" style="margin-top:6px;">
        <button type="submit">Create channel</button>
      </div>
    </form>

    <style>
      .chk {{ display:inline-flex; align-items:center; gap:6px; padding:6px 10px; background:var(--bg); border:1px solid var(--border); border-radius:6px; cursor:pointer; user-select:none; font-size:13px; }}
      .chk:has(input:checked) {{ background:rgba(122,162,255,0.10); border-color:rgba(122,162,255,0.40); color:var(--accent); }}
      .chk input {{ width:auto; margin:0; }}
    </style>

    <script>
      // ------- Auto-fill folder name from account username -------
      function slug(s) {{ return s.toLowerCase().replace(/[^a-z0-9_]+/g,'_').replace(/^_+|_+$/g,'').slice(0,40) || 'my_channel'; }}
      const acct = document.getElementById('acct-select');
      const folderName = document.getElementById('folder-name');
      let folderTouched = false;
      folderName.addEventListener('input', () => folderTouched = true);
      acct.addEventListener('change', () => {{
        if (!folderTouched && acct.value) folderName.value = slug(acct.value);
      }});

      // ------- Schedule preset buttons -------
      document.querySelectorAll('button[data-times]').forEach(b => b.addEventListener('click', () => {{
        document.getElementById('sched-times').value = b.dataset.times;
      }}));
      document.querySelectorAll('button[data-days]').forEach(b => b.addEventListener('click', () => {{
        const wanted = new Set(b.dataset.days.split(/\\s+/));
        document.querySelectorAll('input[name=days]').forEach(cb => cb.checked = wanted.has(cb.value));
      }}));
      document.querySelectorAll('button[data-jitter]').forEach(b => b.addEventListener('click', () => {{
        document.getElementById('jitter').value = b.dataset.jitter;
      }}));

      const warmupSummaries = {{
        off:        'No warmup — full schedule from day 1.',
        slow:       'Slow ramp: 1/d for 3 days → 2/d → 3/d → 4/d → full at day 14.',
        medium:     'Medium ramp: 1/d for 2 days → 2/d → 3/d → full at day 7.',
        aggressive: 'Aggressive ramp: 1/d for 1 day → 3/d → full at day 3.',
      }};
      document.querySelectorAll('button[data-warmup]').forEach(b => b.addEventListener('click', () => {{
        const v = b.dataset.warmup;
        document.getElementById('warmup-preset').value = v;
        document.getElementById('warmup-summary').textContent = warmupSummaries[v] || '';
      }}));

      // ------- Copy settings from existing channel -------
      const tpl = document.getElementById('tpl-channel');
      if (tpl) tpl.addEventListener('change', async () => {{
        if (!tpl.value) return;
        const r = await fetch('/api/channel/' + tpl.value + '/config');
        if (!r.ok) return;
        const cfg = await r.json();
        const sched = cfg.schedule || {{}};
        const yt = cfg.youtube || {{}};
        if (sched.times)    document.getElementById('sched-times').value = sched.times.join(', ');
        if (sched.timezone) document.getElementById('tz').value = sched.timezone;
        if (sched.days) {{
          const wanted = new Set(sched.days);
          document.querySelectorAll('input[name=days]').forEach(cb => cb.checked = wanted.has(cb.value));
        }}
        if (yt.title_template) document.getElementById('title-tpl').value = yt.title_template;
        if (yt.pinned_message) document.getElementById('pinned').value = yt.pinned_message;
        if (yt.description)    document.getElementById('desc').value = yt.description;
      }});
    </script>
    """
    return render("Add Channel", body, active="add")


@app.route("/add", methods=["POST"])
def add_channel_post():
    name = request.form.get("name", "").strip().lower()
    if not safe_name(name):
        return render("Error", '<h1>Invalid folder name</h1><a href="/add">Back</a>'), 400
    target = CHANNELS_DIR / name
    if target.exists():
        return render("Error", f'<h1>Folder {html_escape(name)} already exists</h1><a href="/add">Back</a>'), 400

    times = [t.strip() for t in request.form.get("times", "").split(",") if t.strip()]
    days = request.form.getlist("days") or request.form.get("days", "").split()
    source_folder = request.form.get("source_folder", "").strip() or None
    try:
        jitter = max(0, int(request.form.get("jitter_minutes", "30")))
    except ValueError:
        jitter = 30

    warmup_preset = request.form.get("warmup_preset", "off")
    warmup_ramps = {
        "slow": [
            {"after_days": 0, "per_day": 1},
            {"after_days": 3, "per_day": 2},
            {"after_days": 7, "per_day": 3},
            {"after_days": 10, "per_day": 4},
            {"after_days": 14, "per_day": 999},
        ],
        "medium": [
            {"after_days": 0, "per_day": 1},
            {"after_days": 2, "per_day": 2},
            {"after_days": 4, "per_day": 3},
            {"after_days": 7, "per_day": 999},
        ],
        "aggressive": [
            {"after_days": 0, "per_day": 1},
            {"after_days": 1, "per_day": 3},
            {"after_days": 3, "per_day": 999},
        ],
    }

    try:
        buffer_size = max(0, int(request.form.get("media_buffer_size", "10")))
    except ValueError:
        buffer_size = 10

    config: dict = {
        "social_account": request.form.get("social_account", "").strip(),
        "move_after_post": True,
        "catch_up_window_minutes": 30,
        "cleanup_after_publish": "archive",
        "prescheduling_window_hours": 8,
        "media_buffer_size": buffer_size,
        "schedule": {
            "times": times,
            "days": days,
            "timezone": request.form.get("timezone", "UTC").strip(),
            "jitter_minutes": jitter,
        },
        "youtube": {
            "title_template": request.form.get("title_template", "{smart_title}").strip(),
            "pinned_message": request.form.get("pinned_message", "").strip(),
            "description": request.form.get("description", "").strip() or "Subscribe for more!",
        },
    }
    if warmup_preset in warmup_ramps:
        config["warmup"] = {"ramp": warmup_ramps[warmup_preset]}
    if source_folder:
        config["source_folder"] = source_folder

    target.mkdir(parents=True)
    (target / "inbox").mkdir(exist_ok=True)
    (target / "posted").mkdir(exist_ok=True)
    if source_folder:
        try:
            Path(source_folder).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # don't block creation if path can't be made (e.g. read-only fs)
    write_config(target, config)
    return redirect(url_for("channel_detail", name=name, flash=f"Channel created"))


# -------------------- folder reveal --------------------


def _resolve_folder(channel_dir: Path, config: dict, which: str) -> Path | None:
    """Return the absolute path for a folder kind, or None if not applicable."""
    which = (which or "").lower()
    if which == "source":
        sf = config.get("source_folder")
        if sf:
            p = Path(sf).expanduser()
            return p
        return channel_dir / "inbox"
    if which == "inbox":
        return channel_dir / "inbox"
    if which == "posted":
        return channel_dir / "posted"
    if which == "archive":
        return channel_dir / "archive"
    if which == "channel":
        return channel_dir
    return None


@app.route("/channel/<name>/open/<which>", methods=["POST"])
def open_folder(name: str, which: str):
    """Reveal a folder in Finder via macOS `open`. Creates the folder first
    if it doesn't exist (so the user can immediately drop files in)."""
    if not safe_name(name):
        abort(400)
    channel_dir, config, _ = load_channel(name)
    target = _resolve_folder(channel_dir, config, which)
    if target is None:
        return redirect(url_for("channel_detail", name=name, flash=f"Unknown folder: {which}"))
    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["open", str(target)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return redirect(url_for("channel_detail", name=name, flash=f"open failed: {e}"))
    return redirect(url_for("channel_detail", name=name, flash=f"Opened {target.name}/ in Finder"))


# -------------------- API --------------------


@app.route("/api/channel/<name>/config")
def api_channel_config(name: str):
    """Return a channel's parsed config.yaml as JSON. Used by Add-Channel
    form to populate "copy settings from existing channel"."""
    if not safe_name(name):
        abort(400)
    _, config, _ = load_channel(name)
    return jsonify(config)


@app.route("/api/status")
def api_status():
    out: list[dict] = []
    for chan in channel_dirs():
        config = yaml.safe_load((chan / "config.yaml").read_text()) or {}
        state = {"videos": []}
        sf = chan / "_state.json"
        if sf.exists():
            try:
                state = json.loads(sf.read_text())
            except json.JSONDecodeError:
                pass
        out.append({"name": chan.name, **channel_stats(state, config)})
    return jsonify(out)


# -------------------- entry --------------------


def run_in_thread(host: str = "127.0.0.1", port: int = 5050):
    def _run():
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    t = threading.Thread(target=_run, daemon=True, name="ui-server")
    t.start()
    return t


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
