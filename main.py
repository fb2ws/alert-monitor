import json
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")
FB_PAGES = [
    os.environ.get(f"FB_PAGE_{number}")
    for number in range(1, 4)
    if os.environ.get(f"FB_PAGE_{number}")
]

OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"
HK_TZ = timezone(timedelta(hours=8 ))

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


def log_event(state, category, message):
    """Print a log entry and retain the latest 100 entries in state.json."""
    entry = f"[{get_hk_time()}] [{category}] {message}"
    print(entry, flush=True)
    state.setdefault("history", []).append(entry)
    state["history"] = state["history"][-100:]


def send_email(subject, body):
    """Send a UTF-8 Gmail SMTP email and return True only on confirmed submission."""
    if not (EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL):
        print(
            "[EMAIL] Credentials are missing: check EMAIL_USER, EMAIL_PASS, RECEIVER_EMAIL.",
            flush=True,
        )
        return False

    try:
        message = MIMEMultipart()
        message["Subject"] = subject
        message["From"] = EMAIL_USER
        message["To"] = RECEIVER_EMAIL
        message.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [RECEIVER_EMAIL], message.as_string())

        print(f"[EMAIL] Sent successfully to {RECEIVER_EMAIL}: {subject}", flush=True)
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
    state["monitors"].pop("levis", None)
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
        "page_title": shorten(page.title( ), 160),
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
    monitor.pop("stock_phrase_found", None)
    monitor["last_check"] = get_hk_time()

    try:
        response = page.goto(OWNDAYS_URL, timeout=45000, wait_until="domcontentloaded")
        try:
            page.locator("form#cart-add button").first.wait_for(
                state="attached", timeout=12000
            )
        except Exception:
            pass

        visible_text = page.locator("body").inner_text(timeout=12000)
        normalized_text = shorten(visible_text, 100000)
        update_fetch_details(monitor, response, page, normalized_text, OWNDAYS_URL)

        if page_looks_blocked(page, normalized_text):
            monitor["status"] = (
                "BLOCKED: Owndays showed a CAPTCHA/access check; prior stock state preserved"
            )
            log_event(state, "OWNDAYS", monitor["status"])
            return state

        if len(normalized_text) < 100:
            monitor["status"] = (
                "UNVERIFIED: Too little visible text; prior stock state preserved"
            )
            log_event(state, "OWNDAYS", monitor["status"])
            return state

        cart_button = page.locator("form#cart-add button").first
        if cart_button.count() == 0:
            monitor["cart_button"] = {
                "found": False,
                "selector": "form#cart-add button",
            }
            monitor["status"] = (
                "UNVERIFIED: form#cart-add button was not found; prior stock state preserved"
            )
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

        is_out_of_stock = is_disabled or button_text.casefold() == "out of stock online"
        if is_out_of_stock:
            monitor["in_stock"] = False
            monitor["count"] = 0
            monitor["status"] = (
                "OK: Fetch succeeded; cart-add button is unavailable / Out Of Stock Online"
            )
            log_event(
                state,
                "OWNDAYS",
                (
                    f"Fetch OK (HTTP {monitor['fetch']['http_status']} ); "
                    f"cart button text='{button_text}', disabled={is_disabled}."
                ),
            )
            return state

        log_event(
            state,
            "OWNDAYS",
            (
                f"Fetch OK (HTTP {monitor['fetch']['http_status']} ); "
                f"cart button text='{button_text}', disabled={is_disabled}; "
                "availability condition met."
            ),
        )

        if not monitor["in_stock"]:
            success = send_email(
                "[ALERT] Owndays Item Appears Available (1/5)",
                "The requested Owndays cart button condition indicates availability.\n\n"
                f"Product: {OWNDAYS_URL}\n"
                "Button selector: form#cart-add button\n"
                f"Button text: {button_text}\n"
                f"Button disabled: {is_disabled}\n"
                f"Page title: {monitor['fetch']['page_title']}\n\n"
                f"Checked: {get_hk_time()}",
            )
            if success:
                monitor["in_stock"] = True
                monitor["count"] = 1
                monitor["status"] = (
                    "ALERTING: Cart button indicates availability; email 1/5 sent"
                )
                log_event(state, "OWNDAYS", "Availability alert email 1/5 sent.")
            else:
                monitor["status"] = (
                    "EMAIL FAILED: Availability condition detected; will retry next run"
                )
                log_event(
                    state,
                    "OWNDAYS",
                    "Availability condition detected but email failed; state not advanced.",
                )

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
                monitor["status"] = (
                    f"ALERTING: Availability persists; email {reminder_number}/5 sent"
                )
                log_event(
                    state,
                    "OWNDAYS",
                    f"Availability reminder email {reminder_number}/5 sent.",
                )
            else:
                monitor["status"] = (
                    "EMAIL FAILED: Availability persists; will retry current reminder"
                )
                log_event(
                    state,
                    "OWNDAYS",
                    "Availability reminder email failed; counter not advanced.",
                )

        else:
            monitor["status"] = (
                "PAUSED: Availability persists; 5/5 email reminders already sent"
            )
            log_event(state, "OWNDAYS", "Availability remains positive; reminder cap reached.")

    except Exception as exc:
        monitor["status"] = (
            "ERROR: Owndays fetch/check failed; prior state preserved "
            f"({str(exc)[:120]})"
        )
        log_event(state, "OWNDAYS", monitor["status"])

    return state


