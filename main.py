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
    """Sends a WhatsApp notification via CallMeBot API."""
    if not PHONE_NUMBER or not API_KEY:
        print("CallMeBot credentials not set. Skipping alert.")
        return False
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message )}&apikey={API_KEY}"
    try:
        response = requests.get(url, timeout=15)
        return response.status_code == 200
    except Exception as e:
        print(f"Error sending alert: {e}")
        return False

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, "r"))
        except:
            pass
    return {"levis_sale_seen": False, "owndays_in_stock": False, "fb_posts": {}}

def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))

def check_owndays(page, state):
    """Checks if Owndays product is NOT 'Out Of Stock Online'."""
    print(f"Checking Owndays stock: {OWNDAYS_URL}")
    try:
        page.goto(OWNDAYS_URL, timeout=60000, wait_until="domcontentloaded")
        time.sleep(4)
        
        body_text = page.inner_text("body").lower()
        is_out_of_stock = "out of stock online" in body_text
        currently_in_stock = not is_out_of_stock
        
        print(f"Owndays in stock: {currently_in_stock}")
        
        if currently_in_stock and not state.get("owndays_in_stock", False):
            send_whatsapp_alert(f"🟢 *OWNDAYS RESTOCK ALERT* 🟢\n\nSENICHI31 (SKU: 6259) is now *IN STOCK ONLINE*!\nCheck: {OWNDAYS_URL}")
            state["owndays_in_stock"] = True
        elif not currently_in_stock:
            state["owndays_in_stock"] = False
            
    except Exception as e:
        print(f"Owndays check error: {e}")
    return state

def check_levis(page, state):
    """Checks Levi's for 50% off sale."""
    print("Checking Levi's sale...")
    try:
        page.goto(LEVIS_URL, timeout=60000, wait_until="domcontentloaded")
        time.sleep(3)
        content = page.content().lower()
        found_sale = any(kw in content for kw in ["50% off", "half off", "50 percent off", "save 50%"])
        
        if found_sale and not state.get("levis_sale_seen", False):
            send_whatsapp_alert("🚨 *LEVI'S 50% OFF SALE* 🚨\n\n50% off detected on Levi's US!\nCheck: https://www.levi.com/US/en_US/" )
            state["levis_sale_seen"] = True
        elif not found_sale:
            state["levis_sale_seen"] = False
    except Exception as e:
        print(f"Levi's check error: {e}")
    return state

def check_facebook_page(page, url, state):
    """Checks Facebook page by comparing the first 10 characters of the latest post."""
    print(f"Checking Facebook: {url}")
    try:
        page.goto(url, timeout=60000, wait_until="networkidle")
        time.sleep(5)
        
        # Close login modal if present
        try:
            close_btn = page.locator("div[role='button'][aria-label='Close'], div[aria-label='關閉']").first
            if close_btn.is_visible():
                close_btn.click()
        except:
            pass

        # Extract text of the first post specifically
        post_text = ""
        selectors = ["div[data-ad-comet-preview='message']", "div[dir='auto']", "div[role='article']"]
        for sel in selectors:
            element = page.locator(sel).first
            if element.is_visible():
                text = element.inner_text().strip()
                if len(text) > 3:
                    post_text = text
                    break
        
        if not post_text:
            post_text = page.title()

        # Compare first 10 characters for precision
        new_snippet = post_text[:10]
        
        if "fb_posts" not in state:
            state["fb_posts"] = {}
            
        old_snippet = state["fb_posts"].get(url)
        print(f"Old 10-char: '{old_snippet}' | New 10-char: '{new_snippet}'")
        
        if old_snippet and old_snippet != new_snippet:
            send_whatsapp_alert(f"📱 *NEW FACEBOOK POST* 📱\n\nPage: {url}\n\nPreview: {post_text[:100]}...")
            print("New post detected!")
            
        state["fb_posts"][url] = new_snippet
    except Exception as e:
        print(f"Facebook check error ({url}): {e}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("Stop alerts active.")
        return

    state = load_state()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        
        # 1. Check Owndays Stock
        state = check_owndays(page, state)
        time.sleep(3)
        
        # 2. Check Levi's Sale
        state = check_levis(page, state)
        time.sleep(3)
        
        # 3. Check Facebook Pages
        for url in FACEBOOK_PAGES:
            state = check_facebook_page(page, url, state)
            time.sleep(3)
            
        browser.close()
        
    save_state(state)
    print("All checks completed successfully.")

if __name__ == "__main__":
    main()
