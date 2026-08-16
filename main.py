import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta
from twilio.rest import Client

# --- Configuration ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "whatsapp:+17372507786")
MY_PHONE = os.environ.get("MY_PHONE")

FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]

LEVIS_CACHE_URL = "https://webcache.googleusercontent.com/search?q=cache:https://www.levi.com/US/en_US/"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

# HK Timezone (UTC+8 )
HK_TZ = timezone(timedelta(hours=8))

def get_hk_time():
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp(msg):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and MY_PHONE):
        print("Twilio credentials missing. Message not sent.")
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        to_number = MY_PHONE if MY_PHONE.startswith("whatsapp:") else f"whatsapp:{MY_PHONE}"
        from_number = TWILIO_FROM if TWILIO_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_FROM}"
        
        message = client.messages.create(
            body=msg,
            from_=from_number,
            to=to_number
        )
        print(f"Twilio message sent successfully. SID: {message.sid}")
        return True
    except Exception as e:
        print(f"Twilio Error: {e}")
        return False

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
    if len(state["history"]) > 40: state["history"] = state["history"][-40:]
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
                success = send_whatsapp(f"🟢 *OWNDAYS STOCK (1/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
                mon["status"] = "ALERTING: 1/5 reminders sent" if success else "ERROR: Twilio failed to send"
            elif mon["count"] < 5:
                mon["count"] += 1
                success = send_whatsapp(f"🟢 *OWNDAYS REMINDER ({mon['count']}/5)* 🟢\n{OWNDAYS_URL}")
                mon["status"] = f"ALERTING: {mon['count']}/5 reminders sent" if success else "ERROR: Twilio failed to send"
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
                success = send_whatsapp(f"🚨 *LEVI'S SALE (1/5)* 🚨\nFound: *{sale.upper()}*")
                mon["status"] = f"ALERTING: New {sale} found (1/5)" if success else "ERROR: Twilio failed"
            elif mon["count"] < 5:
                mon["count"] += 1
                success = send_whatsapp(f"🚨 *LEVI'S REMINDER ({mon['count']}/5)* 🚨\n*{sale.upper()}* active")
                mon["status"] = f"ALERTING: {sale} active ({mon['count']}/5)" if success else "ERROR: Twilio failed"
            else:
                mon["status"] = f"PAUSED: 5/5 reminders for {sale} done"
            mon["sale"] = sale
        else:
            mon["sale"], mon["count"] = "", 0
            mon["status"] = "Monitoring: No Sale Found"
    except Exception as e: mon["status"] = f"ERROR: {str(e)[:50]}"
    return state

def kill_facebook_modals(page):
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
    fb_monitors = state["monitors"].setdefault("fb", {})
    f = fb_monitors.setdefault(url, {"status": "Initializing", "last_post_text": "", "last_check": ""})
    f["last_check"] = get_hk_time()
    
    try:
        page.goto(base_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        
        kill_facebook_modals(page)
        
        body_text = page.inner_text("body")
        if "Log In" in body_text and "Email address or mobile number" in body_text:
            page.mouse.wheel(0, 500)
            time.sleep(3)

        # Grab post text using Facebook's exact preview attribute from fbtest.rtf
        post_elements = page.locator("div[data-ad-comet-preview='message']").all()
        latest_text = ""
        
        if post_elements:
            latest_text = post_elements[0].inner_text().strip()
        
        if not latest_text:
            posts = page.locator("div[role='article']").all()
            if posts:
                latest_text = posts[0].inner_text().strip()[:200]

        if not latest_text:
            snippet = body_text.replace('\n', ' ')[:80]
            f["status"] = f"Idle: No post text found. Snippet: {snippet}"
            log_event(state, f"FB No post text found for {base_url}.")
            return state

        cleaned_text = ' '.join(latest_text.split())

        if f.get("last_post_text") != cleaned_text:
            f["last_post_text"] = cleaned_text
            msg = f"📱 *NEW FB POST* 📱\nPage: {url}\n\n{cleaned_text[:300]}"
            success = send_whatsapp(msg)
            
            if success:
                f["status"] = f"NEW POST NOTIFIED: {cleaned_text[:50]}..."
                log_event(state, f"FB WhatsApp sent for {url}")
            else:
                f["status"] = f"ERROR: Twilio failed for new post on {url}"
                log_event(state, f"FB Twilio failed for {url}")
        else:
            f["status"] = f"Idle: Up to date ({cleaned_text[:30]}...)"
            
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
