import imaplib
import hashlib
import json
import os
import re
import smtplib
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")
# Opt-in only. Set GitHub repository variable CLEAN_SENT_ALERTS=true to remove
# the Sent label from each alert after confirmed SMTP delivery.
CLEAN_SENT_ALERTS = os.environ.get("CLEAN_SENT_ALERTS", "false").lower() == "true"


def load_facebook_pages():
    """Read a deduplicated Facebook URL list from one environment variable.

    FB_PAGES accepts newline-separated or comma-separated URLs. A JSON array
    is accepted too. The old FB_PAGE_1..FB_PAGE_20 variables remain a fallback
    so existing repositories continue working during migration.
    """
    configured = os.environ.get("FB_PAGES", "").strip()
    if configured:
        try:
            decoded = json.loads(configured)
            raw_values = [str(value) for value in decoded] if isinstance(decoded, list) else re.split(r"[\n,]", configured)
        except json.JSONDecodeError:
            raw_values = re.split(r"[\n,]", configured)
    else:
        raw_values = [os.environ.get(f"FB_PAGE_{number}", "") for number in range(1, 21)]

    pages = []
    seen = set()
    for value in raw_values:
        url = value.strip().rstrip("/")
        if not url:
            continue
        if not re.match(r"^https?://(?:www\.)?(?:m\.)?facebook\.com/", url, re.IGNORECASE):
            print(f"[CONFIG] Ignoring invalid Facebook URL: {url}", flush=True)
            continue
        key = url.casefold()
        if key not in seen:
            pages.append(url)
            seen.add(key)
    return pages


FB_PAGES = load_facebook_pages()

OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"
HK_TZ = timezone(timedelta(hours=8))

BLOCK_MARKERS = (
    "captcha",
    "verify you are human",
    "access denied",
    "temporarily blocked",
    "unusual traffic",
    "security check",
    "please enable javascript",
)


# -----------------------------------------------------------------------------
# General utilities
# -----------------------------------------------------------------------------
def get_hk_time():
    """Return an unambiguous Hong Kong local timestamp (UTC+8)."""
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S HK")


def shorten(text, limit=240):
    """Normalize whitespace and return a readable, bounded diagnostic string."""
    return " ".join((text or "").split())[:limit]


def facebook_candidate_fingerprint(text):
    """Return a stable digest for a normalized Facebook candidate."""
    normalized = clean_facebook_post_text(text).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def compare_facebook_candidates(
    previous_text,
    candidate_text,
    recent_candidates=None,
    previous_permalink="",
    candidate_permalink="",
):
    """Explain whether two renderings represent the same Facebook post.

    The permalink is the strongest identity signal. The same post can render
    once as only its first line plus "See more" and later as the full body, so
    text equality alone is not sufficient.
    """
    previous = clean_facebook_post_text(previous_text)
    candidate = clean_facebook_post_text(candidate_text)
    previous_folded = previous.casefold()
    candidate_folded = candidate.casefold()
    ratio = SequenceMatcher(None, previous_folded, candidate_folded).ratio() if previous_folded and candidate_folded else 0.0

    if previous_permalink and candidate_permalink:
        if clean_facebook_permalink(previous_permalink).casefold() == clean_facebook_permalink(candidate_permalink).casefold():
            return True, "same Facebook post permalink", ratio, False

    if not previous_folded:
        return False, "no previous candidate", ratio, False
    if previous_folded == candidate_folded:
        return True, "exact normalized match", ratio, False
    if previous_folded in candidate_folded or candidate_folded in previous_folded:
        return True, "one normalized candidate contains the other", ratio, False

    # The post body is now stored in full instead of being cut at 900
    # characters, so the same post can legitimately differ slightly between
    # runs (a reaction count, an "Edited" tag, a late-loading trailing block).
    # A matching opening plus a comparable length therefore still means the
    # same post, not a new one.
    previous_identity = facebook_body_identity(previous)
    candidate_identity = facebook_body_identity(candidate)
    if previous_identity and previous_identity == candidate_identity:
        shorter, longer = sorted((len(previous_folded), len(candidate_folded)))
        if longer - shorter <= max(120, int(longer * 0.25)):
            return True, "identical post opening with comparable length", ratio, False

    # A collapsed "See more" rendering may contain only a short opening. If
    # no permalink was available, suppress a later expanded rendering when
    # the complete shorter candidate is the same opening after removing the
    # visual ellipsis.
    previous_opening = facebook_short_opening(previous)
    candidate_opening = facebook_short_opening(candidate)
    if previous_opening and candidate_opening and (
        previous_opening in candidate_opening or candidate_opening in previous_opening
    ):
        return True, "same post opening across collapsed/expanded rendering", ratio, False

    if len(previous_folded) >= 24 and len(candidate_folded) >= 24 and ratio >= 0.92:
        return True, f"high text similarity ({ratio:.3f})", ratio, False

    # A candidate that was already alerted is treated as a duplicate even if
    # Facebook temporarily rendered it differently between workflow runs.
    candidate_digest = facebook_candidate_fingerprint(candidate)
    for item in recent_candidates or []:
        if item.get("fingerprint") == candidate_digest and item.get("alerted"):
            return True, "candidate fingerprint was alerted previously", ratio, True
    return False, "candidate differs from saved post", ratio, False


