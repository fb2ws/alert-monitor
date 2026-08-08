import os
import json
import time
import requests
from playwright.sync_api import sync_playwright

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FACEBOOK_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4)]
FACEBOOK_PAGES = [url for url in FACEBOOK_PAGES if url]
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
LEVIS_URL = "https://www.levi.com/US/en_US/"
STATE_FILE = "state.json"

def send_whatsapp_alert(message ):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message )}&apikey={API_KEY}"
    requests.get(url, timeout=15)

def load_state():
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE, "r"))
        except: pass
    return {"levis_sale_text": "", "owndays_in_stock": False, "fb_posts": {}}

def check_owndays(page, state):
    print("Checking Owndays...")
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(4)
        is_in_stock = "out of stock online" not in page.inner_text("body").lower()
        
        # Only alert if status CHANGED from Out-of-Stock to In-Stock
        if is_in_stock and not state.get("owndays_in_stock", False):
            send_whatsapp_alert("🟢 *OWNDAYS RESTOCK* 🟢\nSENICHI31 is IN STOCK!\n" + OWNDAYS_URL)
        
        state["owndays_in_stock"] = is_in_stock
    except: pass
    return state

def check_levis(page, state):
    print("Checking Levi's...")
    try:
        page.goto(LEVIS_URL, timeout=60000)
        time.sleep(3)
        content = page.content().lower()
        
        # Find the specific sale text (e.g., "50% off")
        current_sale = ""
        for kw in ["50% off", "60% off", "40% off", "half off"]:
            if kw in content:
                current_sale = kw
                break
        
        # Only alert if the sale is NEW or the discount level CHANGED
        old_sale = state.get("levis_sale_text", "")
        if current_sale and current_sale != old_sale:
            send_whatsapp_alert(f"🚨 *LEVI'S SALE UPDATE* 🚨\nNew Sale Found: *{current_sale.upper()}*\nhttps://www.levi.com/US/en_US/" )
        
        state["levis_sale_text"] = current_sale
    except: pass
    return state

def check_facebook(page, url, state):
    print(f"Checking FB: {url}")
    try:
        page.goto(url, timeout=60000)
        time.sleep(5)
        # Target first post text
        post_text = ""
        for sel in ["div[data-ad-comet-preview='message']", "div[dir='auto']"]:
            el = page.locator(sel).first
            if el.is_visible():
                post_text = el.inner_text().strip()[:10] # Compare first 10 chars
                break
        
        if not post_text: post_text = page.title()[:10]
        
        if "fb_posts" not in state: state["fb_posts"] = {}
        if state["fb_posts"].get(url) != post_text:
            if state["fb_posts"].get(url): # Don't alert on the very first run
                send_whatsapp_alert(f"📱 *NEW FB POST* 📱\nPage: {url}")
            state["fb_posts"][url] = post_text
    except: pass
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        state = check_owndays(page, state)
        state = check_levis(page, state)
        for url in FACEBOOK_PAGES: state = check_facebook(page, url, state)
        browser.close()
    json.dump(state, open(STATE_FILE, "w"))

if __name__ == "__main__": main()
