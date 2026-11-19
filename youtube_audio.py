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
import random
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time
import unicodedata
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
    "LionInvestment": {
        "id": "UCgyTp-zlMz6D-6BSB9yB62A",
        "url": "https://www.youtube.com/@Lion哥的投資世界/videos",
        "title_excludes": ["中文字幕", "有片", "廣東話"],
    },
    "hkpeanutscom": {
        "id": "UCsaaqRi_Y0a1-MmdHG7FypQ",
        "url": "https://www.youtube.com/@hkpeanutscom/streams",
        "title_contains": "大君伐",
    },
}
CHANNEL_SUBJECT_PREFIXES = {
    "HKLocalTV": "神駒",
    "Qiage": "港燦",
    "memeHongKong": "燈神",
    "WhirlingCloudsValley": "陶傑",
    "LionInvestment": "Liongor",
    "hkpeanutscom": "花生台",
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
CHANNEL_PAUSE_MIN = max(0.0, float(os.environ.get("YOUTUBE_CHANNEL_PAUSE_MIN", "10")))
CHANNEL_PAUSE_MAX = max(CHANNEL_PAUSE_MIN, float(os.environ.get("YOUTUBE_CHANNEL_PAUSE_MAX", "30")))
METADATA_PAUSE_MIN = max(0.0, float(os.environ.get("YOUTUBE_METADATA_PAUSE_MIN", "3")))
METADATA_PAUSE_MAX = max(METADATA_PAUSE_MIN, float(os.environ.get("YOUTUBE_METADATA_PAUSE_MAX", "10")))


def log(message: str) -> None:
    print(f"[YOUTUBE] {message}", flush=True)


def random_pause(label: str, minimum: float, maximum: float) -> None:
    """Throttle sequential requests without retrying or parallelizing them."""
    if maximum <= 0:
        return
    seconds = random.uniform(minimum, maximum)
    log(f"pause {seconds:.1f}s before {label}")
    time.sleep(seconds)


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


def normalized_match_text(value: str) -> str:
    """Normalize compatibility characters before applying title filters."""
    return unicodedata.normalize("NFKC", value).casefold()


def run_command(args: list[str], timeout: int) -> None:
    log("Running: " + " ".join(args[:3]) + (" ..." if len(args) > 3 else ""))
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout}s: {args[0]}") from exc
    if result.returncode != 0:
        tail = (result.stdout or "")[-2000:]
        raise RuntimeError(f"{args[0]} failed with exit code {result.returncode}: {tail}")


def yt_dlp_prefix() -> list[str]:
    """Return optional authentication arguments without logging cookie data."""
    cookie_file = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if cookie_file:
        path = Path(cookie_file)
        if not path.is_file():
            raise RuntimeError(f"YOUTUBE_COOKIES_FILE does not exist: {cookie_file}")
        if path.stat().st_size == 0:
            raise RuntimeError("YOUTUBE_COOKIES_FILE is empty")
        first_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not any(line.startswith("# Netscape HTTP Cookie File") or line.startswith("# HTTP Cookie File") for line in first_lines[:3]):
            raise RuntimeError("YOUTUBE_COOKIES_FILE is not Netscape/Mozilla cookie-file format")
        return ["--cookies", cookie_file]
    return []


