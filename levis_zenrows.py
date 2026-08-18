import json
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY")
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")

LEVIS_URL = "https://www.levi.com/US/en_US/clothing/men/shirts/housemark-polo-shirt/p/358830292"
EXACT_PROMOTION_TEXT = "Extra 50% Off Applied at Checkout"
FIFTY_OFF_PATTERN = re.compile(r"\b50\s*%\s*off\b", re.IGNORECASE)
ZENROWS_FETCH_URL = "https://api.zenrows.com/v1/"
ZENROWS_SUBSCRIPTION_URL = "https://api.zenrows.com/v1/subscriptions/self/details"
STATE_FILE = "levis_state.json"

# Official ZenRows Free-plan allocation. Every Fetch call below is authorized
# only if a worst-case protected request (25 credits) still fits this limit.
FREE_CREDIT_LIMIT = 5000.0
WORST_CASE_FETCH_COST = 25.0
HK_TZ = timezone(timedelta(hours=8))
BLOCK_MARKERS = (
    "access denied",
    "captcha",
    "verify you are human",
    "temporarily blocked",
    "unusual traffic",
    "security check",
)


# -----------------------------------------------------------------------------
# State, time, and email utilities
# -----------------------------------------------------------------------------
def now_hk():
    return datetime.now(HK_TZ)


def get_hk_time():
    return now_hk().strftime("%Y-%m-%d %H:%M:%S HK")


def parse_provider_time(timestamp):
    """Convert ZenRows ISO-8601 timestamps to timezone-aware datetime values."""
    if not timestamp:
        raise ValueError("ZenRows subscription response did not include a billing timestamp")
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def shorten(text, limit=360):
    return " ".join((text or "").split())[:limit]


def log_event(state, category, message):
    entry = f"[{get_hk_time()}] [{category}] {message}"
    print(entry, flush=True)
    state.setdefault("history", []).append(entry)
    state["history"] = state["history"][-100:]


def default_state():
    return {
        "system": {
            "timezone": "Asia/Hong_Kong (UTC+8)",
            "total_runs": 0,
            "last_run": "",
            "last_completed": "",
        },
        "budget": {},
        "levis": {
            "status": "Initializing",
            "promotion_active": False,
            "reminder_count": 0,
            "last_check": "",
        },
        "history": [],
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = default_state()

    state.setdefault("system", {})
    state.setdefault("budget", {})
    state.setdefault("levis", {})
    state.setdefault("history", [])
    state["system"].setdefault("total_runs", 0)
    state["system"]["timezone"] = "Asia/Hong_Kong (UTC+8)"
    state["levis"].setdefault("promotion_active", False)
    state["levis"].setdefault("reminder_count", 0)
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)


def send_email(subject, body):
    if not (EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL):
        print("[EMAIL] Missing EMAIL_USER, EMAIL_PASS, or RECEIVER_EMAIL.", flush=True)
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

        print(f"[EMAIL] Sent to {RECEIVER_EMAIL}: {subject}", flush=True)
        return True
    except Exception as exc:
        print(f"[EMAIL] FAILED: {type(exc).__name__}: {exc}", flush=True)
        return False


