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
STATE_FILE = "state.json"

def send_whatsapp_alert(message):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message )}&apikey={API_KEY}"
    requests.get(url, timeout=15)

def load_state():
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE, "r"))
        except: pass
    return {"levis_sale_seen": False, "fb_posts": {}}

def check_facebook_page(page, url, state):
    print(f"Checking FB: {url}")
    try:
        page.goto(url, timeout=60000, wait_until="networkidle")
        time.sleep(5)
        
        # 1. Close the annoying login popup if it appears
        try:
            close_btn = page.locator("div[role='button'][aria-label='Close'], div[aria-label='關閉']").first
            if close_btn.is_visible(): close_btn.click()
        except: pass

        # 2. Target the first post specifically
        # We look for the first text block inside a post container
        post_text = ""
        # Selector for the actual message text in FB posts
        selectors = ["div[data-ad-comet-preview='message']", "div[dir='auto']", "div[role='article']"]
        
        for sel in selectors:
            element = page.locator(sel).first
            if element.is_visible():
                post_text = element.inner_text().strip()
                if post_text: break
        
        if not post_text:
            print("Could not find post text, falling back to page title.")
            post_text = page.title()

        # Compare only the first 50 characters to detect a "new" post
        new_fingerprint = post_text[:50]
        
        if "fb_posts" not in state: state["fb_posts"] = {}
        old_fingerprint = state["fb_posts"].get(url)
        
        if old_fingerprint and old_fingerprint != new_fingerprint:
            send_whatsapp_alert(f"📱 *NEW FB POST* 📱\n\nPage: {url}\n\nContent: {post_text[:100]}...")
            print("New post detected!")
        
        state["fb_posts"][url] = new_fingerprint
    except Exception as e:
        print(f"FB Error: {e}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    state = load_state()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()
        
        # 1. Check Levi's
        try:
            page.goto("https://www.levi.com/US/en_US/", timeout=60000 )
            content = page.content().lower()
            found_sale = any(kw in content for kw in ["50% off", "half off", "save 50%"])
            if found_sale and not state.get("levis_sale_seen"):
                send_whatsapp_alert("🚨 *LEVI'S 50% OFF ALERT* 🚨\nCheck: https://www.levi.com/US/en_US/" )
                state["levis_sale_seen"] = True
            elif not found_sale: state["levis_sale_seen"] = False
        except: pass

        # 2. Check FB Pages
        for url in FACEBOOK_PAGES:
            state = check_facebook_page(page, url, state)
            
        browser.close()
    
    json.dump(state, open(STATE_FILE, "w"))

if __name__ == "__main__": main()