# -----------------------------------------------------------------------------
# Facebook: compare clean post content and email once per new post
# -----------------------------------------------------------------------------
FACEBOOK_INTERFACE_MARKERS = (
    "facebook",
    "log in",
    "create new account",
    "all reactions",
    "view more comments",
    "view more replies",
)

FACEBOOK_INTERFACE_ONLY = {
    "home",
    "menu",
    "like",
    "comment",
    "share",
}


def clean_facebook_post_text(raw_text):
    """Remove Facebook's dynamic interface/reaction text from candidate content."""
    text = raw_text.replace("See more", "").replace("See More", "")
    text = re.sub(r"All reactions:.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(
        r"\bLike\s+Comment(?:\s+Share)?\b.*",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\bView more(?: comments| replies)?\b.*",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return shorten(text, 900)


def is_clean_facebook_candidate(text):
    """Accept only meaningful post-body candidates, never generic Facebook UI text."""
    normalized = shorten(text, 900)
    lowered = normalized.casefold()
    if len(normalized) < 5:
        return False
    if any(marker in lowered for marker in FACEBOOK_INTERFACE_MARKERS):
        return False
    if lowered in FACEBOOK_INTERFACE_ONLY:
        return False
    if re.fullmatch(
        r"(?:\d+\s*)?(?:m|h|d|w|y|mins?|hours?|days?|weeks?)\s*·?",
        lowered,
    ):
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

        if any(candidate == existing or candidate in existing for existing in kept):
            continue
        kept = [existing for existing in kept if existing not in candidate]
        kept.append(candidate)

    return shorten(" ".join(kept), 900)


def facebook_post_is_unchanged(previous_text, candidate_text):
    """Treat a cleaner rendering of the saved post as unchanged, not as a new post."""
    previous = clean_facebook_post_text(previous_text).casefold()
    candidate = clean_facebook_post_text(candidate_text).casefold()
    return bool(
        previous
        and candidate
        and (previous == candidate or previous in candidate or candidate in previous)
    )


def extract_facebook_post_text(page, page_url):
    """Read the first clean post message from explicit Facebook message containers."""
    diagnostics = {}
    page_identifier = page_url.rstrip("/").rsplit("/", 1)[-1]

    sources = [
        (
            "data-ad-comet-preview",
            page.locator(
                "div[data-ad-comet-preview='message'] :is(div, span)[dir='auto']"
            ),
        ),
        (
            "data-ad-preview",
            page.locator(
                "div[data-ad-preview='message'] :is(div, span)[dir='auto']"
            ),
        ),
    ]

    # Do NOT fall back to div[role='article']. Facebook uses that role for
    # posts, comments, and replies. A prior article_2 fallback incorrectly
    # treated the commenter name “San Lee” as a new page post.
    diagnostics["article_fallback"] = (
        "disabled: comments/replies can also be articles"
    )

    for source_name, locator in sources:
        try:
            block_count = locator.count()
            diagnostics[source_name] = block_count
            if block_count == 0:
                continue

            combined = combine_facebook_blocks(
                locator.all_inner_texts(), page_identifier
            )
            if is_clean_facebook_candidate(combined):
                return combined, source_name, diagnostics

        except Exception as exc:
            diagnostics[source_name] = f"read error: {type(exc).__name__}"

    return "", "", diagnostics


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
            page.wait_for_timeout(interval_ms)

    return "", "", last_diagnostics


def check_facebook(page, url, state):
    base_url = url.rstrip("/")
    monitors = state["monitors"].setdefault("fb", {})
    monitor = monitors.setdefault(
        url,
        {"status": "Initializing", "last_post_text": "", "last_check": ""},
    )
    monitor.pop("id", None)
    monitor["last_check"] = get_hk_time()

    try:
        response = page.goto(base_url, timeout=45000, wait_until="domcontentloaded")
        page.locator("body").wait_for(state="visible", timeout=15000)

        # Facebook frequently returns a public profile shell first, then adds
        # post elements after rendering/hydration. Check once immediately and
        # then retry a bounded four times at three-second intervals.
        initial_visible_text = page.locator("body").inner_text(timeout=15000)
        if page_looks_blocked(page, initial_visible_text):
            update_fetch_details(
                monitor,
                response,
                page,
                initial_visible_text,
                base_url,
            )
            monitor["status"] = (
                "BLOCKED: Facebook showed a CAPTCHA/access check; "
                "prior post state preserved"
            )
            log_event(state, "FACEBOOK", f"Access check detected for {base_url}.")
            return state

        final_text, extraction_source, extraction_diagnostics = (
            wait_for_facebook_post_text(page, base_url)
        )

        # Record the page after the bounded feed-readiness wait, not only the
        # initial profile shell.
        visible_text = page.locator("body").inner_text(timeout=15000)
        update_fetch_details(monitor, response, page, visible_text, base_url)
        monitor["extraction"] = {
            "source": extraction_source or "none",
            "selector_counts": extraction_diagnostics,
        }
        monitor["candidate_post_text"] = final_text

        if not final_text:
            monitor["status"] = (
                "UNVERIFIED: Facebook returned a profile shell or no clean "
                "post body after bounded feed wait; prior post state preserved"
            )
            log_event(
                state,
                "FACEBOOK",
                (
                    f"No clean latest-post text found for {base_url} after "
                    f"{extraction_diagnostics.get('attempt', 0)} extraction attempt(s); "
                    f"selector counts: {extraction_diagnostics}"
                ),
            )
            return state

        if facebook_post_is_unchanged(monitor["last_post_text"], final_text):
            monitor["status"] = "OK: Latest post unchanged; no email required"
            log_event(state, "FACEBOOK", f"Latest post unchanged for {base_url}.")
            return state

        page_name = base_url.rsplit("/", 1)[-1]
        success = send_email(
            f"[NEW FB POST] {page_name}",
            "New Facebook post detected.\n\n"
            f"Page: {base_url}\n\n"
            f"Content:\n{final_text}\n\n"
            f"Checked: {get_hk_time()}",
        )

        if success:
            monitor["last_post_text"] = final_text
            monitor["status"] = f"NOTIFIED ONCE: {shorten(final_text, 60)}"
            log_event(state, "FACEBOOK", f"New-post email sent for {base_url}.")
        else:
            monitor["status"] = "EMAIL FAILED: New post detected; will retry next run"
            log_event(
                state,
                "FACEBOOK",
                f"New post detected but email failed for {base_url}.",
            )

    except Exception as exc:
        monitor["status"] = (
            "ERROR: Facebook fetch/check failed; prior state preserved "
            f"({str(exc)[:120]})"
        )
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
        log_event(
            state,
            "SYSTEM",
            f"Global browser failure: {type(exc).__name__}: {str(exc)[:180]}",
        )

    state["system"]["last_completed"] = get_hk_time()
    log_event(state, "SYSTEM", "Monitoring cycle complete.")
    save_state(state)

    print("\n--- FINAL STATE REPORT (state.json) ---", flush=True)
    print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
    print("--- END STATE REPORT ---\n", flush=True)


if __name__ == "__main__":
    main()
