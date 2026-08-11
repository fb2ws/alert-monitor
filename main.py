import os
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime
from urllib.parse import urlparse

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")

# Handle both page IDs AND full URLs in secrets
raw_fb_pages = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]
# Normalize URLs - extract page ID from full URLs or use as-is
FB_PAGES = []
for raw in raw_fb_pages:
    if "facebook.com/" in raw:
        # Extract username/ID from full URL
        parsed = urlparse(raw)
        page_id = parsed.path.strip('/').split('/')[0]
        FB_PAGES.append(page_id)
    else:
        FB_PAGES.append(raw)

# URLs
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

# Better headers to avoid 403 on Levi's
COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",  # Don't compress - some sites block gzip
    "Connection": "keep-alive",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "Cache-Control": "max-age=0",
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
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                old_state = json.load(f)
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
    state["_last_run"] = datetime.now().isoformat()
    state["_config"]["fb_pages"] = FB_PAGES
    state["_config"]["checked_at"] = datetime.now().isoformat()
    state["_config"]["fb_urls_used"] = [f"https://www.facebook.com/{p}/posts/" for p in FB_PAGES]
    
    if len(state.get("_errors", [])) > 20:
        state["_errors"] = state["_errors"][-20:]
    
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        print(f"[INFO] State saved to {STATE_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to save state: {e}")

def add_error(state, source, message, code=None):
    if "_errors" not in state:
        state["_errors"] = []
    
    state["_errors"].append({
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "message": str(message)[:200],
        "code": code
    })
    print(f"[ERROR][{source}] {message}")

