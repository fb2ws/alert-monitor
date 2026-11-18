#!/usr/bin/env python3
"""Detect new YouTube channel uploads, download tiny audio files, and email them.

Designed for authorized personal use with public YouTube videos. The feed is
used for detection; yt-dlp downloads only the specific unseen video.
"""
from __future__ import annotations

import email.utils
import html
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

RSS_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
CHANNELS = {
    "HKLocalTV": {
        "id": "UCzuzpkqcLSDmqnVpkksriow",
        "url": "https://www.youtube.com/@HKLocalTV/videos",
    },
    "Qiage": {
        "id": "UCwWgaLuFZBRB-dQQP7pxnbA",
        "url": "https://www.youtube.com/@qiage/videos",
    },
    "memeHongKong": {
        "id": "UCu_YquoQYKR3GpP82TO-zRw",
        "url": "https://www.youtube.com/@memehongkong/videos",
        "title_contains": "蕭若元",
    },
    "WhirlingCloudsValley": {
        "id": "UCCVFgDdTfiGxQ695BxMBMUg",
        "url": "https://www.youtube.com/@WhirlingCloudsValley/videos",
        "title_excludes": "合集",
    },
}
STATE_PATH = Path(os.environ.get("YOUTUBE_STATE_FILE", "youtube_state.json"))
BOOTSTRAP_ALERT = os.environ.get("YOUTUBE_BOOTSTRAP_ALERT", "false").lower() == "true"
MAX_NEW_VIDEOS_PER_RUN = int(os.environ.get("YOUTUBE_MAX_NEW_PER_RUN", "3"))
DISABLED_CHANNELS = {
    value.strip().lstrip("@").casefold()
    for value in re.split(r"[\n,]", os.environ.get("YOUTUBE_DISABLED_CHANNELS", ""))
    if value.strip()
}
RESET_CHANNELS = {
    value.strip().lstrip("@").casefold()
    for value in re.split(r"[\n,]", os.environ.get("YOUTUBE_RESET_CHANNELS", ""))
    if value.strip()
}
MAX_HISTORY = 100
MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 15 * 60
USER_AGENT = "Mozilla/5.0 (compatible; PersonalYouTubeAudioMonitor/1.0)"


def log(message: str) -> None:
    print(f"[YOUTUBE] {message}", flush=True)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"channels": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        state.setdefault("channels", {})
        log(f"state loaded: {STATE_PATH} ({len(state['channels'])} channel entries)")
        return state
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Cannot load {STATE_PATH}: {exc}") from exc


def save_state(state: dict) -> None:
    temporary = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def fetch_feed(channel_id: str) -> list[dict]:
    url = RSS_TEMPLATE.format(channel_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"RSS request failed: {exc}") from exc

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError(f"RSS XML was invalid: {exc}") from exc

    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    entries = []
    for entry in root.findall(f"{atom}entry"):
        video_id = (entry.findtext(f"{yt}videoId") or "").strip()
        title = html.unescape((entry.findtext(f"{atom}title") or "").strip())
        published = (entry.findtext(f"{atom}published") or "").strip()
        updated = (entry.findtext(f"{atom}updated") or "").strip()
        link = ""
        for link_node in entry.findall(f"{atom}link"):
            if link_node.attrib.get("rel", "alternate") == "alternate":
                link = link_node.attrib.get("href", "")
                break
        if not link and video_id:
            link = f"https://www.youtube.com/watch?v={video_id}"
        if video_id and title and link:
            entries.append({"id": video_id, "title": title, "published": published, "updated": updated, "url": link})
    return entries


def safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")
    return (name[:120] or "youtube-audio")


def run_command(args: list[str], timeout: int) -> None:
    log("Running: " + " ".join(args[:3]) + (" ..." if len(args) > 3 else ""))
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout}s: {args[0]}") from exc
    if result.returncode != 0:
        tail = (result.stdout or "")[-2000:]
        raise RuntimeError(f"{args[0]} failed with exit code {result.returncode}: {tail}")


