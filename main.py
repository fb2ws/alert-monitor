import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright
from datetime import datetime

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]

# Using Google Cache to bypass Levi's Akamai protection
LEVIS_CACHE_URL = "https://webcache.googleusercontent.com/search?q=cache:https://www.levi.com/US/en_US/"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

def send_whatsapp(msg ):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg )}&apikey={API_KEY}"
    try: requests.get(url, timeout=15)
    except: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try: 
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"levis": {"sale": "", "count": 0, "last_check": ""}, "owndays": {"in_stock": False, "count": 0, "last_check": ""}, "fb": {}, "history": []}

def log_event(state, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    if "history" not in state: state["history"] = []
    state["history"].append(entry)
    if len(state["history"]) > 50: state["history"] = state["history"][-50:]
    return state

def check_owndays(page, state):
    log_event(state, "Checking Owndays stock...")
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(random.uniform(5, 8))
        body = page.inner_text("body").lower()
        is_in_stock = "out of stock online" not in body
        o = state.get("owndays", {"in_stock": False, "count": 0, "last_check": ""})
        o["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if is_in_stock:
            if not o["in_stock"]: 
                o["count"] = 1
                send_whatsapp(f"🟢 *OWNDAYS STOCK ALERT (1/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
                log_event(state, "Owndays: IN STOCK - Alert sent (1/5)")
            elif o.get("count", 0) < 5: 
                o["count"] = o.get("count", 0) + 1
                send_whatsapp(f"🟢 *OWNDAYS REMINDER ({o['count']}/5)* 🟢\nStill available!\n{OWNDAYS_URL}")
                log_event(state, f"Owndays: IN STOCK - Reminder sent ({o['count']}/5)")
            o["in_stock"] = True
        else:
            if o["in_stock"]: log_event(state, "Owndays: Now OUT OF STOCK")
            o["in_stock"], o["count"] = False, 0
        state["owndays"] = o
    except Exception as e: log_event(state, f"Owndays Error: {str(e)[:100]}")
    return state

def check_levis(page, state):
    log_event(state, "Checking Levi's Sale (via Google Cache)...")
    try:
        page.goto(LEVIS_CACHE_URL, timeout=60000)
        time.sleep(random.uniform(5, 8))
        content = page.content().lower()
        sale = next((kw for kw in ["50% off", "60% off", "70% off", "half off", "extra 50%"] if kw in content), "")
        l = state.get("levis", {"sale": "", "count": 0, "last_check": ""})
        l["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if sale:
            if l["sale"] != sale: 
                l["count"] = 1
                send_whatsapp(f"🚨 *LEVI'S SALE ALERT (1/5)* 🚨\nFound: *{sale.upper()}*\n(via Google Cache)")
                log_event(state, f"Levi's: {sale.upper()} found - Alert sent (1/5)")
            elif l.get("count", 0) < 5: 
                l["count"] = l.get("count", 0) + 1
                send_whatsapp(f"🚨 *LEVI'S REMINDER ({l['count']}/5)* 🚨\n*{sale.upper()}* active!")
                log_event(state, f"Levi's: {sale.upper()} active - Reminder sent ({l['count']}/5)")
            l["sale"] = sale
        else:
            if l["sale"]: log_event(state, "Levi's: Sale ended")
            l["sale"], l["count"] = "", 0
        state["levis"] = l
    except Exception as e: log_event(state, f"Levi's Error: {str(e)[:100]}")
    return state

def check_facebook(page, url, state):
    base_url = url.rstrip('/')
    photo_url = base_url + "/photos/"
    log_event(state, f"Checking FB Activity: {photo_url}")
    try:
        page.goto(photo_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        photo_links = page.locator("a[href*='/photo/']").all()
        latest_id = ""
        for link in photo_links:
            href = link.get_attribute("href")
            if "/photo/" in href:
                latest_id = href.split("fbid=")[1].split("&")[0] if "fbid=" in href else href.split("/photo/")[1].split("/")[0]
                break
        if not latest_id:
            log_event(state, f"FB: No activity found for {base_url}.")
            return state
        if "fb" not in state: state["fb"] = {}
        f = state["fb"].get(url, {"id": "", "count": 0, "last_check": ""})
        f["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if f["id"] != latest_id:
            f["id"], f["count"] = latest_id, 1
            send_whatsapp(f"📱 *NEW FB POST* 📱\nPage: {url}\nCheck: {photo_url}")
            log_event(state, f"FB: New post on {base_url} - Alert sent (Once)")
        else:
            log_event(state, f"FB: No new post on {base_url} (ID: {latest_id})")
        state["fb"][url] = f
    except Exception as e: log_event(state, f"FB Error ({base_url}): {str(e)[:100]}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    time.sleep(random.uniform(5, 20))
    state = load_state()
    log_event(state, "--- STARTING MONITORING CYCLE ---")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            page = context.new_page()
            state = check_owndays(page, state)
            state = check_levis(page, state)
            for url in FB_PAGES: state = check_facebook(page, url, state)
            browser.close()
    except Exception as e: log_event(state, f"GLOBAL CRASH: {str(e)[:200]}")
    log_event(state, "--- CYCLE COMPLETE ---")
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

if __name__ == "__main__": main()
