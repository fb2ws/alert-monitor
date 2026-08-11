import os
import json
import time
import random
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FB_PAGES = json.loads(os.environ.get("FB_PAGES", "[]"))
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

def send_whatsapp(msg):
    if not (PHONE_NUMBER and API_KEY): 
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg)}&apikey={API_KEY}"
    try: 
        requests.get(url, timeout=15)
    except: 
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        try: 
            return json.load(open(STATE_FILE, "r"))
        except: 
            pass
    return {"levis": {"sale": "", "count": 0}, "owndays": {"in_stock": False, "count": 0}, "fb": {}}

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
        else: 
            l["sale"], l["count"] = "", 0
        state["levis"] = l
    except Exception as e:
        print(f"LEVIS error: {e}")
    return state

def check_facebook_mbasic(page_url, state):
    try:
        page_id = page_url.rstrip('/').split('/')[-1].split('?')[0]
        mbasic_url = f"https://mbasic.facebook.com/{page_id}"
        
        headers = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.0'}
        resp = requests.get(mbasic_url, headers=headers, timeout=30)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Try to find post content
        post = soup.find('div', role='article') or soup.find('p')
        if not post:
            return state
            
        text = post.get_text(strip=True)
        if len(text) < 10 or "log in" in text.lower():
            return state
            
        snippet = text[:120]
        
        if "fb" not in state: 
            state["fb"] = {}
            
        f = state["fb"].get(page_url, {"snippet": "", "count": 0})
        
        if f["snippet"] != snippet[:50]:
            f["snippet"], f["count"] = snippet[:50], 1
            send_whatsapp(f"📱 *NEW FB POST (1/5)* 📱\nPage: {page_url}\n\n{snippet}...")
        elif 0 < f["count"] < 5:
            f["count"] += 1
            send_whatsapp(f"📱 *FB REMINDER ({f['count']}/5)* 📱\nPage: {page_url}\n\n{snippet}...")
            
        state["fb"][page_url] = f
        
    except Exception as e:
        print(f"FB error for {page_url}: {e}")
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": 
        return
    
    time.sleep(random.uniform(5, 20))
    state = load_state()
    
    # No Playwright needed - pure requests
    state = check_owndays(state)
    state = check_levis(state)
    
    for url in FB_PAGES: 
        state = check_facebook_mbasic(url, state)
    
    with open(STATE_FILE, "w") as f: 
        json.dump(state, f)

if __name__ == "__main__": 
    main()