def log_event(state, category, message):
    """Print a log entry and retain the latest 100 entries in state.json."""
    entry = f"[{get_hk_time()}] [{category}] {message}"
    print(entry, flush=True)
    state.setdefault("history", []).append(entry)
    state["history"] = state["history"][-100:]


def find_gmail_sent_mailbox(imap_client):
    """Discover the locale-independent Gmail Sent mailbox through its \\Sent flag."""
    status, folders = imap_client.list()
    if status != "OK":
        raise RuntimeError("Gmail IMAP folder list was unavailable")

    for raw_folder in folders or []:
        folder_line = raw_folder.decode("utf-8", errors="replace")
        if "\\Sent" in folder_line:
            # Preserve the final quoted IMAP mailbox name, including spaces;
            # e.g., "[Gmail]/Sent Mail" rather than only its final word.
            quoted_mailbox = re.search(r'("[^"]*")$', folder_line)
            if quoted_mailbox:
                return quoted_mailbox.group(1)
            return folder_line.rsplit(" ", 1)[-1]

    raise RuntimeError("Gmail Sent mailbox with \\Sent special-use flag was not found")


def remove_sent_label_from_alert(message_id):
    """Remove the Sent label from precisely one SMTP-created alert message."""
    if not CLEAN_SENT_ALERTS:
        return

    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30) as imap_client:
            imap_client.login(EMAIL_USER, EMAIL_PASS)
            sent_mailbox = find_gmail_sent_mailbox(imap_client)
            status, _ = imap_client.select(sent_mailbox, readonly=False)
            if status != "OK":
                raise RuntimeError("Could not select Gmail Sent mailbox")

            # Gmail may write the SMTP copy shortly after sendmail returns.
            # Search only for this unique RFC Message-ID, never by broad subject.
            for attempt in range(1, 6):
                status, data = imap_client.uid(
                    "SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"'
                )
                if status == "OK" and data and data[0]:
                    uids = data[0].split()
                    for uid in uids:
                        status, _ = imap_client.uid(
                            "STORE", uid, "-X-GM-LABELS", r"(\Sent)"
                        )
                        if status != "OK":
                            raise RuntimeError("Could not remove Gmail Sent label")
                    print(
                        f"[IMAP] Removed Sent label from alert Message-ID {message_id}.",
                        flush=True,
                    )
                    return

                if attempt < 5:
                    time.sleep(1)

            print(
                "[IMAP] Alert email was sent, but its Sent copy was not found within 5 seconds; "
                "no message was changed.",
                flush=True,
            )
    except Exception as exc:
        # A cleanup failure must never make an already-sent alert retry and
        # create a duplicate alert. It affects only Sent-folder convenience.
        print(f"[IMAP] Sent-label cleanup skipped: {type(exc).__name__}: {exc}", flush=True)


def send_email(subject, body):
    """Send a UTF-8 Gmail alert; optionally remove only its Sent-label copy."""
    if not (EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL):
        print("[EMAIL] Credentials are missing: check EMAIL_USER, EMAIL_PASS, RECEIVER_EMAIL.", flush=True)
        return False

    try:
        message = MIMEMultipart()
        message_id = make_msgid(domain=EMAIL_USER.rsplit("@", 1)[-1])
        message["Message-ID"] = message_id
        message["X-Alert-Monitor"] = "github-actions"
        message["Subject"] = subject
        message["From"] = EMAIL_USER
        message["To"] = RECEIVER_EMAIL
        message.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [RECEIVER_EMAIL], message.as_string())

        print(f"[EMAIL] Sent successfully to {RECEIVER_EMAIL}: {subject}", flush=True)
        remove_sent_label_from_alert(message_id)
        return True
    except Exception as exc:
        print(f"[EMAIL] FAILED: {type(exc).__name__}: {exc}", flush=True)
        return False


def load_state():
    """Load state.json and remove the retired Levi's monitor from existing cache state."""
    default_state = {
        "system": {
            "last_run": "",
            "last_completed": "",
            "total_runs": 0,
            "timezone": "Asia/Hong_Kong (UTC+8)",
        },
        "monitors": {
            "owndays": {},
            "fb": {},
        },
        "history": [],
    }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = default_state

    state.setdefault("system", {})
    state.setdefault("monitors", {})
    state.setdefault("history", [])
    state["monitors"].setdefault("owndays", {})
    state["monitors"].setdefault("fb", {})
    state["monitors"].pop("levis", None)  # Retired permanently.
    state["system"].setdefault("total_runs", 0)
    state["system"]["timezone"] = "Asia/Hong_Kong (UTC+8)"
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)


def update_fetch_details(monitor, response, page, visible_text, url):
    """Store human-readable diagnostics after a page read."""
    monitor["fetch"] = {
        "when": get_hk_time(),
        "url": url,
        "http_status": response.status if response else None,
        "page_title": shorten(page.title(), 160),
        "visible_text_length": len(visible_text),
        "visible_text_sample": shorten(visible_text, 260),
    }


