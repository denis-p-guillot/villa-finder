from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright, Page, Browser

from villa_csv import MAIN_CSV_COLUMNS, normalize_row

# Extra columns common to AI scrapers
EXTRA_COLUMNS = ["agent_phone", "agent_email"]


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


LISTING_SYSTEM_PROMPT = """
You are helping to find villa detail pages on a real estate listing website.

You will receive:
- The URL of a listing/search/result page
- The full HTML of that page

Your task:
- Inspect the HTML and find ALL links that lead to INDIVIDUAL villa/property detail pages.
- These are typically property detail URLs (not filters, not pagination).

Output:
- A single JSON object with ONE key:
  - detail_urls: array of absolute URLs (strings) of the villa detail pages.

Rules:
- Do not include listing pages, search pages, or pagination links; only detail pages.
- Prefer canonical/clean URLs when possible (no tracking/query parameters if avoidable).
"""


DETAIL_SYSTEM_PROMPT = """
You are a data extractor for villa listings in Bali and Indonesia.

You receive:
- The URL of a property detail page
- The full HTML of the page

Your job is to read the page and output ONE JSON object with these exact keys:
- title: short marketing name of the property (string)
- price_idr: price in Indonesian Rupiah as a human-readable string, e.g. "Rp 5.000.000.000" or "USD 350,000" if IDR is not available (string)
- duration: rental duration like "Rental yearly", "Rental monthly", "Rental daily", or empty string for sales (string)
- location: area/location name, e.g. "Canggu", "Seminyak, Bali" (string)
- bedrooms: number of bedrooms, as a string, e.g. "3" (string)
- bathrooms: number of bathrooms, as a string (string)
- land_size_m2: land size in square meters, numeric formatted as string, e.g. "400" (string)
- building_size_m2: building size in square meters, numeric formatted as string (string)
- certificate: certificate / ownership type, e.g. "Freehold", "Leasehold", "Hak Milik", or empty string if unknown (string)
- description: clean, human-readable description of the villa, max a few paragraphs, no HTML (string)
- facilities: comma-separated list of facilities, each chosen ONLY from this list:
  Air conditioning, WiFi, TV, Kitchen, Fully equipped kitchen, Washing machine, Dishwasher,
  Office / workspace, Pool, Private pool, Garden, BBQ, Beach access, Balcony, Terrace,
  Lounge, Gym, Spa, Pet friendly, Parking, Daily cleaning, Staff, Maid service,
  Security, Sea view, Rice field view
- agent_name: name of the marketing agency or contact person if clearly shown, else empty string (string)
- updated_by: ISO-8601 style timestamp if available, otherwise empty string (string)
- main_image: URL of the best/primary property photo (string)
- image_urls: pipe-separated list of image URLs (e.g. "url1 | url2 | url3") (string)
- url: the original page URL you received (string)
- agent_phone: public phone number shown for the agent or agency, in international format if possible (string)
- agent_email: public email address shown for the agent or agency (string)

Rules:
- ONLY use the allowed facilities values; if a facility is not clearly present, do not include it.
- Infer reasonable numeric values when clearly stated (e.g. "3-bedroom villa" -> bedrooms = "3").
- The JSON must be a single object, not an array.
"""


def get_client() -> OpenAI:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in environment or .env")
    return OpenAI(api_key=api_key)


def open_listing_in_chromium(listing_url: str, headless: bool, wait_ms: int) -> tuple[Browser, Page, str, object]:
    pw = sync_playwright().start()
    browser: Browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page: Page = browser.new_page()
    log(f"Opening listing page in {'headless' if headless else 'headed'} mode: {listing_url}")
    wait_until = "domcontentloaded" if headless else "networkidle"
    page.goto(listing_url, wait_until=wait_until)
    if not headless and wait_ms > 0:
        log(f"Waiting {wait_ms} ms for you to clear any challenge in the visible browser...")
        page.wait_for_timeout(wait_ms)
    html = page.content()
    return browser, page, html, pw


def extract_detail_urls_with_ai(client: OpenAI, listing_url: str, html: str) -> List[str]:
    payload = {"url": listing_url, "html": html}
    log(f"OpenAI LISTING call start | url={listing_url} | html_len={len(html)}")
    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": LISTING_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "From this listing page, extract all villa/property detail page URLs:\n"
                + json.dumps(payload, ensure_ascii=False),
            },
        ],
    )
    content = completion.choices[0].message.content or "{}"
    log(f"OpenAI LISTING call done | url={listing_url} | response_len={len(content)}")
    data = json.loads(content)
    urls = data.get("detail_urls") or []
    if not isinstance(urls, list):
        return []
    seen = {}
    for u in urls:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if not u:
            continue
        seen[u] = True
    return list(seen.keys())