def inspect_video(video: dict) -> dict:
    """Read lightweight yt-dlp metadata before downloading any media."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed")
    random_pause(f"metadata inspection {video['id']}", METADATA_PAUSE_MIN, METADATA_PAUSE_MAX)
    args = [
        "yt-dlp", "--ignore-config", "--js-runtimes", "deno", *yt_dlp_prefix(),
        "--dump-single-json", "--skip-download", "--ignore-no-formats-error",
        "--no-playlist",
        "--no-warnings", video["url"],
    ]
    log(f"inspecting metadata: {video['id']}")
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"metadata inspection timed out: {video['id']}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"metadata inspection failed for {video['id']}: {(result.stdout or '')[-1200:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid metadata for {video['id']}") from exc


def video_skip_reason(metadata: dict) -> str | None:
    """Return a permanent/deferred skip reason, or None if downloadable."""
    # The /shorts/ URL is the reliable signal available from yt-dlp metadata.
    urls = [metadata.get("webpage_url", ""), metadata.get("original_url", "")]
    if any(re.search(r"youtube\.com/shorts/", url, re.IGNORECASE) for url in urls):
        return "short"

    live_status = str(metadata.get("live_status") or "").casefold()
    if live_status == "is_upcoming":
        # Do not add this ID to seen_ids. A later run can process it after the
        # premiere has finished and YouTube reports a completed recording.
        return "premiere"
    if metadata.get("is_live") is True or live_status == "is_live":
        # Do not add this ID to seen_ids. A later run can process the completed
        # recording once YouTube reports live_status=was_live/not_live.
        return "live"
    return None


def is_youtube_temporary_error(error: Exception) -> bool:
    """Recognize temporary YouTube access/status errors that should be retried."""
    text = str(error).casefold().replace("’", "'")
    bot_challenge = (
        "sign in to confirm" in text
        and "not a bot" in text
    )
    scheduled_premiere = "premieres in" in text or "premiere in" in text
    no_player_response = "failed to extract any player response" in text
    return bot_challenge or scheduled_premiere or no_player_response


def create_audio(video: dict, workdir: Path) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")

    source_template = str(workdir / "source.%(ext)s")
    run_command(
        [
            "yt-dlp", "--ignore-config", "--js-runtimes", "deno", *yt_dlp_prefix(),
            "--no-playlist",
            "--no-part",
            "--no-write-info-json",
            "--no-write-thumbnail",
            "--restrict-filenames",
            # Prefer a small audio-only source; SponsorBlock removes marked
            # segments before the later MP3 conversion and email step.
            "--format", "worstaudio[ext=m4a]/worstaudio[ext=webm]/worstaudio",
            "--sponsorblock-remove", "all",
            "--output", source_template,
            video["url"],
        ],
        DOWNLOAD_TIMEOUT_SECONDS,
    )
    sources = [p for p in workdir.glob("source.*") if p.is_file() and p.suffix not in {".part", ".ytdl"}]
    if len(sources) != 1:
        raise RuntimeError(f"expected one downloaded source, found {len(sources)}")

    source = sources[0]
    if source.stat().st_size == 0:
        raise RuntimeError("yt-dlp produced an empty audio file")
    log(f"downloaded source: {video['id']} | {source.suffix.lstrip('.').upper()} | {source.stat().st_size:,} bytes")

    output = workdir / f"{safe_filename(video['title'])}.mp3"
    # Convert to a small, broadly compatible speech-oriented MP3 so Gmail's
    # encoded attachment remains well below its 25 MB message limit.
    run_command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "22050",
            "-codec:a", "libmp3lame", "-b:a", "32k", str(output),
        ],
        10 * 60,
    )
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg produced no audio file")
    if output.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise RuntimeError(f"converted audio is {output.stat().st_size} bytes, above the 24 MB safety limit")
    log(f"converted: {video['id']} | MP3 mono 32k | {output.stat().st_size:,} bytes")
    return output


def send_email(channel_name: str, video: dict, audio_path: Path) -> None:
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    raw_recipients = os.environ.get("RECEIVER_EMAILS", "")
    recipients = [value.strip() for value in re.split(r"[,;\n]+", raw_recipients) if value.strip()]
    if not user or not password or not recipients:
        raise RuntimeError("EMAIL_USER, EMAIL_PASS, and RECEIVER_EMAILS are required for YouTube alerts")

    message = MIMEMultipart()
    subject_prefix = CHANNEL_SUBJECT_PREFIXES.get(channel_name, channel_name)
    message["Subject"] = f"{subject_prefix}: {video['title']}"
    message["From"] = user
    message["To"] = ", ".join(recipients)
    message["X-Alert-Monitor"] = "youtube-audio"

    published = video.get("published") or "Unknown"
    body = (
        f"Channel: {channel_name}\n"
        f"Video: {video['url']}\n"
        f"Video ID: {video['id']}\n"
        f"Published: {published}\n\n"
        "A small speech-oriented audio file is attached.\n"
        "Format: MP3, mono, 32 kbps\n"
    )
    message.attach(MIMEText(body, "plain", "utf-8"))
    with audio_path.open("rb") as audio_file:
        subtype = "mpeg" if audio_path.suffix.lower() == ".mp3" else "octet-stream"
        attachment = MIMEApplication(audio_file.read(), _subtype=subtype)
    attachment.add_header("Content-Disposition", "attachment", filename=audio_path.name)
    message.attach(attachment)

    log(f"sending email: {channel_name} | {video['id']} | {audio_path.name}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, recipients, message.as_string())
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
    configured_excludes = config.get("title_excludes", [])
    if isinstance(configured_excludes, str):
        configured_excludes = [configured_excludes]
    title_excludes = [term.strip() for term in configured_excludes if term.strip()]
    normalized_filter = normalized_match_text(title_filter)
    normalized_excludes = [normalized_match_text(term) for term in title_excludes]
    matching = [
        item for item in feed_items
        if (not normalized_filter or normalized_filter in normalized_match_text(item["title"]))
        and all(term not in normalized_match_text(item["title"]) for term in normalized_excludes)
    ]
    skipped = [item for item in feed_items if item not in matching]
    if title_filter:
        log(f"{channel_name}: title filter={title_filter!r} | matched={len(matching)} | skipped={len(skipped)}")
    elif title_excludes:
        log(f"{channel_name}: title exclusions={title_excludes!r} | matched={len(matching)} | skipped={len(skipped)}")
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
        unseen = [item for item in matching if item["id"] not in seen][:MAX_NEW_VIDEOS_PER_RUN]
    else:
        # Normal mode processes a backlog oldest-first so delivery order is
        # chronological when more than one upload is waiting.
        unseen = [item for item in reversed(matching) if item["id"] not in seen]

    log(f"{channel_name}: matching={len(matching)} | unseen_matching={len(unseen)}")
    if not unseen:
        channel_state["status"] = "no new videos"
        log(f"{channel_name}: no new videos")
        return

    log(f"{channel_name}: queued={len(unseen)}; this run will process up to {MAX_NEW_VIDEOS_PER_RUN}")
    processed = 0
    deferred = 0
    deferred_premieres = 0
    deferred_live = 0
    for video in unseen[:MAX_NEW_VIDEOS_PER_RUN]:
        log(f"{channel_name}: processing {video['id']} | {video['title']}")
        try:
            metadata = inspect_video(video)
            skip_reason = video_skip_reason(metadata)
            if skip_reason == "short":
                seen.add(video["id"])
                channel_state["skipped_shorts_count"] = channel_state.get("skipped_shorts_count", 0) + 1
                channel_state["seen_ids"] = list(seen)[-MAX_HISTORY:]
                channel_state["status"] = f"skipped Short: {video['id']}"
                save_state(state)
                log(f"{channel_name}: skipped Short {video['id']} (recorded permanently)")
                continue
            if skip_reason in {"premiere", "live"}:
                if skip_reason == "premiere":
                    deferred_premieres += 1
                else:
                    deferred_live += 1
                channel_state["status"] = f"deferred {skip_reason}: {video['id']}"
                save_state(state)
                log(f"{channel_name}: deferred {skip_reason} {video['id']} (will retry later)")
                continue
            with tempfile.TemporaryDirectory(prefix="youtube-audio-") as temporary_dir:
                audio_path = create_audio(video, Path(temporary_dir))
                audio_bytes = audio_path.stat().st_size
                send_email(channel_name, video, audio_path)
        except Exception as exc:
            if is_youtube_temporary_error(exc):
                deferred += 1
                channel_state["status"] = f"deferred YouTube bot challenge: {video['id']}"
                save_state(state)
                log(
                    f"{channel_name}: WARNING deferred {video['id']} because YouTube "
                    "returned a temporary access or player-extraction status; "
                    "retry remains enabled; "
                    "workflow will not be marked failed"
                )
                continue
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
    deferred_parts = []
    if deferred_premieres:
        deferred_parts.append(f"deferred {deferred_premieres} premiere(s)")
    if deferred_live:
        deferred_parts.append(f"deferred {deferred_live} live video(s)")
    if deferred:
        deferred_parts.append(f"deferred {deferred} temporary access issue(s)")
    if deferred_parts:
        delivery = f"sent {processed} new audio email(s)" if processed else "no email sent"
        channel_state["status"] = f"{delivery}; {'; '.join(deferred_parts)}"
    else:
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
    channel_items = list(CHANNELS.items())
    for index, (channel_name, config) in enumerate(channel_items):
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
        if index < len(channel_items) - 1:
            random_pause("next channel check", CHANNEL_PAUSE_MIN, CHANNEL_PAUSE_MAX)
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
