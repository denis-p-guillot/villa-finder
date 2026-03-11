#!/usr/bin/env python3
"""
Scraper for Bali Home Immo villa rentals (yearly and monthly).
- Yearly: min_price=100M, max_price=1.5B IDR, property_types[]=yearly
- Monthly: min_price=15M, max_price=300M IDR, property_types[]=monthly
Outputs the same CSV columns as other scrapers. Uses Playwright.
"""

import argparse
import csv
import multiprocessing
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from villa_csv import MAIN_CSV_COLUMNS, get_logger, has_title_and_price, normalize_row

log = get_logger()

BASE = "https://bali-home-immo.com"

# Listing URLs (yearly and monthly) — no area filters; use "LOAD MORE PROPERTIES" to expand
LISTING_YEARLY = (
    "https://bali-home-immo.com/realestate-property/for-rent/villa/all"
    "?ref_tab=rent&property_category=villa&currency=IDR"
    "&min_price=100000000&max_price=1500000000&price_on_request=false"
    "&property_types%5B%5D=yearly"
)
LISTING_MONTHLY = (
    "https://bali-home-immo.com/realestate-property/for-rent/villa/all"
    "?ref_tab=rent&property_category=villa&currency=IDR"
    "&min_price=15000000&max_price=300000000&price_on_request=false"
    "&property_types%5B%5D=monthly"
)

# Single URL per listing type; site uses "LOAD MORE PROPERTIES" instead of pagination
LISTING_CONFIGS = [
    (LISTING_YEARLY, 1),   # max_load_more_clicks applied per URL
    (LISTING_MONTHLY, 1),
]
MAX_LOAD_MORE_CLICKS = 999  # click until no more (site has 160+ "pages" per listing)


def _human_delay(base: float, jitter: float = 2.0, scale: float = 1.0) -> None:
    if scale <= 0:
        return
    t = (base + random.uniform(0, jitter)) * scale
    if t > 0:
        time.sleep(t)


def get_browser_pages(headless: bool = False, num_pages: int = 1):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Install playwright: pip install playwright && playwright install chromium")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    pages = []
    for _ in range(num_pages):
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1920, "height": 1080},
        )
        context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        pages.append(context.new_page())
    return pages, browser, pw


def _is_bhi_rent_detail_url(full: str) -> bool:
    """True if URL is a for-rent villa detail page (yearly or monthly), not listing/area index."""
    if not full.startswith(BASE):
        return False
    parsed = urlparse(full)
    path = (parsed.path or "").strip("/").lower()
    if "/realestate-property/for-rent/villa/" not in path and "/realestate-property/rent/villa/" not in path:
        return False
    parts = [p for p in path.split("/") if p]
    if "all" in parts:
        return False
    try:
        idx = parts.index("villa")
    except ValueError:
        return False
    after_villa = parts[idx + 1:]
    if not after_villa:
        return False
    if after_villa[0] == "all":
        return False
    # Detail: .../villa/yearly|monthly/area/slug (3+ segments) or .../villa/slug (1+ segment)
    if after_villa[0] in ("yearly", "monthly"):
        return len(after_villa) >= 3  # e.g. monthly, canggu, villa-name-slug
    return True  # single slug after villa


# Match detail URLs in raw HTML (handles escaped & and quoted URLs)
_BHI_DETAIL_URL_RE = re.compile(
    r"https?://(?:www\.)?bali-home-immo\.com/(?:en/)?realestate-property/(?:for-rent|rent)/villa/"
    r"(?:yearly|monthly)/[a-z0-9_-]+/[a-z0-9][a-z0-9_-]{10,}(?:\?[^\"'\s]*)?",
    re.I,
)


