import os
import json
import time
import random
import re
import requests
from datetime import datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")

# Read individual page secrets
FB_PAGES = []
for i in range(1, 10):
    page = os.environ.get(f"FB_PAGE_{i}")
    if page:
        FB_PAGES.append(page)

print(f"Loaded {len(FB_PAGES)} Facebook pages: {FB_PAGES}")

FB_COOKIES = os.environ.get("FB_COOKIES", "")
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

MOBILE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

def send_whatsapp(msg):
    if not (PHONE_NUMBER and API_KEY): 
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg)}&apikey={API_KEY}"
        resp = requests.get(url, timeout=15)
        print(f"WhatsApp sent: {resp.status_code}")
    except Exception as e: 
        print(f"WhatsApp error: {e}")

def load_state():
    default = {
        "levis": {"sale": "", "count": 0}, 
        "owndays": {"in_stock": False, "count": 0}, 
        "fb": {}
    }
    if os.path.exists(STATE_FILE):
        try: 
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e: 
            print(f"State load error: {e}")
    return default

def init_fb_state(state):
    """Initialize FB state for all pages so they appear in JSON even before first check"""
    if "fb" not in state:
        state["fb"] = {}
    for page in FB_PAGES:
        if page not in state["fb"]:
            state["fb"][page] = {
                "last_id": "",
                "count": 0,
                "last_check": None,
                "status": "initialized"
            }
    return state

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f: 
            json.dump(state, f, indent=2)
        print(f"State saved to {STATE_FILE}")
    except Exception as e:
        print(f"State save error: {e}")

