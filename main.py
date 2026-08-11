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

# Read individual page secrets (FB_PAGE_1, FB_PAGE_2, FB_PAGE_3)
FB_PAGES = []
for i in range(1, 10):  # Check FB_PAGE_1 through FB_PAGE_9
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
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0',
}

def send_whatsapp(msg):
    if not (PHONE_NUMBER and API_KEY): 
        return
    try:
        url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg)}&apikey={API_KEY}"
        requests.get(url, timeout=15)
    except: 
        pass

def load_state():
    default = {"levis": {"sale": "", "count": 0}, "owndays": {"in_stock": False, "count": 0}, "fb": {}}
    if os.path.exists(STATE_FILE):
        try: 
            return json.load(open(STATE_FILE, "r"))
        except: 
            pass
    return default

def save_state(state):
    with open(STATE_FILE, "w") as f: 
        json.dump(state, f, indent=2)

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
    return parsed.path.strip('/').split('/')[-1].split('?')[0]

def parse_facebook_posts(html):
    """Extract posts from m.facebook.com HTML"""
    posts = []
    
    # Method 1: Parse HTML structure
    soup = BeautifulSoup(html, 'html.parser')
    
    # Find article elements (posts)
    articles = soup.find_all('div', role='article')
    if not articles:
        articles = soup.find_all('div', {'data-ft': True})
    
    for article in articles[:3]:
        post_data = {'text': '', 'post_id': '', 'time': ''}
        
        # Get text
        text_elem = article.find('p') or article.find('span')
        if text_elem:
            post_data['text'] = text_elem.get_text(strip=True)
        
        # Get post ID from data-ft
        data_ft = article.get('data-ft', '')
        if data_ft:
            try:
                ft_json = json.loads(data_ft)
                post_data['post_id'] = str(ft_json.get('top_level_post_id', ft_json.get('mf_story_key', '')))
            except:
                pass
        
        # Get timestamp
        time_elem = article.find('abbr')
        if time_elem:
            post_data['time'] = time_elem.get_text(strip=True)
        
        if post_data['text'] or post_data['post_id']:
            posts.append(post_data)
    
    return posts

def check_facebook(page_url, state):
    username = extract_username(page_url)
    if not username:
        return state
    
    try:
        time.sleep(random.uniform(3, 6))
        
        # Parse cookies if provided
        cookies = {}
        if FB_COOKIES:
            for cookie in FB_COOKIES.split(';'):
                if '=' in cookie:
                    k, v = cookie.strip().split('=', 1)
                    cookies[k.strip()] = v.strip()
        
        url = f"https://m.facebook.com/{username}"
        resp = requests.get(url, headers=MOBILE_HEADERS, cookies=cookies, timeout=30)
        
        if resp.status_code != 200:
            print(f"FB HTTP {resp.status_code} for {username}")
            return state
        
        posts = parse_facebook_posts(resp.text)
        
        if not posts:
            print(f"No posts found for {username}")
            return state
        
        # Get first valid post
        latest = None
        for p in posts:
            text = p.get('text', '')
            if text:
                latest = p
                break
        
        if not latest:
            latest = posts[0]
        
        text = latest.get('text') or "(Media post)"
        post_id = str(latest.get('post_id') or hash(text[:50]))
        
        if "fb" not in state:
            state["fb"] = {}
        
        page_state = state["fb"].get(page_url, {"last_id": "", "count": 0})
        
        if page_state["last_id"] != post_id:
            print(f"New FB post from {username}")
            page_state["last_id"] = post_id
            page_state["count"] = 1
            
            snippet = text[:300] if len(text) > 300 else text
            send_whatsapp(f"📱 *NEW FB POST* (1/5)\nPage: {username}\n\n{snippet}...\n\n🔗 {page_url}")
        elif 0 < page_state["count"] < 5:
            page_state["count"] += 1
            snippet = text[:150] if len(text) > 150 else text
            send_whatsapp(f"📱 *FB REMINDER* ({page_state['count']}/5)\nPage: {username}\n\n{snippet}...")
        
        state["fb"][page_url] = page_state
        
    except Exception as e:
        print(f"FB error for {username}: {e}")
    
    return state

def main():
    if os.environ.get("STOP_ALERTS", "").lower() == "true":
        return
    
    time.sleep(random.uniform(5, 20))
    state = load_state()
    
    print(f"Starting check at {datetime.now().isoformat()}")
    print(f"Monitoring {len(FB_PAGES)} Facebook pages: {FB_PAGES}")
    
    state = check_owndays(state)
    time.sleep(random.uniform(2, 4))
    
    state = check_levis(state)
    time.sleep(random.uniform(2, 4))
    
    for page_url in FB_PAGES:
        if page_url:
            state = check_facebook(page_url, state)
            time.sleep(random.uniform(5, 10))
    
    save_state(state)
    print("Check completed")

if __name__ == "__main__":
    main()
