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
        body_text = page.inner_text("body").lower()
        is_in_stock = "out of stock online" not in body_text
        o = state.get("owndays", {"in_stock": False, "count": 0})
        if is_in_stock:
            if not o["in_stock"] or o["count"] < 5:
                o["count"] += 1
                send_whatsapp_alert(f"🟢 *OWNDAYS STOCK ({o['count']}/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
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
        
        # Target post content
        post_text = ""
        for sel in ["div[data-ad-comet-preview='message']", "div[dir='auto']", "div[role='article']"]:
            elements = page.locator(sel).all()
            for el in elements:
                if el.is_visible():
                    text = el.inner_text().strip()
                    # Filter out short strings or language selectors
                    if len(text) > 20 and "english" not in text.lower():
                        post_text = text
                        break
            if post_text: break
            
        if not post_text: post_text = page.title()
        
        # --- Advanced Anti-Bot / Wall Detection ---
        # We block common "Consent" and "Login" wall phrases
        wall_keywords = [
            "facebook", "log in", "sign up", "see more from", 
            "english (us)", "english (uk)", "cookie policy", "privacy policy",
            "create new account", "forgotten account"
        ]
        
        if any(kw in post_text.lower() for kw in wall_keywords):
            print(f"⚠️ FB Wall/Consent detected for {url}. Skipping.")
            return state

        # We use a 20-character snippet for more stable comparison
        new_snippet = post_text[:20]
        if "fb" not in state: state["fb"] = {}
        
        old_snippet = state["fb"].get(url)
        if old_snippet and old_snippet != new_snippet:
            # 150-character preview as requested
            preview = post_text[:150].replace('\n', ' ')
            send_whatsapp_alert(f"📱 *NEW FB POST* 📱\nPage: {url}\n\nPreview: {preview}...")
            print(f"Alert sent for {url}")
            
        state["fb"][url] = new_snippet
    except Exception as e:
        print(f"FB Error: {e}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    time.sleep(random.uniform(5, 15))
    state = load_state()
    
    my_ua = ua_generator.random if ua_generator else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=my_ua, viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        state = check_owndays(page, state)
        state = check_levis(page, state)
        for url in FACEBOOK_PAGES:
            state = check_facebook(page, url, state)
            
        browser.close()
    
    json.dump(state, open(STATE_FILE, "w"))
    print("Done.")

if __name__ == "__main__": main()
