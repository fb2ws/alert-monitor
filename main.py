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
    print("Checking Owndays...")
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(5)
        is_in_stock = "out of stock online" not in page.inner_text("body").lower()
        o = state.get("owndays", {"in_stock": False, "count": 0})
        if is_in_stock:
            if not o["in_stock"] or o["count"] < 5:
                o["count"] += 1
                send_whatsapp_alert(f"🟢 *OWNDAYS STOCK ({o['count']}/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
            o["in_stock"] = True
        else: o["in_stock"], o["count"] = False, 0
        state["owndays"] = o
    except: pass
    return state

def check_levis(page, state):
    print("Checking Levi's...")
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
    print(f"Checking FB: {url}")
    try:
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(10) # Wait for the annoying login modal to appear

        # --- MODAL DESTROYER ---
        # We find the 'Close' button (X) and click it, or just delete the modal layer
        try:
            # Try clicking the 'X' button
            close_btn = page.locator("div[role='button'][aria-label='Close'], div[aria-label='關閉']").first
            if close_btn.is_visible():
                close_btn.click()
                time.sleep(2)
        except: pass

        # Extract post text using very specific selectors for the content area
        post_text = ""
        # Selector for the actual message text in FB posts (modern layout)
        selectors = [
            "div[data-ad-comet-preview='message']", 
            "div[data-ad-preview='message']",
            "div.x1iorvi4.x1pi30zi.x1l90r2v.x1swvt1m", # Common post text class
            "div[dir='auto']"
        ]
        
        for sel in selectors:
            elements = page.locator(sel).all()
            for el in elements:
                if el.is_visible():
                    text = el.inner_text().strip()
                    # Filter out the 'Login' garbage
                    if len(text) > 10 and not any(kw in text.lower() for kw in ["facebook", "log in", "sign up", "english (us)"]):
                        post_text = text
                        break
            if post_text: break

        if not post_text:
            print(f"No post found for {url}. Page title: {page.title()}")
            return state

        # 20-character snippet for stability
        new_snippet = post_text[:20]
        if "fb" not in state: state["fb"] = {}
        
        # Get state for this specific URL
        f_state = state["fb"].get(url, {"snippet": "", "count": 0})
        
        # LOGIC: If snippet changed OR we are in the '5-alert' window
        if new_snippet != f_state["snippet"]:
            # It's a brand new post! Reset count and alert
            f_state["snippet"] = new_snippet
            f_state["count"] = 1
            preview = post_text[:150].replace('\n', ' ')
            send_whatsapp_alert(f"📱 *NEW FB POST (1/5)* 📱\nPage: {url}\n\nPreview: {preview}...")
        elif f_state["count"] > 0 and f_state["count"] < 5:
            # Same post, but we need to remind you up to 5 times
            f_state["count"] += 1
            preview = post_text[:150].replace('\n', ' ')
            send_whatsapp_alert(f"📱 *FB POST REMINDER ({f_state['count']}/5)* 📱\nPage: {url}\n\nPreview: {preview}...")
        
        state["fb"][url] = f_state
    except Exception as e: print(f"FB Error: {e}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    time.sleep(random.uniform(5, 15))
    state = load_state()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
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