def _extract_links_from_html(html: str, base: str, seen_keys: set) -> list[str]:
    """Parse HTML and append any new for-rent villa detail URLs; also regex-scan for client-rendered links."""
    added = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        full = urljoin(base, href)
        if not _is_bhi_rent_detail_url(full):
            continue
        norm = urlparse(full)
        key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/").split("?")[0]
        if key not in seen_keys:
            seen_keys.add(key)
            added.append(full)
    # Fallback: regex on raw HTML in case links are in Vue/JS or escaped (e.g. &amp;)
    for m in _BHI_DETAIL_URL_RE.finditer(html):
        raw = m.group(0).replace("&amp;", "&").strip("'\"")
        full = urljoin(base, raw) if not raw.startswith("http") else raw
        if not _is_bhi_rent_detail_url(full):
            continue
        norm = urlparse(full)
        key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/").split("?")[0]
        if key not in seen_keys:
            seen_keys.add(key)
            added.append(full)
    return added


def fetch_listing_links(page, listing_configs=None, fast_scale: float = 1.0) -> list[str]:
    """
    Collect property detail URLs from yearly and monthly listing pages.
    Pages use lazy load: click 'LOAD MORE PROPERTIES' repeatedly until no more.
    """
    listing_configs = listing_configs or LISTING_CONFIGS
    seen_keys = set()
    url_list = []
    for base_url, _ in listing_configs:
        _human_delay(2, jitter=2, scale=fast_scale)
        try:
            # domcontentloaded avoids waiting for all images/analytics; site often never fires "load"
            page.goto(base_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            log.warning("balihomeimmo | failed to load listing %s: %s", base_url[:80], e)
            continue
        # Wait for listing content (JS may render after DOM ready)
        _human_delay(5, jitter=3, scale=fast_scale)
        try:
            page.wait_for_selector(
                "a[href*='realestate-property/for-rent/villa'], a[href*='realestate-property/for-sale/villa'], a[href*='realestate-property/rent/villa']",
                timeout=25000,
            )
        except Exception:
            pass
        # Wait for at least one detail-style link (Vue may render after DOM ready)
        try:
            page.wait_for_selector("a[href*='for-rent/villa/monthly/'], a[href*='for-rent/villa/yearly/']", timeout=15000)
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")  # trigger viewport-based load
            _human_delay(2, jitter=1, scale=fast_scale)
        except Exception:
            pass
        label = "yearly" if "property_types%5B%5D=yearly" in base_url else "monthly"
        start_count = len(url_list)
        load_more_clicks = 0
        while load_more_clicks < MAX_LOAD_MORE_CLICKS:
            try:
                load_btn = page.get_by_text(re.compile(r"load more properties", re.I)).first
                if load_btn.is_visible(timeout=4000):
                    load_btn.scroll_into_view_if_needed()
                    _human_delay(0.8, jitter=0.5, scale=fast_scale)
                    load_btn.click()
                    load_more_clicks += 1
                    if load_more_clicks % 10 == 0:
                        log.info("balihomeimmo | %s: load more click %s", label, load_more_clicks)
                    _human_delay(1.2, jitter=1, scale=fast_scale)
                else:
                    break
            except Exception:
                break
        _human_delay(2, jitter=2, scale=fast_scale)
        # Prefer live DOM: listing may be JS-rendered or HTML snapshot may be from wrong state
        try:
            raw_hrefs = page.evaluate("""() => {
                const out = new Set();
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = (a.getAttribute('href') || '').trim();
                    if (h && h.includes('realestate-property') && h.includes('/villa/'))
                        out.add(h);
                });
                return Array.from(out);
            }""")
        except Exception as e:
            raw_hrefs = []
            log.debug("balihomeimmo | DOM link collection failed: %s", e)
        dom_added = 0
        for href in (raw_hrefs or []):
            full = urljoin(BASE, href)
            if not _is_bhi_rent_detail_url(full):
                continue
            norm = urlparse(full)
            key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/").split("?")[0]
            if key not in seen_keys:
                seen_keys.add(key)
                url_list.append(full)
                dom_added += 1
        if dom_added:
            log.info("balihomeimmo | %s: collected %s links from live DOM", label, dom_added)
        try:
            html = page.content()
        except Exception:
            html = ""
        added = _extract_links_from_html(html, BASE, seen_keys)
        url_list.extend(added)
        if len(url_list) == start_count:
            try:
                debug_path = Path("bhi_debug_listing.html")
                debug_path.write_text(html[:500000] if len(html) > 500000 else html, encoding="utf-8")
                log.info("balihomeimmo | saved page HTML to %s for inspection", debug_path.resolve())
            except Exception:
                pass
        count_this_listing = len(url_list) - start_count
        log.info("balihomeimmo | %s: %s load-more clicks, %s unique links", label, load_more_clicks, count_this_listing)
    return url_list


def _get_page_content(page, max_retries: int = 3) -> str:
    for _ in range(max_retries):
        try:
            return page.content()
        except Exception as e:
            if "navigating" in str(e).lower():
                time.sleep(1.5)
                continue
            raise
    return page.content()


def _parse_price_idr(text: str) -> tuple[str, str]:
    """Parse 'IDR 926.000.000 /year' or similar -> (Rp ..., duration)."""
    text = (text or "").strip()
    duration = "month"
    if re.search(r"/year|per year|yearly", text, re.I):
        duration = "year"
    num_str = re.sub(r"[^\d]", "", text)
    if not num_str:
        return "", duration
    try:
        amount = int(num_str)
    except ValueError:
        return "", duration
    if amount < 1_000_000:
        return "", duration
    formatted = f"Rp {amount:,}".replace(",", ".")
    return formatted, duration


def parse_detail_page(page, url: str, duration_hint: str = "", delay: float = 1.0, jitter_scale: float = 1.0) -> dict | None:
    """Fetch one Bali Home Immo property page and return row (unified schema)."""
    _human_delay(delay, jitter=2 * jitter_scale, scale=1.0)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:
            log.debug("balihomeimmo | goto %s attempt %s: %s", url[:60], attempt + 1, e)
            if attempt == 0:
                _human_delay(5, jitter=3, scale=jitter_scale)
            continue
        _human_delay(1.5, jitter=1.5, scale=jitter_scale)
        try:
            html = _get_page_content(page)
        except Exception as e:
            log.warning("balihomeimmo | detail %s: %s", url[:60], e)
            return None
        break
    else:
        log.warning("balihomeimmo | detail failed after retries: %s", url[:60])
        return None
    soup = BeautifulSoup(html, "html.parser")
    body_text = soup.get_text(separator=" ", strip=True)

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True).replace(",", "")
    if not title:
        for tag in soup.find_all(["h2", "h3"], class_=re.compile(r"title|property", re.I)):
            t = tag.get_text(strip=True).replace(",", "")
            if 3 <= len(t) <= 120:
                title = t
                break

    price_idr = ""
    duration = duration_hint or "year"
    for m in re.finditer(r"IDR\s*[\d.,]+\s*(?:/\s*[Yy]ear|per\s*[Yy]ear|/\s*[Mm]onth|per\s*[Mm]onth)?", body_text):
        candidate = m.group(0)
        p, d = _parse_price_idr(candidate)
        if p:
            price_idr, duration = p, d
            break
    if not price_idr:
        for el in soup.find_all(string=re.compile(r"IDR\s*[\d.,]+", re.I)):
            t = (el.strip() if isinstance(el, str) else el) or ""
            if "IDR" in t and re.search(r"[\d.,]{3,}", t):
                p, d = _parse_price_idr(t)
                if p:
                    price_idr, duration = p, d
                    break

    location = ""
    # Detail page often has "Uluwatu Pecatu" or "Area: Uluwatu" style
    for pattern in [
        r"(?:Area|Location)\s*:\s*([A-Za-z][A-Za-z\s&/,-]{2,50}?)(?:\s*Sub|\s*Bathroom|$)",
        r"^([A-Za-z]+\s+[A-Za-z]+)\s*$",
    ]:
        m = re.search(pattern, body_text, re.M | re.I)
        if m:
            loc = m.group(1).strip().replace(",", "")[:50]
            if 2 <= len(loc) <= 50 and "Bedroom" not in loc and "Bathroom" not in loc:
                location = loc
                break
    if not location:
        for tag in soup.find_all(class_=re.compile(r"location|area|address", re.I)):
            t = tag.get_text(strip=True).replace(",", "")
            if 2 <= len(t) <= 50:
                location = t
                break

    bedrooms = ""
    bathrooms = ""
    m = re.search(r"Bedroom\s*:\s*(\d+)", body_text, re.I)
    if m:
        bedrooms = m.group(1)
    if not bedrooms:
        m = re.search(r"(\d+)\s*Bedroom", body_text, re.I)
        if m:
            bedrooms = m.group(1)
    m = re.search(r"Bathroom\s*:\s*(\d+)", body_text, re.I)
    if m:
        bathrooms = m.group(1)
    if not bathrooms:
        m = re.search(r"(\d+)\s*Bathroom", body_text, re.I)
        if m:
            bathrooms = m.group(1)

    land_size_m2 = ""
    building_size_m2 = ""
    m = re.search(r"Land\s*Size\s*:\s*([\d.,]+)\s*(?:m²|m2|sqm)?", body_text, re.I)
    if m:
        land_size_m2 = re.sub(r"[^\d]", "", m.group(1))
    m = re.search(r"Building\s*Size\s*:\s*([\d.,]+)\s*(?:m²|m2|sqm)?", body_text, re.I)
    if m:
        building_size_m2 = re.sub(r"[^\d]", "", m.group(1))

    description = ""
    for tag in soup.find_all(["div", "section"], class_=re.compile(r"description|content|overview", re.I)):
        t = tag.get_text(separator=" ", strip=True).replace(",", "")
        if len(t) > 100:
            description = t[:2000]
            break
    if not description:
        for p in soup.find_all("p"):
            t = p.get_text(strip=True).replace(",", "")
            if len(t) > 80:
                description = t[:2000]
                break

    facilities_parts = []
    for tag in soup.find_all(["li", "td", "div"], string=re.compile(
        r"Pool|Furnished|Swimming|Garden|Parking|Kitchen|Furniture|Internet|Air Conditioner", re.I
    )):
        t = (tag.string or tag.get_text(strip=True) or "").strip()
        if 2 < len(t) < 80:
            facilities_parts.append(t.replace(",", ""))
    facilities = " | ".join(list(dict.fromkeys(facilities_parts))[:25]) if facilities_parts else ""

    main_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        main_image = (og["content"] or "").strip()
    if not main_image:
        for img in soup.find_all("img", src=True):
            src = (img.get("src") or "").strip()
            if "bali-home-immo" in src or "property" in src.lower():
                main_image = urljoin(BASE, src)
                break

    return {
        "url": url,
        "main_image": main_image,
        "title": title,
        "price_idr": price_idr,
        "duration": duration,
        "location": location,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "land_size_m2": land_size_m2,
        "building_size_m2": building_size_m2,
        "certificate": "",
        "description": description,
        "facilities": facilities,
        "agent_name": "Bali Home Immo",
        "updated_by": "",
    }