def page_looks_blocked(page, visible_text):
    """Identify access-check pages conservatively without attempting to bypass them."""
    lower_text = (visible_text or "").lower()
    if any(marker in lower_text for marker in BLOCK_MARKERS):
        return True

    try:
        challenge = page.locator(
            "iframe[src*='captcha'], iframe[title*='challenge' i], "
            "input[name='cf-turnstile-response'], [id*='captcha' i]"
        )
        return challenge.count() > 0
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Owndays: exact cart-add button availability monitor
# -----------------------------------------------------------------------------
def check_owndays(page, state):
    """Alert when form#cart-add button is enabled and not marked Out Of Stock Online."""
    monitor = state["monitors"]["owndays"]
    monitor.setdefault("in_stock", False)
    monitor.setdefault("count", 0)
    monitor.setdefault("status", "Initializing")
    monitor.pop("stock_phrase_found", None)  # Old phrase-only monitor field.
    monitor["last_check"] = get_hk_time()

    try:
        response = page.goto(OWNDAYS_URL, timeout=45000, wait_until="domcontentloaded")
        # Wait for the monitored element rather than a fixed multi-second delay.
        try:
            page.locator("form#cart-add button").first.wait_for(state="attached", timeout=12000)
        except Exception:
            pass  # The later verification records a clear UNVERIFIED state.
        visible_text = page.locator("body").inner_text(timeout=12000)
        normalized_text = shorten(visible_text, 100000)
        update_fetch_details(monitor, response, page, normalized_text, OWNDAYS_URL)

        if page_looks_blocked(page, normalized_text):
            monitor["status"] = "BLOCKED: Owndays showed a CAPTCHA/access check; prior stock state preserved"
            log_event(state, "OWNDAYS", monitor["status"])
            return state

        if len(normalized_text) < 100:
            monitor["status"] = "UNVERIFIED: Too little visible text; prior stock state preserved"
            log_event(state, "OWNDAYS", monitor["status"])
            return state

        cart_button = page.locator("form#cart-add button").first
        if cart_button.count() == 0:
            monitor["cart_button"] = {"found": False, "selector": "form#cart-add button"}
            monitor["status"] = "UNVERIFIED: form#cart-add button was not found; prior stock state preserved"
            log_event(state, "OWNDAYS", monitor["status"])
            return state

        button_text = shorten(cart_button.inner_text(timeout=10000), 160)
        is_disabled = cart_button.is_disabled(timeout=10000)
        monitor["cart_button"] = {
            "found": True,
            "text": button_text,
            "disabled": is_disabled,
            "selector": "form#cart-add button",
        }
        monitor["stock_evidence"] = button_text

        # Exact requested condition: unavailable if disabled OR text is Out Of Stock Online.
        is_out_of_stock = is_disabled or button_text.casefold() == "out of stock online"
        if is_out_of_stock:
            monitor["in_stock"] = False
            monitor["count"] = 0
            monitor["status"] = "OK: Fetch succeeded; cart-add button is unavailable / Out Of Stock Online"
            log_event(
                state,
                "OWNDAYS",
                f"Fetch OK (HTTP {monitor['fetch']['http_status']}); cart button text='{button_text}', disabled={is_disabled}.",
            )
            return state

        log_event(
            state,
            "OWNDAYS",
            f"Fetch OK (HTTP {monitor['fetch']['http_status']}); cart button text='{button_text}', disabled={is_disabled}; availability condition met.",
        )

        if not monitor["in_stock"]:
            success = send_email(
                "[ALERT] Owndays Item Appears Available (1/5)",
                "The requested Owndays cart button condition indicates availability.\n\n"
                f"Product: {OWNDAYS_URL}\n"
                f"Button selector: form#cart-add button\n"
                f"Button text: {button_text}\n"
                f"Button disabled: {is_disabled}\n"
                f"Page title: {monitor['fetch']['page_title']}\n\n"
                f"Checked: {get_hk_time()}",
            )
            if success:
                monitor["in_stock"] = True
                monitor["count"] = 1
                monitor["status"] = "ALERTING: Cart button indicates availability; email 1/5 sent"
                log_event(state, "OWNDAYS", "Availability alert email 1/5 sent.")
            else:
                monitor["status"] = "EMAIL FAILED: Availability condition detected; will retry next run"
                log_event(state, "OWNDAYS", "Availability condition detected but email failed; state not advanced.")

        elif monitor["count"] < 5:
            reminder_number = monitor["count"] + 1
            success = send_email(
                f"[REMINDER] Owndays Item Appears Available ({reminder_number}/5)",
                "The requested Owndays cart button condition still indicates availability.\n\n"
                f"Product: {OWNDAYS_URL}\n"
                f"Button text: {button_text}\n"
                f"Button disabled: {is_disabled}\n\n"
                f"Checked: {get_hk_time()}",
            )
            if success:
                monitor["count"] = reminder_number
                monitor["status"] = f"ALERTING: Availability persists; email {reminder_number}/5 sent"
                log_event(state, "OWNDAYS", f"Availability reminder email {reminder_number}/5 sent.")
            else:
                monitor["status"] = "EMAIL FAILED: Availability persists; will retry current reminder"
                log_event(state, "OWNDAYS", "Availability reminder email failed; counter not advanced.")

        else:
            monitor["status"] = "PAUSED: Availability persists; 5/5 email reminders already sent"
            log_event(state, "OWNDAYS", "Availability remains positive; reminder cap reached.")

    except Exception as exc:
        monitor["status"] = f"ERROR: Owndays fetch/check failed; prior state preserved ({str(exc)[:120]})"
        log_event(state, "OWNDAYS", monitor["status"])

    return state


