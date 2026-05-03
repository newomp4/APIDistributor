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
import random
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

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None

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


def post_to_postbridge(
    s: Settings,
    social_account_id: int,
    media_id: str,
    caption: str,
    title: str,
    scheduled_at: datetime | None = None,
) -> dict:
    """Send a post to Post Bridge. If `scheduled_at` is None, posts immediately
    (type=now); otherwise schedules at that UTC datetime."""
    payload: dict = {
        "caption": caption,
        "social_accounts": [int(social_account_id)],
        "media": [media_id],
        "platform_configurations": {"youtube": {"title": title}},
    }
    if scheduled_at is not None:
        payload["scheduled_at"] = (
            scheduled_at.astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000Z")
        )
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


# Backwards-compatible alias
def post_now(s, social_account_id, media_id, caption, title):
    return post_to_postbridge(s, social_account_id, media_id, caption, title, None)


def fetch_post_results(s: Settings, post_id: str) -> list[dict]:
    """Returns the [{success, error, platform_data}, ...] entries for a post."""
    try:
        r = requests.get(
            f"{s.api_url}/post-results",
            headers=auth_headers(s),
            params={"post_id": post_id},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        return payload.get("data", []) if isinstance(payload, dict) else (payload or [])
    except requests.RequestException:
        return []


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


def channel_age_days(state: dict, target_day, tz: ZoneInfo) -> int:
    """How many days the channel has been live as of `target_day`. Determined
    by the earliest scheduled video; returns 0 if none yet."""
    earliest = None
    for v in state.get("videos", []):
        try:
            d = datetime.fromisoformat(v["scheduled_for"].replace("Z", "+00:00")).astimezone(tz).date()
            if earliest is None or d < earliest:
                earliest = d
        except (KeyError, ValueError):
            continue
    if earliest is None:
        return 0
    return max(0, (target_day - earliest).days)


def per_day_for(config: dict, age_days: int) -> int:
    """Number of slots/day active given the channel's age, respecting warmup.
    Falls back to the full `times` list if no warmup is configured."""
    times = config.get("schedule", {}).get("times", [])
    full = len(times) if times else 0
    warmup = config.get("warmup") or {}
    ramp = warmup.get("ramp") or []
    if not ramp or not full:
        return full
    active = full
    for step in sorted(ramp, key=lambda s: int(s.get("after_days", 0))):
        if age_days >= int(step.get("after_days", 0)):
            active = int(step.get("per_day", full))
    return max(0, min(active, full))


def next_free_slot(
    config: dict, after: datetime, used: set[datetime],
    state: dict | None = None,
) -> datetime:
    """Find the next free schedule slot strictly after `after`. Honors:
      - schedule.times / days / timezone
      - warmup.ramp (if state given): caps per_day for early days
      - schedule.jitter_minutes: randomizes the actual time ±N min around the base
    """
    sched = config.get("schedule", {})
    times = sched.get("times", ["12:00"])
    days_allowed = set(sched.get("days", DAY_NAMES))
    tz = get_schedule_tz(config)
    after_local = after.astimezone(tz)
    used_local = {dt.astimezone(tz).replace(microsecond=0) for dt in used}
    try:
        jitter = max(0, int(sched.get("jitter_minutes", 0)))
    except (TypeError, ValueError):
        jitter = 0
    # Tolerance for "is this base slot taken?" — wider when jitter could move
    # neighboring slots toward each other.
    used_tolerance_sec = max(60, jitter * 30)

    for offset in range(0, 365):
        candidate = after_local.date() + timedelta(days=offset)
        if DAY_NAMES[candidate.weekday()] not in days_allowed:
            continue
        per_day = per_day_for(config, channel_age_days(state or {}, candidate, tz))
        active_times = times[:per_day] if per_day > 0 else times
        for tstr in active_times:
            try:
                hh, mm = parse_time_of_day(tstr)
            except ValueError:
                continue
            base = datetime(
                candidate.year, candidate.month, candidate.day,
                hh, mm, tzinfo=tz,
            )
            if base <= after_local + timedelta(minutes=2):
                continue
            if any(abs((base - u).total_seconds()) < used_tolerance_sec for u in used_local):
                continue
            if jitter > 0:
                base = base + timedelta(minutes=random.randint(-jitter, jitter))
            return base
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
    return next_free_slot(config, after=now, used=used, state=state)


def cancel_postbridge_post(s: Settings, post_id: str) -> bool:
    """Best-effort DELETE of a scheduled post in Post Bridge.
    Returns True if the post is gone (deleted or already 404), False otherwise."""
    try:
        r = requests.delete(
            f"{s.api_url}/posts/{post_id}",
            headers=auth_headers(s),
            timeout=15,
        )
        return r.ok or r.status_code == 404
    except requests.RequestException as e:
        log.warning("Could not cancel Post Bridge post %s: %s", post_id, e)
        return False


def unschedule_video(s: Settings | None, video: dict) -> None:
    """Reset a video's submission state so it can be re-submitted at a new
    time. Cancels any existing Post Bridge post first if we have an `s`."""
    post_id = video.get("post_id")
    if post_id and s is not None:
        cancel_postbridge_post(s, post_id)
    video["fired"] = False
    video["prescheduled"] = False
    for k in ("post_id", "fired_at"):
        video.pop(k, None)


def reschedule_all_queued(state: dict, config: dict, s: Settings | None = None) -> int:
    """Repack every unfired video onto the current schedule, starting from now.
    Preserves the original order (by previous scheduled_for). Used when the
    user changes times-per-day or days-of-week and wants the existing queue
    to follow the new schedule."""
    tz = get_schedule_tz(config)
    now = datetime.now(tz)
    fired_slots: set[datetime] = set()
    for v in state["videos"]:
        if v.get("fired"):
            try:
                fired_slots.add(parse_iso(v["scheduled_for"], tz))
            except (KeyError, ValueError):
                pass

    # Treat any video whose state shows it's been submitted to Post Bridge
    # but isn't yet published as eligible to be rescheduled. (Truly published
    # ones have published_url and we leave them alone.)
    queued_or_prescheduled = [
        v for v in state["videos"]
        if not v.get("published_url")
    ]
    queued_or_prescheduled.sort(key=lambda v: v.get("scheduled_for", ""))

    used = set(fired_slots)
    rebalanced = 0
    for v in queued_or_prescheduled:
        new_slot = next_free_slot(config, after=now, used=used, state=state)
        new_iso = new_slot.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if v.get("scheduled_for") != new_iso:
            unschedule_video(s, v)
            v["scheduled_for"] = new_iso
            rebalanced += 1
        used.add(new_slot)
    return rebalanced


def add_bonus_slots_today(
    state: dict, config: dict, times: list[str], s: Settings | None = None,
) -> int:
    """Take the earliest queued (un-published) videos and reschedule them onto
    extra slots today at the given times. Used for "post more today" cases.
    Cancels Post Bridge pre-scheduled posts so the watcher re-submits cleanly."""
    tz = get_schedule_tz(config)
    today = datetime.now(tz).date()
    now = datetime.now(tz)

    new_slots: list[datetime] = []
    for t in times:
        try:
            hh, mm = parse_time_of_day(t)
        except (ValueError, AttributeError):
            continue
        slot = datetime(today.year, today.month, today.day, hh, mm, tzinfo=tz)
        if slot <= now + timedelta(minutes=2):
            continue  # only future slots today
        new_slots.append(slot)
    new_slots.sort()
    if not new_slots:
        return 0

    used = set()
    for v in state["videos"]:
        if v.get("published_url"):
            continue
        try:
            used.add(parse_iso(v["scheduled_for"], tz))
        except (KeyError, ValueError):
            pass

    queued = [v for v in state["videos"] if not v.get("published_url")]
    queued.sort(key=lambda v: v.get("scheduled_for", ""))

    moved = 0
    for slot in new_slots:
        if slot.replace(microsecond=0) in {u.replace(microsecond=0) for u in used}:
            continue  # already filled
        # Find the next queued video that's NOT already at-or-before this slot
        target = None
        for v in queued:
            if v.get("_bonus_assigned"):
                continue
            try:
                v_slot = parse_iso(v["scheduled_for"], tz)
            except (KeyError, ValueError):
                continue
            if v_slot <= slot:
                continue  # already firing earlier today, no need to move
            target = v
            break
        if target is None:
            break
        target["_bonus_assigned"] = True
        unschedule_video(s, target)
        target["scheduled_for"] = slot.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        used.add(slot)
        moved += 1

    for v in state["videos"]:
        v.pop("_bonus_assigned", None)
    return moved


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


def pick_variant(variants: list[str] | None, fallback: str = "") -> str:
    """Return a random non-empty variant if any are present, else fallback."""
    if not variants:
        return fallback
    cleaned = [v.strip() for v in variants if isinstance(v, str) and v.strip()]
    if not cleaned:
        return fallback
    return random.choice(cleaned)


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


def build_caption(
    config: dict, sidecar_description: str | None,
) -> tuple[str, dict]:
    """Compose the YouTube description.

    If `description_variants` / `pinned_message_variants` are set in the
    youtube config, picks one at random; otherwise falls back to the single
    `description` / `pinned_message`. Sidecar always wins for the body.

    Returns (caption_text, choice_metadata) so we can record which variant
    was used in _state.json.
    """
    yt = config.get("youtube", {})
    chose: dict = {}

    if sidecar_description is not None:
        body = sidecar_description.strip()
    elif yt.get("description_variants"):
        body = pick_variant(yt["description_variants"], yt.get("description", ""))
        if body:
            try:
                chose["description_variant_index"] = list(yt["description_variants"]).index(body)
            except ValueError:
                pass
    else:
        body = yt.get("description", "").strip()

    if yt.get("pinned_message_variants"):
        pinned = pick_variant(yt["pinned_message_variants"], yt.get("pinned_message", ""))
        if pinned:
            try:
                chose["pinned_variant_index"] = list(yt["pinned_message_variants"]).index(pinned)
            except ValueError:
                pass
    else:
        pinned = yt.get("pinned_message", "").strip()

    pinned = pinned.strip()
    body = body.strip()
    if pinned and body:
        return f"{pinned}\n\n{body}", chose
    return (pinned or body), chose


def build_title(
    config: dict, file_path: Path, smart: str, sidecar_title: str | None,
) -> tuple[str, dict]:
    """Pick a title — sidecar > variants > template. Returns (title, choice)."""
    yt = config.get("youtube", {})
    chose: dict = {}
    if sidecar_title:
        return sidecar_title, chose
    variants = yt.get("title_template_variants")
    if variants:
        chosen_template = pick_variant(variants, yt.get("title_template", "{smart_title}"))
        title = title_from_template(chosen_template, file_path, smart)
        try:
            chose["title_template_variant_index"] = list(variants).index(chosen_template)
        except ValueError:
            pass
        return title, chose
    return title_from_template(yt.get("title_template", "{smart_title}"), file_path, smart), chose


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
        new_slot = next_free_slot(config, after=now, used=used, state=state)
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
        title, title_choice = build_title(config, video, smart, sidecar.get("title"))
        description, desc_choice = build_caption(config, sidecar.get("description"))
        variant_choices = {**title_choice, **desc_choice}

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
            "variants_used": variant_choices,
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


def preschedule_upcoming(
    s: Settings, state: dict, config: dict,
    account: dict, channel_name: str,
) -> int:
    """For each unfired video whose slot falls within the prescheduling window,
    send it to Post Bridge with scheduled_at=slot_time. Once submitted, Post
    Bridge fires the upload from their cloud at the exact time — your Mac can
    be asleep or off during the window without missing posts.

    Set `prescheduling_window_hours: 0` in config to disable.
    """
    window_hours = config.get("prescheduling_window_hours", 8)
    try:
        window = timedelta(hours=float(window_hours))
    except (TypeError, ValueError):
        window = timedelta(hours=8)
    if window.total_seconds() <= 0:
        return 0

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc + window
    submitted = 0
    for v in state["videos"]:
        if v.get("fired") or v.get("post_id"):
            continue
        try:
            slot_utc = parse_iso(v["scheduled_for"], get_schedule_tz(config)).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        if slot_utc > cutoff:
            continue  # too far in future, wait
        if slot_utc < now_utc - timedelta(minutes=1):
            continue  # already past, fire_due_slots / rebalance handle it
        media = v.get("media") or {}
        media_id = media.get("id")
        if not media_id:
            continue
        try:
            log.info(
                "[%s] pre-scheduling '%s' for %s (in %s)",
                channel_name, v["title"], slot_utc.isoformat(),
                slot_utc - now_utc,
            )
            response = post_to_postbridge(
                s,
                social_account_id=int(account["id"]),
                media_id=media_id,
                caption=v.get("description", ""),
                title=v["title"],
                scheduled_at=slot_utc,
            )
            v["fired"] = True
            v["prescheduled"] = True
            v["fired_at"] = now_utc.isoformat().replace("+00:00", "Z")
            try:
                v["post_id"] = response.get("id") or response.get("data", {}).get("id")
            except Exception:
                pass
            submitted += 1
            time.sleep(1)
        except requests.HTTPError as e:
            log.error(
                "[%s] pre-schedule failed for '%s': %s",
                channel_name, v.get("title", "?"), e,
            )
            return submitted
    return submitted


def cleanup_published(
    s: Settings, state: dict, config: dict, channel_dir: Path, channel_name: str,
) -> int:
    """For each fired video that hasn't been confirmed published yet, ask
    Post Bridge for the post-result. If success: capture the YouTube URL and
    apply the cleanup_after_publish policy to the source file.

    Policies (config.cleanup_after_publish):
      - 'archive' (default): move to channels/<name>/archive/
      - 'trash': move to macOS Trash (recoverable)
      - 'delete': permanent delete
      - 'none' / 'keep': leave the file in posted/
    """
    policy = (config.get("cleanup_after_publish") or "archive").strip().lower()
    cleaned = 0
    for v in state["videos"]:
        if not v.get("fired"):
            continue
        if v.get("published_url") or v.get("publish_failed"):
            continue
        post_id = v.get("post_id")
        if not post_id:
            continue
        results = fetch_post_results(s, post_id)
        if not results:
            continue
        result = results[0]
        if not result.get("success"):
            err = result.get("error")
            if err:
                v["publish_failed"] = err
                log.warning("[%s] publish failed for '%s': %s",
                            channel_name, v.get("title", "?"), err)
                cleaned += 1
            continue

        platform_url = (result.get("platform_data") or {}).get("url")
        v["published_url"] = platform_url
        v["result_id"] = result.get("id")

        if policy in ("none", "keep"):
            cleaned += 1
            continue

        posted_path = channel_dir / "posted" / v["filename"]
        if not posted_path.exists():
            cleaned += 1
            continue

        try:
            if policy == "trash":
                if send2trash is None:
                    log.warning(
                        "[%s] cleanup_after_publish=trash but send2trash isn't "
                        "installed; archiving instead", channel_name,
                    )
                    archive = channel_dir / "archive"
                    archive.mkdir(exist_ok=True)
                    shutil.move(str(posted_path), str(archive / v["filename"]))
                else:
                    send2trash(str(posted_path))
            elif policy == "delete":
                posted_path.unlink()
            else:  # archive (default)
                archive = channel_dir / "archive"
                archive.mkdir(exist_ok=True)
                target = archive / v["filename"]
                if target.exists():
                    target = archive / f"{int(time.time())}_{v['filename']}"
                shutil.move(str(posted_path), str(target))
            log.info("[%s] published '%s' -> %s; %s file",
                     channel_name, v.get("title", "?"),
                     platform_url, policy)
        except Exception as e:
            log.exception("[%s] cleanup of %s failed: %s",
                          channel_name, v["filename"], e)
        cleaned += 1
    return cleaned


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

    # Pre-schedule rolling window FIRST so Post Bridge has them queued; then
    # fire any that are due RIGHT NOW (catches up if watcher was offline).
    if preschedule_upcoming(s, state, config, account, channel_dir.name):
        save_state(channel_dir, state)

    if fire_due_slots(s, state, config, account, channel_dir.name):
        save_state(channel_dir, state)

    if cleanup_published(s, state, config, channel_dir, channel_dir.name):
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
