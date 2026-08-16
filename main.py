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

def send_whatsapp_test(msg: str) -> bool:
    print("\n==================================================")
    print("           TWILIO WHATSAPP TEST RUNNER            ")
    print("==================================================")

    # 1. Inspect Environment Variables
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    my_phone = os.environ.get("MY_PHONE")
    twilio_from = os.environ.get("TWILIO_FROM")
    content_sid = os.environ.get("TWILIO_CONTENT_SID")

    print("[1/5] Checking Environment Variables...")
    print(f"  • TWILIO_ACCOUNT_SID: {'✅ Detected (' + account_sid[:6] + '...)' if account_sid else '❌ MISSING'}")
    print(f"  • TWILIO_AUTH_TOKEN:  {'✅ Detected (' + '*' * 8 + ')' if auth_token else '❌ MISSING'}")
    print(f"  • MY_PHONE:           {'✅ Detected (' + str(my_phone) + ')' if my_phone else '❌ MISSING'}")
    print(f"  • TWILIO_FROM:        {'✅ Detected (' + str(twilio_from) + ')' if twilio_from else '❌ MISSING'}")
    print(f"  • TWILIO_CONTENT_SID: {'ℹ️  Detected (' + content_sid + ')' if content_sid else 'ℹ️  Not set (Using Freeform)'}")

    if not all([account_sid, auth_token, my_phone, twilio_from]):
        print("\n❌ CRITICAL ERROR: Required environment variables are missing! Aborting test.")
        return False

    # 2. Format Phone Numbers
    to_num = my_phone if my_phone.startswith("whatsapp:") else f"whatsapp:{my_phone}"
    from_num = twilio_from if twilio_from.startswith("whatsapp:") else f"whatsapp:{twilio_from}"

    print("\n[2/5] Formatted Target Destinations...")
    print(f"  • Sender (From):     {from_num}")
    print(f"  • Recipient (To):    {to_num}")

    try:
        # 3. Initialize Twilio Client
        print("\n[3/5] Initializing Twilio Client...")
        client = Client(account_sid, auth_token)

        # 4. Active Credential & Account Status Validation (API Call)
        print("[4/5] Validating Credentials with Twilio API...")
        account_info = client.api.v2010.accounts(account_sid).fetch()
        print(f"  • Account Name:   {account_info.friendly_name}")
        print(f"  • Account Status: {account_info.status.upper()}")
        print(f"  • Account Type:   {account_info.type.upper()}")
        print("  ✅ Credentials verified successfully with Twilio!")

        # 5. Dispatch Message
        print("\n[5/5] Attempting to Send WhatsApp Message...")
        if content_sid:
            print("  • Dispatch Mode: Content Template SID")
            payload = {"1": msg}
            print(f"  • Variable Payload: {json.dumps(payload)}")
            
            message = client.messages.create(
                from_=from_num,
                to=to_num,
                content_sid=content_sid,
                content_variables=json.dumps(payload)
            )
        else:
            print("  • Dispatch Mode: Direct Freeform Text")
            print(f"  • Text Body: \"{msg}\"")
            
            message = client.messages.create(
                body=msg,
                from_=from_num,
                to=to_num
            )

        print("\n==================================================")
        print(" 🎉 MESSAGE SENT SUCCESSFULLY!")
        print("==================================================")
        print(f"  • Message SID:  {message.sid}")
        print(f"  • Status:       {message.status}")
        print(f"  • Date Created: {message.date_created}")
        print(f"  • Price/Unit:   {message.price} {message.price_unit}")
        print("==================================================\n")
        return True

    except TwilioRestException as e:
        print("\n==================================================")
        print(" 🚨 TWILIO REST API ERROR DETECTED")
        print("==================================================")
        print(f"  • HTTP Status:  {e.status}")
        print(f"  • Error Code:   {e.code}")
        print(f"  • Details:      {e.msg}")
        if e.code == 20003:
            print("  💡 Tip: Auth Token or Account SID is invalid.")
        elif e.code == 63016:
            print("  💡 Tip: Message failed because recipient is outside the 24h window and no Content Template was used.")
        print("==================================================\n")
        return False
    except Exception as e:
        print("\n==================================================")
        print(" ❌ SYSTEM / PYTHON EXCEPTION")
        print("==================================================")
        print(f"  • Error Type: {type(e).__name__}")
        print(f"  • Details:    {str(e)}")
        print("==================================================\n")
        return False

def get_hk_time():
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S")

def send_whatsapp(msg):
    # 1. Safely retrieve variables (assuming they are set in your environment)
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    my_phone = os.environ.get("MY_PHONE")
    twilio_from = os.environ.get("TWILIO_FROM")
    content_sid = os.environ.get("TWILIO_CONTENT_SID")

    # 2. Check ALL required variables, importantly including the sender number
    if not all([account_sid, auth_token, my_phone, twilio_from]):
        print("Twilio credentials or phone numbers missing.")
        return False

    try:
        # 3. Initialize the Client
        client = Client(account_sid, auth_token)
        
        # Ensure numbers are formatted with the 'whatsapp:' prefix
        to_num = my_phone if my_phone.startswith("whatsapp:") else f"whatsapp:{my_phone}"
        from_num = twilio_from if twilio_from.startswith("whatsapp:") else f"whatsapp:{twilio_from}"
        
        # 4. Route the message request
        if content_sid:
            # Send using an approved Content Template to bypass the 24h rule
            # NOTE: Your Twilio template MUST have exactly one variable configured as {{1}}
            message = client.messages.create(
                from_=from_num,
                to=to_num,
                content_sid=content_sid,
                content_variables=json.dumps({"1": msg}) 
            )
        else:
            # Send a freeform message (Only works if recipient replied in the last 24 hours)
            message = client.messages.create(
                body=msg, 
                from_=from_num, 
                to=to_num
            )
            
        print(f"Twilio Success! SID: {message.sid}")
        return True

    # 5. Catch Twilio-specific errors to expose the actual API rejection reason
    except TwilioRestException as e:
        print(f"--- TWILIO API ERROR ---")
        print(f"Code: {e.code} | Status: {e.status}")
        print(f"Message: {e.msg}")
        return False
    except Exception as e:
        print(f"--- GENERAL ERROR ---")
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

if __name__ == "__main__":
    # Test execution message
    test_payload = "🤖 Test Run Alert: Twilio credentials verification active."
    send_whatsapp_test(test_payload)