# -----------------------------------------------------------------------------
# Facebook: compare clean post content and email once per new post
# -----------------------------------------------------------------------------
# Chrome that can never be genuine post content, whatever the length.
FACEBOOK_STRONG_MARKERS = (
    "create new account",
    "all reactions",
    "view more comments",
    "view more replies",
)

# Words that are interface text on a shell page but ordinary words inside a
# real post ("...log in to redeem", a URL containing "facebook"). They only
# disqualify a candidate that is short or made up mostly of such text.
FACEBOOK_WEAK_MARKERS = (
    "facebook",
    "log in",
)

FACEBOOK_INTERFACE_ONLY = {
    "home",
    "menu",
    "like",
    "comment",
    "share",
}

# The post body is no longer cut short: an alert email should carry the whole
# latest post. The cap only exists to keep one pathological post from
# producing an unserious payload.
FACEBOOK_CONTENT_LIMIT = 20000
# state.json keeps a smaller copy so the repository cache stays reasonable.
FACEBOOK_STATE_TEXT_LIMIT = 8000
# Length of the normalized opening used to recognise the same post again.
FACEBOOK_IDENTITY_CHARS = 220
# How many trailing blocks a single post body may contribute (paragraphs,
# sub-messages, quoted text) before we stop collecting.
FACEBOOK_MAX_BLOCKS = 40

# A photo post can have an empty message container. Keep alerting on those by
# describing the image instead of silently reporting UNVERIFIED.
FACEBOOK_ALERT_ON_IMAGE_ONLY_POSTS = True

MESSAGE_CONTAINER_SELECTORS = (
    "div[data-ad-preview='message']",
    "div[data-ad-comet-preview='message']",
    "div[data-ad-rendering-role='story_message']",
    "div[data-testid='post_message']",
)

MESSAGE_CONTAINER_SELECTOR_ALL = ", ".join(MESSAGE_CONTAINER_SELECTORS)

POST_PERMALINK_RE = re.compile(
    r"facebook\.com/.+(?:/posts/|story_fbid=|/photos/|/videos/|/reel/|permalink\.php|/activity/)",
    re.IGNORECASE,
)

POST_PATH_RE = re.compile(
    r"(?:/posts/|story_fbid=|/photos/|/videos/|/reel/|permalink\.php|/activity/)",
    re.IGNORECASE,
)

# Emoji sprites, avatars and tiny thumbnails are interface parts of a post,
# not its media.
FACEBOOK_IMAGE_NOISE_RE = re.compile(
    r"(?:/emoji/|static\.xx\.fbcdn\.net|s50x50|p50x50|s60x60|s40x40|cp0/|/rsrc\.php)",
    re.IGNORECASE,
)

# Internal UI lines that must never be mistaken for post content.
FACEBOOK_UI_LINE = re.compile(
    r"^(?:like|comment|share|reply|follow|message|send|see more|see less|"
    r"most relevant|all reactions?|write a comment|view \d+ (?:comments?|replies)|"
    r"·|\d+\s*(?:s|m|h|d|w|y|min|mins|hr|hrs|hour|hours|day|days|week|weeks|year|years)|"
    r"(?:yesterday|today|just now)(?:\s+at\s+[\d:apm. ]+)?|"
    r"\d{1,2}\s+\w+\s+at\s+[\d:apm. ]+)$",
    re.IGNORECASE,
)

# Walk the DOM and keep paragraph structure. innerText can omit text hidden by
# CSS line clamping ("See more"), while textContent glues every block together
# without separators. This keeps the full text and the original line breaks.
BLOCK_TEXT_JS = """
(element) => {
  const blockTags = new Set([
    "DIV","P","LI","TR","H1","H2","H3","H4","H5","H6","BLOCKQUOTE",
    "SECTION","UL","OL","FIGURE","FIGCAPTION","TABLE"
  ]);
  const out = [];
  const walk = (node) => {
    if (!node) return;
    if (node.nodeType === 3) { out.push(node.nodeValue); return; }
    if (node.nodeType !== 1) return;
    const tag = node.tagName;
    if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") return;
    if (tag === "BR") { out.push("\\n"); return; }
    const isBlock = blockTags.has(tag);
    if (isBlock) out.push("\\n");
    for (const child of node.childNodes) walk(child);
    if (isBlock) out.push("\\n");
  };
  walk(element);
  return out.join("");
}
"""

# Facebook limits how much of a long post is shown until "See more" is used.
# Clicking every visible control is safe: it only expands already loaded text.
EXPAND_SEE_MORE_JS = """
() => {
  let clicked = 0;
  // Never touch anchors: a "See more" link can navigate away from the post
  // and leave the extraction reading a different page.
  const nodes = document.querySelectorAll("[role='button'], div, span");
  for (const node of nodes) {
    if (node.tagName === "A" || node.closest("a")) continue;
    const label = (node.textContent || "").trim();
    if (!/^(see more|see all|…\\s*see more|see more…)$/i.test(label)) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    try { node.click(); clicked += 1; } catch (error) { /* ignore */ }
    if (clicked >= 5) break;
  }
  return clicked;
}
"""


