import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FACEBOOK_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

def send_whatsapp_alert(message ):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message )}&apikey={API_KEY}"
    try: requests.get(url, timeout=15)
    except: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE, "r"))
        except: pass
    return {"levis": {"sale": "", "count": 0}, "owndays": {"in_stock": False, "count": 0}, "fb": {}}

def check_owndays(page, state):
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(5)
        is_in_stock = "out of stock online" not in page.inner_text("body").lower()
        o = state.get("owndays", {"in_stock": False, "count": 0})
        if is_in_stock:
            if not o["in_stock"] or o["count"] < 5:
                o["count"] += 1
                send_whatsapp_alert(f"🟢 *OWNDAYS STOCK ({o['count']}/5)* 🟢\nIN STOCK!\n{OWNDAYS_URL}")
            o["in_stock"] = True
        else: o["in_stock"], o["count"] = False, 0
        state["owndays"] = o
    except: pass
    return state

def check_levis(page, state):
    try:
        page.goto(LEVIS_URL, timeout=60000)
        time.sleep(5)
        content = page.content().lower()
        sale = next((kw for kw in ["50% off", "60% off", "70% off", "half off"] if kw in content), "")
        l = state.get("levis", {"sale": "", "count": 0})
        if sale:
            if sale != l["sale"] or l["count"] < 5:
                if sale != l["sale"]: l["count"] = 0
                l["count"] += 1
                send_whatsapp_alert(f"🚨 *LEVI'S SALE ({l['count']}/5)* 🚨\nFound: *{sale.upper()}*\n{LEVIS_URL}")
            l["sale"] = sale
        else: l["sale"], l["count"] = "", 0
        state["levis"] = l
    except: pass
    return state

def check_facebook(page, url, state):
    # Convert standard URL to M-Basic URL for extreme stability
    m_url = url.replace("www.facebook.com", "mbasic.facebook.com")
    print(f"Checking FB (M-Basic): {m_url}")
    
    try:
        page.goto(m_url, timeout=60000)
        time.sleep(random.uniform(5, 8))
        
        # M-Basic specific logic: Look for the first 'article' or 'story'
        # We extract all text and look for the first significant block
        full_text = page.inner_text("body")
        
        # --- THE NUCLEAR FILTER ---
        # If the page contains these words, it is 100% a login wall.
        blacklist = ["mobile number", "email", "password", "log in", "forgot password", "create account", "sign up"]
        if any(word in full_text.lower() for word in blacklist):
            print(f"⚠️ LOGIN WALL DETECTED for {url}. Aborting to prevent false alert.")
            return state

        # Find the first real post text (M-Basic posts are usually in <div> or <p> tags)
        # We skip the header/intro by looking for text after the 'Intro' or 'About' section
        lines = [line.strip() for line in full_text.split('\n') if len(line.strip()) > 20]
        post_text = ""
        for line in lines:
            # Skip common header text
            if any(x in line.lower() for x in ["followers", "following", "likes", "intro", "about", "photos"]):
                continue
            post_text = line
            break

        if not post_text: post_text = page.title()

        new_snippet = post_text[:15] # 15 chars for better uniqueness
        if "fb" not in state: state["fb"] = {}
        
        old_snippet = state["fb"].get(url)
        # Only alert if we have an old state AND it's actually different
        if old_snippet and old_snippet != new_snippet:
            preview = post_text[:150].replace('\n', ' ')
            send_whatsapp_alert(f"📱 *NEW FB POST* 📱\nPage: {url}\n\nPreview: {preview}...")
            print(f"✅ Real Alert Sent for {url}")
        
        state["fb"][url] = new_snippet
    except Exception as e: print(f"FB Error: {e}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    time.sleep(random.uniform(5, 15))
    state = load_state()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        # Use a mobile-like user agent for M-Basic consistency
        context = browser.new_context(user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1")
        page = context.new_page()
        
        state = check_owndays(page, state)
        state = check_levis(page, state)
        for url in FACEBOOK_PAGES: state = check_facebook(page, url, state)
        browser.close()
    
    json.dump(state, open(STATE_FILE, "w"))
    print("Stealth cycle complete.")

if __name__ == "__main__": main()
