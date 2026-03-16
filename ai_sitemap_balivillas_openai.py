"""
Scrape villa pages from a list of sitemap URLs.

You provide maps.txt: one sitemap URL per line (e.g. https://example.com/sitemap.xml).
The script:
  1. Fetches each sitemap (and follows sitemap indexes to child sitemaps).
  2. Collects all <loc> URLs as villa pages to scrape.
  3. Opens each page in Playwright, sends HTML to OpenAI for extraction.
  4. Writes rows to CSV (same schema + rules: phone/email required, clean integers, Bali precinct).
"""
from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from typing import Set, List, Tuple
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Browser

from villa_csv import MAIN_CSV_COLUMNS

# Reuse extraction + rules from discover script
from ai_discover_balivillas_openai import (
    EXTRA_COLUMNS,
    get_client,
    ai_extract_row,
    ai_extract_site_contact,
    log,
)

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
SITEMAP_NS_ALT = "https://www.sitemaps.org/schemas/sitemap/0.9"


def fetch_sitemap_xml(url: str, timeout: int = 30) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VillaFinder/1.0)"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def extract_locs_from_sitemap_xml(xml_text: str, base_url: str) -> tuple[list[str], list[str]]:
    """
    Parse sitemap XML. Returns (list of page URLs, list of child sitemap URLs if index).
    """
    page_urls: list[str] = []
    child_sitemaps: list[str] = []

    # Regex fallback: works with or without namespaces
    is_index = "sitemapindex" in xml_text.lower() or "<sitemap>" in xml_text.lower()
    for m in re.finditer(r"<loc>\s*([^<]+)\s*</loc>", xml_text, re.I):
        u = m.group(1).strip()
        if not u:
            continue
        u = urljoin(base_url, u)
        if is_index:
            child_sitemaps.append(u)
        else:
            page_urls.append(u)
    return page_urls, child_sitemaps


def collect_urls_from_sitemaps(sitemap_entries: List[Tuple[str, str]], max_sitemaps: int = 200) -> list[str]:
    """
    Fetch sitemaps (and follow index), return deduped list of page URLs.
    sitemap_entries: list of (sitemap_url, required_substring). If required_substring is non-empty,
    only URLs containing that substring are kept for that sitemap tree.
    """
    seen_pages: Set[str] = set()
    # Queue of (sitemap_url, required_substring)
    to_fetch: list[Tuple[str, str]] = list(sitemap_entries)
    fetched_sitemaps: Set[str] = set()
    count = 0

    while to_fetch and count < max_sitemaps:
        url, required = to_fetch.pop(0)
        if url in fetched_sitemaps:
            continue
        fetched_sitemaps.add(url)
        count += 1
        log(f"Sitemap {count}: {url[:70]}...")
        try:
            xml_text = fetch_sitemap_xml(url)
        except Exception as e:
            log(f"  Failed to fetch sitemap: {e}")
            continue
        pages, children = extract_locs_from_sitemap_xml(xml_text, url)
        # Apply per-sitemap URL filter if provided
        if required:
            pages = [u for u in pages if required in u]
            children = [u for u in children if required in u]
        for u in pages:
            if u not in seen_pages:
                seen_pages.add(u)
        for u in children:
            if u not in fetched_sitemaps:
                to_fetch.append((u, required))
    return sorted(seen_pages)