def normalize_facebook_whitespace(text):
    """Collapse runs of spaces but keep paragraph breaks intact."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\u00ad", "").replace("\u00a0", " ")
    lines = []
    for raw_line in text.split("\n"):
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip()


def clean_facebook_post_text(raw_text, limit=FACEBOOK_CONTENT_LIMIT):
    """Return the full post body without Facebook's interface/reaction chrome."""
    text = raw_text or ""
    # "See more" / "See less" are expand controls, not content. Only standalone
    # occurrences and trailing ellipsis forms are removed so that a sentence
    # which legitimately mentions the words is preserved.
    text = re.sub(r"(?m)^\s*(?:see|show)\s+(?:more|less)\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:…|\.\.\.)\s*(?:see|show)\s+more\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)[ \t]+(?:see|show)\s+more\s*$", "", text, flags=re.IGNORECASE)

    # Everything below the engagement bar belongs to the interface, comments
    # or other posts, never to the post body itself.
    text = re.sub(r"\n?All reactions:?.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\bLike\s+Comment(?:\s+Share)?\b.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\bView more(?: comments| replies)?\b.*", "", text, flags=re.IGNORECASE | re.DOTALL)

    kept_lines = []
    for line in normalize_facebook_whitespace(text).split("\n"):
        if not line:
            kept_lines.append("")
            continue
        if FACEBOOK_UI_LINE.match(line.strip()):
            continue
        kept_lines.append(line)

    cleaned = normalize_facebook_whitespace("\n".join(kept_lines))
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n\n[post text truncated at the configured limit]"
    return cleaned


def facebook_body_identity(text, limit=FACEBOOK_IDENTITY_CHARS):
    """A short normalized opening used to recognise a post across runs."""
    normalized = " ".join(clean_facebook_post_text(text).casefold().split())
    return normalized[:limit]


def facebook_short_opening(text, limit=48):
    """Normalize the short opening exposed before Facebook's See more control."""
    normalized = " ".join(clean_facebook_post_text(text).casefold().split())
    normalized = re.sub(r"(?:…|\.\.\.)\s*$", "", normalized).strip()
    return normalized[:limit].rstrip(" .,;:!?…")


def is_clean_facebook_candidate(text):
    """Accept only meaningful post-body candidates, never generic Facebook UI text."""
    normalized = clean_facebook_post_text(text)
    lowered = normalized.casefold()
    if len(normalized) < 5:
        return False
    if any(marker in lowered for marker in FACEBOOK_STRONG_MARKERS):
        return False

    # A logged-out shell is made almost entirely of these words; a real post
    # that happens to mention them is long and contains far more besides.
    weak_hits = sum(lowered.count(marker) for marker in FACEBOOK_WEAK_MARKERS)
    if weak_hits and len(normalized) < 120:
        return False
    if weak_hits and weak_hits * 30 > len(normalized):
        return False
    if any(lowered.strip() == marker for marker in FACEBOOK_WEAK_MARKERS):
        return False
    if lowered in FACEBOOK_INTERFACE_ONLY:
        return False
    if re.fullmatch(r"(?:\d+\s*)?(?:m|h|d|w|y|mins?|hours?|days?|weeks?)\s*·?", lowered):
        return False
    return True


def combine_facebook_blocks(blocks, page_identifier=""):
    """Keep post-body blocks in DOM order while removing nested/duplicate text."""
    page_identifier = page_identifier.casefold()
    kept = []
    for block in blocks:
        candidate = clean_facebook_post_text(block)
        if not is_clean_facebook_candidate(candidate):
            continue
        if page_identifier and candidate.casefold() == page_identifier:
            continue

        # Facebook frequently emits both a parent div and a nested div with the
        # same text. Keep the most complete version exactly once.
        if any(candidate == existing or candidate in existing for existing in kept):
            continue
        kept = [existing for existing in kept if existing not in candidate]
        kept.append(candidate)
        if len(kept) >= FACEBOOK_MAX_BLOCKS:
            break

    return clean_facebook_post_text("\n\n".join(kept))


def expand_facebook_see_more(page):
    """Expand truncated post text so the whole body is present in the DOM."""
    try:
        clicked = page.evaluate(EXPAND_SEE_MORE_JS)
        if clicked:
            page.wait_for_timeout(600)
        return int(clicked or 0)
    except Exception:
        return 0


def facebook_page_identifier(page_url):
    """Derive the handle to look for in author/permalink links."""
    try:
        parts = urlsplit(page_url)
    except Exception:
        return ""
    slug = parts.path.rstrip("/").rsplit("/", 1)[-1]
    if slug.casefold() in ("", "profile.php", "people"):
        query = dict(parse_qsl(parts.query))
        return (query.get("id") or "").strip()
    return slug


def article_links_to_page(article, identifier):
    """True when an article carries a link back to the watched page."""
    if not identifier:
        return False
    try:
        hrefs = article.locator("a[href]").evaluate_all(
            "(elements) => elements.map((element) => element.getAttribute('href') || '')"
        )
    except Exception:
        return False
    key = identifier.casefold()
    for href in hrefs:
        lowered = href.casefold()
        if f"/{key}/" in lowered or lowered.rstrip("/").endswith(f"/{key}") or f"id={key}" in lowered:
            return True
    return False


