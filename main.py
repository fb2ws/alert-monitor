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
TWILIO_FROM = os.environ.get("TWILIO_FROM")
MY_PHONE = os.environ.get("MY_PHONE")
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]
STATE_FILE = "state.json"
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
        
        # Send message
        message = client.messages.create(body=msg, from_=from_num, to=to_num)
        print(f"Twilio Success! SID: {message.sid}")
        return True
    except Exception as e:
        print(f"--- TWILIO ERROR DIAGNOSTIC ---")
        print(f"Error: {str(e)}")
        print("HINT: Have you sent 'Hello' to your Twilio number in the last 24 hours?")
        return False

def load_state():
    if os.path.exists(STATE_FILE):
        try: 
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                # Ensure 'id' is removed from all monitors
                if "monitors" in state and "fb" in state["monitors"]:
                    for url in state["monitors"]["fb"]:
                        state["monitors"]["fb"][url].pop("id", None)
                return state
        except: pass
    return {"system": {"total_runs": 0}, "monitors": {"fb": {}}, "history": []}

def check_facebook(page, url, state):
    base_url = url.rstrip('/')
    fb_monitors = state["monitors"].setdefault("fb", {})
    f = fb_monitors.setdefault(url, {"status": "Init", "last_post_text": "", "last_check": ""})
    f.pop("id", None) # Remove ID if it exists
    f["last_check"] = get_hk_time()
    
    try:
        page.goto(base_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        
        # Target the message container
        post_container = page.locator("div[data-ad-comet-preview='message']").first
        raw_text = ""
        
        if post_container.is_visible():
            # Get all text blocks inside the message
            text_blocks = post_container.locator("div[dir='auto']").all_inner_texts()
            raw_text = " ".join(text_blocks)
        else:
            # Fallback to article role
            raw_text = page.locator("div[role='article']").first.inner_text()

        if not raw_text:
            f["status"] = "Idle: No post content"
            return state

        # CLEANING LOGIC: Remove reactions, dates, and "See more"
        # This regex removes things like "hahaphone.hk 2d", "All reactions: 129", "Like Comment"
        cleaned = raw_text.replace("See more", "")
        cleaned = re.sub(r"(All reactions:.*|Like\s+Comment.*|View more.*|\d+[dhms]\s+\u00b7)", "", cleaned, flags=re.IGNORECASE)
        cleaned = ' '.join(cleaned.split()).strip()
        
        # Limit to 900 chars (50% longer than before)
        final_text = cleaned[:900]
        
        if not final_text or len(final_text) < 5:
            f["status"] = "Idle: Content too short/filtered"
            return state

        if f.get("last_post_text") != final_text:
            f["last_post_text"] = final_text
            msg = f"📱 *NEW FB POST* 📱\nPage: {url}\n\n{final_text}"
            if send_whatsapp(msg):
                f["status"] = f"NOTIFIED: {final_text[:30]}..."
            else:
                f["status"] = "ERROR: Twilio 24h Window Closed"
        else:
            f["status"] = f"Idle: Up to date ({final_text[:20]}...)"
            
    except Exception as e: 
        f["status"] = f"ERROR: {str(e)[:50]}"
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    state = load_state()
    state["system"]["total_runs"] = state["system"].get("total_runs", 0) + 1
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        for url in FB_PAGES: state = check_facebook(page, url, state)
        browser.close()
    
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

if __name__ == "__main__": main()
