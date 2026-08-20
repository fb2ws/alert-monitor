"""One-time public-access diagnostic for the hkcsl Xiaomi catalogue.

This script does not automate a browser, solve a challenge, use a proxy, or retry a
blocked request. It makes one ordinary HTTP request and reports either a catalogue
summary or an explicit BLOCKED/UNVERIFIED result.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://eshop.hkcsl.com/zh_HK/xiaomi"
HK_TZ = timezone(timedelta(hours=8 ))
RAW_OUTPUT = Path("hkcsl_raw_response.html")
SUMMARY_OUTPUT = Path("hkcsl_catalogue_summary.json")

BLOCK_MARKERS = (
    "just a moment",
    "captcha",
    "verify you are human",
    "cf-chl",
    "challenge-platform",
    "attention required",
)


def hk_time():
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S HK")


def clean(value, limit=None):
    value = " ".join((value or "").split())
    return value[:limit] if limit else value


def money_to_number(value):
    return int(value.replace(",", ""))


def first_product_like_line(text):
    """Return a cautious card label, or an empty string if none can be identified."""
    ignored = {
        "view details",
        "sale",
        "low stock",
        "sold out",
        "price reduced from",
        "suggested retail price",
        "brand",
        "xiaomi",
    }

    for line in text.splitlines():
        candidate = clean(line, 160)
        lowered = candidate.lower()
        if (
            len(candidate) >= 4
            and "hk$" not in lowered
            and lowered not in ignored
            and not re.fullmatch(r"\d+% off", lowered)
            and not re.fullmatch(r"[\d,]+", candidate)
        ):
            return candidate

    return ""


def parse_cards(soup):
    """Extract likely catalogue cards without assuming one storefront theme."""
    selectors = (
        ".product-tile",
        ".product",
        "[data-pid]",
        "li.grid-tile",
        "article",
    )
    cards = []
    seen = set()

    for selector in selectors:
        for node in soup.select(selector):
            text = clean(node.get_text("\n"), 3000)
            if "hk$" not in text.lower():
                continue

            key = clean(text, 500)
            if not key or key in seen:
                continue
            seen.add(key)

            name_node = node.select_one(
                ".product-name, .pdp-link, .name, h1, h2, h3, h4, [data-testid*='name']"
            )
            name = clean(name_node.get_text(" ") if name_node else "", 160)
            if not name:
                name = first_product_like_line(node.get_text("\n"))

            price_match = re.search(
                r"Price reduced from\s*HK\$\s*([\d,]+)\s*to\s*HK\$\s*([\d,]+)",
                text,
                flags=re.IGNORECASE,
            )
            if price_match:
                original = money_to_number(price_match.group(1))
                current = money_to_number(price_match.group(2))
                discount_percent = round((original - current) / original * 100, 2)
                cards.append(
                    {
                        "product": name or "Unlabelled product card",
                        "original_hkd": original,
                        "current_hkd": current,
                        "discount_percent": discount_percent,
                        "text_sample": clean(text, 300),
                    }
                )

    return cards


def main():
    checked_at = hk_time()
    summary = {
        "system": {
            "checked_at": checked_at,
            "timezone": "Asia/Hong_Kong (UTC+8)",
            "mode": "one-time normal public HTTP diagnostic",
            "url": URL,
        },
        "result": {},
    }

    try:
        response = requests.get(
            URL,
            timeout=30,
            headers={
                "User-Agent": "hkcsl-catalogue-diagnostic/1.0 (GitHub Actions; public access test)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            },
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        summary["result"] = {
            "status": "ERROR",
            "reason": (
                "Normal public HTTP request failed: "
                f"{type(exc).__name__}: {str(exc)[:240]}"
            ),
        }
        write_outputs(summary)
        return 0

    html = response.text
    RAW_OUTPUT.write_text(html, encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    visible_text = clean(soup.get_text("\n"), 200000)
    page_title = clean(soup.title.get_text(" ") if soup.title else "", 200)
    lower_html = html.lower()
    lower_text = visible_text.lower()

    gate_markers_found = [
        marker
        for marker in BLOCK_MARKERS
        if marker in lower_html or marker in lower_text
    ]

    response_info = {
        "http_status": response.status_code,
        "final_url": response.url,
        "page_title": page_title,
        "response_bytes": len(response.content ),
        "visible_text_length": len(visible_text),
        "visible_text_sample": clean(visible_text, 500),
        "gate_markers_found": gate_markers_found,
    }

    if response.status_code >= 400 or gate_markers_found:
        summary["result"] = {
            "status": "BLOCKED",
            "reason": (
                "The site returned an access-control or challenge page; "
                "no catalogue data was trusted."
            ),
            "response": response_info,
            "catalogue": [],
        }
        write_outputs(summary)
        return 0

    cards = parse_cards(soup)
    reductions_50_or_more = [
        card for card in cards if card["discount_percent"] >= 50
    ]
    catalogue_signal = (
        "xiaomi" in lower_text
        and ("price reduced from" in lower_text or "hk$" in lower_text)
    )

    if not catalogue_signal:
        summary["result"] = {
            "status": "UNVERIFIED",
            "reason": (
                "The response was not a recognised Xiaomi catalogue page; "
                "no discount decision was made."
            ),
            "response": response_info,
            "catalogue": [],
        }
        write_outputs(summary)
        return 0

    summary["result"] = {
        "status": "SUCCESS",
        "reason": "A normal public request returned Xiaomi catalogue content.",
        "response": response_info,
        "price_reduction_cards_found": len(cards),
        "discounts_50_percent_or_more": reductions_50_or_more,
        "all_parsed_price_reductions": cards[:50],
    }
    write_outputs(summary)
    return 0


def write_outputs(summary):
    SUMMARY_OUTPUT.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = summary["result"]
    print("--- HKCSL XIAOMI ONE-TIME CATALOGUE DIAGNOSTIC ---")
    print(f"Status: {result['status']}")
    print(f"Reason: {result['reason']}")

    response = result.get("response", {})
    if response:
        print(f"HTTP status: {response.get('http_status' )}")
        print(f"Final URL: {response.get('final_url')}")
        print(f"Page title: {response.get('page_title')}")
        print(f"Response bytes: {response.get('response_bytes')}")
        print(f"Gate markers: {response.get('gate_markers_found')}")

    if result.get("status") == "SUCCESS":
        parsed = result.get("all_parsed_price_reductions", [])
        qualifying = result.get("discounts_50_percent_or_more", [])
        print(f"Parsed old/new price reductions: {len(parsed)}")
        print(f"Discounts >=50%: {len(qualifying)}")

        for product in qualifying:
            print(
                f"  - {product['product']}: HK${product['original_hkd']} -> "
                f"HK${product['current_hkd']} "
                f"({product['discount_percent']}% off)"
            )

    print("Artifacts: hkcsl_catalogue_summary.json and hkcsl_raw_response.html")
    print("--- END DIAGNOSTIC ---")

    summary_line = (
        "## hkcsl Xiaomi one-time diagnostic\n\n"
        f"**Status:** `{result['status']}`  \n"
        f"**Reason:** {result['reason']}  \n"
        f"**Checked:** {summary['system']['checked_at']}  \n"
    )

    if result.get("response"):
        summary_line += (
            f"**HTTP:** `{result['response']['http_status']}`  \n"
            f"**Page title:** {result['response']['page_title']}  \n"
         )

    if result.get("status") == "SUCCESS":
        summary_line += (
            f"**Parsed price reductions:** "
            f"{result.get('price_reduction_cards_found', 0)}  \n"
            f"**Discounts ≥50%:** "
            f"{len(result.get('discounts_50_percent_or_more', []))}  \n"
        )

    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        with open(github_summary, "a", encoding="utf-8") as handle:
            handle.write(summary_line)


if __name__ == "__main__":
    sys.exit(main())
