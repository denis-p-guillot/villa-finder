#!/usr/bin/env python3
"""
Scraper for Villa-Bali.com (https://www.villa-bali.com/en/search).
Outputs the same CSV format as scrape_rumah123.py so you can use the same file.
Uses Playwright for JS-rendered content and to avoid Cloudflare blocks.
"""

import argparse
import csv
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup


def _human_delay(base: float, jitter: float = 3.0) -> None:
    """Sleep for base + random jitter seconds to appear less robotic."""
    time.sleep(base + random.uniform(0, jitter))

# Same columns as Rumah123 scraper (url last, main_image before it)
MAIN_CSV_COLUMNS = [
    "title",
    "price_idr",
    "duration",
    "location",
    "bedrooms",
    "bathrooms",
    "land_size_m2",
    "building_size_m2",
    "certificate",
    "description",
    "facilities",
    "agent_name",
    "updated_by",
    "main_image",
    "url",
]

BASE = "https://www.villa-bali.com"
SEARCH_URL = f"{BASE}/en/search"
# Approximate USD to IDR for display (update as needed)
USD_TO_IDR = 16_000


def _csv_cell(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r" +", " ", s).strip()
    if len(s) > 8000:
        s = s[:8000] + "..."
    return s.replace(",", "")


def _usd_to_idr(usd_amount: float) -> str:
    amount_int = int(round(usd_amount * USD_TO_IDR))
    return f"Rp {amount_int:,}".replace(",", ".")


def _parse_price_usd(price_text: str) -> tuple[str, str]:
    """Parse 'from USD 137 per night' -> (price_idr, 'day')."""
    price_text = (price_text or "").strip()
    duration = "day"
    m = re.search(r"USD\s*[\d,]+(?:\.\d+)?", price_text, re.I)
    if not m:
        return "", duration
    num_str = re.sub(r"[^\d.]", "", m.group(0))
    try:
        usd = float(num_str)
        return _usd_to_idr(usd), duration
    except ValueError:
        return "", duration


def get_browser_page(headless: bool = False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Install playwright: pip install playwright && playwright install chromium")
    pw = sync_playwright().start()
    # Reduce automation detection (helps avoid "you have been blocked")
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1920, "height": 1080},
        device_scale_factor=1,
        has_touch=False,
        is_mobile=False,
    )
    context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    page = context.new_page()
    return page, browser, pw


def fetch_listing_links(page) -> list[str]:
    """Get all villa detail URLs from search page. Scrolls to load more if needed."""
    _human_delay(2, jitter=2)  # Pause before first request
    page.goto(SEARCH_URL, wait_until="load", timeout=45000)
    _human_delay(5, jitter=4)  # Let page settle
    for _ in range(5):
        page.evaluate("window.scrollBy(0, 800)")
        _human_delay(1.2, jitter=1.5)
    page.evaluate("window.scrollTo(0, 0)")
    _human_delay(1.5, jitter=1)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if "/en/villa/" in href:
            full = urljoin(BASE, href)
            if full not in links:
                links.append(full)
    return links


def _is_blocked(html: str) -> bool:
    """True if page is a block/captcha message."""
    lower = html.lower()
    return "you have been blocked" in lower or "have been blocked" in lower or "captcha" in lower or "access denied" in lower


