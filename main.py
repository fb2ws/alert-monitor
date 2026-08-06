import os
import json
import time
import requests
import random
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

# --- Configuration ---
# CallMeBot WhatsApp API configuration
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")

# Target URLs
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
FACEBOOK_PAGES = [
    "https://www.facebook.com/hahaphone.hk",
    os.environ.get("FB_PAGE_2", "https://www.facebook.com/meta"), # Replace with actual page 2
    os.environ.get("FB_PAGE_3", "https://www.facebook.com/zuck")  # Replace with actual page 3
]

# State file to keep track of seen posts and sales to avoid duplicate alerts
STATE_FILE = "state.json"

# --- Helper Functions ---

def send_whatsapp_alert(message):
    """Sends a WhatsApp message using CallMeBot API."""
    if not PHONE_NUMBER or not API_KEY:
        print("CallMeBot credentials not set. Skipping alert.")
        return False
        
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(message)}&apikey={API_KEY}"
    try:
        response = requests.get(url)
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

def get_headers():
    """Generates random headers to bypass basic anti-bot protections."""
    ua = UserAgent()
    return {
        "User-Agent": ua.random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

# --- Scraping Functions ---

def check_levis_sale(state):
    """Checks Levi's US website for 50% off sales."""
    print("Checking Levi's for sales...")
    try:
        # We use a session to maintain cookies which helps with anti-bot
        session = requests.Session()
        headers = get_headers()
        
        # Add a referer to look more like normal traffic
        headers["Referer"] = "https://www.google.com/"
        
        response = session.get(LEVIS_URL, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"Failed to fetch Levi's. Status code: {response.status_code}")
            return state
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Look for text indicating 50% off
        page_text = soup.get_text().lower()
        
        # Various ways 50% off might be written
        sale_keywords = ["50% off", "half off", "50 percent off", "save 50%"]
        
        found_sale = any(keyword in page_text for keyword in sale_keywords)
        
        if found_sale and not state.get("levis_sale_seen"):
            alert_msg = "🚨 *LEVI'S SALE ALERT* 🚨\n\n50% OFF detected on Levi's US website!\nCheck it out now: https://www.levi.com/US/en_US/"
            if send_whatsapp_alert(alert_msg):
                state["levis_sale_seen"] = True
        elif not found_sale:
            # Reset state if sale is over
            state["levis_sale_seen"] = False
            
        return state
        
    except Exception as e:
        print(f"Error checking Levi's: {e}")
        return state

def check_facebook_page(url, page_name, state):
    """
    Checks a Facebook page for new posts.
    Note: Facebook is notoriously difficult to scrape without login/API.
    This uses a basic approach that might be blocked by FB's aggressive anti-bot.
    For a more robust solution, an official API or a specialized scraper service is recommended.
    """
    print(f"Checking Facebook page: {url}...")
    try:
        headers = get_headers()
        # Facebook specific headers to bypass basic blocks
        headers["Sec-Ch-Ua"] = '"Chromium";v="116", "Not)A;Brand";v="24", "Google Chrome";v="116"'
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = '"Windows"'
        
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"Failed to fetch {url}. Status code: {response.status_code}")
            return state
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Facebook renders content dynamically via JS, making pure HTML scraping difficult.
        # We try to extract text from meta tags or basic HTML structure if available.
        # A more robust approach for FB without API is using Selenium/Playwright, but that's heavy for Actions.
        
        # Attempt 1: Check meta description for latest post preview
        meta_desc = soup.find("meta", attrs={"name": "description"})
        latest_content = ""
        
        if meta_desc and meta_desc.get("content"):
            latest_content = meta_desc["content"]
        else:
            # Attempt 2: Just hash the visible text content to detect changes
            # This is noisy but works as a fallback
            latest_content = hash(soup.get_text()[:5000]) # First 5000 chars
            
        # Ensure fb_posts dict exists in state
        if "fb_posts" not in state:
            state["fb_posts"] = {}
            
        # Check if content changed
        previous_content = state["fb_posts"].get(url)
        
        if previous_content != str(latest_content) and previous_content is not None:
            alert_msg = f"📱 *NEW FACEBOOK POST* 📱\n\nNew activity detected on {page_name}!\nLink: {url}"
            send_whatsapp_alert(alert_msg)
            
        # Update state
        state["fb_posts"][url] = str(latest_content)
        return state
        
    except Exception as e:
        print(f"Error checking Facebook {url}: {e}")
        return state

# --- Main Execution ---

def main():
    print("Starting monitoring script...")
    
    # Check if stop flag exists
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("Stop flag is active. Exiting without checking.")
        return

    state = load_state()
    
    # 1. Check Levi's
    state = check_levis_sale(state)
    
    # Random delay to mimic human behavior
    time.sleep(random.uniform(2.0, 5.0))
    
    # 2. Check Facebook Pages
    page_names = ["HahaPhone HK", "FB Page 2", "FB Page 3"]
    for i, url in enumerate(FACEBOOK_PAGES):
        state = check_facebook_page(url, page_names[i], state)
        # Random delay between page checks
        time.sleep(random.uniform(3.0, 7.0))
        
    # Save state for next run
    save_state(state)
    print("Monitoring complete.")

if __name__ == "__main__":
    main()