def _fetch_chunk_process(args):
    chunk, index_offset, delay, jitter_scale, headless = args
    pages, browser, pw = get_browser_pages(headless=headless, num_pages=1)
    results = []
    try:
        for i, url in enumerate(chunk):
            row = parse_detail_page(pages[0], url, delay=delay, jitter_scale=jitter_scale)
            if row and has_title_and_price(row):
                results.append((index_offset + i, normalize_row(row)))
    finally:
        browser.close()
        pw.stop()
    return results


def run_balihomeimmo(args) -> list[dict] | list[str] | None:
    """
    Scrape Bali Home Immo (yearly + monthly). Returns list of normalized CSV rows,
    or list of URLs if args.list_only. Returns None if no links found.
    """
    delay = 1.0 if getattr(args, "fast", False) else getattr(args, "delay", 2.0)
    fast_scale = 0.5 if getattr(args, "fast", False) else 1.0
    jitter_scale = 0.5 if getattr(args, "fast", False) else 1.0
    workers = max(1, min(getattr(args, "workers", 1), 16))
    pages_attr = getattr(args, "pages", 20)
    configs = [(base, min(max_p, pages_attr)) for base, max_p in LISTING_CONFIGS]

    log.info("balihomeimmo | launching browser")
    pages, browser, pw = get_browser_pages(headless=getattr(args, "headless", False), num_pages=1 if workers > 1 else workers)
    try:
        links = fetch_listing_links(pages[0], listing_configs=configs, fast_scale=fast_scale)
        log.info("balihomeimmo | collected %s property URLs", len(links))
        if not links:
            return None
        if getattr(args, "list_only", False):
            return links

        log.info("balihomeimmo | fetching %s detail pages (workers=%s)", len(links), workers)
        if workers == 1:
            rows = []
            for i, url in enumerate(links, 1):
                log.debug("balihomeimmo | [%s/%s] %s", i, len(links), url[:60])
                row = parse_detail_page(pages[0], url, delay=delay, jitter_scale=jitter_scale)
                if row and has_title_and_price(row):
                    rows.append(normalize_row(row))
        else:
            chunk_size = (len(links) + workers - 1) // workers
            chunks = [links[i : i + chunk_size] for i in range(0, len(links), chunk_size)]
            args_list = [
                (chunk, idx * chunk_size, delay, jitter_scale, getattr(args, "headless", False))
                for idx, chunk in enumerate(chunks)
            ]
            with multiprocessing.Pool(workers) as pool:
                results_nested = pool.map(_fetch_chunk_process, args_list)
            results = []
            for r in results_nested:
                results.extend(r)
            flat = [row for _, row in sorted(results, key=lambda x: x[0])]
            rows = [r for r in flat if (r.get("title") or "").strip() and (r.get("price_idr") or "").strip()]
        return rows
    finally:
        browser.close()
        pw.stop()