def parse_detail_page(page, url: str, delay: float = 1.0) -> dict | None:
    """Fetch one villa detail page and return row in same format as Rumah123."""
    _human_delay(delay, jitter=4)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            if attempt == 0:
                _human_delay(15, jitter=10)
            continue
        _human_delay(2, jitter=2)
        html = page.content()
        if _is_blocked(html):
            if attempt == 0:
                _human_delay(20, jitter=15)
                continue
            return None
        break
    else:
        return None
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True).replace(",", "")

    price_idr = ""
    duration = "day"
    for el in soup.find_all(string=re.compile(r"USD\s*\d|per night|per week", re.I)):
        t = el.strip() if isinstance(el, str) else el
        if "USD" in t:
            price_idr, duration = _parse_price_usd(t)
            break

    location = ""
    for el in soup.find_all(string=re.compile(r"Candidasa|Jimbaran|Seminyak|Canggu|Ubud|Sanur|Pererenan|Seseh|Amed|Nusa Dua|The Bukit", re.I)):
        t = el.strip() if isinstance(el, str) else el
        if 2 <= len(t) <= 80:
            location = t.replace(",", "")
            break

    bedrooms = ""
    bathrooms = ""
    for el in soup.find_all(string=re.compile(r"\d+\s*bedroom|\d+\s*bathroom", re.I)):
        t = el.strip() if isinstance(el, str) else el
        m_b = re.search(r"(\d+)\s*bedroom", t, re.I)
        m_a = re.search(r"(\d+)\s*bathroom", t, re.I)
        if m_b:
            bedrooms = m_b.group(1)
        if m_a:
            bathrooms = m_a.group(1)

    description = ""
    desc_el = soup.find(class_=re.compile(r"description|overview|about", re.I))
    if desc_el:
        description = desc_el.get_text(separator=" ", strip=True).replace(",", "")[:5000]
    if not description:
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if len(t) > 100:
                description = t.replace(",", "")[:5000]
                break

    facilities = ""
    fac_el = soup.find(class_=re.compile(r"facilit|amenit|feature", re.I))
    if fac_el:
        items = fac_el.find_all(["li", "span", "div"])
        parts = [n.get_text(strip=True).replace(",", "") for n in items if 2 < len(n.get_text(strip=True)) < 60]
        facilities = " | ".join(parts[:30]) if parts else ""

    main_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        main_image = (og["content"] or "").strip()

    return {
        "url": url,
        "main_image": main_image,
        "title": title,
        "price_idr": price_idr,
        "duration": duration,
        "location": location,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "land_size_m2": "",
        "building_size_m2": "",
        "certificate": "",
        "description": description,
        "facilities": facilities,
        "agent_name": "Villa-Bali.com",
        "updated_by": "",
    }


def flatten_row(row: dict) -> dict:
    """Output row with same columns as Rumah123; strip commas from text fields."""
    out = {
        "title": _csv_cell(row.get("title", "")),
        "price_idr": row.get("price_idr", ""),
        "duration": row.get("duration", "day"),
        "location": _csv_cell(row.get("location", "")),
        "bedrooms": row.get("bedrooms", ""),
        "bathrooms": row.get("bathrooms", ""),
        "land_size_m2": row.get("land_size_m2", ""),
        "building_size_m2": row.get("building_size_m2", ""),
        "certificate": row.get("certificate", ""),
        "description": _csv_cell(row.get("description", "")),
        "facilities": _csv_cell(row.get("facilities", "")),
        "agent_name": _csv_cell(row.get("agent_name", "")),
        "updated_by": row.get("updated_by", ""),
        "main_image": row.get("main_image", ""),
        "url": row.get("url", ""),
    }
    return out


def main():
    ap = argparse.ArgumentParser(description="Scrape Villa-Bali.com into same CSV format as Rumah123")
    ap.add_argument("--delay", type=float, default=6.0, help="Base delay between detail requests (seconds); random jitter added")
    ap.add_argument("--workers", "-w", type=int, default=1, help="Ignored (Playwright is single-threaded); kept for CLI compatibility")
    ap.add_argument("--output", "-o", default="villa_listings.csv", help="Output CSV path (same as Rumah123)")
    ap.add_argument("--append", action="store_true", help="Append to existing CSV instead of overwriting")
    ap.add_argument("--list-only", action="store_true", help="Only collect villa URLs")
    ap.add_argument("--headless", action="store_true", help="Run browser in headless mode (no window)")
    args = ap.parse_args()

    print("Launching browser (Villa-Bali.com may use Cloudflare)...")
    page, browser, pw = get_browser_page(headless=args.headless)
    try:
        links = fetch_listing_links(page)
        print(f"Collected {len(links)} villa URLs from {SEARCH_URL}")
        if not links:
            print("No villa links found. The site structure may have changed or content is loaded dynamically.")
            return

        if args.list_only:
            out_path = Path(args.output).with_suffix(".txt")
            out_path.write_text("\n".join(links), encoding="utf-8")
            print(f"Wrote {out_path}")
            return

        print(f"Fetching {len(links)} detail pages (delay {args.delay}s per request) ...")
        rows = []
        for i, url in enumerate(links, 1):
            print(f"[{i}/{len(links)}] {url}")
            row = parse_detail_page(page, url, delay=args.delay)
            if row:
                rows.append(flatten_row(row))

        if not rows:
            print("No data collected.")
            return

        out_path = Path(args.output)
        file_exists = out_path.exists()
        with open(out_path, "a" if args.append else "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
            if not args.append or not file_exists:
                w.writeheader()
            w.writerows(rows)
        mode = "Appended" if args.append else "Wrote"
        print(f"{mode} {len(rows)} rows to {out_path}")
    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
