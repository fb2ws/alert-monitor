import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright
from fake_useragent import UserAgent

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FACEBOOK_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4)]
FACEBOOK_PAGES = [url for url in FACEBOOK_PAGES if url]

LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

def send_whatsapp_alert(message ):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message )}&apikey={API_KEY}"
    try:
        requests.get(url, timeout=15)
    except: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE, "r"))
        except: pass
    return {
        "levis": {"sale_text": "", "alert_count": 0},
        "owndays": {"in_stock": False, "alert_count": 0},
        "fb_posts": {}
    }

def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))

def check_owndays(page, state):
    print("Checking Owndays...")
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(random.uniform(4.5, 7.2)) # Human-like pause
        body_text = page.inner_text("body").lower()
        is_in_stock = "out of stock online" not in body_text
        
        o_state = state.get("owndays", {"in_stock": False, "alert_count": 0})
        if is_in_stock:
            if not o_state["in_stock"] or o_state["alert_count"] < 5:
                o_state["alert_count"] += 1
                send_whatsapp_alert(f"🟢 *OWNDAYS STOCK ({o_state['alert_count']}/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
            o_state["in_stock"] = True
        else:
            o_state["in_stock"] = False
            o_state["alert_count"] = 0
        state["owndays"] = o_state
    except: pass
    return state

def check_levis(page, state):
    print("Checking Levi's...")
    try:
        page.goto(LEVIS_URL, timeout=60000)
        time.sleep(random.uniform(3.8, 6.5))
        content = page.content().lower()
        current_sale = ""
        for kw in ["50% off", "60% off", "70% off", "half off"]:
            if kw in content:
                current_sale = kw
                break
        
        l_state = state.get("levis", {"sale_text": "", "alert_count": 0})
        if current_sale:
            if current_sale != l_state["sale_text"] or l_state["alert_count"] < 5:
                if current_sale != l_state["sale_text"]: l_state["alert_count"] = 0
                l_state["alert_count"] += 1
                send_whatsapp_alert(f"🚨 *LEVI'S SALE ({l_state['alert_count']}/5)* 🚨\nFound: *{current_sale.upper()}*\n{LEVIS_URL}")
            l_state["sale_text"] = current_sale
        else:
            l_state["sale_text"] = ""
            l_state["alert_count"] = 0
        state["levis"] = l_state
    except: pass
    return state

def check_facebook(page, url, state):
    print(f"Checking FB: {url}")
    try:
        page.goto(url, timeout=60000, wait_until="networkidle")
        time.sleep(random.uniform(5.5, 9.0))
        
        post_text = ""
        for sel in ["div[data-ad-comet-preview='message']", "div[dir='auto']", "div[role='article']"]:
            el = page.locator(sel).first
            if el.is_visible():
                text = el.inner_text().strip()
                if len(text) > 5:
                    post_text = text
                    break
        
        if not post_text: post_text = page.title()
        
        # --- Smart Anti-Bot Filter ---
        wall_keywords = ["facebook", "log in", "sign up", "create new account", "see more from"]
        if any(kw in post_text.lower() for kw in wall_keywords):
            print(f"⚠️ FB Wall detected. Skipping to prevent false alarm.")
            return state

        new_snippet = post_text[:10]
        if "fb_posts" not in state: state["fb_posts"] = {}
        
        old_snippet = state["fb_posts"].get(url)
        if old_snippet and old_snippet != new_snippet:
            preview = post_text[:150].replace('\n', ' ')
            send_whatsapp_alert(f"📱 *NEW FB POST* 📱\nPage: {url}\n\nPreview: {preview}...")
        
        state["fb_posts"][url] = new_snippet
    except: pass
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    
    # 1. Random Startup Jitter (5-20 seconds) to avoid fixed patterns
    jitter = random.uniform(5, 20)
    print(f"Starting in {jitter:.2f} seconds...")
    time.sleep(jitter)
    
    state = load_state()
    ua = UserAgent()
    
    with sync_playwright() as p:
        # 2. Advanced Stealth Browser Launch
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", 
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars"
        ])
        
        # 3. Fresh Identity for every run
        context = browser.new_context(
            user_agent=ua.random,
            viewport={"width": 1920, "height": 1080},
            locale="en-US"
        )
        page = context.new_page()
        
        # Inject script to further hide robot identity
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        state = check_owndays(page, state)
        time.sleep(random.uniform(2, 5))
        
        state = check_levis(page, state)
        time.sleep(random.uniform(2, 5))
        
        for url in FACEBOOK_PAGES:
            state = check_facebook(page, url, state)
            time.sleep(random.uniform(3, 6))
            
        browser.close()
    
    save_state(state)
    print("Stealth cycle complete.")

if __name__ == "__main__": main()
