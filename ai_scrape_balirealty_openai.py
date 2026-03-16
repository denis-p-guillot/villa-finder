from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests
from bs4 import BeautifulSoup  # type: ignore
from dotenv import load_dotenv
from openai import OpenAI

from villa_csv import MAIN_CSV_COLUMNS, normalize_row
from scrape_balirealty import parse_balirealty_listing_html


SYSTEM_PROMPT = """
You are a data extractor for Bali villa listings.

You receive:
- The URL of a property detail page on https://www.balirealty.com (or similar site)
- The full HTML of the page

Your job is to read the page and output ONE JSON object with these exact keys:
- title: short marketing name of the property (string)
- price_idr: price in Indonesian Rupiah as a human-readable string, e.g. "Rp 5.000.000.000" or "USD 350,000" if IDR is not available
- duration: rental duration like "Rental yearly", "Rental monthly", or empty string for sales (string)
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
- updated_by: ISO-8601 style timestamp if available (e.g. "2026-01-30T08:02:58+07:00"), otherwise empty string (string)
- main_image: URL of the best/primary property photo (string)
- image_urls: pipe-separated list of image URLs (e.g. "url1 | url2 | url3") (string)
- url: the original page URL you received (string)

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


def fetch_html(url: str, timeout: float = 30.0) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def extract_row_from_html(client: OpenAI, url: str, html: str) -> Dict[str, str]:
    user_payload = {
        "url": url,
        "html": html,
    }
    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Extract one villa listing in the required JSON shape from this page:\n"
                + json.dumps(user_payload, ensure_ascii=False),
            },
        ],
    )
    content = completion.choices[0].message.content or "{}"
    data = json.loads(content)

    # Ensure all expected keys exist
    for key in MAIN_CSV_COLUMNS:
        if key not in data:
            data[key] = ""

    # Normalize everything for CSV using existing strategy
    return normalize_row(data)


def load_listing_urls(source: str) -> List[str]:
    """
    Load BaliRealty LISTING page URLs from:
    - a single URL
    - a .txt file (one URL per line)
    """
    if source.startswith("http://") or source.startswith("https://"):
        return [source.strip()]

    path = Path(source)
    if not path.exists():
        raise SystemExit(f"Listing URL source not found: {source}")

    urls: List[str] = []
    if path.suffix.lower() in {".txt", ".list"}:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                urls.append(line)
    else:
        raise SystemExit(
            "Unsupported URL source type. Use a .txt file with one listing URL per line, "
            "or pass a single listing URL directly."
        )

    return urls


def main() -> None:
    ap = argparse.ArgumentParser(
        description="From BaliRealty LISTING URLs, visit detail pages with OpenAI and output a fresh CSV matching the unified schema."
    )
    ap.add_argument(
        "listing_source",
        help="Either: (a) a single BaliRealty LISTING URL (e.g. /bali-villas-for-sale/), or (b) a .txt file with one listing URL per line.",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="balirealty_openai.csv",
        help="Output CSV path (default: balirealty_openai.csv)",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between OpenAI calls in seconds (default: 0.5, increase if you hit rate limits).",
    )
    args = ap.parse_args()

    listing_urls = load_listing_urls(args.listing_source)
    if not listing_urls:
        print("No listing URLs found to process.", file=sys.stderr)
        raise SystemExit(1)

    client = get_client()
    out_path = Path(args.output)

    with out_path.open("w", encoding="utf-8", newline="") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=MAIN_CSV_COLUMNS, delimiter=";")
        writer.writeheader()

        all_detail_urls: List[str] = []
        for l_idx, listing_url in enumerate(listing_urls, start=1):
            print(f"[Listing {l_idx}/{len(listing_urls)}] Fetching listing {listing_url} ...")
            try:
                listing_html = fetch_html(listing_url)
            except Exception as e:
                print(f"  ! HTTP error for listing {listing_url}: {e} (skipping)")
                continue

            # Use existing BeautifulSoup-based parser to extract property cards
            listing_rows = parse_balirealty_listing_html(listing_html)
            detail_urls = [r.get("url") for r in listing_rows if r.get("url")]
            print(f"  → Found {len(detail_urls)} detail URLs on this listing page.")
            all_detail_urls.extend(detail_urls)

        # Deduplicate detail URLs
        all_detail_urls = list(dict.fromkeys(u for u in all_detail_urls if u))

        total = len(all_detail_urls)
        print(f"Total unique detail URLs to process: {total}")

        for idx, detail_url in enumerate(all_detail_urls, start=1):
            print(f"[Detail {idx}/{total}] Fetching {detail_url} ...")
            try:
                detail_html = fetch_html(detail_url)
            except Exception as e:
                print(f"  ! HTTP error for detail {detail_url}: {e} (skipping)")
                continue

            try:
                row = extract_row_from_html(client, detail_url, detail_html)
            except Exception as e:
                print(f"  ! OpenAI error for detail {detail_url}: {e} (skipping)")
                continue

            writer.writerow(row)
            print(f"  ✓ Wrote row: {row.get('title', '')[:80]!r}")

            if args.delay > 0:
                time.sleep(args.delay)

    print(f"Done. Attempted {total} detail URLs. Output: {out_path}")


if __name__ == "__main__":
    main()