def extract_row_from_html(client: OpenAI, url: str, html: str) -> Dict[str, str]:
    user_payload = {"url": url, "html": html}
    log(f"OpenAI DETAIL call start | url={url} | html_len={len(html)}")
    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": DETAIL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Extract one villa listing in the required JSON shape from this page:\n"
                + json.dumps(user_payload, ensure_ascii=False),
            },
        ],
    )
    content = completion.choices[0].message.content or "{}"
    log(f"OpenAI DETAIL call done | url={url} | response_len={len(content)}")
    data = json.loads(content)

    for key in MAIN_CSV_COLUMNS:
        if key not in data:
            data[key] = ""
    for key in EXTRA_COLUMNS:
        if key not in data:
            data[key] = ""

    row = normalize_row(data)
    for key in EXTRA_COLUMNS:
        v = data.get(key, "")
        row[key] = "" if v is None else str(v)
    return row


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Open a generic villa listing in Chromium, let OpenAI discover detail pages and extract villas into CSV."
    )
    ap.add_argument(
        "listing_url",
        help="A listing/search URL, e.g. https://www.villabalisale.com/realestate-property/for-rent/villa?...",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="generic_openai_playwright.csv",
        help="Output CSV path (default: generic_openai_playwright.csv)",
    )
    ap.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chromium visible so you can clear any challenges/logins.",
    )
    ap.add_argument(
        "--wait-ms",
        type=int,
        default=10000,
        help="How long to wait (ms) on the visible browser for you to solve challenges (default: 10000).",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Optional extra delay between OpenAI calls in seconds (default: 0).",
    )
    args = ap.parse_args()

    listing_url = args.listing_url
    if not listing_url.startswith("http"):
        log("listing_url must be an absolute URL (starting with http/https).")
        raise SystemExit(1)

    client = get_client()
    user_forced_headed = args.no_headless
    headless = not user_forced_headed

    # Step 1: open listing in Chromium, capture HTML (headless first, fallback to headed if needed)
    try:
        browser, page, listing_html, pw = open_listing_in_chromium(
            listing_url, headless=headless, wait_ms=args.wait_ms if not headless else 0
        )
    except Exception as e:
        if headless:
            log(f"Headless listing load failed ({e}); retrying in visible browser...")
            browser = page = pw = None  # type: ignore[assignment]
            browser, page, listing_html, pw = open_listing_in_chromium(
                listing_url, headless=False, wait_ms=args.wait_ms
            )
        else:
            raise

    # Step 2: let OpenAI discover all villa detail URLs on that listing page
    log("Letting OpenAI inspect listing HTML to find villa detail URLs...")
    detail_urls = extract_detail_urls_with_ai(client, listing_url, listing_html)
    if not detail_urls:
        log("OpenAI did not return any detail URLs.")
        raise SystemExit(1)

    detail_urls = list(dict.fromkeys(detail_urls))
    total = len(detail_urls)
    log(f"OpenAI found {total} unique detail URLs.")

    out_path = Path(args.output)
    fieldnames = MAIN_CSV_COLUMNS + EXTRA_COLUMNS
    try:
        with out_path.open("w", encoding="utf-8", newline="") as f_out:
            writer = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()

            # Step 3: for each detail URL, use the SAME Chromium session to fetch HTML
            for idx, url in enumerate(detail_urls, start=1):
                log(f"[Detail {idx}/{total}] Navigating to {url} in Chromium ...")
                try:
                    start_nav = time.time()
                    page.goto(url, wait_until="networkidle")
                    nav_ms = int((time.time() - start_nav) * 1000)
                    log(f"[Detail {idx}/{total}] Page loaded | nav_time_ms={nav_ms}")
                    html = page.content()
                except Exception as e:
                    log(f"  ! Playwright error for detail {url}: {e} (skipping)")
                    continue

                try:
                    ai_start = time.time()
                    row = extract_row_from_html(client, url, html)
                    ai_ms = int((time.time() - ai_start) * 1000)
                    log(f"[Detail {idx}/{total}] OpenAI extraction ok | ai_time_ms={ai_ms}")
                except Exception as e:
                    log(f"  ! OpenAI error for detail {url}: {e} (skipping)")
                    continue

                writer.writerow(row)
                log(f"  ✓ Wrote row: {row.get('title', '')[:80]!r}")

                if args.delay > 0:
                    time.sleep(args.delay)

        log(f"Done. Attempted {total} detail URLs. Output: {out_path}")
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()

