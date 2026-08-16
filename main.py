import os
import json
import time
import random
import requests
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta

# --- Configuration ---
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]

LEVIS_CACHE_URL = "https://webcache.googleusercontent.com/search?q=cache:https://www.levi.com/US/en_US/"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

# HK Timezone (UTC+8 )
HK_TZ = timezone(timedelta(hours=8))

def get_hk_time():
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S")

def send_email(subject, body):
    if not (EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL):
        print("Email credentials missing.")
        return False
    try:
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = EMAIL_USER
        msg['To'] = RECEIVER_EMAIL
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, RECEIVER_EMAIL, msg.as_string())
        print(f"Email Success! Sent to {RECEIVER_EMAIL}")
        return True
    except Exception as e:
        print(f"--- EMAIL ERROR: {str(e)} ---")
        return False

def test_email_connection():
    """
    DELETE OR COMMENT OUT THIS FUNCTION CALL IN main() AFTER YOU RECEIVE THE TEST EMAIL
    """
    print("--- MANUAL EMAIL TEST INITIATED ---")
    success = send_email(
        "🛠️ [MANUAL TEST] Alert System Connection",
        f"If you are reading this, your Gmail SMTP settings are 100% correct!\n\nTime: {get_hk_time()}"
    )
    if success:
        print("TEST SUCCESSFUL! You can now remove the test_email_connection() call from main().")
    else:
        print("TEST FAILED. Check your EMAIL_USER and EMAIL_PASS secrets.")

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
                success = send_email("🟢 [ALERT] Owndays Glasses IN STOCK (1/5)", f"Item is IN STOCK!\n{OWNDAYS_URL}")
                mon["status"] = "ALERTING: 1/5 reminders sent" if success else "ERROR: Email failed"
            elif mon["count"] < 5:
                mon["count"] += 1
                success = send_email(f"🟢 [REMINDER] Owndays Glasses IN STOCK ({mon['count']}/5)", f"Item is still IN STOCK!\n{OWNDAYS_URL}")
                mon["status"] = f"ALERTING: {mon['count']}/5 reminders sent" if success else "ERROR: Email failed"
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
                success = send_email(f"🚨 [ALERT] Levi's Sale Found: {sale.upper()} (1/5)", f"Sale detected: {sale.upper()}!\nTime: {get_hk_time()}")
                mon["status"] = f"ALERTING: New {sale} found (1/5)" if success else "ERROR: Email failed"
            elif mon["count"] < 5:
                mon["count"] += 1
                success = send_email(f"🚨 [REMINDER] Levi's Sale: {sale.upper()} ({mon['count']}/5)", f"Levi's {sale.upper()} sale is still active!")
                mon["status"] = f"ALERTING: {sale} active ({mon['count']}/5)" if success else "ERROR: Email failed"
            else:
                mon["status"] = f"PAUSED: 5/5 reminders for {sale} done"
            mon["sale"] = sale
        else:
            mon["sale"], mon["count"] = "", 0
            mon["status"] = "Monitoring: No Sale Found"
    except Exception as e: mon["status"] = f"ERROR: {str(e)[:50]}"
    return state

def check_facebook(page, url, state):
    base_url = url.rstrip('/')
    fb_monitors = state["monitors"].setdefault("fb", {})
    f = fb_monitors.setdefault(url, {"status": "Init", "last_post_text": "", "last_check": ""})
    f["last_check"] = get_hk_time()
    
    try:
        page.goto(base_url, timeout=60000)
        time.sleep(random.uniform(8, 12))
        
        # Look for the first post container
        post_container = page.locator("div[data-ad-comet-preview='message'], div[role='article']").first
        raw_text = ""
        
        if post_container.is_visible():
            # Strategy: Grab content from div[dir="auto"] inside the message container
            # This is the cleanest way to avoid dates, likes, and comments
            text_blocks = post_container.locator("div[dir='auto']").all_inner_texts()
            raw_text = " ".join([t.strip() for t in text_blocks if t.strip()])

        if not raw_text:
            f["status"] = "Idle: No post content found"
            return state

        # Clean UI noise like "See more", reaction counts, and timestamps
        cleaned = raw_text.replace("See more", "").replace("See More", "")
        cleaned = re.sub(r"(All reactions:.*|Like\s+Comment.*|View more.*|\d+[dhms]\s+\u00b7)", "", cleaned, flags=re.IGNORECASE)
        final_text = ' '.join(cleaned.split()).strip()[:900]
        
        if len(final_text) < 5:
            f["status"] = "Idle: Content too short"
            return state

        if f.get("last_post_text") != final_text:
            f["last_post_text"] = final_text
            page_name = url.split('/')[-1] or url.split('/')[-2]
            subject = f"📱 NEW FB POST: {page_name}"
            body = f"New post detected on Facebook page:\n{url}\n\nContent:\n{final_text}\n\nTime: {get_hk_time()}"
            
            success = send_email(subject, body)
            if success:
                f["status"] = f"NOTIFIED: {final_text[:30]}..."
            else:
                f["status"] = "ERROR: Email failed"
        else:
            f["status"] = "Idle: Up to date"
            
    except Exception as e: 
        f["status"] = f"ERROR: {str(e)[:50]}"
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    
    # --- EMAIL TEST START ---
    # DELETE OR COMMENT OUT THE LINE BELOW AFTER YOU RECEIVE THE TEST EMAIL
    test_email_connection() 
    # --- EMAIL TEST END ---

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