def find_latest_facebook_post(page, page_url=""):
    """Return the newest post container belonging to the watched page.

    Only articles that actually hold a message container are considered, so a
    comment (which can also be an article) is never mistaken for the post. A
    sponsored or "suggested for you" card can also carry a message container,
    so an article that links back to the watched page is preferred; when no
    article links back, the first message-bearing article is used as before.
    """
    try:
        articles = page.locator("div[role='article']")
        count = min(articles.count(), 8)
    except Exception:
        count = 0

    identifier = facebook_page_identifier(page_url)
    first_candidate = None
    for index in range(count):
        article = articles.nth(index)
        try:
            if article.locator(MESSAGE_CONTAINER_SELECTOR_ALL).count() == 0:
                continue
        except Exception:
            continue
        if first_candidate is None:
            first_candidate = (article, f"article[{index}]")
        if article_links_to_page(article, identifier):
            return article, f"article[{index}]"

    return first_candidate or (page, "page")


def read_facebook_block_text(locator):
    """Read one message container with paragraph breaks preserved."""
    try:
        text = locator.evaluate(BLOCK_TEXT_JS)
    except Exception:
        try:
            text = locator.inner_text()
        except Exception:
            text = locator.text_content() or ""
    return normalize_facebook_whitespace(text)


def extract_facebook_post_text(page, page_url):
    """Read the newest post's full body across Facebook layout variants."""
    diagnostics = {"scope": "page", "see_more_clicks": expand_facebook_see_more(page)}
    page_identifier = page_url.rstrip("/").rsplit("/", 1)[-1]

    scope, scope_name = find_latest_facebook_post(page, page_url)
    diagnostics["scope"] = scope_name

    blocks = []
    for selector in MESSAGE_CONTAINER_SELECTORS:
        try:
            locators = scope.locator(selector)
            block_count = locators.count()
        except Exception as exc:
            diagnostics[selector] = f"read error: {type(exc).__name__}"
            continue
        diagnostics[selector] = block_count
        if block_count == 0:
            continue
        for index in range(min(block_count, FACEBOOK_MAX_BLOCKS)):
            try:
                blocks.append(read_facebook_block_text(locators.nth(index)))
            except Exception:
                continue

    diagnostics["raw_blocks"] = len(blocks)
    combined = combine_facebook_blocks(blocks, page_identifier)
    diagnostics["clean_characters"] = len(combined)
    if is_clean_facebook_candidate(combined):
        return combined, f"{scope_name}+message-containers", diagnostics
    return "", "", diagnostics


def clean_facebook_permalink(url):
    """Drop Facebook's tracking parameters so the link stays readable and short."""
    try:
        parts = urlsplit(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=False)
            if not key.startswith("__")
        ]
        cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        return cleaned if len(cleaned) > 12 else url
    except Exception:
        return url


def describe_facebook_post(scope):
    """Collect the post permalink, author, timestamp and attachments."""
    details = {"permalink": "", "author": "", "posted": "", "links": [], "images": []}

    try:
        anchors = scope.locator("a[href]").evaluate_all(
            "(elements) => elements.map((element) => [element.href, "
            "(element.getAttribute('href') || ''), "
            "(element.textContent || '').trim()])"
        )
    except Exception:
        anchors = []

    for href, raw_href, label in anchors:
        if not details["permalink"]:
            if POST_PERMALINK_RE.search(href or ""):
                details["permalink"] = clean_facebook_permalink(href)
            elif raw_href and POST_PATH_RE.search(raw_href):
                # Relative permalinks are common in the compressed mobile
                # render; resolve them against the canonical host.
                details["permalink"] = clean_facebook_permalink(
                    "https://www.facebook.com"
                    + (raw_href if raw_href.startswith("/") else "/" + raw_href)
                )
        cleaned_label = shorten(label, 80)
        if cleaned_label and not details["posted"] and re.fullmatch(
            r"(?:\d+\s*(?:m|h|d|w|y|mins?|hrs?|hours?|days?|weeks?)|"
            r"(?:yesterday|today)(?:\s+at\s+[\d:apm. ]+)?|"
            r"\d{1,2}\s+\w+\s+at\s+[\d:apm. ]+)",
            cleaned_label,
            re.IGNORECASE,
        ):
            details["posted"] = cleaned_label

    try:
        author = scope.locator("h2 a, h3 a, h4 a, strong a").first.inner_text(timeout=2000)
        details["author"] = shorten(author, 120)
    except Exception:
        pass

    try:
        datetime_value = scope.locator("time").first.get_attribute("datetime", timeout=2000)
        if datetime_value:
            details["posted"] = f"{details['posted']} ({datetime_value})".strip()
    except Exception:
        pass

    try:
        images = scope.locator("img[src]").evaluate_all(
            "(elements) => elements.map((element) => [element.src, "
            "(element.alt || '').trim()])"
        )
    except Exception:
        images = []
    for src, alt in images:
        # Only real post media: skip emoji sprites, avatars and 50px
        # thumbnails, which are part of the interface rather than the post.
        if not src or not re.search(r"scontent", src or "", re.IGNORECASE):
            continue
        if FACEBOOK_IMAGE_NOISE_RE.search(src):
            continue
        alt = shorten(alt, 200)
        if alt.casefold() in {"", "no photo description available.", "may be an image of text"}:
            alt = ""
        entry = {"url": src, "description": alt}
        if entry["url"] not in [item["url"] for item in details["images"]]:
            details["images"].append(entry)
        if len(details["images"]) >= 4:
            break

    return details


