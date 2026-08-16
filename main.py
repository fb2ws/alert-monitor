import os
import json
import time
import random
import requests
import traceback
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta
from twilio.rest import Client

# --- Configuration ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM") # e.g. whatsapp:+17372507786
MY_PHONE = os.environ.get("MY_PHONE") # e.g. whatsapp:+85212345678

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
        
        message = client.messages.create(body=msg, from_=from_num, to=to_num)
        print(f"Twilio Success! SID: {message.sid}")
        return True
    except Exception as e:
        # CRITICAL: Print full error to console for user to see
        print(f"--- TWILIO ERROR DIAGNOSTIC ---")
        print(f"Error Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        if hasattr(e, 'code'): print(f"Twilio Error Code: {e.code}")
        return False

def load_state():
    if os.path.exists(STATE_FILE):
        try: 
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: pass
    return {"system": {"total_runs": 0}, "monitors": {"fb": {}}, "history": []}

def check_facebook(page, url, state):
    base_url = url.rstrip('/')
    fb_monitors = state["monitors"].setdefault("fb", {})
    f = fb_monitors.setdefault(url, {"status": "Init", "last_post_text": "", "last_check": ""})
    f["last_check"] = get_hk_time()
    
    try:
        page.goto(base_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        
        # Aggressive text extraction (600 chars)
        post_elements = page.locator("div[data-ad-comet-preview='message'], div[role='article']").all()
        latest_text = ""
        if post_elements:
            latest_text = post_elements[0].inner_text().strip()
        
        if not latest_text:
            f["status"] = "Idle: No post text found"
            return state

        cleaned_text = ' '.join(latest_text.split())
        
        # Compare text (removed 'id' field as requested)
        if f.get("last_post_text") != cleaned_text:
            f["last_post_text"] = cleaned_text
            msg = f"📱 *NEW FB POST* 📱\nPage: {url}\n\n{cleaned_text[:600]}"
            if send_whatsapp(msg):
                f["status"] = f"NEW POST NOTIFIED: {cleaned_text[:40]}..."
            else:
                f["status"] = "ERROR: Twilio delivery failed (See Logs)"
        else:
            f["status"] = f"Idle: Up to date ({cleaned_text[:30]}...)"
            
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
        
        for url in FB_PAGES: 
            state = check_facebook(page, url, state)
            
        browser.close()
    
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

if __name__ == "__main__": main()
