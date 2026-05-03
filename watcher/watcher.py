#!/usr/bin/env python3
"""
APIDistributor folder-watcher v2 — burst-proof scheduler.

This watcher is the SCHEDULER. Postiz is just the publisher.

Lifecycle for each video:
  1. Discover  — file appears in source_folder
  2. Upload    — file gets uploaded to Postiz /upload (returns media id+path)
                 watcher records intended slot in _state.json (fired=False)
                 file is moved to posted/
  3. Fire      — when slot time arrives (within CATCH_UP_WINDOW of now),
                 watcher posts type=now to Postiz, which uploads to YouTube.
                 watcher marks fired=True.
  4. Rebalance — if a slot has been due for more than CATCH_UP_WINDOW and
                 hasn't fired (Mac/Docker was off), push that video's
                 scheduled_for to the next free future slot. Prevents burst.

Per-channel config.yaml:
  integration_name:   Postiz channel name (case-insensitive match).
  source_folder:      Optional absolute path (default: ./inbox/).
  move_after_post:    Bool, default true — move file to posted/ after upload.
  catch_up_window_minutes: Int, default 30 — tolerance for late firing.
  schedule:           times (HH:MM list), days (Mon..Sun), timezone (IANA).
  youtube:            title_template, description, privacy, made_for_kids, tags.
  force_first_slot:   Optional ISO datetime — first NEW video uses this slot.
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
    api_key = os.environ.get("POSTIZ_API_KEY", "").strip()
    if not api_key:
        log.error(
            "POSTIZ_API_KEY missing from .env. "
            "Generate one in Postiz: Settings > Developers > Public API."
        )
        sys.exit(1)
    api_url = os.environ.get(
        "POSTIZ_API_URL", "http://localhost:4007/api/public/v1"
    ).rstrip("/")
    return Settings(
        api_url=api_url,
        api_key=api_key,
        channels_dir=project_root / "channels",
    )


# -------------------- Postiz API client --------------------


def fetch_integrations(s: Settings) -> dict[str, dict]:
    r = requests.get(
        f"{s.api_url}/integrations",
        headers={"Authorization": s.api_key},
        timeout=15,
    )
    r.raise_for_status()
    return {integ["name"].lower(): integ for integ in r.json()}


def upload_media(s: Settings, file_path: Path) -> dict:
    """Upload file to Postiz, return {id, path}. Path is rewritten to
    host.docker.internal so the in-container YouTube worker can fetch it."""
    with file_path.open("rb") as fh:
        r = requests.post(
            f"{s.api_url}/upload",
            headers={"Authorization": s.api_key},
            files={"file": (file_path.name, fh, "video/mp4")},
            timeout=600,
        )
    r.raise_for_status()
    media = r.json()
    if isinstance(media.get("path"), str):
        media["path"] = media["path"].replace(
            "://localhost:4007/", "://host.docker.internal:4007/"
        )
    return media


def post_now(
    s: Settings,
    integration_id: str,
    media: dict,
    config: dict,
    title: str,
    description: str,
    tags: list[str],
) -> dict:
    """Fire a type=now post to Postiz, which immediately uploads to YouTube."""
    yt = config.get("youtube", {})
    payload = {
        "type": "now",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": integration_id},
                "value": [
                    {
                        "content": description,
                        "image": [{"id": media["id"], "path": media["path"]}],
                    }
                ],
                "settings": {
                    "__type": "youtube",
                    "title": title,
                    "type": yt.get("privacy", "public"),
                    "selfDeclaredMadeForKids": yt.get("made_for_kids", "no"),
                    "tags": [{"value": t, "label": t} for t in tags],
                },
            }
        ],
    }
    r = requests.post(
        f"{s.api_url}/posts",
        headers={
            "Authorization": s.api_key,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=60,
    )
    if not r.ok:
        log.error("Postiz rejected /posts: %s %s", r.status_code, r.text[:500])
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
    # Migrate old shape ({"scheduled": [...]}) — discard, start fresh
    if "videos" not in data:
        log.info("Migrating old state file at %s — starting fresh schedule", state_file)
        return {"videos": []}
    return data


def save_state(channel_dir: Path, state: dict) -> None:
    (channel_dir / "_state.json").write_text(json.dumps(state, indent=2))


# -------------------- Scheduling --------------------


def parse_time_of_day(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def get_schedule_tz(config: dict) -> ZoneInfo:
    tz_name = config.get("schedule", {}).get("timezone", "UTC")
    return ZoneInfo(tz_name)


def parse_iso(s: str, default_tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def next_free_slot(
    config: dict,
    after: datetime,
    used_slots: set[datetime],
) -> datetime:
    """First slot that is strictly after `after`, on a configured day/time,
    and not already in `used_slots`."""
    sched = config.get("schedule", {})
    times = sched.get("times", ["12:00"])
    days = set(sched.get("days", DAY_NAMES))
    tz = get_schedule_tz(config)
    after_local = after.astimezone(tz)
    used_local = {dt.astimezone(tz).replace(microsecond=0) for dt in used_slots}
    for offset in range(0, 365):
        candidate_day = after_local.date() + timedelta(days=offset)
        weekday_short = DAY_NAMES[candidate_day.weekday()]
        if weekday_short not in days:
            continue
        for tstr in times:
            hh, mm = parse_time_of_day(tstr)
            slot = datetime(
                candidate_day.year, candidate_day.month, candidate_day.day,
                hh, mm, 0, tzinfo=tz,
            )
            if slot <= after_local:
                continue
            if slot.replace(microsecond=0) in used_local:
                continue
            return slot
    raise RuntimeError("Could not find a free slot in the next 365 days.")


def collect_used_slots(state: dict, default_tz: ZoneInfo) -> set[datetime]:
    """All scheduled_for times (fired or not) — used to avoid double-booking."""
    used = set()
    for v in state["videos"]:
        try:
            used.add(parse_iso(v["scheduled_for"], default_tz))
        except (KeyError, ValueError):
            pass
    return used


def first_slot_for_new_video(
    config: dict,
    state: dict,
) -> datetime:
    """Slot for the next video being added. Honors force_first_slot if no
    videos have been added to state yet AND the time is in the future."""
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
                log.info("force_first_slot %s already passed, using regular schedule", forced)
            except ValueError:
                log.warning("force_first_slot %r invalid, ignoring", force_first)

    return next_free_slot(config, after=now, used_slots=used)


# -------------------- Filename / sidecar helpers --------------------


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


# -------------------- Per-channel pipeline --------------------


def get_catch_up_window(config: dict) -> timedelta:
    minutes = config.get("catch_up_window_minutes", DEFAULT_CATCH_UP_MINUTES)
    try:
        return timedelta(minutes=int(minutes))
    except (TypeError, ValueError):
        return timedelta(minutes=DEFAULT_CATCH_UP_MINUTES)


def rebalance_overdue(
    state: dict, config: dict, channel_name: str
) -> int:
    """Push overdue (past + outside catch-up window) unfired slots to the next
    free future slot. Returns count rebalanced."""
    tz = get_schedule_tz(config)
    now = datetime.now(tz)
    catch_up = get_catch_up_window(config)
    rebalanced = 0

    # Sort unfired by current scheduled_for so earliest-overdue rebalanced first
    unfired = [v for v in state["videos"] if not v.get("fired")]
    unfired.sort(key=lambda v: v["scheduled_for"])

    for v in unfired:
        try:
            slot = parse_iso(v["scheduled_for"], tz)
        except ValueError:
            continue
        if slot + catch_up >= now:
            continue
        # This slot is too late to fire — push to next free future slot
        used = collect_used_slots(state, tz)
        # Don't include this video's own current slot in 'used' (we're moving it)
        used.discard(slot.replace(microsecond=0))
        new_slot = next_free_slot(config, after=now, used_slots=used)
        log.info(
            "[%s] rebalancing '%s' from %s -> %s (was %s late)",
            channel_name, v.get("title", v["filename"]),
            slot.isoformat(), new_slot.isoformat(),
            now - slot,
        )
        v["scheduled_for"] = new_slot.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        rebalanced += 1
    return rebalanced


def fire_due_slots(
    s: Settings, state: dict, config: dict,
    integration: dict, channel_name: str,
) -> int:
    """Fire any unfired slots whose time has arrived (within catch-up window).
    Returns count fired."""
    tz = get_schedule_tz(config)
    now = datetime.now(tz)
    catch_up = get_catch_up_window(config)
    fired_count = 0

    unfired = [v for v in state["videos"] if not v.get("fired")]
    unfired.sort(key=lambda v: v["scheduled_for"])

    for v in unfired:
        try:
            slot = parse_iso(v["scheduled_for"], tz)
        except ValueError:
            continue
        if slot > now:
            break  # rest are future, sorted
        if slot + catch_up < now:
            continue  # too late, will be rebalanced next pass
        # Fire it now
        media = v.get("media")
        if not media or "id" not in media or "path" not in media:
            log.error(
                "[%s] '%s' missing media data, cannot fire — skipping",
                channel_name, v.get("title", v["filename"]),
            )
            v["fired"] = True
            v["error"] = "missing_media"
            continue
        try:
            log.info(
                "[%s] firing '%s' (slot %s, %.1fs late)",
                channel_name, v.get("title", v["filename"]),
                slot.isoformat(), (now - slot).total_seconds(),
            )
            response = post_now(
                s, integration["id"], media, config,
                title=v["title"],
                description=v.get("description", ""),
                tags=v.get("tags", []),
            )
            v["fired"] = True
            v["fired_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                v["post_id"] = response[0]["postId"]
            except (KeyError, IndexError, TypeError):
                pass
            fired_count += 1
            time.sleep(1)
        except requests.HTTPError as e:
            log.error(
                "[%s] firing '%s' failed: %s — will retry next cycle",
                channel_name, v.get("title", v["filename"]), e,
            )
            return fired_count  # stop, retry next loop
    return fired_count


def discover_and_upload(
    s: Settings, channel_dir: Path, state: dict, config: dict, channel_name: str,
) -> int:
    """Find new videos in source folder, upload them to Postiz, schedule a slot,
    and move the file to posted/. Returns count uploaded."""
    source_folder = config.get("source_folder")
    inbox = Path(source_folder).expanduser() if source_folder else channel_dir / "inbox"
    posted = channel_dir / "posted"
    inbox.mkdir(parents=True, exist_ok=True)
    posted.mkdir(parents=True, exist_ok=True)
    move_after_post = bool(config.get("move_after_post", True))

    videos = find_videos(inbox)
    if not videos:
        return 0

    seen_filenames = {v["filename"] for v in state["videos"]}
    yt = config.get("youtube", {})
    title_template = yt.get("title_template", "{smart_title}")
    default_description = yt.get("description", "")
    default_tags = list(yt.get("tags", []) or [])
    uploaded = 0

    for video in videos:
        if video.name in seen_filenames:
            continue
        if not is_file_stable(video):
            log.info("[%s] %s still being written, skipping this cycle",
                     channel_name, video.name)
            continue

        smart = extract_smart_title(video)
        sidecar = load_sidecar(video) or {}
        title = sidecar.get("title") or title_from_template(title_template, video, smart)
        description = sidecar.get("description", default_description)
        tags = sidecar.get("tags", default_tags)

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
            "tags": tags,
            "scheduled_for": slot.astimezone(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            "fired": False,
            "media": {"id": media["id"], "path": media["path"]},
            "post_id": None,
            "fired_at": None,
        })
        seen_filenames.add(video.name)
        uploaded += 1
        save_state(channel_dir, state)
        time.sleep(1)
    return uploaded


def process_channel(
    s: Settings, channel_dir: Path, integrations: dict[str, dict]
) -> None:
    config_path = channel_dir / "config.yaml"
    if not config_path.exists():
        return
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        log.error("[%s] config.yaml invalid: %s", channel_dir.name, e)
        return

    integ_name = (config.get("integration_name") or "").lower().strip()
    if not integ_name:
        log.error("[%s] config.yaml missing 'integration_name'", channel_dir.name)
        return
    integ = integrations.get(integ_name)
    if not integ:
        log.error(
            "[%s] no Postiz channel %r found. Connected: %s",
            channel_dir.name, integ_name, list(integrations.keys()),
        )
        return

    state = load_state(channel_dir)

    rebalanced = rebalance_overdue(state, config, channel_dir.name)
    if rebalanced:
        save_state(channel_dir, state)

    fired = fire_due_slots(s, state, config, integ, channel_dir.name)
    if fired:
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
    log.info("watcher v2 starting; channels dir: %s", s.channels_dir)
    log.info("API: %s", s.api_url)
    while True:
        try:
            integrations = fetch_integrations(s)
        except requests.RequestException as e:
            log.error("Could not reach Postiz API: %s — retrying in %ds", e, POLL_SECONDS)
            time.sleep(POLL_SECONDS)
            continue

        channels = discover_channels(s.channels_dir)
        if not channels:
            log.info("No channel folders found yet (drop one into %s).", s.channels_dir)
        for channel_dir in channels:
            try:
                process_channel(s, channel_dir, integrations)
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