# -----------------------------------------------------------------------------
# ZenRows live-usage budget and dynamic pacing
# -----------------------------------------------------------------------------
def get_subscription_details():
    """Read official ZenRows real-time subscription usage without a Fetch call."""
    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY is missing")

    response = requests.get(
        ZENROWS_SUBSCRIPTION_URL,
        headers={"X-API-Key": ZENROWS_API_KEY},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    usage = float(payload["usage"])
    period_start = parse_provider_time(payload["period_starts_at"])
    period_end = parse_provider_time(payload["period_ends_at"])
    if period_end <= period_start:
        raise ValueError("ZenRows subscription response has invalid billing boundaries")

    return {
        "status": payload.get("status", "UNKNOWN"),
        "usage": usage,
        "usage_percent": payload.get("usage_percent"),
        "period_start": period_start,
        "period_end": period_end,
    }


def prepare_budget(state):
    """Use real ZenRows usage plus observed Fetch cost to decide whether to fetch."""
    details = get_subscription_details()
    now_utc = datetime.now(timezone.utc)
    total_seconds = (details["period_end"] - details["period_start"]).total_seconds()
    elapsed_seconds = max(0, min((now_utc - details["period_start"]).total_seconds(), total_seconds))

    prior_budget = state.get("budget", {})
    expected_next_cost = float(prior_budget.get("expected_next_fetch_credits", WORST_CASE_FETCH_COST))
    expected_next_cost = max(1.0, min(WORST_CASE_FETCH_COST, expected_next_cost))

    target_usage_by_now = max(
        expected_next_cost,
        FREE_CREDIT_LIMIT * elapsed_seconds / total_seconds,
    )
    next_fetch_usage_threshold = min(FREE_CREDIT_LIMIT, details["usage"] + expected_next_cost)
    next_due_seconds = (next_fetch_usage_threshold / FREE_CREDIT_LIMIT) * total_seconds
    next_due_utc = details["period_start"] + timedelta(seconds=next_due_seconds)

    state["budget"] = {
        "plan_status": details["status"],
        "provider_period_start": details["period_start"].astimezone(HK_TZ).strftime("%Y-%m-%d %H:%M:%S HK"),
        "provider_period_end": details["period_end"].astimezone(HK_TZ).strftime("%Y-%m-%d %H:%M:%S HK"),
        "provider_usage_credits": round(details["usage"], 3),
        "provider_usage_percent": details["usage_percent"],
        "free_credit_limit": FREE_CREDIT_LIMIT,
        "worst_case_next_fetch_cost": WORST_CASE_FETCH_COST,
        "expected_next_fetch_credits": round(expected_next_cost, 3),
        "remaining_credits_before_worst_case_fetch": round(
            FREE_CREDIT_LIMIT - details["usage"] - WORST_CASE_FETCH_COST, 3
        ),
        "target_usage_by_now": round(target_usage_by_now, 3),
        "next_fetch_usage_threshold": round(next_fetch_usage_threshold, 3),
        "next_fetch_due_after": next_due_utc.astimezone(HK_TZ).strftime("%Y-%m-%d %H:%M:%S HK"),
        "pacing_strategy": "real-time ZenRows usage plus observed-cost linear catch-up pacing",
    }
    return details, target_usage_by_now, expected_next_cost


def can_make_fetch(details, target_usage_by_now, expected_next_cost):
    usage = details["usage"]
    if usage + WORST_CASE_FETCH_COST > FREE_CREDIT_LIMIT:
        return False, "PAUSED: next 25-credit protected Fetch could exceed the 5,000-credit free limit"
    if usage + expected_next_cost > target_usage_by_now:
        return False, "WAITING: dynamic pacing is reserving credits for the rest of the ZenRows billing period"
    return True, ""


def request_levis_from_zenrows():
    """Fetch Levi's using ZenRows' documented RESP001 remedies for protected pages."""
    params = {
        "apikey": ZENROWS_API_KEY,
        "url": LEVIS_URL,
        "js_render": "true",
        "antibot": "true",          # CRITICAL: Bypasses Akamai Bot Manager
        "premium_proxy": "true",
        "proxy_country": "us",
        "wait": "5000",             # Allow React/Angular components to mount
        "original_status": "true",
        "custom_headers": "true",
    }
    headers = {"Referer": "https://www.google.com/"}
    return requests.get(ZENROWS_FETCH_URL, params=params, headers=headers, timeout=180)


def extract_visible_text(content):
    """Remove any residual non-visible markup before matching promotion text."""
    soup = BeautifulSoup(content or "", "html.parser")
    for ignored_element in soup(["script", "style", "noscript", "template"]):
        ignored_element.decompose()
    return soup.get_text(" ", strip=True)


# -----------------------------------------------------------------------------
# Levi's promotion detection and five-email reminder rule
# -----------------------------------------------------------------------------
def detect_promotion(visible_text):
    exact_match = re.search(re.escape(EXACT_PROMOTION_TEXT), visible_text, re.IGNORECASE)
    if exact_match:
        start = max(0, exact_match.start() - 120)
        end = min(len(visible_text), exact_match.end() + 220)
        return "Extra 50% Off Applied at Checkout", shorten(visible_text[start:end], 420)

    fifty_match = FIFTY_OFF_PATTERN.search(visible_text)
    if fifty_match:
        start = max(0, fifty_match.start() - 120)
        end = min(len(visible_text), fifty_match.end() + 220)
        return "Visible 50% Off phrase", shorten(visible_text[start:end], 420)

    return "", ""


def check_levis(state):
    monitor = state["levis"]
    monitor["last_check"] = get_hk_time()

    try:
        details, target_usage_by_now, expected_next_cost = prepare_budget(state)
    except Exception as exc:
        monitor["status"] = f"ERROR: Could not read ZenRows live usage; Fetch skipped ({str(exc)[:140]})"
        log_event(state, "BUDGET", monitor["status"])
        return state

    allowed, reason = can_make_fetch(details, target_usage_by_now, expected_next_cost)
    if not allowed:
        monitor["status"] = reason
        log_event(state, "LEVIS", reason)
        return state

    try:
        usage_before_fetch = details["usage"]
        response = request_levis_from_zenrows()
        content = response.text or ""
        headers = response.headers
        monitor["fetch"] = {
            "when": get_hk_time(),
            "zenrows_http_status": response.status_code,
            "target_original_status": headers.get("Original-Status"),
            "provider_request_cost": headers.get("X-Request-Cost"),
            "provider_request_id": headers.get("X-Request-Id", ""),
            "final_url": headers.get("Zr-Final-Url", LEVIS_URL),
            "response_length": len(content),
            "response_sample": shorten(content, 360),
            "request_mode": "js_render + antibot + premium_proxy + proxy_country=us + wait=5000",
        }

        if response.status_code != 200:
            try:
                provider_error = response.json()
            except ValueError:
                provider_error = {}
            monitor["fetch"]["provider_error_code"] = provider_error.get("code", "")
            monitor["fetch"]["provider_error_title"] = provider_error.get("title", "")

        if response.status_code == 200:
            try:
                after_fetch = get_subscription_details()
                actual_cost = max(0.0, after_fetch["usage"] - usage_before_fetch)
                if actual_cost > 0:
                    state["budget"]["observed_last_fetch_credits"] = round(actual_cost, 3)
                    state["budget"]["expected_next_fetch_credits"] = round(
                        min(WORST_CASE_FETCH_COST, actual_cost), 3
                    )
                    state["budget"]["provider_usage_after_fetch"] = round(after_fetch["usage"], 3)
            except Exception as usage_exc:
                state["budget"]["usage_refresh_error"] = str(usage_exc)[:140]

        if response.status_code != 200:
            provider_code = monitor["fetch"].get("provider_error_code", "")
            provider_title = monitor["fetch"].get("provider_error_title", "")
            monitor["status"] = (
                f"ERROR: ZenRows HTTP {response.status_code} {provider_code} {provider_title}; "
                "promotion state preserved"
            ).strip()
            log_event(state, "LEVIS", monitor["status"])
            return state

        visible_text = extract_visible_text(content)
        monitor["fetch"]["visible_text_length"] = len(visible_text)
        monitor["fetch"]["visible_text_sample"] = shorten(visible_text, 360)

        if len(visible_text) < 100:
            monitor["status"] = "UNVERIFIED: ZenRows response had too little visible text; promotion state preserved"
            log_event(state, "LEVIS", monitor["status"])
            return state

        if any(marker in visible_text.casefold() for marker in BLOCK_MARKERS):
            monitor["status"] = "UNVERIFIED: Target access-check text detected; promotion state preserved"
            monitor["promotion_evidence"] = monitor["fetch"]["visible_text_sample"]
            log_event(state, "LEVIS", monitor["status"])
            return state

        criterion, evidence = detect_promotion(visible_text)
        if not criterion:
            monitor["promotion_active"] = False
            monitor["reminder_count"] = 0
            monitor["detected_criterion"] = ""
            monitor["promotion_evidence"] = monitor["fetch"]["visible_text_sample"]
            monitor["status"] = "OK: Fetch succeeded; neither 50%-off criterion is visible"
            log_event(state, "LEVIS", "Fetch OK; neither 50%-off criterion is present.")
            return state

        monitor["detected_criterion"] = criterion
        monitor["promotion_evidence"] = evidence
        log_event(state, "LEVIS", f"Fetch OK; promotion criterion detected: {criterion}.")

        if not monitor.get("promotion_active", False):
            success = send_email(
                "[ALERT] Levi's: 50% Off Promotion Detected (1/5)",
                "A requested Levi's promotion criterion is visible.\n\n"
                f"Criterion: {criterion}\n"
                f"Evidence: {evidence}\n\n"
                f"Product: {LEVIS_URL}\n"
                f"Checked: {get_hk_time()}",
            )
            if success:
                monitor["promotion_active"] = True
                monitor["reminder_count"] = 1
                monitor["status"] = f"ALERTING: {criterion}; email 1/5 sent"
                log_event(state, "LEVIS", "Promotion alert email 1/5 sent.")
            else:
                monitor["status"] = "EMAIL FAILED: Promotion detected; will retry on the next permitted Fetch"
                log_event(state, "LEVIS", "Promotion detected but email failed; state not advanced.")

        elif monitor.get("reminder_count", 0) < 5:
            reminder_number = monitor["reminder_count"] + 1
            success = send_email(
                f"[REMINDER] Levi's: 50% Off Promotion ({reminder_number}/5)",
                "A requested Levi's promotion criterion is still visible.\n\n"
                f"Criterion: {criterion}\n"
                f"Evidence: {evidence}\n\n"
                f"Product: {LEVIS_URL}\n"
                f"Checked: {get_hk_time()}",
            )
            if success:
                monitor["reminder_count"] = reminder_number
                monitor["status"] = f"ALERTING: {criterion}; email {reminder_number}/5 sent"
                log_event(state, "LEVIS", f"Promotion reminder email {reminder_number}/5 sent.")
            else:
                monitor["status"] = "EMAIL FAILED: Promotion persists; will retry current reminder"
                log_event(state, "LEVIS", "Promotion reminder email failed; counter not advanced.")

        else:
            monitor["status"] = "PAUSED: Promotion persists; 5/5 email reminders already sent"
            log_event(state, "LEVIS", "Promotion remains visible; reminder cap reached.")

    except requests.RequestException as exc:
        monitor["status"] = f"ERROR: ZenRows Fetch failed; promotion state preserved ({str(exc)[:140]})"
        log_event(state, "LEVIS", monitor["status"])
    except Exception as exc:
        monitor["status"] = f"ERROR: Levi's monitor failed; promotion state preserved ({str(exc)[:140]})"
        log_event(state, "LEVIS", monitor["status"])

    return state


# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
def main():
    if (
        os.environ.get("STOP_ALERTS", "false").lower() == "true"
        or os.environ.get("STOP_LEVIS_ALERTS", "false").lower() == "true"
    ):
        print("[SYSTEM] Levi's monitoring is stopped by STOP_ALERTS or STOP_LEVIS_ALERTS.", flush=True)
        return

    state = load_state()
    state["system"]["last_run"] = get_hk_time()
    state["system"]["total_runs"] += 1
    log_event(state, "SYSTEM", f"ZenRows Levi's cycle {state['system']['total_runs']} started.")

    state = check_levis(state)

    state["system"]["last_completed"] = get_hk_time()
    log_event(state, "SYSTEM", "ZenRows Levi's cycle complete.")
    save_state(state)

    print("\n--- FINAL LEVIS ZENROWS STATE (levis_state.json) ---", flush=True)
    print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
    print("--- END LEVIS ZENROWS STATE ---\n", flush=True)


if __name__ == "__main__":
    main()