def create_audio(video: dict, workdir: Path) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed")

    source_template = str(workdir / "source.%(ext)s")
    run_command(
        [
            "yt-dlp",
            "--no-playlist",
            "--no-part",
            "--no-write-info-json",
            "--no-write-thumbnail",
            "--restrict-filenames",
            # Keep the already-compressed YouTube audio stream. Prefer M4A for
            # compatibility, then WebM. This avoids a full FFmpeg install.
            "--format", "worstaudio[ext=m4a]/worstaudio[ext=webm]/worstaudio",
            "--output", source_template,
            video["url"],
        ],
        DOWNLOAD_TIMEOUT_SECONDS,
    )
    sources = [p for p in workdir.glob("source.*") if p.is_file() and p.suffix not in {".part", ".ytdl"}]
    if len(sources) != 1:
        raise RuntimeError(f"expected one downloaded source, found {len(sources)}")

    output = sources[0]
    if output.stat().st_size == 0:
        raise RuntimeError("yt-dlp produced an empty audio file")
    if output.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise RuntimeError(f"audio is {output.stat().st_size} bytes, above the 24 MB safety limit")
    log(f"downloaded: {video['id']} | {output.suffix.lstrip('.').upper()} | {output.stat().st_size:,} bytes")
    return output


def send_email(channel_name: str, video: dict, audio_path: Path) -> None:
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    recipient = os.environ.get("RECEIVER_EMAIL")
    if not all([user, password, recipient]):
        raise RuntimeError("EMAIL_USER, EMAIL_PASS, and RECEIVER_EMAIL are required")

    message = MIMEMultipart()
    message["Subject"] = f"YouTube Audio: {video['title']}"
    message["From"] = user
    message["To"] = recipient
    message["X-Alert-Monitor"] = "youtube-audio"

    published = video.get("published") or "Unknown"
    body = (
        f"Channel: {channel_name}\n"
        f"Video: {video['url']}\n"
        f"Video ID: {video['id']}\n"
        f"Published: {published}\n\n"
        "The lowest-quality audio-only file is attached.\n"
        f"Format: {audio_path.suffix.lstrip('.').upper()}\n"
    )
    message.attach(MIMEText(body, "plain", "utf-8"))
    with audio_path.open("rb") as audio_file:
        subtype = {".m4a": "mp4", ".webm": "webm"}.get(audio_path.suffix.lower(), "octet-stream")
        attachment = MIMEApplication(audio_file.read(), _subtype=subtype)
    attachment.add_header("Content-Disposition", "attachment", filename=audio_path.name)
    message.attach(attachment)

    log(f"sending email: {channel_name} | {video['id']} | {audio_path.name}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [recipient], message.as_string())
    log(f"email accepted: {channel_name} | {video['id']}")


def process_channel(state: dict, channel_name: str, config: dict) -> None:
    feed_items = fetch_feed(config["id"])
    if not feed_items:
        raise RuntimeError("channel RSS feed contained no videos")

    channel_state = state["channels"].setdefault(channel_name, {"seen_ids": []})
    if channel_name.casefold() in RESET_CHANNELS:
        channel_state["seen_ids"] = []
        log(f"{channel_name}: one-time reset requested; existing IDs will be re-evaluated")
    seen = set(channel_state.get("seen_ids", []))
    initializing = not seen
    title_filter = config.get("title_contains", "").strip()
    title_exclude = config.get("title_excludes", "").strip()
    matching = [
        item for item in feed_items
        if (not title_filter or title_filter.casefold() in item["title"].casefold())
        and (not title_exclude or title_exclude.casefold() not in item["title"].casefold())
    ]
    skipped = [item for item in feed_items if item not in matching]
    if title_filter:
        log(f"{channel_name}: title filter={title_filter!r} | matched={len(matching)} | skipped={len(skipped)}")
    elif title_exclude:
        log(f"{channel_name}: title exclusion={title_exclude!r} | matched={len(matching)} | skipped={len(skipped)}")
        # Remember non-matching uploads so they are not reconsidered and
        # reported as skipped on every run. They never trigger an email.
        skipped_new_ids = {item["id"] for item in skipped if item["id"] not in seen}
        if skipped_new_ids:
            seen.update(skipped_new_ids)
            channel_state["seen_ids"] = list(seen)[-MAX_HISTORY:]
            channel_state["skipped_ids_count"] = channel_state.get("skipped_ids_count", 0) + len(skipped_new_ids)
            save_state(state)

    if initializing and not BOOTSTRAP_ALERT:
        # First run is a baseline for every upload, including filtered-out
        # videos. Do not email historical matches.
        seen.update(item["id"] for item in feed_items)
        channel_state["seen_ids"] = list(seen)[-MAX_HISTORY:]
        channel_state["latest_id"] = feed_items[0]["id"]
        channel_state["latest_title"] = feed_items[0]["title"]
        channel_state["status"] = f"initialized with {len(feed_items)} existing item(s); no email sent"
        save_state(state)
        log(f"{channel_name}: {channel_state['status']}")
        return
    log(
        f"{channel_name}: feed={len(feed_items)} | latest={feed_items[0]['id']} "
        f"| {feed_items[0]['title']} | seen={len(seen)}"
    )
    if not seen and BOOTSTRAP_ALERT:
        # Explicit test/opt-in mode: process the newest item first.
        unseen = feed_items[:MAX_NEW_VIDEOS_PER_RUN]
    else:
        # Normal mode processes a backlog oldest-first so delivery order is
        # chronological when more than one upload is waiting.
        unseen = [item for item in reversed(matching) if item["id"] not in seen]

    if not unseen:
        channel_state["status"] = "no new videos"
        log(f"{channel_name}: no new videos")
        return

    log(f"{channel_name}: queued={len(unseen)}; this run will process up to {MAX_NEW_VIDEOS_PER_RUN}")
    processed = 0
    for video in unseen[:MAX_NEW_VIDEOS_PER_RUN]:
        log(f"{channel_name}: processing {video['id']} | {video['title']}")
        try:
            with tempfile.TemporaryDirectory(prefix="youtube-audio-") as temporary_dir:
                audio_path = create_audio(video, Path(temporary_dir))
                audio_bytes = audio_path.stat().st_size
                send_email(channel_name, video, audio_path)
        except Exception as exc:
            log(f"{channel_name}: FAILED {video['id']} | retry will remain enabled | {exc}")
            raise RuntimeError(f"{channel_name} video {video['id']} failed: {exc}") from exc

        # Checkpoint after every successful delivery. If a later video fails,
        # earlier successful emails are not sent again on the next run.
        seen.add(video["id"])
        processed += 1
        channel_state["seen_ids"] = list(seen)[-MAX_HISTORY:]
        channel_state["latest_id"] = video["id"]
        channel_state["latest_title"] = video["title"]
        channel_state["latest_url"] = video["url"]
        channel_state["latest_bytes"] = audio_bytes
        channel_state["status"] = f"delivered {processed} video(s) this run"
        save_state(state)
        log(f"{channel_name}: delivered and checkpointed {video['id']}")

    channel_state["seen_ids"] = list(seen)[-MAX_HISTORY:]
    channel_state["status"] = f"sent {processed} new audio email(s)"


def main() -> int:
    state = load_state()
    log(
        f"run started: channels={','.join(CHANNELS)} | "
        f"disabled={','.join(sorted(DISABLED_CHANNELS)) or 'none'} | "
        f"reset={','.join(sorted(RESET_CHANNELS)) or 'none'} | "
        f"bootstrap={BOOTSTRAP_ALERT} | max_per_channel={MAX_NEW_VIDEOS_PER_RUN}"
    )
    failures = []
    for channel_name, config in CHANNELS.items():
        if channel_name.casefold() in DISABLED_CHANNELS:
            state["channels"].setdefault(channel_name, {})["status"] = "disabled by YOUTUBE_DISABLED_CHANNELS"
            log(f"{channel_name}: disabled; feed not checked and no email sent")
            continue
        try:
            process_channel(state, channel_name, config)
        except Exception as exc:
            failures.append(f"{channel_name}: {type(exc).__name__}: {exc}")
            state["channels"].setdefault(channel_name, {})["status"] = f"ERROR: {type(exc).__name__}: {exc}"
            log(f"{channel_name}: ERROR: {exc}")
    save_state(state)
    log("run summary:")
    for channel_name in CHANNELS:
        channel_state = state["channels"].get(channel_name, {})
        log(f"  {channel_name}: {channel_state.get('status', 'not processed')}")
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
