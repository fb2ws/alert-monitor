import os
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
# Fixed: Use FB_PAGE_1, FB_PAGE_2, FB_PAGE_3 (matching your GitHub secrets)
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]

# URLs
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

# Request headers to avoid bot detection
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

def send_whatsapp(msg):
    """Send WhatsApp notification via CallMeBot API"""
    if not (PHONE_NUMBER and API_KEY): 
        print("[DEBUG] WhatsApp not configured, skipping alert")
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg)}&apikey={API_KEY}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            print(f"[NOTIF] Alert sent successfully")
        else:
            print(f"[ERROR] WhatsApp API returned {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] WhatsApp send failed: {e}")

def load_state():
    """Load or initialize state file"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load state: {e}")
    return {"levis": {"sale": "", "count": 0}, "owndays": {"in_stock": False, "count": 0}, "fb": {}}

def save_state(state):
    """Persist state to disk"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print(f"[INFO] State saved")
    except Exception as e:
        print(f"[ERROR] Failed to save state: {e}")

def check_owndays_http(state):
    """Check OwnDays inventory using simple HTTP (faster than browser)"""
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        resp = session.get(OWNDAYS_URL, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = resp.text.lower()
        
        # Multiple indicators of out-of-stock
        is_out_of_stock = any([
            "out of stock online" in page_text,
            "sold out" in page_text,
            "在庫なし" in page_text,
            "item unavailable",
            "class=\"unavailable\"",
        ])
        
        # Also check for "Add to Cart" button as positive indicator
        add_to_cart = bool(soup.find(text=lambda t: "add to cart" in str(t).lower() if t else False)) or \
                      "add-to-cart" in resp.text.lower()
        
        is_in_stock = not is_out_of_stock and add_to_cart
        
        o = state.get("owndays", {"in_stock": False, "count": 0})
        
        if is_in_stock:
            if not o["in_stock"]:
                o["count"] = 1
                send_whatsapp(f"🟢 *OWNDAYS STOCK ALERT (1/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
            elif o["count"] < 5:
                o["count"] += 1
                send_whatsapp(f"🟢 *OWNDAYS REMINDER ({o['count']}/5)* 🟢\nStill available: {OWNDAYS_URL}")
            o["in_stock"] = True
        else:
            o["in_stock"], o["count"] = False, 0
        
        state["owndays"] = o
        print(f"[INFO] OwnDays: {'IN STOCK' if is_in_stock else 'OUT OF STOCK'}")
        
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] OwnDays HTTP request failed: {e}")
    except Exception as e:
        print(f"[ERROR] OwnDays parse error: {e}")
    
    return state

def check_levis_http(state):
    """Check Levi's for sales using simple HTTP (much faster than Playwright)"""
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        resp = session.get(LEVIS_URL, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        
        content = resp.text.lower()
        
        # Look for sale indicators in multiple places
        sale_keywords = ["50% off", "60% off", "70% off", "half off", "extra 50%", "extra 60%", "extra 70%"]
        sale = next((kw for kw in sale_keywords if kw in content), "")
        
        # Also check banner/promo text
        if not sale:
            if any(banner in content for banner in ["sale", "clearance", "special offer", "promotion"]):
                if "polo" in content and any(pct in content for pct in ["50", "60", "70"]):
                    if "off" in content:
                        sale = "active promotion"
        
        l = state.get("levis", {"sale": "", "count": 0})
        
        if sale:
            if l["sale"] != sale:
                l["count"] = 1
                send_whatsapp(f"🚨 *LEVI'S SALE ALERT (1/5)* 🚨\nFound: *{sale.upper()}*\n{LEVIS_URL}")
            elif l["count"] < 5:
                l["count"] += 1
                send_whatsapp(f"🚨 *LEVI'S REMINDER ({l['count']}/5)* 🚨\nSale active: *{sale.upper()}\n{LEVIS_URL}")
            l["sale"] = sale
        else:
            l["sale"], l["count"] = "", 0
        
        state["levis"] = l
        print(f"[INFO] Levi's: {sale.upper() if sale else 'NO SALE'}")
        
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Levi's HTTP request failed: {e}")
    except Exception as e:
        print(f"[ERROR] Levi's parse error: {e}")
    
    return state

def check_facebook_page(page_url, state):
    """Check a Facebook page using Playwright (necessary for JS-rendered content)"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )
            
            page = context.new_page()
            
            page.goto(page_url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(4)
            
            # Remove login dialogs and overlays
            try:
                page.evaluate('''() => {
                    const removeSelectors = [
                        'div[role="dialog"]',
                        'div[class*="modal"]',
                        '[data-nosnippet]',
                        '#mountX_plus',
                        '.x1n2onr6',
                        '.x1ja2u2z'
                    ];
                    removeSelectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            if (el.innerText && el.innerText.toLowerCase().match(/log in|facebook/i)) {
                                el.remove();
                            }
                        });
                    });
                }''')
            except:
                pass
            
            # Extract post content from multiple possible selectors
            post_text = ""
            selectors = [
                "article div[dir='auto']",
                "[data-ad-comet-preview='message']",
                "div[role='article'] div[dir='auto']",
                "[data-ft='{\"tn\":\"*1\"}']",
                "span._sp_",
            ]
            
            for sel in selectors:
                try:
                    elements = page.query_selector_all(sel)
                    for el in elements:
                        if el:
                            text = el.inner_text().strip() if el.is_visible() else ""
                            if len(text) > 20 and "log in" not in text.lower() and "continue" not in text.lower():
                                post_text = text
                                break
                    if post_text:
                        break
                except:
                    continue
            
            browser.close()
            
            if not post_text:
                print(f"[INFO] {page_url}: No new posts detected")
                return state
            
            snippet = post_text[:25]
            
            fb_data = state.get("fb", {})
            f = fb_data.get(page_url, {"snippet": "", "count": 0})
            
            if f["snippet"] != snippet:
                f["snippet"], f["count"] = snippet, 1
                preview = post_text[:150].replace("\n", " ")
                send_whatsapp(f"📱 *NEW FACEBOOK POST (1/5)* 📱\nPage: {page_url}\n\n{preview}...")
                print(f"[INFO] New post detected at {page_url}")
            elif f["count"] > 0 and f["count"] < 5:
                f["count"] += 1
                preview = post_text[:150].replace("\n", " ")
                send_whatsapp(f"📱 *FACEBOOK POST REMINDER ({f['count']}/5)* 📱\nPage: {page_url}\n\n{preview}...")
            
            fb_data[page_url] = f
            state["fb"] = fb_data
            
    except Exception as e:
        print(f"[ERROR] Facebook check failed for {page_url}: {e}")
    
    return state

def main():
    """Main execution loop"""
    # Allow emergency stop
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("[INFO] Alerts disabled by STOP_ALERTS environment variable")
        return
    
    # Random delay to stagger concurrent runners
    time.sleep(random.uniform(2, 8))
    
    print(f"[START] Monitoring run starting at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    state = load_state()
    
    # Fast checks first (HTTP requests take seconds)
    print("[STEP 1] Checking OwnDays...")
    state = check_owndays_http(state)
    
    print("[STEP 2] Checking Levi's...")
    state = check_levis_http(state)
    
    # Slow checks last (Playwright takes minutes)
    print("[STEP 3] Checking Facebook pages...")
    for page_id in FB_PAGES:
        if page_id:
            fb_url = f"https://www.facebook.com/{page_id}/posts/"
            print(f"      → {fb_url}")
            state = check_facebook_page(fb_url, state)
    
    # Persist state
    save_state(state)
    
    print(f"[DONE] Monitoring run completed in {time.strftime('%H:%M:%S', time.gmtime())}")

if __name__ == "__main__":
    main()
