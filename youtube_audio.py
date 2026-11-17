#!/usr/bin/env python3
"""Detect new YouTube channel uploads, create tiny MP3s, and email them.

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
}
STATE_PATH = Path(os.environ.get("YOUTUBE_STATE_FILE", "youtube_state.json"))
BOOTSTRAP_ALERT = os.environ.get("YOUTUBE_BOOTSTRAP_ALERT", "false").lower() == "true"
MAX_NEW_VIDEOS_PER_RUN = int(os.environ.get("YOUTUBE_MAX_NEW_PER_RUN", "3"))
DISABLED_CHANNELS = {
    value.strip().casefold()
    for value in re.split(r"[\n,]", os.environ.get("YOUTUBE_DISABLED_CHANNELS", ""))
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
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")

    source_template = str(workdir / "source.%(ext)s")
    run_command(
        [
            "yt-dlp",
            "--no-playlist",
            "--no-part",
            "--no-write-info-json",
            "--no-write-thumbnail",
            "--restrict-filenames",
            "--format", "bestaudio/best",
            "--output", source_template,
            video["url"],
        ],
        DOWNLOAD_TIMEOUT_SECONDS,
    )
    sources = [p for p in workdir.glob("source.*") if p.is_file() and p.suffix not in {".part", ".ytdl"}]
    if len(sources) != 1:
        raise RuntimeError(f"expected one downloaded source, found {len(sources)}")

    output = workdir / f"{safe_filename(video['title'])}.mp3"
    # Speech-oriented, intentionally tiny output. 32 kbps mono keeps typical
    # sub-30-minute episodes far below Gmail's 25 MB attachment limit.
    run_command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(sources[0]), "-vn", "-ac", "1", "-ar", "22050",
            "-codec:a", "libmp3lame", "-b:a", "32k", str(output),
        ],
        10 * 60,
    )
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg produced no audio file")
    if output.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise RuntimeError(f"audio is {output.stat().st_size} bytes, above the 24 MB safety limit")
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
        "The compressed audio file is attached.\n"
        "Format: MP3, mono, 32 kbps\n"
    )
    message.attach(MIMEText(body, "plain", "utf-8"))
    with audio_path.open("rb") as audio_file:
        attachment = MIMEApplication(audio_file.read(), _subtype="mpeg")
    attachment.add_header("Content-Disposition", "attachment", filename=audio_path.name)
    message.attach(attachment)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [recipient], message.as_string())


def process_channel(state: dict, channel_name: str, config: dict) -> None:
    feed_items = fetch_feed(config["id"])
    if not feed_items:
        raise RuntimeError("channel RSS feed contained no videos")

    channel_state = state["channels"].setdefault(channel_name, {"seen_ids": []})
    seen = set(channel_state.get("seen_ids", []))
    unseen = [item for item in reversed(feed_items) if item["id"] not in seen]

    if not seen and not BOOTSTRAP_ALERT:
        channel_state["seen_ids"] = [item["id"] for item in feed_items[:MAX_HISTORY]]
        channel_state["latest_id"] = feed_items[0]["id"]
        channel_state["latest_title"] = feed_items[0]["title"]
        channel_state["status"] = f"initialized with {len(feed_items)} existing item(s); no email sent"
        log(f"{channel_name}: {channel_state['status']}")
        return

    if not unseen:
        channel_state["status"] = "no new videos"
        log(f"{channel_name}: no new videos")
        return

    processed = 0
    for video in unseen[:MAX_NEW_VIDEOS_PER_RUN]:
        with tempfile.TemporaryDirectory(prefix="youtube-audio-") as temporary_dir:
            audio_path = create_audio(video, Path(temporary_dir))
            audio_bytes = audio_path.stat().st_size
            send_email(channel_name, video, audio_path)
        seen.add(video["id"])
        processed += 1
        channel_state["latest_id"] = video["id"]
        channel_state["latest_title"] = video["title"]
        channel_state["latest_url"] = video["url"]
        channel_state["latest_bytes"] = audio_bytes
        log(f"{channel_name}: email sent for {video['title']}")

    channel_state["seen_ids"] = list(seen)[-MAX_HISTORY:]
    channel_state["status"] = f"sent {processed} new audio email(s)"


def main() -> int:
    state = load_state()
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
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