def check_owndays(state):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(OWNDAYS_URL, headers=headers, timeout=30)
        is_in_stock = "out of stock online" not in resp.text.lower()
        
        o = state.get("owndays", {"in_stock": False, "count": 0})
        if is_in_stock:
            if not o["in_stock"]: 
                o["count"] = 1
                send_whatsapp(f"🟢 *OWNDAYS STOCK (1/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
            elif o["count"] < 5: 
                o["count"] += 1
                send_whatsapp(f"🟢 *OWNDAYS REMINDER ({o['count']}/5)* 🟢\n{OWNDAYS_URL}")
            o["in_stock"] = True
        else: 
            o["in_stock"], o["count"] = False, 0
        state["owndays"] = o
        print(f"OWNDAYS: {'In Stock' if is_in_stock else 'Out of Stock'}")
    except Exception as e:
        print(f"OWNDAYS error: {e}")
    return state

def check_levis(state):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(LEVIS_URL, headers=headers, timeout=30)
        content = resp.text.lower()
        
        sale = next((kw for kw in ["50% off", "60% off", "70% off", "half off"] if kw in content), "")
        l = state.get("levis", {"sale": "", "count": 0})
        
        if sale:
            if l["sale"] != sale: 
                l["count"] = 1
                send_whatsapp(f"🚨 *LEVI'S SALE (1/5)* 🚨\nFound: *{sale.upper()}*\n{LEVIS_URL}")
            elif l["count"] < 5: 
                l["count"] += 1
                send_whatsapp(f"🚨 *LEVI'S REMINDER ({l['count']}/5)* 🚨\n*{sale.upper()}* active!\n{LEVIS_URL}")
            l["sale"] = sale
            print(f"Levi's: {sale}")
        else: 
            l["sale"], l["count"] = "", 0
            print("Levi's: No sale")
        state["levis"] = l
    except Exception as e:
        print(f"LEVIS error: {e}")
    return state

def extract_username(url):
    parsed = urlparse(url)
    path = parsed.path.strip('/').split('/')[-1]
    return path.split('?')[0] if path else None

def check_facebook(page_url, state):
    """Check Facebook with detailed logging"""
    username = extract_username(page_url)
    if not username:
        print(f"Could not extract username from {page_url}")
        state["fb"][page_url]["status"] = "invalid_url"
        return state
    
    print(f"\n--- Checking Facebook: {username} ---")
    
    # Ensure state entry exists
    if page_url not in state["fb"]:
        state["fb"][page_url] = {"last_id": "", "count": 0}
    
    try:
        time.sleep(random.uniform(3, 6))
        
        # Parse cookies
        cookies = {}
        if FB_COOKIES:
            for cookie in FB_COOKIES.split(';'):
                if '=' in cookie:
                    k, v = cookie.strip().split('=', 1)
                    cookies[k.strip()] = v.strip()
            print(f"Using {len(cookies)} cookies")
        else:
            print("WARNING: No FB_COOKIES set - likely to fail")
        
        url = f"https://m.facebook.com/{username}"
        print(f"Fetching: {url}")
        
        resp = requests.get(url, headers=MOBILE_HEADERS, cookies=cookies, timeout=30)
        print(f"Status: {resp.status_code}")
        print(f"URL after redirects: {resp.url}")
        
        # DEBUG: Save HTML to see what we got
        if "login" in resp.url.lower() or "login" in resp.text.lower()[:1000]:
            print("ERROR: Redirected to login page!")
            state["fb"][page_url]["status"] = "login_required"
            # Save debug HTML
            with open(f"debug_{username}.html", "w", encoding="utf-8") as f:
                f.write(resp.text[:5000])
            print(f"Saved debug HTML to debug_{username}.html")
            return state
        
        # Parse posts
        soup = BeautifulSoup(resp.text, 'html.parser')
        articles = soup.find_all('div', role='article')
        print(f"Found {len(articles)} article elements")
        
        if not articles:
            # Try alternative selectors
            articles = soup.find_all('div', {'data-ft': True})
            print(f"Found {len(articles)} elements with data-ft")
        
        if not articles:
            print("ERROR: No posts found - page structure changed or blocked")
            state["fb"][page_url]["status"] = "no_posts_found"
            # Save debug HTML
            with open(f"debug_{username}.html", "w", encoding="utf-8") as f:
                f.write(resp.text[:5000])
            return state
        
        # Get first post
        article = articles[0]
        text_elem = article.find('p') or article.find('span')
        text = text_elem.get_text(strip=True) if text_elem else ""
        
        # Get post ID
        data_ft = article.get('data-ft', '')
        post_id = ""
        if data_ft:
            try:
                ft_json = json.loads(data_ft)
                post_id = str(ft_json.get('top_level_post_id', ft_json.get('mf_story_key', '')))
            except:
                pass
        
        if not post_id and text:
            post_id = str(hash(text[:100]))[:16]
        
        print(f"Post ID: {post_id[:30]}...")
        print(f"Text preview: {text[:100]}...")
        
        # Update state
        page_state = state["fb"][page_url]
        page_state["last_check"] = datetime.now().isoformat()
        
        if not text and not post_id:
            print("WARNING: Empty post")
            page_state["status"] = "empty_post"
            return state
        
        if page_state.get("last_id") != post_id:
            print(f"NEW POST DETECTED!")
            page_state["last_id"] = post_id
            page_state["count"] = 1
            page_state["status"] = "new_post"
            
            snippet = text[:300] if len(text) > 300 else text
            send_whatsapp(f"📱 *NEW FB POST* (1/5)\nPage: {username}\n\n{snippet}...\n\n🔗 {page_url}")
        elif 0 < page_state.get("count", 0) < 5:
            page_state["count"] += 1
            page_state["status"] = "reminder"
            snippet = text[:150] if len(text) > 150 else text
            send_whatsapp(f"📱 *FB REMINDER* ({page_state['count']}/5)\nPage: {username}\n\n{snippet}...")
        else:
            page_state["status"] = "no_change"
            print("No new post")
        
        state["fb"][page_url] = page_state
        
    except Exception as e:
        print(f"ERROR checking {username}: {e}")
        import traceback
        traceback.print_exc()
        state["fb"][page_url]["status"] = f"error: {str(e)}"
    
    return state

def main():
    if os.environ.get("STOP_ALERTS", "").lower() == "true":
        print("STOP_ALERTS is true, exiting")
        return
    
    time.sleep(random.uniform(2, 5))
    state = load_state()
    
    # Initialize FB state for all pages first
    state = init_fb_state(state)
    
    print(f"\n{'='*50}")
    print(f"Starting check at {datetime.now().isoformat()}")
    print(f"Pages to check: {FB_PAGES}")
    print(f"{'='*50}\n")
    
    # Check OWNDAYS
    state = check_owndays(state)
    time.sleep(random.uniform(2, 4))
    
    # Check Levi's
    state = check_levis(state)
    time.sleep(random.uniform(2, 4))
    
    # Check Facebook pages
    for page_url in FB_PAGES:
        if page_url:
            state = check_facebook(page_url, state)
            time.sleep(random.uniform(5, 10))
    
    print(f"\n{'='*50}")
    print("Saving state...")
    save_state(state)
    
    # Print final state for debugging
    print(f"\nFinal FB state:")
    for url, data in state.get("fb", {}).items():
        print(f"  {url[:50]}... : {data.get('status', 'unknown')}")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
