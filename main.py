import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright

# Safety import for Stealth Library
try:
    from fake_useragent import UserAgent
    ua_generator = UserAgent()
except:
    ua_generator = None

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
                send_whatsapp_alert(f"🟢 *OWNDAYS STOCK ({o['count']}/5)* 🟢\nSENICHI31 is IN STOCK!\n{OWNDAYS_URL}")
            o["in_stock"] = True
        else:
            o["in_stock"], o["count"] = False, 0
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
        else:
            l["sale"], l["count"] = "", 0
        state["levis"] = l
    except: pass
    return state

def check_facebook(page, url, state):
    try:
        page.goto(url, timeout=60000, wait_until="networkidle")
        time.sleep(8)
        post_text = ""
        for sel in ["div[data-ad-comet-preview='message']", "div[dir='auto']", "div[role='article']"]:
            el = page.locator(sel).first
            if el.is_visible():
                text = el.inner_text().strip()
                if len(text) > 5:
                    post_text = text
                    break
        if not post_text: post_text = page.title()
        
        # Anti-Bot Wall Filter
        if any(kw in post_text.lower() for kw in ["facebook", "log in", "sign up", "see more from"]):
            print(f"⚠️ Wall detected for {url}. Skipping.")
            return state

        new_snippet = post_text[:10]
        if "fb" not in state: state["fb"] = {}
        if state["fb"].get(url) and state["fb"].get(url) != new_snippet:
            preview = post_text[:150].replace('\n', ' ')
            send_whatsapp_alert(f"📱 *NEW FB POST* 📱\nPage: {url}\n\nPreview: {preview}...")
        state["fb"][url] = new_snippet
    except: pass
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    time.sleep(random.uniform(5, 15))
    state = load_state()
    
    # Choose identity: Random or Fallback
    my_ua = ua_generator.random if ua_generator else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=my_ua, viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        state = check_owndays(page, state)
        state = check_levis(page, state)
        for url in FACEBOOK_PAGES: state = check_facebook(page, url, state)
        browser.close()
    
    json.dump(state, open(STATE_FILE, "w"))
    print("Done.")

if __name__ == "__main__": main()