def photo_only_post_text(scope, details):
    """Describe a media post that carries no caption text."""
    if not FACEBOOK_ALERT_ON_IMAGE_ONLY_POSTS:
        return ""
    try:
        if scope.locator(MESSAGE_CONTAINER_SELECTOR_ALL).count() == 0:
            return ""
    except Exception:
        return ""

    images = details.get("images") or []
    if not images:
        return ""

    described = [image["description"] for image in images if image.get("description")]
    lines = ["[Photo or video post with no caption text]"]
    if described:
        lines.extend(["", "What the image shows (Facebook's own description):"])
        lines.extend(described)
    else:
        lines.extend(["", f"The post carries {len(images)} attached image(s); see the post link."])
    return "\n".join(lines)


def collect_links_in_text(text):
    """Return external links written inside the post body, in order."""
    links = []
    for match in re.findall(r"https?://[^\s<>\"']+", text or ""):
        cleaned = match.rstrip(").,;\"'")
        if cleaned not in links and "facebook.com" not in cleaned.casefold():
            links.append(cleaned)
    return links[:10]


def build_facebook_email_body(base_url, final_text, details):
    """Build the compact Facebook email requested by the user.

    Keep only the page, raw captured post body, posted time, and check time.
    The raw body may still contain URLs that Facebook included in the post.
    """
    posted = details.get("posted") or "Unavailable"
    return "\n".join(
        [
            f"Page: {base_url}",
            "",
            final_text,
            "",
            f"posted: {posted}",
            f"Checked: {get_hk_time()}",
        ]
    )


