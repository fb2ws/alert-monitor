import os
import json
import time
import random
import requests
from datetime import datetime
from urllib.parse import urlparse
from facebook_scraper import get_posts

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FB_PAGES = json.loads(os.environ.get("FB_PAGES", "[]"))
FB_COOKIES = os.environ.get("FB_COOKIES", "")  # Optional: for private/restricted pages
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

def send_whatsapp(msg):
    """Send WhatsApp alert via CallMeBot"""
    if not (PHONE_NUMBER and API_KEY): 
        print("WhatsApp credentials not configured")
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg)}&apikey={API_KEY}"
        resp = requests.get(url, timeout=15)
        print(f"WhatsApp sent: {resp.status_code}")
    except Exception as e:
        print(f"WhatsApp error: {e}")

def load_state():
    """Load persistent state"""
    default_state = {
        "levis": {"sale": "", "count": 0}, 
        "owndays": {"in_stock": False, "count": 0}, 
        "fb": {},
        "last_run": None
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
                # Merge with defaults for new fields
                for key, val in default_state.items():
                    if key not in loaded:
                        loaded[key] = val
                return loaded
        except Exception as e:
            print(f"State load error: {e}")
    return default_state

def save_state(state):
    """Save state to file"""
    state["last_run"] = datetime.now().isoformat()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"State save error: {e}")

def check_owndays(state):
    """Check OWNDAYS stock status"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        resp = requests.get(OWNDAYS_URL, headers=headers, timeout=30)
        is_in_stock = "out of stock online" not in resp.text.lower() and "add to cart" in resp.text.lower()
        
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
    """Check Levi's for sales"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        }
        resp = requests.get(LEVIS_URL, headers=headers, timeout=30)
        content = resp.text.lower()
        
        sale_keywords = ["50% off", "60% off", "70% off", "half off", "40% off", "30% off", "sale"]
        sale = next((kw for kw in sale_keywords if kw in content), "")
        
        l = state.get("levis", {"sale": "", "count": 0})
        
        if sale:
            if l["sale"] != sale: 
                l["count"] = 1
                send_whatsapp(f"🚨 *LEVI'S SALE (1/5)* 🚨\nFound: *{sale.upper()}*\n{LEVIS_URL}")
            elif l["count"] < 5: 
                l["count"] += 1
                send_whatsapp(f"🚨 *LEVI'S REMINDER ({l['count']}/5)* 🚨\n*{sale.upper()}* active!\n{LEVIS_URL}")
            l["sale"] = sale
            print(f"Levi's: Found {sale}")
        else: 
            l["sale"], l["count"] = "", 0
            print("Levi's: No sale detected")
        state["levis"] = l
    except Exception as e:
        print(f"LEVIS error: {e}")
    return state

def extract_username(url):
    """Extract page username from Facebook URL"""
    parsed = urlparse(url)
    path = parsed.path.strip('/').split('/')[-1]
    return path.split('?')[0] if path else None

def check_facebook(page_url, state):
    """
    Check Facebook page using facebook-scraper library
    Much more reliable than mbasic scraping
    """
    username = extract_username(page_url)
    if not username:
        print(f"Could not extract username from {page_url}")
        return state
    
    try:
        # Parse cookies if provided (recommended for reliability)
        cookies = None
        if FB_COOKIES:
            cookies = {}
            for cookie in FB_COOKIES.split(';'):
                if '=' in cookie:
                    k, v = cookie.strip().split('=', 1)
                    cookies[k] = v
        
        print(f"Checking Facebook: {username}")
        
        # Get posts (pages=1 means only first page, faster)
        posts = list(get_posts(
            username, 
            pages=1,
            cookies=cookies,
            options={"comments": False, "reactors": False, "allow_extra_requests": False}
        ))
        
        if not posts:
            print(f"No posts found for {username} (page might require login)")
            return state
        
        latest = posts[0]
        post_id = str(latest.get("post_id") or latest.get("post_url", "").split('/')[-1])
        text = latest.get("text") or latest.get("post_text") or "(Media post)"
        post_url = latest.get("post_url") or f"https://facebook.com/{post_id}"
        time_posted = latest.get("time")
        
        # Create unique signature
        content_sig = f"{post_id}:{text[:60]}" if text else post_id
        
        if "fb" not in state:
            state["fb"] = {}
        
        page_state = state["fb"].get(page_url, {"last_sig": "", "count": 0, "last_post": ""})
        
        if page_state["last_sig"] != content_sig:
            print(f"New post detected from {username}")
            page_state["last_sig"] = content_sig
            page_state["count"] = 1
            page_state["last_post"] = text[:200] if text else "(Media)"
            
            snippet = text[:300] if text else "(New media post)"
            time_str = f"Posted: {time_posted.strftime('%Y-%m-%d %H:%M')}" if time_posted else ""
            
            send_whatsapp(
                f"📱 *NEW FB POST* (1/5)\n"
                f"Page: {username}\n"
                f"{time_str}\n\n"
                f"{snippet}{'...' if len(text) > 300 else ''}\n\n"
                f"🔗 {post_url}"
            )
        elif 0 < page_state["count"] < 5:
            # Reminder
            page_state["count"] += 1
            snippet = page_state["last_post"][:150] if page_state["last_post"] else "(Previous post)"
            send_whatsapp(
                f"📱 *FB REMINDER* ({page_state['count']}/5)\n"
                f"Page: {username}\n\n"
                f"{snippet}..."
            )
        
        state["fb"][page_url] = page_state
        
    except Exception as e:
        print(f"Facebook error for {username}: {e}")
        # Don't let Facebook errors stop other checks
    
    return state

def main():
    if os.environ.get("STOP_ALERTS", "").lower() == "true":
        print("STOP_ALERTS is true, exiting")
        return
    
    # Random startup delay (helps avoid pattern detection)
    time.sleep(random.uniform(5, 20))
    
    state = load_state()
    print(f"Starting check at {datetime.now().isoformat()}")
    print(f"Monitoring {len(FB_PAGES)} Facebook pages")
    
    # Check OWNDAYS
    state = check_owndays(state)
    time.sleep(random.uniform(2, 4))
    
    # Check Levi's
    state = check_levis(state)
    time.sleep(random.uniform(2, 4))
    
    # Check Facebook pages
    for page_url in FB_PAGES:
        if not page_url:
            continue
        state = check_facebook(page_url, state)
        # Be nice to Facebook - wait between pages
        time.sleep(random.uniform(5, 10))
    
    save_state(state)
    print(f"Completed at {datetime.now().isoformat()}")

if __name__ == "__main__": 
    main()