def read_maps_file(path: Path) -> List[Tuple[str, str]]:
    """
    Read maps.txt.
    Each non-comment line format:
      <sitemap_url>[,<required_substring>]
    Example:
      https://www.villabalisale.com/sitemap_property.xml,/en/
    The required_substring must be present in page URLs coming from this sitemap tree
    (otherwise those URLs are ignored).
    """
    entries: List[Tuple[str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Split into sitemap_url and optional required_substring
            parts = [p.strip() for p in line.split(",", 1)]
            sitemap_url = parts[0]
            if not (sitemap_url.startswith("http://") or sitemap_url.startswith("https://")):
                continue
            required = parts[1] if len(parts) > 1 else ""
            entries.append((sitemap_url, required))
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scrape villa pages from sitemap URLs listed in maps.txt; output CSV via OpenAI extraction.",
    )
    ap.add_argument(
        "maps_file",
        nargs="?",
        default="maps.txt",
        help="Path to file with one sitemap URL per line (default: maps.txt)",
    )
    ap.add_argument("-o", "--output", default="balivillas_sitemap.csv", help="Output CSV path")
    ap.add_argument("--max-sitemaps", type=int, default=200, help="Max sitemaps to follow (default 200)")
    ap.add_argument("--max-pages", type=int, default=2000, help="Max villa pages to scrape (default 2000)")
    ap.add_argument("--no-headless", action="store_true", help="Run browser visible (e.g. for Cloudflare)")
    ap.add_argument("--wait-ms", type=int, default=8000, help="Wait (ms) in visible browser before starting (default 8000)")
    ap.add_argument("--delay", type=float, default=0.0, help="Extra delay (s) between page fetches (default 0)")
    args = ap.parse_args()

    maps_path = Path(args.maps_file)
    if not maps_path.exists():
        log(f"Maps file not found: {maps_path}")
        raise SystemExit(1)

    sitemap_entries = read_maps_file(maps_path)
    if not sitemap_entries:
        log("No sitemap entries found in maps file.")
        raise SystemExit(1)
    log(f"Loaded {len(sitemap_entries)} sitemap entry(ies) from {maps_path}")

    page_urls = collect_urls_from_sitemaps(sitemap_entries, max_sitemaps=args.max_sitemaps)
    # Keep English: /en/ or no language segment (base = English). Skip /fr/, /es/, /id/, etc.
    lang_skip = ("/fr/", "/es/", "/id/", "/de/", "/it/", "/nl/", "/pt/", "/ru/", "/ja/", "/zh/", "/th/", "/vi/")
    before_filter = len(page_urls)
    page_urls = [u for u in page_urls if ("/en/" in u or not any(lang in u for lang in lang_skip))]
    if before_filter != len(page_urls):
        log(f"Language filter: keeping /en/ or no-lang (English) URLs → {len(page_urls)} of {before_filter} page(s)")
    if not page_urls:
        log("No English or base-language URLs found after filtering. Check sitemaps.")
        raise SystemExit(1)
    page_urls = page_urls[: args.max_pages]
    log(f"Collected {len(page_urls)} page URL(s) to scrape (English only)")

    client = get_client()
    headless = not args.no_headless
    pw = sync_playwright().start()
    browser: Browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page: Page = browser.new_page()
    try:
        page.context.route(
            "**/*",
            lambda route, request: route.abort()
            if request.resource_type in ("image", "stylesheet", "font", "media")
            else route.continue_(),
        )
        log("Playwright: blocking images/styles/fonts/media to speed up sitemap scraping")
    except Exception:
        pass

    out_path = Path(args.output)
    fieldnames = MAIN_CSV_COLUMNS + EXTRA_COLUMNS
    written = 0
    # Cache site-wide default contact info per base domain
    site_contact: dict[str, dict[str, str]] = {}

    try:
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for idx, url in enumerate(page_urls, 1):
                log(f"[{idx}/{len(page_urls)}] {url[:75]}...")
                try:
                    page.goto(url, wait_until="networkidle", timeout=25000)
                    html = page.content()
                except Exception as e:
                    log(f"  Fetch failed: {e}")
                    continue
                if not html or len(html) < 300:
                    continue
                try:
                    row = ai_extract_row(client, url, html)
                    # If price is missing, retry once with a fresh context that loads full assets.
                    if not (row.get("price_idr") or "").strip():
                        log("  Price missing; retrying with full page load (no blocking)...")
                        ctx2 = None
                        try:
                            ctx2 = browser.new_context()
                            page2 = ctx2.new_page()
                            page2.goto(url, wait_until="networkidle", timeout=30000)
                            html2 = page2.content()
                            if html2 and len(html2) >= 300:
                                row = ai_extract_row(client, url, html2)
                        except Exception as e2:
                            log(f"  Full-load retry failed: {e2}")
                        finally:
                            if ctx2 is not None:
                                try:
                                    ctx2.close()
                                except Exception:
                                    pass
                except Exception as e:
                    log(f"  Extract failed: {e}")
                    continue
                phone = (row.get("agent_phone") or "").strip()
                email = (row.get("agent_email") or "").strip()

                # If missing both, try to fill from site-wide defaults for this base domain
                if not phone and not email:
                    parsed = urlparse(url)
                    base = f"{parsed.scheme}://{parsed.netloc}"
                    defaults = site_contact.get(base)
                    if defaults is None:
                        # Fetch base HTML once and ask OpenAI for default contact
                        try:
                            log(f"  No contact on page; fetching site defaults for {base} ...")
                            r = requests.get(base, headers={"User-Agent": "Mozilla/5.0 (compatible; VillaFinder/1.0)"}, timeout=20)
                            r.raise_for_status()
                            defaults = ai_extract_site_contact(client, base, r.text)
                        except Exception as e:
                            log(f"  Failed to fetch/extract site contact for {base}: {e}")
                            defaults = {"agent_phone": "", "agent_email": ""}
                        site_contact[base] = defaults
                    if not phone and defaults.get("agent_phone"):
                        phone = defaults["agent_phone"].strip()
                        row["agent_phone"] = phone
                    if not email and defaults.get("agent_email"):
                        email = defaults["agent_email"].strip()
                        row["agent_email"] = email

                # Final rule: skip rows that still have neither phone nor email
                if not phone and not email:
                    log("  Skipping: missing both agent_phone and agent_email (after defaults)")
                    continue
                writer.writerow(row)
                written += 1
                log(f"  ✓ Wrote: {row.get('title', '')[:60]!r}")
                if args.delay > 0:
                    time.sleep(args.delay)
    finally:
        log("Closing browser and cleaning up...")
        try:
            page.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    log(f"Done. Wrote {written} rows to {out_path}")


if __name__ == "__main__":
    main()
