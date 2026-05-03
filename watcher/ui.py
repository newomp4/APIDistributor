#!/usr/bin/env python3
"""
APIDistributor web UI — runs alongside watcher.py in the same process.

Visit http://localhost:5050 to:
  - Monitor every channel: queued / fired / overdue / next slot
  - Add a new channel (form-based, writes config.yaml)
  - View per-channel detail with the scheduled video list
  - Edit a channel's config inline
  - Restart Docker containers without using terminal
"""

import json
import os
import re
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from flask import Flask, abort, jsonify, redirect, render_template_string, request, url_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = PROJECT_ROOT / "channels"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yaml"

app = Flask(__name__)


# -------------------- helpers --------------------


def api_url() -> str:
    return os.environ.get("POSTIZ_API_URL", "http://localhost:4007/api/public/v1").rstrip("/")


def api_key() -> str:
    return os.environ.get("POSTIZ_API_KEY", "")


def fetch_integrations() -> list[dict]:
    try:
        r = requests.get(
            f"{api_url()}/integrations",
            headers={"Authorization": api_key()},
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def channels() -> list[Path]:
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
    state = {"videos": []}
    state_file = channel_dir / "_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            pass
    return channel_dir, config, state


def channel_stats(state: dict, config: dict) -> dict:
    tz_name = config.get("schedule", {}).get("timezone", "UTC")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    queued = [v for v in state.get("videos", []) if not v.get("fired")]
    fired = [v for v in state.get("videos", []) if v.get("fired")]
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
    return {
        "queued": len(queued),
        "fired": len(fired),
        "overdue": overdue,
        "next_slot": next_slot.strftime("%a %b %d %I:%M %p %Z") if next_slot else None,
    }


CHANNEL_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def safe_name(name: str) -> bool:
    return bool(CHANNEL_NAME_RE.match(name))


# -------------------- templates --------------------


BASE = """
<!doctype html>
<html><head><title>APIDistributor — {{ title }}</title>
<meta charset="utf-8">
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; background: #0e0e10; color: #e8e8e8; }
  h1, h2 { font-weight: 600; }
  a { color: #6cc1ff; text-decoration: none; } a:hover { text-decoration: underline; }
  .nav { padding: 12px 0; border-bottom: 1px solid #2a2a2e; margin-bottom: 24px; display: flex; gap: 16px; align-items: center; }
  .nav .brand { font-weight: 700; font-size: 18px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
  .card { background: #18181b; padding: 16px; border-radius: 10px; border: 1px solid #2a2a2e; }
  .card h3 { margin: 0 0 8px 0; }
  .stat { display: inline-block; margin-right: 14px; font-size: 13px; color: #aaa; }
  .stat strong { color: #fff; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #2a2a2e; color: #aaa; }
  .pill.ok { background: #14532d; color: #86efac; }
  .pill.warn { background: #713f12; color: #fde68a; }
  .pill.err { background: #7f1d1d; color: #fca5a5; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #2a2a2e; }
  th { color: #aaa; font-weight: 500; font-size: 12px; text-transform: uppercase; }
  form .field { margin: 12px 0; }
  form label { display: block; font-size: 13px; color: #aaa; margin-bottom: 4px; }
  form input[type=text], form input[type=number], form select, form textarea {
    width: 100%; padding: 8px 10px; background: #0e0e10; color: #e8e8e8;
    border: 1px solid #2a2a2e; border-radius: 6px; font-size: 14px; font-family: inherit; box-sizing: border-box;
  }
  form textarea { min-height: 80px; }
  button, .button { padding: 8px 16px; background: #6cc1ff; color: #0e0e10; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }
  button.danger { background: #f87171; }
  button.subtle { background: #2a2a2e; color: #e8e8e8; }
  .row { display: flex; gap: 8px; align-items: center; }
  pre { background: #0e0e10; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
  .muted { color: #888; font-size: 13px; }
  .right { float: right; }
</style>
</head><body>
<div class="nav">
  <span class="brand">APIDistributor</span>
  <a href="{{ url_for('dashboard') }}">Dashboard</a>
  <a href="{{ url_for('add_channel') }}">+ Add Channel</a>
  <span class="muted right">connected to {{ api }}</span>
</div>
{{ body|safe }}
</body></html>
"""


def render(title: str, body: str, **ctx) -> str:
    return render_template_string(BASE, title=title, body=body, api=api_url(), **ctx)


# -------------------- dashboard --------------------


@app.route("/")
def dashboard():
    rows = []
    for chan in channels():
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
        rows.append({
            "name": chan.name,
            "integration": config.get("integration_name", "?"),
            "source": config.get("source_folder") or "(channel inbox/)",
            "times": ", ".join(sched.get("times", [])) or "(none)",
            "days": " ".join(sched.get("days", [])) or "(all)",
            **stats,
        })

    integ_count = len(fetch_integrations())
    cards_html = ""
    for r in rows:
        pill_class = "ok" if r["overdue"] == 0 else "warn"
        cards_html += f'''
        <div class="card">
          <h3><a href="/channel/{r['name']}">{r['name']}</a></h3>
          <div class="muted">{r['integration']} · {r['times']}</div>
          <div style="margin: 10px 0;">
            <span class="stat"><strong>{r['queued']}</strong> queued</span>
            <span class="stat"><strong>{r['fired']}</strong> fired</span>
            <span class="pill {pill_class}">{r['overdue']} overdue</span>
          </div>
          <div class="muted">Next: {r['next_slot'] or '— nothing queued —'}</div>
        </div>
        '''
    if not rows:
        cards_html = '<div class="card muted">No channels configured yet. Click <a href="/add">+ Add Channel</a> to start.</div>'

    body = f'''
    <h1>Channels <span class="muted" style="font-size:14px;">({integ_count} integrations connected in Postiz)</span></h1>
    <div class="grid">{cards_html}</div>
    <h2 style="margin-top:32px;">Quick actions</h2>
    <form method="post" action="/restart-docker" style="display:inline;">
      <button class="subtle" type="submit" onclick="return confirm('Restart all Docker containers? Postiz will be unreachable for ~30 seconds.');">↻ Restart Docker stack</button>
    </form>
    <a class="button subtle" href="/logs" style="margin-left:8px;">View recent logs</a>
    '''
    return render("Dashboard", body)


# -------------------- channel detail --------------------


@app.route("/channel/<name>")
def channel_detail(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, config, state = load_channel(name)
    stats = channel_stats(state, config)
    sched = config.get("schedule", {})
    yt = config.get("youtube", {})

    videos = list(state.get("videos", []))
    videos.sort(key=lambda v: v.get("scheduled_for", ""))

    rows_html = ""
    for v in videos:
        status_pill = '<span class="pill ok">fired</span>' if v.get("fired") else '<span class="pill warn">queued</span>'
        try:
            slot_dt = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00"))
            slot_str = slot_dt.astimezone(ZoneInfo(sched.get("timezone", "UTC"))).strftime("%a %b %d %I:%M %p")
        except Exception:
            slot_str = v.get("scheduled_for", "")
        title_html = v.get("title", v["filename"])
        if v.get("post_id"):
            title_html += f'<br><span class="muted">post {v["post_id"]}</span>'
        rows_html += f'<tr><td>{title_html}</td><td>{slot_str}</td><td>{status_pill}</td></tr>'

    body = f'''
    <h1>{name}</h1>
    <div class="muted">Postiz channel: <strong>{config.get('integration_name','?')}</strong></div>
    <div class="muted">Source: <code>{config.get('source_folder') or '(channel inbox/)'}</code></div>
    <div style="margin: 16px 0;">
      <span class="stat"><strong>{stats['queued']}</strong> queued</span>
      <span class="stat"><strong>{stats['fired']}</strong> fired</span>
      <span class="pill {('ok' if stats['overdue']==0 else 'warn')}">{stats['overdue']} overdue</span>
      <span class="muted" style="margin-left:12px;">Next: {stats['next_slot'] or '—'}</span>
    </div>

    <h2>Schedule</h2>
    <div class="card">
      <div class="muted">Times: <strong>{', '.join(sched.get('times', [])) or '(none)'}</strong></div>
      <div class="muted">Days: <strong>{' '.join(sched.get('days', [])) or '(all)'}</strong></div>
      <div class="muted">Timezone: <strong>{sched.get('timezone','UTC')}</strong></div>
      <div class="muted">Privacy: <strong>{yt.get('privacy','public')}</strong></div>
    </div>

    <h2 style="margin-top:24px;">Videos ({len(videos)})</h2>
    <table>
      <tr><th>Title</th><th>Slot</th><th>Status</th></tr>
      {rows_html or '<tr><td colspan="3" class="muted">No videos yet — drop one in the source folder.</td></tr>'}
    </table>

    <h2 style="margin-top:24px;">Edit config</h2>
    <form method="post" action="/channel/{name}/config">
      <textarea name="config_yaml" rows="20" style="font-family: monospace; font-size: 13px;">{yaml.safe_dump(config, sort_keys=False, default_flow_style=False)}</textarea>
      <div class="row" style="margin-top:8px;">
        <button type="submit">Save config</button>
        <form method="post" action="/channel/{name}/delete" onsubmit="return confirm('Delete the channel folder and all its state? Videos in posted/ stay.');" style="margin-left:auto;">
          <button class="danger" type="submit">Delete channel folder</button>
        </form>
      </div>
    </form>
    '''
    return render(name, body)


@app.route("/channel/<name>/config", methods=["POST"])
def update_config(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, _, _ = load_channel(name)
    raw = request.form.get("config_yaml", "")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return render("Error", f'<h2>Invalid YAML</h2><pre>{e}</pre><a href="/channel/{name}">Back</a>'), 400
    if not isinstance(parsed, dict):
        return render("Error", f'<h2>Config must be a YAML object</h2><a href="/channel/{name}">Back</a>'), 400
    (channel_dir / "config.yaml").write_text(raw)
    return redirect(url_for("channel_detail", name=name))


@app.route("/channel/<name>/delete", methods=["POST"])
def delete_channel(name: str):
    if not safe_name(name):
        abort(400)
    channel_dir, _, _ = load_channel(name)
    # Don't delete posted/ — keep video archive. Move out of channels/.
    archive = CHANNELS_DIR.parent / ".deleted_channels" / f"{name}_{int(datetime.now().timestamp())}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    channel_dir.rename(archive)
    return redirect(url_for("dashboard"))


# -------------------- add channel --------------------


@app.route("/add")
def add_channel():
    integrations = fetch_integrations()
    options_html = "".join(
        f'<option value="{i["name"]}">{i["name"]} ({i["identifier"]})</option>'
        for i in integrations
    )
    if not options_html:
        options_html = '<option value="">⚠️ No integrations found — connect a channel in Postiz first</option>'

    body = f'''
    <h1>Add a new channel</h1>
    <form method="post" action="/add">
      <div class="field">
        <label>Folder name (lowercase letters, digits, underscores) — used as the directory name</label>
        <input name="name" type="text" placeholder="my_new_channel" required pattern="[a-z0-9_]{{1,40}}">
      </div>
      <div class="field">
        <label>Postiz integration (the channel as connected in Postiz)</label>
        <select name="integration_name" required>{options_html}</select>
      </div>
      <div class="field">
        <label>Source folder (absolute path; videos will be picked up from here)</label>
        <input name="source_folder" type="text" placeholder="/Users/you/myfactory/output">
      </div>
      <div class="field">
        <label>Schedule — comma-separated times (24-hour, channel timezone)</label>
        <input name="times" type="text" placeholder="09:00, 18:00" value="09:00, 18:00">
      </div>
      <div class="field">
        <label>Days of week (space-separated)</label>
        <input name="days" type="text" placeholder="Mon Tue Wed Thu Fri Sat Sun" value="Mon Tue Wed Thu Fri Sat Sun">
      </div>
      <div class="field">
        <label>Timezone (IANA)</label>
        <input name="timezone" type="text" placeholder="America/New_York" value="America/New_York">
      </div>
      <div class="field">
        <label>YouTube title template (use <code>{{smart_title}}</code> for filename-derived title)</label>
        <input name="title_template" type="text" value="{{smart_title}}">
      </div>
      <div class="field">
        <label>Description</label>
        <textarea name="description" placeholder="Subscribe for more!"></textarea>
      </div>
      <div class="field">
        <label>Privacy</label>
        <select name="privacy"><option>public</option><option>unlisted</option><option>private</option></select>
      </div>
      <div class="field">
        <label>Tags (comma-separated)</label>
        <input name="tags" type="text" placeholder="shorts, viral">
      </div>
      <button type="submit">Create channel</button>
    </form>
    '''
    return render("Add Channel", body)


@app.route("/add", methods=["POST"])
def add_channel_post():
    name = request.form.get("name", "").strip().lower()
    if not safe_name(name):
        return render("Error", '<h2>Invalid folder name</h2><a href="/add">Back</a>'), 400
    target = CHANNELS_DIR / name
    if target.exists():
        return render("Error", f'<h2>Folder {name} already exists</h2><a href="/add">Back</a>'), 400

    times = [t.strip() for t in request.form.get("times", "").split(",") if t.strip()]
    days = request.form.get("days", "").split()
    tags_raw = request.form.get("tags", "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    config = {
        "integration_name": request.form.get("integration_name", "").strip(),
        "source_folder": request.form.get("source_folder", "").strip() or None,
        "move_after_post": True,
        "schedule": {
            "times": times,
            "days": days,
            "timezone": request.form.get("timezone", "UTC").strip(),
        },
        "youtube": {
            "title_template": request.form.get("title_template", "{smart_title}").strip(),
            "description": request.form.get("description", "").strip() or "Subscribe for more!",
            "privacy": request.form.get("privacy", "public"),
            "made_for_kids": "no",
            "tags": tags,
        },
    }
    if not config["source_folder"]:
        del config["source_folder"]

    target.mkdir(parents=True)
    (target / "inbox").mkdir(exist_ok=True)
    (target / "posted").mkdir(exist_ok=True)
    (target / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False))

    return redirect(url_for("channel_detail", name=name))


# -------------------- docker actions --------------------


@app.route("/restart-docker", methods=["POST"])
def restart_docker():
    cmd = ["docker", "compose", "--project-directory", str(PROJECT_ROOT), "restart"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return redirect(url_for("dashboard"))


# -------------------- logs --------------------


@app.route("/logs")
def logs():
    log_path = Path("/tmp/apidistributor-watcher.log")
    text = "(no log file yet — watcher writes to stdout, not to a file)"
    if log_path.exists():
        text = log_path.read_text()[-8000:]
    body = f'<h1>Recent watcher activity</h1><pre>{text}</pre><div class="muted">For full live logs, watch the Terminal window where you ran <code>start-watcher.command</code>.</div>'
    return render("Logs", body)


# -------------------- API --------------------


@app.route("/api/status")
def api_status():
    out = []
    for chan in channels():
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
    """Start the UI in a background thread alongside the watcher loop."""
    def _run():
        # Use Flask's dev server — single-user local app, this is fine.
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    t = threading.Thread(target=_run, daemon=True, name="ui-server")
    t.start()
    return t


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