def check_owndays_http(state):
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
        
        add_to_cart_button = soup.find(string=lambda t: t and "add to cart" in str(t).lower())
        add_to_cart_class = bool(soup.find(class_=lambda c: c and "add-to-cart" in c.lower()) if c else False)
        add_to_cart = bool(add_to_cart_button) or add_to_cart_class or "add-to-cart" in resp.text.lower()
        
        is_in_stock = not is_out_of_stock and add_to_cart
        result["status"] = "in_stock" if is_in_stock else "out_of_stock"
        result["details"] = f"Out-of-stock text={'yes' if is_out_of_stock else 'no'}, Add-to-Cart={'yes' if add_to_cart else 'no'}"
        
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
        o["html_sample"] = page_text[:500]  # First 500 chars for debugging
        state["owndays"] = o
        
        print(f"[INFO] OwnDays: {'IN STOCK ✓' if is_in_stock else 'OUT OF STOCK ✗'} ({elapsed}s)")
        
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
    result = {"status": "unknown", "details": "", "response_code": None}
    
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        # Add retry logic for 403s
        max_retries = 3
        for attempt in range(max_retries):
            start = time.time()
            resp = session.get(LEVIS_URL, timeout=30, allow_redirects=True)
            elapsed = round(time.time() - start, 2)
            
            result["response_code"] = resp.status_code
            result["elapsed_sec"] = elapsed
            
            if resp.status_code == 403 and attempt < max_retries - 1:
                print(f"[WARN] Levi's returned 403, retrying ({attempt+1}/{max_retries})...")
                time.sleep(2 + random.random() * 3)
                continue
            
            break
        
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
        l["html_sample"] = content[:500]
        state["levis"] = l
        
        print(f"[INFO] Levi's: {sale.upper() if sale else 'NO SALE'} (Status: {resp.status_code}, {elapsed}s)")
        
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
    fb_url = f"https://www.facebook.com/{page_id}/posts/"
    result = {"status": "unknown", "details": "", "post_snippet": None, "captured_text_length": 0}
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ]
            )
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
                locale="en-US",
                timezone_id="America/New_York",
            )
            
            page = context.new_page()
            
            start = time.time()
            
            # Go to page with proper navigation wait
            try:
                page.goto(fb_url, wait_until="networkidle", timeout=90000)
            except Exception as goto_err:
                result["status"] = "navigation_failed"
                result["details"] = f"Goto error: {str(goto_err)[:100]}"
                add_error(state, f"facebook_{page_id}_nav", str(goto_err)[:100])
                browser.close()
                
                fb_data = state.get("fb", {})
                if page_id not in fb_data:
                    fb_data[page_id] = {
                        "snippet": "",
                        "count": 0,
                        "last_check": datetime.now().isoformat(),
                        "status": "navigation_failed",
                        "page_url_attempted": fb_url,
                        "page_load_time_sec": round(time.time() - start, 2),
                        "debug_info": "Browser navigated but may have hit login wall or 404"
                    }
                    state["fb"] = fb_data
                return state
            
            page_load_time = round(time.time() - start, 2)
            
            # Wait longer for JS rendering
            time.sleep(8)
            
            # Take screenshot for debugging (optional - commented out)
            # page.screenshot(path=f"/tmp/fb_{page_id}.png")
            
            # Remove login dialogs and overlays
            try:
                page.evaluate('''() => {
                    const removeSelectors = [
                        'div[role="dialog"]',
                        'div[class*="modal"]',
                        '[data-nosnippet]',
                        '#mountX_plus',
                        '.x1n2onr6',
                        '.x1ja2u2z',
                        '[data-cookiebanner="banner"]',
                        '[role="presentation"]',
                    ];
                    removeSelectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            if (el.innerText && el.innerText.toLowerCase().match(/log in|sign up|continue|facebook/i)) {
                                el.style.display = 'none';
                                el.remove();
                            }
                        });
                    });
                    
                    // Remove any element with very high z-index
                    document.querySelectorAll('*').forEach(el => {
                        const zIndex = parseInt(window.getComputedStyle(el).zIndex);
                        if (zIndex > 1000) el.style.display = 'none';
                    });
                }''')
            except:
                pass
            
            # Try multiple selectors - be more aggressive
            post_text = ""
            selectors = [
                "[data-ad-comet-preview='message']",
                "article div[dir='auto']",
                "div[role='article'] div[dir='auto']",
                "[data-ft]",
                "span._sp_",
                "div.x1i10hfl.x1qjc9v5.xjbqb8w.xjqpnuy.xa49m3k.xqeqjp1.x2lxn1h.x5yr2zz.xekx6yh.x1iyjqo2.xs83m0k.x1n2onr6.x1ypdoh8",
                "div.x1n2onr6.x1ja2u2z",
            ]
            
            all_elements_text = []  # Capture ALL visible text for debugging
            
            for sel in selectors:
                try:
                    elements = page.query_selector_all(sel)
                    for idx, el in enumerate(elements):
                        if el:
                            text = el.inner_text().strip() if el.is_visible() else ""
                            all_elements_text.append(text[:200])
                            
                            if len(text) > 20 and "log in" not in text.lower() and "continue" not in text.lower():
                                post_text = text
                                print(f"[INFO] Found post via selector {sel} (element #{idx})")
                                break
                    if post_text:
                        break
                except Exception as sel_err:
                    continue
            
            # If no posts found via selectors, try extracting ALL text from body
            if not post_text:
                try:
                    all_body_text = page.inner_text("body")
                    all_lines = [l.strip() for l in all_body_text.split('\n') if l.strip() and len(l.strip()) > 10]
                    all_lines = [l for l in all_lines if "log in" not in l.lower() and "facebook" not in l.lower()]
                    if all_lines:
                        post_text = all_lines[0]  # Take first substantial line
                        result["found_via"] = "fallback_body_extraction"
                        print(f"[INFO] Used fallback: extracted from body, {len(all_lines)} candidates found")
                except:
                    pass
            
            browser.close()
            
            result["page_load_time_sec"] = page_load_time
            result["total_elements_scanned"] = sum(len(page.query_selector_all(sel)) for sel in selectors) if 'selectors' in locals() else 0
            result["all_candidates_preview"] = all_elements_text[:3]  # Store up to 3 candidates
            
            fb_data = state.get("fb", {})
            f = fb_data.get(page_id, {"snippet": "", "count": 0})
            
            if not post_text:
                result["status"] = "no_posts_found"
                result["details"] = f"All selectors failed, page_load: {page_load_time}s, elements scanned: {result.get('total_elements_scanned', 'N/A')}"
                result["debug_all_candidates"] = "\n".join(all_elements_text[:5])
                print(f"[INFO] {page_id}: No valid posts detected")
                
                if page_id not in fb_data:
                    fb_data[page_id] = {
                        "snippet": "",
                        "count": 0,
                        "last_check": datetime.now().isoformat(),
                        "status": "no_posts_found",
                        "page_url_attempted": fb_url,
                        "page_load_time_sec": page_load_time,
                        "candidates_found": len(all_elements_text),
                        "candidate_previews": all_elements_text[:5],
                        "debug_info": "Likely login wall blocking content or page doesn't exist"
                    }
                    state["fb"] = fb_data
                return state
            
            # Post was found
            result["status"] = "success"
            result["captured_text_length"] = len(post_text)
            result["post_snippet"] = post_text[:100]
            
            snippet = post_text[:25]
            
            if f["snippet"] != snippet:
                f["snippet"], f["count"] = snippet, 1
                preview = post_text[:150].replace("\n", " ")
                send_whatsapp(f"📱 *NEW FACEBOOK POST (1/5)* 📱\nPage ID: {page_id}\n\n{preview}...")
                print(f"[INFO] NEW POST DETECTED at {page_id}")
            elif f["count"] > 0 and f["count"] < 5:
                f["count"] += 1
                preview = post_text[:150].replace("\n", " ")
                send_whatsapp(f"📱 *FACEBOOK POST REMINDER ({f['count']}/5)* 📱\nPage ID: {page_id}\n\n{preview}...")
            
            f["last_check"] = datetime.now().isoformat()
            f["page_load_time_sec"] = page_load_time
            f["status"] = "success"
            f["candidates_found"] = len(all_elements_text)
            f["candidate_previews"] = all_elements_text[:5]
            f["full_post_sample"] = post_text[:300]  # First 300 chars for debugging
            fb_data[page_id] = f
            state["fb"] = fb_data
            
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
    print(f"[CONFIG] Raw FB secrets: {raw_fb_pages}")
    print(f"[CONFIG] Normalized FB page IDs: {FB_PAGES}")
    print(f"[CONFIG] FB URLs will be: {[f'https://www.facebook.com/{p}/posts/' for p in FB_PAGES]}")
    
    state = load_state()
    
    print("[STEP 1] Checking OwnDays...")
    state = check_owndays_http(state)
    
    print("[STEP 2] Checking Levi's...")
    state = check_levis_http(state)
    
    print("[STEP 3] Checking Facebook pages...")
    if not FB_PAGES:
        print("[WARN] No Facebook page IDs configured! Check your GitHub secrets:")
        print("      - FB_PAGE_1, FB_PAGE_2, FB_PAGE_3 (should be page IDs like 'hahaphone.hk' NOT full URLs)")
        add_error(state, "config", "No Facebook page IDs found in environment variables")
    else:
        for page_id in FB_PAGES:
            if page_id:
                fb_url = f"https://www.facebook.com/{page_id}/posts/"
                print(f"      → Checking: {page_id} (URL: {fb_url})")
                state = check_facebook_page(page_id, state)
            else:
                print(f"      → Skipping empty page ID")
    
    save_state(state)
    
    print(f"[DONE] Run completed. Total tracked errors: {len(state.get('_errors', []))}")

if __name__ == "__main__":
    main()
