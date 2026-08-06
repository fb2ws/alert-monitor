import os
import json
import time
import requests
from playwright.sync_api import sync_playwright

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")

LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc" 
# We fetch all 3 Facebook pages from your GitHub Secrets for privacy
FACEBOOK_PAGES = [
    os.environ.get("FB_PAGE_1" ),
    os.environ.get("FB_PAGE_2"),
    os.environ.get("FB_PAGE_3")
]
FACEBOOK_PAGES = [url for url in FACEBOOK_PAGES if url] # Remove empty ones

STATE_FILE = "state.json"

def send_whatsapp_alert(message):
    """Sends a WhatsApp message using CallMeBot API."""
    if not PHONE_NUMBER or not API_KEY:
        print("CallMeBot credentials not set. Skipping alert.")
        return False
        
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message )}&apikey={API_KEY}"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            print(f"Alert sent successfully: {message[:30]}...")
            return True
        else:
            print(f"Failed to send alert. Status code: {response.status_code}")
            return False
    except Exception as e:
        print(f"Error sending alert: {e}")
        return False

def load_state():
    """Loads the previous state to avoid duplicate alerts."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            return {"levis_sale_seen": False, "fb_posts": {}}
    return {"levis_sale_seen": False, "fb_posts": {}}

def save_state(state):
    """Saves the current state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def check_levis_with_playwright(state):
    """Checks Levi's using headless browser to bypass Akamai/Cloudflare anti-bot."""
    print("Checking Levi's US for 50% off sale...")
    found_sale = False
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US"
        )
        # Stealth: hide the fact that this is a robot
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        try:
            page.goto(LEVIS_URL, timeout=45000, wait_until="domcontentloaded")
            time.sleep(3) # Wait for page scripts to run
            
            content = page.content().lower()
            sale_keywords = ["50% off", "half off", "50 percent off", "save 50%"]
            found_sale = any(kw in content for kw in sale_keywords)
            
            print(f"Levi's check completed. 50% off detected: {found_sale}")
        except Exception as e:
            print(f"Error browsing Levi's: {e}")
        finally:
            browser.close()
            
    # Alert logic
    if found_sale and not state.get("levis_sale_seen"):
        alert_msg = "🚨 *LEVI'S SALE ALERT* 🚨\n\n50% OFF detected on Levi's US website!\nCheck it out now: https://www.levi.com/US/en_US/"
        if send_whatsapp_alert(alert_msg ):
            state["levis_sale_seen"] = True
    elif not found_sale:
        state["levis_sale_seen"] = False
        
    return state

def check_facebook_with_playwright(url, state):
    """Checks Facebook page for new posts using headless browser."""
    print(f"Checking Facebook page: {url}...")
    latest_content = ""
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US"
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = context.new_page()
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(5) # Allow FB public view to render
            
            # Extract meta description or visible text to detect changes
            meta_desc = page.locator("meta[name='description']").get_attribute("content")
            if meta_desc:
                latest_content = meta_desc
            else:
                latest_content = page.inner_text("body")[:2000]
                
            print(f"Successfully scraped {url}.")
        except Exception as e:
            print(f"Error scraping Facebook {url}: {e}")
        finally:
            browser.close()
            
    if not latest_content:
        return state
        
    if "fb_posts" not in state:
        state["fb_posts"] = {}
        
    previous_content = state["fb_posts"].get(url)
    
    if previous_content != latest_content and previous_content is not None:
        alert_msg = f"📱 *NEW FACEBOOK POST* 📱\n\nNew activity detected on page:\n{url}"
        send_whatsapp_alert(alert_msg)
        
    state["fb_posts"][url] = latest_content
    return state

def main():
    print("Starting robust Playwright monitoring script...")
    
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("Stop flag is active. Exiting.")
        return

    state = load_state()
    
    # 1. Check Levi's
    state = check_levis_with_playwright(state)
    time.sleep(5)
    
    # 2. Check Facebook Pages
    for url in FACEBOOK_PAGES:
        state = check_facebook_with_playwright(url, state)
        time.sleep(5)
        
    save_state(state)
    print("Monitoring cycle complete.")

if __name__ == "__main__":
    main()
