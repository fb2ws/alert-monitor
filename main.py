import os
import json
import time
import random
import requests
import re
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta
from twilio.rest import Client

# --- Configuration ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM") # e.g. whatsapp:+17372507786
TWILIO_CONTENT_SID = os.environ.get("TWILIO_CONTENT_SID") # Your approved Content Template SID
MY_PHONE = os.environ.get("MY_PHONE") # e.g. whatsapp:+85212345678

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
        print("Twilio credentials missing.")
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        to_num = MY_PHONE if MY_PHONE.startswith("whatsapp:") else f"whatsapp:{MY_PHONE}"
        from_num = TWILIO_FROM if TWILIO_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_FROM}"
        
        # Using ContentSid to bypass the 24h window restriction
        if TWILIO_CONTENT_SID:
            message = client.messages.create(
                from_=from_num,
                to=to_num,
                content_sid=TWILIO_CONTENT_SID,
                content_variables=json.dumps({"1": msg})
            )
        else:
            message = client.messages.create(body=msg, from_=from_num, to=to_num)
            
        print(f"Twilio Success! SID: {message.sid}")
        return True
    except Exception as e:
        print(f"--- TWILIO ERROR ---")
        print(f"Error: {str(e)}")
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
                success = send_whatsapp(f"🟢 OWNDAYS STOCK (1/5): Item is IN STOCK! {OWNDAYS_URL}")
                mon["status"] = "ALERTING: 1/5 reminders sent" if success else "ERROR: Twilio failed"
            elif mon["count"] < 5:
                mon["count"] += 1
                success = send_whatsapp(f"🟢 OWNDAYS REMINDER ({mon['count']}/5): {OWNDAYS_URL}")
                mon["status"] = f"ALERTING: {mon['count']}/5 reminders sent" if success else "ERROR: Twilio failed"
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
                success = send_whatsapp(f"🚨 LEVI'S SALE (1/5): Found {sale.upper()}!")
                mon["status"] = f"ALERTING: New {sale} found (1/5)" if success else "ERROR: Twilio failed"
            elif mon["count"] < 5:
                mon["count"] += 1
                success = send_whatsapp(f"🚨 LEVI'S REMINDER ({mon['count']}/5): {sale.upper()} active")
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
        close_buttons = page.locator("div[aria-label='Close'], div[role='button']:has-text('Close')").all()
        for btn in close_buttons:
            if btn.is_visible():
                btn.click()
                time.sleep(1)
    except:
        pass

def check_facebook(page, url, state):
    base_url = url.rstrip('/')
    fb_monitors = state["monitors"].setdefault("fb", {})
    f = fb_monitors.setdefault(url, {"status": "Init", "last_post_text": "", "last_check": ""})
    f["last_check"] = get_hk_time()
    
    try:
        page.goto(base_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        kill_facebook_modals(page)
        
        post_container = page.locator("div[data-ad-comet-preview='message']").first
        raw_text = ""
        if post_container.is_visible():
            text_blocks = post_container.locator("div[dir='auto']").all_inner_texts()
            raw_text = " ".join(text_blocks)
        else:
            raw_text = page.locator("div[role='article']").first.inner_text()

        if not raw_text:
            f["status"] = "Idle: No post content"
            return state

        cleaned = raw_text.replace("See more", "")
        cleaned = re.sub(r"(All reactions:.*|Like\s+Comment.*|View more.*|\d+[dhms]\s+\u00b7)", "", cleaned, flags=re.IGNORECASE)
        final_text = ' '.join(cleaned.split()).strip()[:600]
        
        if len(final_text) < 5:
            f["status"] = "Idle: Content too short"
            return state

        if f.get("last_post_text") != final_text:
            f["last_post_text"] = final_text
            msg = f"📱 NEW FB POST on {url.split('/')[-1]}:\n\n{final_text}"
            success = send_whatsapp(msg)
            if success:
                f["status"] = f"NOTIFIED: {final_text[:30]}..."
            else:
                f["status"] = "ERROR: Twilio failed"
        else:
            f["status"] = f"Idle: Up to date"
            
    except Exception as e: 
        f["status"] = f"ERROR: {str(e)[:50]}"
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
    
    print("\n--- FINAL STATE REPORT ---")
    print(json.dumps(state, indent=2))
    print("--------------------------\n")

if __name__ == "__main__": main()
