#!/usr/bin/env python3
"""
APIDistributor folder-watcher (Post Bridge edition).

The watcher is the SCHEDULER. Post Bridge is the publisher.

Lifecycle for each video:
  1. Discover  — file appears in source_folder
  2. Upload    — get pre-signed URL from Post Bridge, PUT the file to S3
                 record media_id + intended slot in _state.json (fired=False)
                 file is moved to posted/
  3. Fire      — when slot time arrives (within CATCH_UP_WINDOW of now),
                 watcher posts type=now to Post Bridge, which uploads to
                 YouTube. Marks fired=True.
  4. Rebalance — if a slot has been due for more than CATCH_UP_WINDOW and
                 hasn't fired, push that video's scheduled_for to the next
                 free future slot. Prevents burst.

Per-channel config.yaml:
  social_account: Post Bridge channel name (case-insensitive match to
                  social-accounts username) OR numeric account id.
  source_folder:  Optional absolute path (default: ./inbox/).
  move_after_post: Bool, default true.
  catch_up_window_minutes: Int, default 30.
  schedule:       times (HH:MM list), days (Mon..Sun), timezone (IANA).
  youtube:        title_template, description, pinned_message
  force_first_slot: Optional ISO datetime.
"""

import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

import ui as ui_module

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
VIDEO_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4"}
POLL_SECONDS = 30
DEFAULT_CATCH_UP_MINUTES = 30
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watcher")


@dataclass
class Settings:
    api_url: str
    api_key: str
    channels_dir: Path


def load_settings() -> Settings:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    api_key = os.environ.get("POSTBRIDGE_API_KEY", "").strip()
    if not api_key:
        log.error(
            "POSTBRIDGE_API_KEY missing from .env. "
            "Generate one at https://www.post-bridge.com/dashboard/api-keys"
        )
        sys.exit(1)
    api_url = os.environ.get(
        "POSTBRIDGE_API_URL", "https://api.post-bridge.com/v1"
    ).rstrip("/")
    return Settings(
        api_url=api_url,
        api_key=api_key,
        channels_dir=project_root / "channels",
    )


# -------------------- Post Bridge API client --------------------


