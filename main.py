import os
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]

# URLs
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

def send_whatsapp(msg):
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
    """Load or initialize state with full tracking"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                old_state = json.load(f)
                # Merge old tracking info into new structure
                return {
                    "levis": old_state.get("levis", {"sale": "", "count": 0}),
                    "owndays": old_state.get("owndays", {"in_stock": False, "count": 0}),
                    "fb": old_state.get("fb", {}),
                    "_last_run": old_state.get("_last_run"),
                    "_errors": old_state.get("_errors", []),
                    "_config": old_state.get("_config", {"fb_pages": [], "checked_at": None})
                }
        except Exception as e:
            print(f"[WARN] Failed to load state: {e}")
    
    return {
        "levis": {"sale": "", "count": 0},
        "owndays": {"in_stock": False, "count": 0},
        "fb": {},
        "_last_run": None,
        "_errors": [],
        "_config": {
            "fb_pages": FB_PAGES,
            "checked_at": datetime.now().isoformat()
        }
    }

def save_state(state):
    """Persist state to disk with enhanced tracking"""
    state["_last_run"] = datetime.now().isoformat()
    state["_config"]["fb_pages"] = FB_PAGES  # Always track configured pages
    state["_config"]["checked_at"] = datetime.now().isoformat()
    
    # Limit errors to last 20 entries
    if len(state.get("_errors", [])) > 20:
        state["_errors"] = state["_errors"][-20:]
    
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print(f"[INFO] State saved to {STATE_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to save state: {e}")

def add_error(state, source, message, code=None):
    """Add error to state tracking"""
    if "_errors" not in state:
        state["_errors"] = []
    
    state["_errors"].append({
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "message": str(message)[:200],  # Truncate long messages
        "code": code
    })
    print(f"[ERROR][{source}] {message}")

def check_owndays_http(state):
    """Check OwnDays inventory using simple HTTP"""
    result = {"status": "unknown", "details": "", "response_code": None}
    
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        start = time.time()
        resp = session.get(OWNDAYS_URL, timeout=30, allow_redirects=True)
        elapsed = round(time.time() - start, 2)
        
        result["response_code"] = resp.status_code
        result["elapsed_sec"] = elapsed
        
        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = resp.text.lower()
        
        is_out_of_stock = any([
            "out of stock online" in page_text,
            "sold out" in page_text,
            "在庫なし" in page_text,
            "item unavailable",
            "class=\"unavailable\"",
        ])
        
        add_to_cart = bool(soup.find(text=lambda t: "add to cart" in str(t).lower() if t else False)) or \
                      "add-to-cart" in resp.text.lower()
        
        is_in_stock = not is_out_of_stock and add_to_cart
        result["status"] = "in_stock" if is_in_stock else "out_of_stock"
        result["details"] = f"No stock text found, add_to_cart={'yes' if add_to_cart else 'no'}"
        
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
        
        o["last_check"] = datetime.now().isoformat()
        o["last_response_code"] = resp.status_code
        o["parse_details"] = result["details"]
        state["owndays"] = o
        
        print(f"[INFO] OwnDays: {'IN STOCK' if is_in_stock else 'OUT OF STOCK'} ({elapsed}s)")
        add_error(state, "owndays_ok", None)  # Clear previous errors on success
        
    except requests.exceptions.RequestException as e:
        result["status"] = "error"
        result["details"] = str(e)
        add_error(state, "owndays", f"HTTP request failed: {str(e)[:100]}")
    except Exception as e:
        result["status"] = "error"
        result["details"] = str(e)
        add_error(state, "owndays", f"Parse error: {str(e)[:100]}")
    
    return state

def check_levis_http(state):
    """Check Levi's for sales using simple HTTP"""
    result = {"status": "unknown", "details": "", "response_code": None}
    
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        start = time.time()
        resp = session.get(LEVIS_URL, timeout=30, allow_redirects=True)
        elapsed = round(time.time() - start, 2)
        
        result["response_code"] = resp.status_code
        result["elapsed_sec"] = elapsed
        
        content = resp.text.lower()
        
        sale_keywords = ["50% off", "60% off", "70% off", "half off", "extra 50%", "extra 60%", "extra 70%"]
        sale = next((kw for kw in sale_keywords if kw in content), "")
        
        if not sale:
            if any(banner in content for banner in ["sale", "clearance", "special offer", "promotion"]):
                if "polo" in content and any(pct in content for pct in ["50", "60", "70"]):
                    if "off" in content:
                        sale = "active promotion"
        
        result["status"] = "sale_active" if sale else "no_sale"
        result["details"] = f"Found sale keyword: '{sale}'"
        
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
        
        l["last_check"] = datetime.now().isoformat()
        l["last_response_code"] = resp.status_code
        l["parse_details"] = result["details"]
        state["levis"] = l
        
        print(f"[INFO] Levi's: {sale.upper() if sale else 'NO SALE'} ({elapsed}s)")
        add_error(state, "levis_ok", None)
        
    except requests.exceptions.RequestException as e:
        result["status"] = "error"
        result["details"] = str(e)
        add_error(state, "levis", f"HTTP request failed: {str(e)[:100]}")
    except Exception as e:
        result["status"] = "error"
        result["details"] = str(e)
        add_error(state, "levis", f"Parse error: {str(e)[:100]}")
    
    return state

