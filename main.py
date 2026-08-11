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
FB_PAGES = []
for raw in raw_fb_pages:
    if "facebook.com/" in raw:
        parsed = urlparse(raw)
        page_id = parsed.path.strip('/').split('/')[0]
        FB_PAGES.append(page_id)
    else:
        FB_PAGES.append(raw)

LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
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
        "_config": {"fb_pages": FB_PAGES, "checked_at": datetime.now().isoformat()}
    }

def save_state(state):
    state["_last_run"] = datetime.now().isoformat()
    state["_config"]["fb_pages"] = FB_PAGES
    state["_config"]["checked_at"] = datetime.now().isoformat()
    state["_config"]["fb_urls_used"] = [f"https://www.facebook.com/{p}/" for p in FB_PAGES]
    
    if len(state.get("_errors", [])) > 20:
        state["_errors"] = state["_errors"][-20:]
    
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
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
        add_to_cart_class = "add-to-cart" in resp.text.lower()
        add_to_cart = bool(add_to_cart_button) or add_to_cart_class
        
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
        result["details"] = f"Found sale keyword: '{sale}' (HTTP {resp.status_code})"
        
        l = state.get("levis", {"sale": "", "count": 0})
        
        if sale:
            if l["sale"] != sale:
                l["count"] = 1
                send_whatsapp(f"🚨 *LEVI'S SALE ALERT (1/5)* 🚨\nFound: *{sale.upper()}*\n{LEVIS_URL}")
            elif l["count"] < 5:
                l["count"] += 1
                send_whatsapp(f"🚨 *LEVI'S REMINDER ({l['count']}/5)* 🚨\nSale active: *{sale.upper()}*\n{LEVIS_URL}")
            l["sale"] = sale
        else:
            l["sale"], l["count"] = "", 0
        
        l["last_check"] = datetime.now().isoformat()
        l["last_response_code"] = resp.status_code
        l["parse_details"] = result["details"]
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

def extract_post_text(page):
    """
    Extract post text from Facebook page using the exact DOM structure:
    div[data-ad-comet-preview="message"] > ... > span[dir="auto"] > div > div[dir="auto"]
    
    Multiple posts may exist. We collect ALL post texts and return the newest (first) one.
    Also clicks "See more" to expand truncated content.
    """
    all_posts = []
    
    # Step 1: Click all "See more" buttons to expand truncated posts
    try:
        see_more_buttons = page.query_selector_all('div[role="button"]:has-text("See more")')
        for btn in see_more_buttons[:5]:  # Limit to first 5 posts
            try:
                if btn.is_visible():
                    btn.click()
                    time.sleep(0.5)
            except:
                pass
    except:
        pass
    
    time.sleep(1)  # Wait for expanded content
    
    # Step 2: Find all post message containers
    post_containers = page.query_selector_all('[data-ad-comet-preview="message"]')
    print(f"[DEBUG] Found {len(post_containers)} post containers with data-ad-comet-preview")
    
    for container_idx, container in enumerate(post_containers):
        try:
            # Within each container, find all div[dir="auto"] elements and concatenate their text
            dir_auto_elements = container.query_selector_all('div[dir="auto"]')
            post_lines = []
            
            for el in dir_auto_elements:
                try:
                    text = el.inner_text().strip()
                    # Skip "See more" button text and other UI noise
                    if text and len(text) > 0 and text.lower() not in ["see more", "see less", ""]:
                        # Avoid duplicates - only add if not already in list
                        if text not in post_lines:
                            post_lines.append(text)
                except:
                    pass
            
            # Join all lines into one post text
            post_text = "\n".join(post_lines)
            
            if len(post_text) > 10 and "log in" not in post_text.lower():
                all_posts.append({
                    "index": container_idx,
                    "text": post_text,
                    "lines": post_lines,
                    "length": len(post_text)
                })
                print(f"[DEBUG] Post #{container_idx}: {post_text[:80]}...")
        except Exception as e:
            print(f"[DEBUG] Error parsing post container #{container_idx}: {e}")
            continue
    
    # Step 3: If no posts found via primary selector, try fallback selectors
    if not all_posts:
        print("[DEBUG] Primary selector found nothing, trying fallbacks...")
        fallback_selectors = [
            "div[role='article'] div[dir='auto']",
            "article div[dir='auto']",
            "div[role='article']",
        ]
        
        for sel in fallback_selectors:
            try:
                elements = page.query_selector_all(sel)
                for el in elements:
                    text = el.inner_text().strip() if el.is_visible() else ""
                    if len(text) > 20 and "log in" not in text.lower() and "continue" not in text.lower():
                        all_posts.append({
                            "index": len(all_posts),
                            "text": text,
                            "lines": [text],
                            "length": len(text)
                        })
                if all_posts:
                    print(f"[DEBUG] Fallback selector '{sel}' found {len(all_posts)} posts")
                    break
            except:
                continue
    
    return all_posts

