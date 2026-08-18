import json
import os
import random
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


def log_event(state, category, message):
    """Print a log entry and retain the latest 100 entries in state.json."""
    entry = f"[{get_hk_time()}] [{category}] {message}"
    print(entry, flush=True)
    state.setdefault("history", []).append(entry)
    state["history"] = state["history"][-100:]


def send_email(subject, body):
    """Send a UTF-8 Gmail SMTP email and return True only on confirmed submission."""
    if not (EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL):
        print("[EMAIL] Credentials are missing: check EMAIL_USER, EMAIL_PASS, RECEIVER_EMAIL.", flush=True)
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
        response = page.goto(OWNDAYS_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(4500, 7000))
        visible_text = page.locator("body").inner_text(timeout=15000)
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
def clean_facebook_post_text(raw_text):
    text = raw_text.replace("See more", "").replace("See More", "")
    text = re.sub(r"All reactions:.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\bLike\s+Comment\b.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\bView more(?: comments| replies)?\b.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    return shorten(text, 900)


def check_facebook(page, url, state):
    base_url = url.rstrip("/")
    monitors = state["monitors"].setdefault("fb", {})
    monitor = monitors.setdefault(
        url,
        {"status": "Initializing", "last_post_text": "", "last_check": ""},
    )
    monitor.pop("id", None)  # Remove retired photo-ID tracking data.
    monitor["last_check"] = get_hk_time()

    try:
        response = page.goto(base_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(8000, 12000))

        post_container = page.locator("div[data-ad-comet-preview='message']").first
        raw_text = ""
        if post_container.is_visible():
            text_blocks = post_container.locator("div[dir='auto']").all_inner_texts()
            raw_text = " ".join(block.strip() for block in text_blocks if block.strip())

        if not raw_text:
            article = page.locator("div[role='article']").first
            if article.is_visible():
                text_blocks = article.locator("div[dir='auto']").all_inner_texts()
                raw_text = " ".join(block.strip() for block in text_blocks if block.strip())

        visible_text = page.locator("body").inner_text(timeout=15000)
        update_fetch_details(monitor, response, page, visible_text, base_url)

        if not raw_text:
            monitor["status"] = "UNVERIFIED: No clean div[dir='auto'] post text found; prior post state preserved"
            log_event(state, "FACEBOOK", f"No clean latest-post text found for {base_url}.")
            return state

        final_text = clean_facebook_post_text(raw_text)
        monitor["candidate_post_text"] = final_text
        if len(final_text) < 5:
            monitor["status"] = "UNVERIFIED: Extracted post text was too short; prior post state preserved"
            log_event(state, "FACEBOOK", f"Post content too short for {base_url}.")
            return state

        if monitor["last_post_text"] == final_text:
            monitor["status"] = "OK: Latest post unchanged; no email required"
            log_event(state, "FACEBOOK", f"Latest post unchanged for {base_url}.")
            return state

        page_name = base_url.rsplit("/", 1)[-1]
        success = send_email(
            f"[NEW FB POST] {page_name}",
            f"New Facebook post detected.\n\n"
            f"Page: {base_url}\n\n"
            f"Content:\n{final_text}\n\n"
            f"Checked: {get_hk_time()}",
        )
        if success:
            # Write new content only after email succeeds; failed sends retry next run.
            monitor["last_post_text"] = final_text
            monitor["status"] = f"NOTIFIED ONCE: {shorten(final_text, 60)}"
            log_event(state, "FACEBOOK", f"New-post email sent for {base_url}.")
        else:
            monitor["status"] = "EMAIL FAILED: New post detected; will retry next run"
            log_event(state, "FACEBOOK", f"New post detected but email failed for {base_url}.")

    except Exception as exc:
        monitor["status"] = f"ERROR: Facebook fetch/check failed; prior post state preserved ({str(exc)[:120]})"
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
