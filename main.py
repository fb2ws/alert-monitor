import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]

LEVIS_CACHE_URL = "https://webcache.googleusercontent.com/search?q=cache:https://www.levi.com/US/en_US/"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

# HK Timezone (UTC+8 )
HK_TZ = timezone(timedelta(hours=8))

def get_hk_time():
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp(msg):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg )}&apikey={API_KEY}"
    try: requests.get(url, timeout=15)
    except: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try: 
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if "monitors" in data: return data
        except: pass
    return {
        "system": {"last_run": "", "total_runs": 0},
        "monitors": {
            "levis": {"status": "Initializing", "sale": "", "count": 0, "last_check": ""},
            "owndays": {"status": "Initializing", "in_stock": False, "count": 0, "last_check": ""},
            "fb": {}
        },
        "history": []
    }

def log_event(state, message):
    ts = get_hk_time()
    entry = f"[{ts}] {message}"
    print(entry)
    state.setdefault("history", []).append(entry)
    if len(state["history"]) > 30: state["history"] = state["history"][-30:]
    return state

def check_owndays(page, state):
    mon = state["monitors"]["owndays"]
    mon["last_check"] = get_hk_time()
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(random.uniform(5, 8))
        body = page.inner_text("body").lower()
        is_in_stock = "out of stock online" not in body
        
        if is_in_stock:
            if not mon["in_stock"]:
                mon["count"] = 1
                send_whatsapp(f"🟢 *OWNDAYS STOCK (1/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
                mon["status"] = "ALERTING: 1/5 reminders sent"
            elif mon["count"] < 5:
                mon["count"] += 1
                send_whatsapp(f"🟢 *OWNDAYS REMINDER ({mon['count']}/5)* 🟢\n{OWNDAYS_URL}")
                mon["status"] = f"ALERTING: {mon['count']}/5 reminders sent"
            else:
                mon["status"] = "PAUSED: 5/5 reminders completed"
            mon["in_stock"] = True
        else:
            mon["in_stock"], mon["count"] = False, 0
            mon["status"] = "Monitoring: Out of Stock"
    except Exception as e: mon["status"] = f"ERROR: {str(e)[:50]}"
    return state

def check_levis(page, state):
    mon = state["monitors"]["levis"]
    mon["last_check"] = get_hk_time()
    try:
        page.goto(LEVIS_CACHE_URL, timeout=60000)
        time.sleep(random.uniform(5, 8))
        content = page.content().lower()
        sale = next((kw for kw in ["50% off", "60% off", "70% off", "half off"] if kw in content), "")
        
        if sale:
            if mon["sale"] != sale:
                mon["count"] = 1
                send_whatsapp(f"🚨 *LEVI'S SALE (1/5)* 🚨\nFound: *{sale.upper()}*")
                mon["status"] = f"ALERTING: New {sale} found (1/5)"
            elif mon["count"] < 5:
                mon["count"] += 1
                send_whatsapp(f"🚨 *LEVI'S REMINDER ({mon['count']}/5)* 🚨\n*{sale.upper()}* active")
                mon["status"] = f"ALERTING: {sale} active ({mon['count']}/5)"
            else:
                mon["status"] = f"PAUSED: 5/5 reminders for {sale} done"
            mon["sale"] = sale
        else:
            mon["sale"], mon["count"] = "", 0
            mon["status"] = "Monitoring: No Sale Found"
    except Exception as e: mon["status"] = f"ERROR: {str(e)[:50]}"
    return state

def kill_facebook_modals(page):
    """Attempt to close or remove login walls and cookie banners."""
    try:
        close_buttons = page.locator("div[aria-label='Close'], div[role='button']:has-text('Close'), i[data-visualcompletion='css-img']").all()
        for btn in close_buttons:
            if btn.is_visible():
                btn.click()
                time.sleep(1)
    except:
        pass

def check_facebook(page, url, state):
    base_url = url.rstrip('/')
    photo_url = base_url + "/photos/"
    fb_monitors = state["monitors"].setdefault("fb", {})
    f = fb_monitors.setdefault(url, {"status": "Initializing", "id": "", "last_check": ""})
    f["last_check"] = get_hk_time()
    
    try:
        page.goto(photo_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        
        kill_facebook_modals(page)
        
        body_text = page.inner_text("body")
        if "Log In" in body_text and "Email address or mobile number" in body_text:
            f["status"] = "Wall Detected: Hard Login Required"
            log_event(state, f"FB Wall hit on {base_url}")
            return state

        latest_id = ""
        photo_links = page.locator("a[href*='/photo/']").all()
        for link in photo_links:
            try:
                href = link.get_attribute("href")
                if href and "/photo/" in href:
                    if "fbid=" in href:
                        latest_id = href.split("fbid=")[1].split("&")[0]
                    else:
                        latest_id = href.split("/photo/")[1].split("/")[0]
                    break
            except:
                continue
        
        if not latest_id:
            post_links = page.locator("a[href*='/posts/']").all()
            for link in post_links:
                try:
                    href = link.get_attribute("href")
                    if href and "/posts/" in href:
                        latest_id = href.split("/posts/")[1].split("/")[0]
                        break
                except:
                    continue

        if not latest_id:
            snippet = body_text.replace('\n', ' ')[:80]
            f["status"] = f"Idle: No ID found. Body: {snippet}"
            log_event(state, f"FB No ID found for {base_url}. Snippet: {snippet}")
            return state

        if f["id"] != latest_id:
            old_id = f["id"]
            f["id"] = latest_id
            send_whatsapp(f"📱 *NEW FB POST* 📱\nPage: {url}")
            f["status"] = f"NEW POST: Notified ID {latest_id} (Prev: {old_id})"
            log_event(state, f"FB Alert sent for {url} (New ID: {latest_id})")
        else:
            f["status"] = f"Idle: Up to date (ID {latest_id})"
            
    except Exception as e: 
        f["status"] = f"ERROR: {str(e)[:50]}"
        log_event(state, f"FB Error ({base_url}): {str(e)[:100]}")
        
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    state = load_state()
    state["system"]["last_run"] = get_hk_time()
    state["system"]["total_runs"] = state["system"].get("total_runs", 0) + 1
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        
        state = check_owndays(page, state)
        state = check_levis(page, state)
        for url in FB_PAGES: 
            state = check_facebook(page, url, state)
            
        browser.close()
    
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

if __name__ == "__main__": main()