def check_facebook_page(page_id, state):
    """
    Check a Facebook page using Playwright.
    Uses main page URL (not /posts/) and targets [data-ad-comet-preview="message"] selectors.
    Captures what the system grabs for state.json debugging.
    """
    # KEY CHANGE: Use main page URL, NOT /posts/
    fb_url = f"https://www.facebook.com/{page_id}/"
    result = {
        "status": "unknown",
        "url_used": fb_url,
        "post_count": 0,
        "posts_grabbed": [],
        "page_load_time_sec": 0,
        "debug_info": ""
    }
    
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
            
            # Navigate to main page (NOT /posts/)
            try:
                page.goto(fb_url, timeout=90000, wait_until="domcontentloaded")
            except Exception as goto_err:
                result["status"] = "navigation_failed"
                result["debug_info"] = f"Goto error: {str(goto_err)[:150]}"
                add_error(state, f"facebook_{page_id}_nav", str(goto_err)[:100])
                browser.close()
                
                fb_data = state.get("fb", {})
                f = fb_data.get(page_id, {"snippet": "", "count": 0})
                f["last_check"] = datetime.now().isoformat()
                f["status"] = "navigation_failed"
                f["url_used"] = fb_url
                f["page_load_time_sec"] = round(time.time() - start, 2)
                f["debug_info"] = result["debug_info"]
                fb_data[page_id] = f
                state["fb"] = fb_data
                return state
            
            page_load_time = round(time.time() - start, 2)
            result["page_load_time_sec"] = page_load_time
            
            # Wait for JS to render posts
            time.sleep(8)
            
            # Remove login walls, cookie banners, and overlays
            try:
                page.evaluate('''() => {
                    // Remove dialogs
                    document.querySelectorAll('div[role="dialog"]').forEach(el => {
                        if (el.innerText && el.innerText.toLowerCase().match(/log in|sign up|continue|cookie/i)) {
                            el.remove();
                        }
                    });
                    
                    // Remove cookie banners
                    document.querySelectorAll('[data-cookiebanner="banner"], [role="presentation"]').forEach(el => {
                        el.remove();
                    });
                    
                    // Remove high z-index overlays (login walls)
                    document.querySelectorAll('*').forEach(el => {
                        try {
                            const zIndex = parseInt(window.getComputedStyle(el).zIndex);
                            if (zIndex > 1000) el.remove();
                        } catch(e) {}
                    });
                }''')
            except:
                pass
            
            time.sleep(1)
            
            # Extract posts using the dedicated function
            all_posts = extract_post_text(page)
            
            result["post_count"] = len(all_posts)
            result["posts_grabbed"] = [
                {"index": p["index"], "text_preview": p["text"][:200], "length": p["length"]}
                for p in all_posts[:5]  # Store up to 5 posts in state
            ]
            
            browser.close()
            
            fb_data = state.get("fb", {})
            f = fb_data.get(page_id, {"snippet": "", "count": 0})
            
            if not all_posts:
                result["status"] = "no_posts_found"
                result["debug_info"] = "No [data-ad-comet-preview='message'] elements found. Likely login wall or page doesn't exist."
                print(f"[INFO] {page_id}: No posts detected")
                
                f["last_check"] = datetime.now().isoformat()
                f["status"] = "no_posts_found"
                f["url_used"] = fb_url
                f["page_load_time_sec"] = page_load_time
                f["post_count"] = 0
                f["grabbed_content"] = []
                f["debug_info"] = result["debug_info"]
                fb_data[page_id] = f
                state["fb"] = fb_data
                return state
            
            # Use the FIRST (newest) post for comparison
            newest_post = all_posts[0]
            post_text = newest_post["text"]
            
            # Use first 25 chars as snippet for change detection
            snippet = post_text[:25]
            
            result["status"] = "success"
            result["post_snippet"] = snippet
            result["post_full_text"] = post_text[:300]
            
            print(f"[INFO] {page_id}: Found {len(all_posts)} posts. Newest: {post_text[:80]}...")
            
            if f["snippet"] != snippet:
                f["snippet"] = snippet
                f["count"] = 1
                preview = post_text[:150].replace("\n", " | ")
                send_whatsapp(
                    f"📱 *NEW FB POST (1/5)* 📱\n"
                    f"Page: {page_id}\n\n"
                    f"{preview}...\n"
                    f"{fb_url}"
                )
                print(f"[INFO] NEW POST DETECTED at {page_id}")
            elif f["count"] > 0 and f["count"] < 5:
                f["count"] += 1
                preview = post_text[:150].replace("\n", " | ")
                send_whatsapp(
                    f"📱 *FB REMINDER ({f['count']}/5)* 📱\n"
                    f"Page: {page_id}\n\n"
                    f"{preview}...\n"
                    f"{fb_url}"
                )
            
            # Store detailed results in state
            f["last_check"] = datetime.now().isoformat()
            f["status"] = "success"
            f["url_used"] = fb_url
            f["page_load_time_sec"] = page_load_time
            f["post_count"] = len(all_posts)
            f["grabbed_content"] = result["posts_grabbed"]
            f["current_snippet"] = snippet
            f["current_post_preview"] = post_text[:300]
            fb_data[page_id] = f
            state["fb"] = fb_data
            
    except Exception as e:
        result["status"] = "error"
        result["debug_info"] = str(e)[:200]
        add_error(state, f"facebook_{page_id}", f"Check failed: {str(e)[:100]}")
        
        # Still record the error in state
        fb_data = state.get("fb", {})
        f = fb_data.get(page_id, {"snippet": "", "count": 0})
        f["last_check"] = datetime.now().isoformat()
        f["status"] = "error"
        f["url_used"] = fb_url
        f["debug_info"] = result["debug_info"]
        fb_data[page_id] = f
        state["fb"] = fb_data
    
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true":
        print("[INFO] Alerts disabled by STOP_ALERTS environment variable")
        return
    
    time.sleep(random.uniform(2, 8))
    
    print(f"[START] Monitoring run starting at {datetime.now().isoformat()}")
    print(f"[CONFIG] Normalized FB page IDs: {FB_PAGES}")
    print(f"[CONFIG] FB URLs will be: {[f'https://www.facebook.com/{p}/' for p in FB_PAGES]}")
    
    state = load_state()
    
    print("[STEP 1] Checking OwnDays...")
    state = check_owndays_http(state)
    
    print("[STEP 2] Checking Levi's...")
    state = check_levis_http(state)
    
    print("[STEP 3] Checking Facebook pages...")
    if not FB_PAGES:
        print("[WARN] No Facebook page IDs configured!")
        add_error(state, "config", "No Facebook page IDs found in environment variables")
    else:
        for page_id in FB_PAGES:
            if page_id:
                print(f"      → Checking: {page_id}")
                state = check_facebook_page(page_id, state)
    
    save_state(state)
    
    print(f"[DONE] Run completed. Total tracked errors: {len(state.get('_errors', []))}")

if __name__ == "__main__":
    main()