def check_facebook_page(page_id, state):
    """Check a Facebook page using Playwright"""
    fb_url = f"https://www.facebook.com/{page_id}/posts/"
    result = {"status": "unknown", "details": "", "post_snippet": None}
    
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
            
            start = time.time()
            page.goto(fb_url, timeout=60000, wait_until="domcontentloaded")
            page_load_time = round(time.time() - start, 2)
            
            time.sleep(4)
            
            # Remove login dialogs
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
                result["status"] = "no_posts_found"
                result["details"] = f"All selectors failed or only login wall present (page_load: {page_load_time}s)"
                print(f"[INFO] {page_id}: No new posts detected")
                
                # Still track this page in state even if no posts
                fb_data = state.get("fb", {})
                if page_id not in fb_data:
                    fb_data[page_id] = {
                        "snippet": "",
                        "count": 0,
                        "last_check": datetime.now().isoformat(),
                        "status": "no_posts_found",
                        "page_load_time_sec": page_load_time
                    }
                    state["fb"] = fb_data
                return state
            
            result["status"] = "success"
            result["details"] = f"Post found (length: {len(post_text)})"
            result["post_snippet"] = post_text[:50]
            result["page_load_time_sec"] = page_load_time
            
            snippet = post_text[:25]
            
            fb_data = state.get("fb", {})
            f = fb_data.get(page_id, {"snippet": "", "count": 0})
            
            if f["snippet"] != snippet:
                f["snippet"], f["count"] = snippet, 1
                preview = post_text[:150].replace("\n", " ")
                send_whatsapp(f"📱 *NEW FACEBOOK POST (1/5)* 📱\nPage ID: {page_id}\n\n{preview}...")
                print(f"[INFO] New post detected at {page_id}")
            elif f["count"] > 0 and f["count"] < 5:
                f["count"] += 1
                preview = post_text[:150].replace("\n", " ")
                send_whatsapp(f"📱 *FACEBOOK POST REMINDER ({f['count']}/5)* 📱\nPage ID: {page_id}\n\n{preview}...")
            
            f["last_check"] = datetime.now().isoformat()
            f["page_load_time_sec"] = page_load_time
            fb_data[page_id] = f
            state["fb"] = fb_data
            add_error(state, "facebook_ok", None)
            
    except Exception as e:
        result["status"] = "error"
        result["details"] = str(e)
        add_error(state, f"facebook_{page_id}", f"Check failed: {str(e)[:100]}")
    
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("[INFO] Alerts disabled by STOP_ALERTS environment variable")
        return
    
    time.sleep(random.uniform(2, 8))
    
    print(f"[START] Monitoring run starting at {datetime.now().isoformat()}")
    print(f"[CONFIG] FB Pages configured: {FB_PAGES}")
    
    state = load_state()
    
    print("[STEP 1] Checking OwnDays...")
    state = check_owndays_http(state)
    
    print("[STEP 2] Checking Levi's...")
    state = check_levis_http(state)
    
    print("[STEP 3] Checking Facebook pages...")
    if not FB_PAGES:
        print("[WARN] No Facebook page IDs configured! Check your GitHub secrets:")
        print("      - FB_PAGE_1, FB_PAGE_2, FB_PAGE_3")
        add_error(state, "config", "No Facebook page IDs found in environment variables")
    else:
        for page_id in FB_PAGES:
            if page_id:
                print(f"      → Checking page ID: {page_id}")
                state = check_facebook_page(page_id, state)
            else:
                print(f"      → Skipping empty page ID")
    
    save_state(state)
    
    print(f"[DONE] Run completed. Errors this run: {len([e for e in state.get('_errors', []) if e.get('timestamp', '').endswith(str(datetime.now().strftime('%Y-%m-%dT%H:%M')))])}")

if __name__ == "__main__":
    main()