def main():
    from villa_csv import setup_logging
    setup_logging("INFO", None)

    ap = argparse.ArgumentParser(description="Scrape Bali Home Immo yearly/monthly villa listings")
    ap.add_argument("--pages", type=int, default=20, help="Max listing pages per index")
    ap.add_argument("--delay", type=float, default=2.0, help="Base delay between detail requests")
    ap.add_argument("--output", "-o", default="villa_listings.csv", help="Output CSV path")
    ap.add_argument("--append", action="store_true", help="Append to existing CSV")
    ap.add_argument("--list-only", action="store_true", help="Only collect property URLs")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    ap.add_argument("--fast", "-f", action="store_true", help="Shorter delays")
    ap.add_argument("--workers", "-w", type=int, default=1, choices=list(range(1, 17)), help="Parallel workers 1-16")
    args = ap.parse_args()

    result = run_balihomeimmo(args)
    if result is None:
        return
    if isinstance(result, list) and result and isinstance(result[0], str):
        out_path = Path(args.output).with_suffix(".txt")
        out_path.write_text("\n".join(result), encoding="utf-8")
        log.info("balihomeimmo | wrote %s URLs to %s", len(result), out_path)
        return
    rows = result
    if not rows:
        log.warning("balihomeimmo | no rows with title and price")
        return
    out_path = Path(args.output)
    file_exists = out_path.exists()
    with open(out_path, "a" if args.append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
        if not args.append or not file_exists:
            w.writeheader()
        w.writerows(rows)
    log.info("balihomeimmo | wrote %s rows to %s", len(rows), out_path)


if __name__ == "__main__":
    main()
