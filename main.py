import os
import json
import time
import random
import requests
from playwright.sync_api import sync_playwright

# --- Configuration ---
PHONE_NUMBER = os.environ.get("CALLMEBOT_PHONE")
API_KEY = os.environ.get("CALLMEBOT_API_KEY")
FB_PAGES = [os.environ.get(f"FB_PAGE_{i}") for i in range(1, 4) if os.environ.get(f"FB_PAGE_{i}")]
LEVIS_URL = "https://www.levi.com/US/en_US/search/polo/facets/feature-gender/men/sort/price-asc"
OWNDAYS_URL = "https://www.owndays.com/jp/en/products/SENICHI31?sku=6259"
STATE_FILE = "state.json"

def send_whatsapp(msg ):
    if not (PHONE_NUMBER and API_KEY): return
    url = f"https://api.callmebot.com/whatsapp.php?phone={PHONE_NUMBER}&text={requests.utils.quote(msg )}&apikey={API_KEY}"
    try: requests.get(url, timeout=15)
    except: pass

def load_state():
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE, "r"))
        except: pass
    return {"levis": {"sale": "", "count": 0}, "owndays": {"in_stock": False, "count": 0}, "fb": {}}

def clean_fb_dom(page):
    """Force-deletes login walls and overlays from the page."""
    page.evaluate('''() => {
        const selectors = ['div[role="dialog"]', 'div.x1n2onr6.x1ja2u2z', 'div.x1iorvi4'];
        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => {
                if (el.innerText.toLowerCase().includes('log in') || el.innerText.toLowerCase().includes('facebook')) el.remove();
            });
        });
        document.querySelectorAll('*').forEach(el => {
            if (parseInt(window.getComputedStyle(el).zIndex) > 100) el.remove();
        });
    }''')

def check_owndays(page, state):
    try:
        page.goto(OWNDAYS_URL, timeout=60000)
        time.sleep(5)
        is_in_stock = "out of stock online" not in page.inner_text("body").lower()
        o = state.get("owndays", {"in_stock": False, "count": 0})
        if is_in_stock:
            if not o["in_stock"]: o["count"] = 1; send_whatsapp(f"🟢 *OWNDAYS STOCK (1/5)* 🟢\nItem is IN STOCK!\n{OWNDAYS_URL}")
            elif o["count"] < 5: o["count"] += 1; send_whatsapp(f"🟢 *OWNDAYS REMINDER ({o['count']}/5)* 🟢\n{OWNDAYS_URL}")
            o["in_stock"] = True
        else: o["in_stock"], o["count"] = False, 0
        state["owndays"] = o
    except: pass
    return state

def check_levis(page, state):
    try:
        page.goto(LEVIS_URL, timeout=60000)
        time.sleep(5)
        content = page.content().lower()
        sale = next((kw for kw in ["50% off", "60% off", "70% off", "half off"] if kw in content), "")
        l = state.get("levis", {"sale": "", "count": 0})
        if sale:
            if l["sale"] != sale: l["count"] = 1; send_whatsapp(f"🚨 *LEVI'S SALE (1/5)* 🚨\nFound: *{sale.upper()}*\n{LEVIS_URL}")
            elif l["count"] < 5: l["count"] += 1; send_whatsapp(f"🚨 *LEVI'S REMINDER ({l['count']}/5)* 🚨\n*{sale.upper()}* active!\n{LEVIS_URL}")
            l["sale"] = sale
        else: l["sale"], l["count"] = "", 0
        state["levis"] = l
    except: pass
    return state

def check_facebook(page, url, state):
    try:
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(10)
        clean_fb_dom(page)
        post_text = ""
        for sel in ["div[data-ad-comet-preview='message']", "div[role='article'] div[dir='auto']"]:
            el = page.locator(sel).first
            if el.is_visible():
                text = el.inner_text().strip()
                if len(text) > 15 and "log in" not in text.lower():
                    post_text = text; break
        if not post_text: return state
        snippet = post_text[:25]
        if "fb" not in state: state["fb"] = {}
        f = state["fb"].get(url, {"snippet": "", "count": 0})
        if f["snippet"] != snippet:
            f["snippet"], f["count"] = snippet, 1
            send_whatsapp(f"📱 *NEW FB POST (1/5)* 📱\nPage: {url}\n\nPreview: {post_text[:150]}...")
        elif f["count"] > 0 and f["count"] < 5:
            f["count"] += 1
            send_whatsapp(f"📱 *FB REMINDER ({f['count']}/5)* 📱\nPage: {url}\n\nPreview: {post_text[:150]}...")
        state["fb"][url] = f
    except: pass
    return state

def main():
    if os.environ.get("STOP_ALERTS", "false").lower() == "true": return
    time.sleep(random.uniform(5, 20))
    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        state = check_owndays(page, state)
        state = check_levis(page, state)
        for url in FB_PAGES: state = check_facebook(page, url, state)
        browser.close()
    with open(STATE_FILE, "w") as f: json.dump(state, f)

if __name__ == "__main__": main()