def wait_for_facebook_post_text(page, page_url, attempts=5, interval_ms=3000):
    """Allow the delayed public Facebook feed to replace the profile shell."""
    last_diagnostics = {}
    for attempt in range(1, attempts + 1):
        final_text, source, diagnostics = extract_facebook_post_text(page, page_url)
        diagnostics["attempt"] = attempt
        diagnostics["max_attempts"] = attempts
        if final_text:
            return final_text, source, diagnostics
        last_diagnostics = diagnostics
        if attempt < attempts:
            # A short scroll nudges Facebook into hydrating the timeline.
            try:
                page.mouse.wheel(0, 400)
                page.wait_for_timeout(interval_ms // 2)
                page.mouse.wheel(0, -400)
                page.wait_for_timeout(interval_ms // 2)
            except Exception:
                page.wait_for_timeout(interval_ms)
    return "", "", last_diagnostics


def check_facebook(page, url, state):
    base_url = url.rstrip("/")
    monitors = state["monitors"].setdefault("fb", {})
    monitor = monitors.setdefault(
        url,
        {"status": "Initializing", "last_post_text": "", "last_check": ""},
    )
    monitor.pop("id", None)  # Remove retired photo-ID tracking data.
    monitor.setdefault("candidate_history", [])
    monitor.setdefault("comparison", {})
    monitor["last_check"] = get_hk_time()

    try:
        response = page.goto(base_url, timeout=45000, wait_until="domcontentloaded")
        page.locator("body").wait_for(state="visible", timeout=15000)

        # Facebook often returns the public profile shell first and hydrates the
        # feed afterwards. Retry extraction for a bounded window rather than
        # accepting that first shell as the final page state.
        initial_visible_text = page.locator("body").inner_text(timeout=15000)
        if page_looks_blocked(page, initial_visible_text):
            update_fetch_details(monitor, response, page, initial_visible_text, base_url)
            monitor["status"] = "BLOCKED: Facebook showed a CAPTCHA/access check; prior post state preserved"
            log_event(state, "FACEBOOK", f"Access check detected for {base_url}.")
            return state

        final_text, extraction_source, extraction_diagnostics = wait_for_facebook_post_text(
            page, base_url
        )
        visible_text = page.locator("body").inner_text(timeout=15000)
        update_fetch_details(monitor, response, page, visible_text, base_url)
        monitor["extraction"] = {
            "source": extraction_source or "none",
            "selector_counts": extraction_diagnostics,
        }
        scope, _ = find_latest_facebook_post(page, base_url)
        post_details = describe_facebook_post(scope)

        if not final_text:
            # A photo-only post has an empty message container. Rather than
            # reporting UNVERIFIED forever, describe what the image shows.
            fallback_text = photo_only_post_text(scope, post_details)
            if fallback_text:
                final_text = fallback_text
                monitor["extraction"]["source"] = "photo-only-fallback"
                log_event(
                    state,
                    "FACEBOOK",
                    f"No caption text for {base_url}; using the image description of a photo-only post.",
                )
            else:
                monitor["candidate_post_text"] = ""
                monitor["status"] = "UNVERIFIED: No clean post-body text found across Facebook layout selectors; prior post state preserved"
                log_event(
                    state,
                    "FACEBOOK",
                    f"No clean latest-post text found for {base_url}; selector counts: {extraction_diagnostics}",
                )
                return state

        monitor["candidate_post_text"] = final_text[:FACEBOOK_STATE_TEXT_LIMIT]

        previous_text = monitor.get("last_post_text", "")
        previous_permalink = monitor.get("last_post_permalink", "")
        candidate_permalink = post_details.get("permalink", "")
        previous_fingerprint = facebook_candidate_fingerprint(previous_text) if previous_text else ""
        candidate_fingerprint = facebook_candidate_fingerprint(final_text)
        unchanged, comparison_reason, similarity, previously_alerted = compare_facebook_candidates(
            previous_text,
            final_text,
            monitor.get("candidate_history", []),
            previous_permalink,
            candidate_permalink,
        )
        monitor["comparison"] = {
            "checked_at": get_hk_time(),
            "decision": "duplicate_or_unchanged" if unchanged else "candidate_changed",
            "reason": comparison_reason,
            "similarity": round(similarity, 3),
            "previous_fingerprint": previous_fingerprint,
            "candidate_fingerprint": candidate_fingerprint,
            "previous_permalink": previous_permalink,
            "candidate_permalink": candidate_permalink,
            "previous_candidate": shorten(previous_text, 300),
            "latest_candidate": shorten(final_text, 300),
            "candidate_characters": len(final_text),
            "previously_alerted": previously_alerted,
        }
        monitor["candidate_history"].append(
            {
                "observed_at": get_hk_time(),
                "fingerprint": candidate_fingerprint,
                "candidate": shorten(final_text, 300),
                "characters": len(final_text),
                "alerted": False,
                "comparison_reason": comparison_reason,
            }
        )
        monitor["candidate_history"] = monitor["candidate_history"][-20:]

        if unchanged:
            monitor["status"] = "OK: Latest post unchanged; no email required"
            log_event(
                state,
                "FACEBOOK",
                f"Duplicate suppressed for {base_url}; reason={comparison_reason}; "
                f"previous={shorten(previous_text, 160)!r}; latest={shorten(final_text, 160)!r}",
            )
            return state

        page_name = base_url.rsplit("/", 1)[-1]
        headline = shorten(final_text, 90)
        success = send_email(
            f"{page_name}: {headline}" if headline else page_name,
            build_facebook_email_body(base_url, final_text, post_details),
        )
        if success:
            # Write new content only after email succeeds; failed sends retry next run.
            monitor["last_post_text"] = final_text[:FACEBOOK_STATE_TEXT_LIMIT]
            monitor["last_post_characters"] = len(final_text)
            monitor["last_post_permalink"] = post_details.get("permalink", "")
            for item in reversed(monitor["candidate_history"]):
                if item["fingerprint"] == candidate_fingerprint:
                    item["alerted"] = True
                    break
            monitor["status"] = f"NOTIFIED ONCE: {shorten(final_text, 60)}"
            log_event(
                state,
                "FACEBOOK",
                f"New-post email sent for {base_url}; {len(final_text)} characters extracted; "
                f"previous={shorten(previous_text, 160)!r}; latest={shorten(final_text, 160)!r}; "
                f"fingerprint={candidate_fingerprint}",
            )
        else:
            monitor["status"] = "EMAIL FAILED: New post detected; will retry next run"
            log_event(state, "FACEBOOK", f"New post detected but email failed for {base_url}.")

    except Exception as exc:
        monitor["status"] = f"ERROR: Facebook fetch/check failed; prior state preserved ({str(exc)[:120]})"
        log_event(state, "FACEBOOK", monitor["status"])

    return state


# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("[SYSTEM] STOP_ALERTS=true. Monitoring cycle intentionally skipped.", flush=True)
        return

    state = load_state()
    if FB_PAGES:
        log_event(state, "CONFIG", f"Loaded {len(FB_PAGES)} Facebook page(s).")
    else:
        log_event(state, "CONFIG", "No valid Facebook pages configured; Facebook checks skipped.")
    state["system"]["last_run"] = get_hk_time()
    state["system"]["total_runs"] += 1
    log_event(state, "SYSTEM", f"Monitoring cycle {state['system']['total_runs']} started.")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 1000},
                locale="en-US",
            )

            # Images, media, and fonts are not needed for text/button checks.
            # Blocking them reduces transfer and startup time without changing
            # the monitored DOM conditions. (Image URLs are read from the DOM,
            # so attachment links still work even when the file is not loaded.)
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )

            owndays_page = context.new_page()
            facebook_page = context.new_page()
            state = check_owndays(owndays_page, state)
            for facebook_url in FB_PAGES:
                state = check_facebook(facebook_page, facebook_url, state)

            context.close()
            browser.close()

    except Exception as exc:
        log_event(state, "SYSTEM", f"Global browser failure: {type(exc).__name__}: {str(exc)[:180]}")

    state["system"]["last_completed"] = get_hk_time()
    log_event(state, "SYSTEM", "Monitoring cycle complete.")
    save_state(state)

    # Printed inside GitHub Actions → monitor → Run monitoring script.
    print("\n--- FINAL STATE REPORT (state.json) ---", flush=True)
    print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
    print("--- END STATE REPORT ---\n", flush=True)


if __name__ == "__main__":
    main()