def auth_headers(s: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {s.api_key}"}


def fetch_social_accounts(s: Settings) -> dict[str, dict]:
    """Returns {username_lower: {id, platform, username}} for connected accounts."""
    r = requests.get(
        f"{s.api_url}/social-accounts",
        headers=auth_headers(s),
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    return {item["username"].lower(): item for item in items}


def upload_media(s: Settings, file_path: Path) -> dict:
    """Two-step upload:
      1. POST /v1/media/create-upload-url with metadata -> get media_id + signed URL.
      2. PUT the file to the signed URL.
    Returns {"id": media_id, "name": filename}.
    """
    size = file_path.stat().st_size
    mime = VIDEO_MIME.get(file_path.suffix.lower(), "video/mp4")

    r = requests.post(
        f"{s.api_url}/media/create-upload-url",
        headers={**auth_headers(s), "Content-Type": "application/json"},
        json={"mime_type": mime, "size_bytes": size, "name": file_path.name},
        timeout=30,
    )
    r.raise_for_status()
    info = r.json()
    media_id = info["media_id"]
    upload_url = info["upload_url"]

    with file_path.open("rb") as fh:
        put = requests.put(
            upload_url,
            data=fh,
            headers={"Content-Type": mime},
            timeout=600,
        )
    if not put.ok:
        log.error(
            "S3 upload PUT failed: %s %s", put.status_code, put.text[:500]
        )
        put.raise_for_status()

    return {"id": media_id, "name": file_path.name}


def post_now(
    s: Settings,
    social_account_id: int,
    media_id: str,
    caption: str,
    title: str,
) -> dict:
    """Fire a type=now post to Post Bridge."""
    payload = {
        "caption": caption,
        "social_accounts": [int(social_account_id)],
        "media": [media_id],
        "platform_configurations": {
            "youtube": {"title": title},
        },
    }
    r = requests.post(
        f"{s.api_url}/posts",
        headers={**auth_headers(s), "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=60,
    )
    if not r.ok:
        log.error(
            "Post Bridge rejected /posts: %s %s", r.status_code, r.text[:500]
        )
        r.raise_for_status()
    return r.json()


# -------------------- State file --------------------


def load_state(channel_dir: Path) -> dict:
    state_file = channel_dir / "_state.json"
    if not state_file.exists():
        return {"videos": []}
    try:
        data = json.loads(state_file.read_text())
    except json.JSONDecodeError:
        log.warning("State file corrupt at %s — starting fresh", state_file)
        return {"videos": []}
    if "videos" not in data:
        log.info("State file at %s in old format — starting fresh", state_file)
        return {"videos": []}
    return data


def save_state(channel_dir: Path, state: dict) -> None:
    (channel_dir / "_state.json").write_text(json.dumps(state, indent=2))


# -------------------- Scheduling --------------------


def parse_time_of_day(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def get_schedule_tz(config: dict) -> ZoneInfo:
    return ZoneInfo(config.get("schedule", {}).get("timezone", "UTC"))


def parse_iso(s: str, default_tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def next_free_slot(
    config: dict, after: datetime, used: set[datetime],
) -> datetime:
    sched = config.get("schedule", {})
    times = sched.get("times", ["12:00"])
    days = set(sched.get("days", DAY_NAMES))
    tz = get_schedule_tz(config)
    after_local = after.astimezone(tz)
    used_local = {dt.astimezone(tz).replace(microsecond=0) for dt in used}
    for offset in range(0, 365):
        candidate = after_local.date() + timedelta(days=offset)
        if DAY_NAMES[candidate.weekday()] not in days:
            continue
        for tstr in times:
            hh, mm = parse_time_of_day(tstr)
            slot = datetime(
                candidate.year, candidate.month, candidate.day,
                hh, mm, tzinfo=tz,
            )
            if slot <= after_local:
                continue
            if slot.replace(microsecond=0) in used_local:
                continue
            return slot
    raise RuntimeError("No free slot in 365 days.")


def collect_used_slots(state: dict, default_tz: ZoneInfo) -> set[datetime]:
    used = set()
    for v in state["videos"]:
        try:
            used.add(parse_iso(v["scheduled_for"], default_tz))
        except (KeyError, ValueError):
            pass
    return used


def first_slot_for_new_video(config: dict, state: dict) -> datetime:
    tz = get_schedule_tz(config)
    now = datetime.now(tz)
    used = collect_used_slots(state, tz)
    if not state["videos"]:
        force_first = config.get("force_first_slot")
        if force_first:
            try:
                forced = parse_iso(str(force_first), tz)
                if forced > now:
                    return forced
                log.info("force_first_slot %s passed, using regular schedule", forced)
            except ValueError:
                log.warning("force_first_slot %r invalid, ignoring", force_first)
    return next_free_slot(config, after=now, used=used)


# -------------------- Title / sidecar helpers --------------------


def extract_smart_title(file_path: Path) -> str:
    stem = file_path.stem
    stem = re.sub(r"_[A-Za-z0-9_-]{8,}$", "", stem)
    parts = stem.replace("_", " ").split("-")
    cleaned = " ".join(p for p in parts if p)
    words = []
    for w in cleaned.split():
        words.append(w if w.isupper() else w[:1].upper() + w[1:])
    title = " ".join(words).strip()
    if not (2 <= len(title) <= 100):
        title = (title[:100] if len(title) >= 2 else f"Video {file_path.stem}")[:100]
    return title


def title_from_template(template: str, file_path: Path, smart: str) -> str:
    title = template.replace("{filename}", file_path.stem).replace("{smart_title}", smart)
    if not (2 <= len(title) <= 100):
        title = smart[:100]
    return title


def load_sidecar(video: Path) -> dict | None:
    sidecar = video.with_suffix(".json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except json.JSONDecodeError as e:
        log.warning("Sidecar %s invalid JSON: %s — ignoring", sidecar, e)
        return None


def find_videos(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def is_file_stable(path: Path) -> bool:
    try:
        first = path.stat().st_size
    except FileNotFoundError:
        return False
    time.sleep(5)
    try:
        return path.stat().st_size == first
    except FileNotFoundError:
        return False


def build_caption(config: dict, sidecar_description: str | None) -> str:
    """Compose the YouTube description. If pinned_message is set in config,
    prepend it (acts as the visible-above-the-fold first line)."""
    yt = config.get("youtube", {})
    pinned = yt.get("pinned_message", "").strip()
    body = (sidecar_description if sidecar_description is not None
            else yt.get("description", "")).strip()
    if pinned and body:
        return f"{pinned}\n\n{body}"
    return pinned or body


# -------------------- Per-channel pipeline --------------------


def get_catch_up_window(config: dict) -> timedelta:
    minutes = config.get("catch_up_window_minutes", DEFAULT_CATCH_UP_MINUTES)
    try:
        return timedelta(minutes=int(minutes))
    except (TypeError, ValueError):
        return timedelta(minutes=DEFAULT_CATCH_UP_MINUTES)


def resolve_account(
    config: dict, accounts: dict[str, dict],
) -> dict | None:
    """Resolve config's social_account (string username or numeric id) to an
    account dict. Falls back to legacy `integration_name`."""
    target = config.get("social_account") or config.get("integration_name") or ""
    target = str(target).strip()
    if not target:
        return None
    # Numeric id?
    try:
        target_id = int(target)
        for acct in accounts.values():
            if int(acct.get("id", -1)) == target_id:
                return acct
    except ValueError:
        pass
    return accounts.get(target.lower())


def rebalance_overdue(state: dict, config: dict, channel_name: str) -> int:
    tz = get_schedule_tz(config)
    now = datetime.now(tz)
    catch_up = get_catch_up_window(config)
    rebalanced = 0
    unfired = sorted(
        (v for v in state["videos"] if not v.get("fired")),
        key=lambda v: v["scheduled_for"],
    )
    for v in unfired:
        try:
            slot = parse_iso(v["scheduled_for"], tz)
        except ValueError:
            continue
        if slot + catch_up >= now:
            continue
        used = collect_used_slots(state, tz)
        used.discard(slot.replace(microsecond=0))
        new_slot = next_free_slot(config, after=now, used=used)
        log.info(
            "[%s] rebalancing '%s' from %s -> %s (was %s late)",
            channel_name, v.get("title", v.get("filename", "?")),
            slot.isoformat(), new_slot.isoformat(), now - slot,
        )
        v["scheduled_for"] = new_slot.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        rebalanced += 1
    return rebalanced


def fire_due_slots(
    s: Settings, state: dict, config: dict,
    account: dict, channel_name: str,
) -> int:
    tz = get_schedule_tz(config)
    now = datetime.now(tz)
    catch_up = get_catch_up_window(config)
    fired_count = 0
    unfired = sorted(
        (v for v in state["videos"] if not v.get("fired")),
        key=lambda v: v["scheduled_for"],
    )
    for v in unfired:
        try:
            slot = parse_iso(v["scheduled_for"], tz)
        except ValueError:
            continue
        if slot > now:
            break
        if slot + catch_up < now:
            continue
        media = v.get("media") or {}
        media_id = media.get("id")
        if not media_id:
            log.error(
                "[%s] '%s' missing media id, skipping",
                channel_name, v.get("title", v.get("filename", "?")),
            )
            v["fired"] = True
            v["error"] = "missing_media"
            continue
        try:
            log.info(
                "[%s] firing '%s' (slot %s, %.1fs late)",
                channel_name, v["title"], slot.isoformat(),
                (now - slot).total_seconds(),
            )
            response = post_now(
                s,
                social_account_id=int(account["id"]),
                media_id=media_id,
                caption=v.get("description", ""),
                title=v["title"],
            )
            v["fired"] = True
            v["fired_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                v["post_id"] = response.get("id") or response.get("data", {}).get("id")
            except Exception:
                pass
            fired_count += 1
            time.sleep(1)
        except requests.HTTPError as e:
            log.error(
                "[%s] firing '%s' failed: %s — will retry next cycle",
                channel_name, v.get("title", "?"), e,
            )
            return fired_count
    return fired_count


def discover_and_upload(
    s: Settings, channel_dir: Path, state: dict, config: dict, channel_name: str,
) -> int:
    source_folder = config.get("source_folder")
    inbox = Path(source_folder).expanduser() if source_folder else channel_dir / "inbox"
    posted = channel_dir / "posted"
    inbox.mkdir(parents=True, exist_ok=True)
    posted.mkdir(parents=True, exist_ok=True)
    move_after_post = bool(config.get("move_after_post", True))

    videos = find_videos(inbox)
    if not videos:
        return 0

    seen = {v["filename"] for v in state["videos"]}
    yt = config.get("youtube", {})
    title_template = yt.get("title_template", "{smart_title}")
    uploaded = 0

    for video in videos:
        if video.name in seen:
            continue
        if not is_file_stable(video):
            log.info("[%s] %s still being written, skipping",
                     channel_name, video.name)
            continue

        smart = extract_smart_title(video)
        sidecar = load_sidecar(video) or {}
        title = sidecar.get("title") or title_from_template(title_template, video, smart)
        description = build_caption(config, sidecar.get("description"))

        try:
            slot = first_slot_for_new_video(config, state)
        except RuntimeError as e:
            log.error("[%s] %s", channel_name, e)
            return uploaded

        log.info(
            "[%s] uploading %s as '%s' -> slot %s",
            channel_name, video.name, title, slot.isoformat(),
        )

        try:
            media = upload_media(s, video)
        except requests.HTTPError as e:
            log.error("[%s] upload of %s failed: %s — will retry next cycle",
                      channel_name, video.name, e)
            return uploaded
        except Exception as e:
            log.exception("[%s] upload of %s exception: %s",
                          channel_name, video.name, e)
            return uploaded

        if move_after_post:
            target = posted / video.name
            if target.exists():
                target = posted / f"{int(time.time())}_{video.name}"
            shutil.move(str(video), str(target))

        state["videos"].append({
            "filename": video.name,
            "title": title,
            "description": description,
            "scheduled_for": slot.astimezone(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            "fired": False,
            "media": media,
            "post_id": None,
            "fired_at": None,
        })
        seen.add(video.name)
        uploaded += 1
        save_state(channel_dir, state)
        time.sleep(1)
    return uploaded


def process_channel(
    s: Settings, channel_dir: Path, accounts: dict[str, dict],
) -> None:
    config_path = channel_dir / "config.yaml"
    if not config_path.exists():
        return
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        log.error("[%s] config.yaml invalid: %s", channel_dir.name, e)
        return

    account = resolve_account(config, accounts)
    if not account:
        log.error(
            "[%s] no Post Bridge account matches %r. Connected: %s",
            channel_dir.name,
            config.get("social_account") or config.get("integration_name"),
            [a["username"] for a in accounts.values()],
        )
        return

    state = load_state(channel_dir)

    if rebalance_overdue(state, config, channel_dir.name):
        save_state(channel_dir, state)

    if fire_due_slots(s, state, config, account, channel_dir.name):
        save_state(channel_dir, state)

    discover_and_upload(s, channel_dir, state, config, channel_dir.name)


def discover_channels(channels_dir: Path) -> list[Path]:
    if not channels_dir.exists():
        return []
    return [
        p for p in channels_dir.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "config.yaml").exists()
    ]


def main_loop(s: Settings) -> None:
    log.info("watcher starting (Post Bridge edition); channels: %s", s.channels_dir)
    log.info("API: %s", s.api_url)
    while True:
        try:
            accounts = fetch_social_accounts(s)
        except requests.RequestException as e:
            log.error("Could not reach Post Bridge: %s — retrying in %ds", e, POLL_SECONDS)
            time.sleep(POLL_SECONDS)
            continue
        channels = discover_channels(s.channels_dir)
        if not channels:
            log.info("No channel folders yet (drop one into %s).", s.channels_dir)
        for channel_dir in channels:
            try:
                process_channel(s, channel_dir, accounts)
            except Exception as e:
                log.exception("Error processing %s: %s", channel_dir.name, e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    settings = load_settings()
    ui_module.run_in_thread()
    log.info("UI running at http://localhost:5050")
    try:
        main_loop(settings)
    except KeyboardInterrupt:
        log.info("watcher stopped")
