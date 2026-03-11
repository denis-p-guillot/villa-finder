#!/usr/bin/env python3
"""
Scraper for Bali Coconut Living monthly/yearly villa listings.
- https://balicoconutliving.com/property/villa-for-monthly-rental
- https://balicoconutliving.com/property/villa-for-yearly-rental
Outputs the same CSV columns as Rumah123/scrape_balilongterm. Uses Playwright.
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


def _human_delay(base: float, jitter: float = 2.0, scale: float = 1.0) -> None:
    if scale <= 0:
        return
    t = (base + random.uniform(0, jitter)) * scale
    if t > 0:
        time.sleep(t)


BASE = "https://balicoconutliving.com"
LISTING_INDEX_PATHS = frozenset({"villa-for-monthly-rental", "villa-for-yearly-rental"})
# 999 = no practical limit; crawl until no more listing pages
LISTING_CONFIGS = [
    (f"{BASE}/property/villa-for-monthly-rental", 999),
    (f"{BASE}/property/villa-for-yearly-rental", 999),
]


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


# Listing page uses onclick="openDetail('/bali-villa-yearly-rental/...')" not href; paths start with this
BCL_DETAIL_PATH_PREFIXES = (
    "bali-villa-yearly-rental/",
    "bali-villa-monthly-rental/",
    "bali-villa-sale-freehold/",
    "bali-villa-sale-leasehold/",
)


def _is_bcl_detail_url(full: str) -> bool:
    """True if URL is a property detail page (not the monthly/yearly listing index)."""
    if not full.startswith(BASE):
        return False
    parsed = urlparse(full)
    path = (parsed.path or "").strip("/")
    # BCL listing uses onclick openDetail("/bali-villa-yearly-rental/Location/ID-Slug/Name")
    if any(path.startswith(p) for p in BCL_DETAIL_PATH_PREFIXES):
        return True
    # Legacy: /property/export/123, /property/view/123
    if path.startswith("property/"):
        rest = path[len("property/"):].strip("/")
    elif path.startswith("view/") or path.startswith("export/"):
        rest = path
    else:
        return False
    if not rest:
        return False
    first = rest.split("/")[0].split("?")[0]
    if first in LISTING_INDEX_PATHS:
        return False
    return True


def fetch_listing_links(page, listing_configs=None, fast_scale: float = 1.0) -> list[str]:
    """Collect all property detail URLs from monthly and yearly listing pages (with pagination)."""
    listing_configs = listing_configs or LISTING_CONFIGS
    seen_keys = set()
    url_list = []
    for base_url, max_pages in listing_configs:
        for page_num in range(1, max_pages + 1):
            url = f"{base_url}?page={page_num}" if page_num > 1 else base_url
            _human_delay(2, jitter=2, scale=fast_scale)
            try:
                page.goto(url, wait_until="load", timeout=45000)
            except Exception as e:
                log.warning("balicoconut | failed to load listing %s: %s", url, e)
                break
            # First page (monthly or yearly) may need extra time for listing cards to render
            wait_extra = 8 if page_num == 1 else 0
            _human_delay(5 + wait_extra, jitter=4, scale=fast_scale)
            try:
                page.wait_for_selector(".property-thumb, [onclick*='openDetail']", timeout=20000)
            except Exception:
                pass
            _human_delay(3, jitter=2, scale=fast_scale)
            try:
                html = page.content()
            except Exception:
                html = ""
            soup = BeautifulSoup(html, "html.parser")
            page_count = 0
            all_with_href = 0
            # 1) Collect from <a href="..."> (legacy /property/export/ or /property/view/)
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                all_with_href += 1
                full = urljoin(BASE, href)
                if not _is_bcl_detail_url(full):
                    continue
                norm = urlparse(full)
                path = (norm.path or "").strip("/")
                if path.startswith("view/") or path.startswith("export/"):
                    full = f"{BASE}/property/{path}"
                    norm = urlparse(full)
                key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/")
                if key not in seen_keys:
                    seen_keys.add(key)
                    url_list.append(full)
                    page_count += 1
            # 2) BCL listing uses onclick="openDetail(&quot;/bali-villa-yearly-rental/...&quot;)" — no href
            for match in re.finditer(r'openDetail\s*\(\s*(?:&quot;|["\'])(.+?)(?:&quot;|["\'])', html):
                path = (match.group(1) or "").strip().lstrip("/")
                if not path:
                    continue
                full = f"{BASE}/{path}" if not path.startswith("http") else path
                if not full.startswith(BASE):
                    continue
                if not _is_bcl_detail_url(full):
                    continue
                norm = urlparse(full)
                key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/")
                if key not in seen_keys:
                    seen_keys.add(key)
                    url_list.append(full)
                    page_count += 1
            # Fallback: if BeautifulSoup found 0 detail links, try Playwright live DOM (JS-rendered or different structure)
            if page_count == 0:
                if page_num == 1:
                    log.info("balicoconut | page has %s <a href> total; 0 matched. Trying live DOM fallback.", all_with_href)
                try:
                    raw_hrefs = page.evaluate("""() => {
                        const out = new Set();
                        document.querySelectorAll('a[href]').forEach(a => {
                            const h = (a.getAttribute('href') || '').trim();
                            if (h && (h.includes('property') || h.includes('view') || h.includes('export')))
                                out.add(h);
                        });
                        return Array.from(out);
                    }""")
                    for href in (raw_hrefs or []):
                        full = urljoin(BASE, href)
                        if not _is_bcl_detail_url(full):
                            continue
                        norm = urlparse(full)
                        path = (norm.path or "").strip("/")
                        if path.startswith("view/") or path.startswith("export/"):
                            full = f"{BASE}/property/{path}"
                            norm = urlparse(full)
                        key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/")
                        if key not in seen_keys:
                            seen_keys.add(key)
                            url_list.append(full)
                            page_count += 1
                    if page_count > 0 and page_num == 1:
                        log.info("balicoconut | fallback found %s links from live DOM", page_count)
                except Exception as e:
                    log.debug("balicoconut | live DOM fallback failed: %s", e)
                # If still 0 on first page, wait longer and retry once (monthly often loads slower)
                if page_count == 0 and page_num == 1:
                    _human_delay(10, jitter=5, scale=fast_scale)
                    try:
                        html = page.content()
                    except Exception:
                        pass
                    for match in re.finditer(r'openDetail\s*\(\s*(?:&quot;|["\'])(.+?)(?:&quot;|["\'])', html):
                        path = (match.group(1) or "").strip().lstrip("/")
                        if not path:
                            continue
                        full = f"{BASE}/{path}" if not path.startswith("http") else path
                        if not full.startswith(BASE) or not _is_bcl_detail_url(full):
                            continue
                        norm = urlparse(full)
                        key = f"{norm.scheme}://{norm.netloc}{norm.path}".rstrip("/")
                        if key not in seen_keys:
                            seen_keys.add(key)
                            url_list.append(full)
                            page_count += 1
                    if page_count > 0:
                        log.info("balicoconut | retry after extra wait: found %s links", page_count)
                    else:
                        try:
                            debug_path = Path("bcl_debug_listing.html")
                            debug_path.write_text(html[:300000] if len(html) > 300000 else html, encoding="utf-8")
                            log.info("balicoconut | saved page HTML to %s for inspection", debug_path.resolve())
                        except Exception:
                            pass
            if page_num == 1:
                label = "monthly" if "monthly" in base_url else "yearly"
                log.info("balicoconut | %s page 1: found %s new links", label, page_count)
            log.debug("balicoconut | listing page %s: %s new links", page_num, page_count)
            if page_count == 0:
                break
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


def _parse_price_idr_bcl(price_text: str, min_amount: int = 1_000_000) -> tuple[str, str]:
    """Parse 'IDR 55.000.000' or 'IDR 450.000.000' -> (Rp ..., duration from context)."""
    price_text = (price_text or "").strip()
    duration = "month"
    if re.search(r"[Yy]ear|yearly", price_text):
        duration = "year"
    num_str = re.sub(r"[^\d]", "", price_text)
    if not num_str:
        return "", duration
    try:
        amount = int(num_str)
    except ValueError:
        return "", duration
    if amount < min_amount:
        return "", duration
    formatted = f"Rp {amount:,}".replace(",", ".")
    return formatted, duration


def parse_detail_page(page, url: str, duration_hint: str = "", delay: float = 1.0, jitter_scale: float = 1.0) -> dict | None:
    """Fetch one BCL property page and return row (same schema as other scrapers)."""
    _human_delay(delay, jitter=2 * jitter_scale, scale=1.0)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:
            log.debug("balicoconut | goto %s attempt %s: %s", url, attempt + 1, e)
            if attempt == 0:
                _human_delay(8, jitter=5, scale=jitter_scale)
            continue
        _human_delay(1.5, jitter=1.5, scale=jitter_scale)
        try:
            html = _get_page_content(page)
        except Exception as e:
            log.warning("balicoconut | detail %s: %s", url, e)
            return None
        break
    else:
        log.warning("balicoconut | detail failed after retries: %s", url)
        return None
    soup = BeautifulSoup(html, "html.parser")
    body_text = soup.get_text(separator=" ", strip=True)

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True).replace(",", "")
    if not title:
        for tag in soup.find_all(["h2", "h3"], class_=re.compile(r"title|property|name", re.I)):
            t = tag.get_text(strip=True).replace(",", "")
            if 3 <= len(t) <= 120:
                title = t
                break

    price_idr = ""
    duration = duration_hint or "month"
    for m in re.finditer(r"IDR\s*[\d.,]+\s*(?:/\s*[Yy]ear|per\s*[Yy]ear|/\s*[Mm]onth|per\s*[Mm]onth)?", body_text):
        candidate = m.group(0)
        p, d = _parse_price_idr_bcl(candidate)
        if p:
            price_idr, duration = p, d
            break
    if not price_idr:
        for el in soup.find_all(string=re.compile(r"IDR\s*[\d.,]+", re.I)):
            t = (el.strip() if isinstance(el, str) else el) or ""
            if "IDR" in t and re.search(r"[\d.,]{3,}", t):
                p, d = _parse_price_idr_bcl(t)
                if p:
                    price_idr, duration = p, d
                    break

    location = ""
    for pattern in [
        r"([A-Za-z][A-Za-z\s/,]{2,40}?)\s*\|\s*Villa\b",
        r"(?:Location|Area)\s*[:\s]*([A-Za-z][A-Za-z\s/,]{2,40})",
        r"(Canggu|Pererenan|Ubud|Umalas|Berawa|Seminyak|Kerobokan|Padonan|Babakan|Kayu Tulang|Padang Linjong|Pererenan|Gianyar)\b",
    ]:
        m = re.search(pattern, body_text, re.I)
        if m:
            loc = m.group(1).strip().replace(",", "")[:50]
            if 2 <= len(loc) <= 50 and "Bedroom" not in loc and "Bathroom" not in loc:
                location = loc
                break

    bedrooms = ""
    bathrooms = ""
    m = re.search(r"(\d+)\s*Bedroom", body_text, re.I)
    if m:
        bedrooms = m.group(1)
    if not bedrooms:
        m = re.search(r"(\d+)\s*bedrooms?", body_text, re.I)
        if m:
            bedrooms = m.group(1)
    m = re.search(r"(\d+)\s*Bathroom", body_text, re.I)
    if m:
        bathrooms = m.group(1)
    if not bathrooms:
        m = re.search(r"(\d+)\s*bathrooms?", body_text, re.I)
        if m:
            bathrooms = m.group(1)

    land_size_m2 = ""
    building_size_m2 = ""
    for m in re.finditer(r"(\d+(?:\.\d{3})?)\s*m2", body_text):
        val = m.group(1).replace(".", "").strip()
        if val.isdigit() and not land_size_m2:
            land_size_m2 = str(int(val))
            break
    m = re.search(r"Land\s*[Ss]ize[:\s]*([\d.,]+)\s*(?:m2|sqm)?", body_text, re.I)
    if m:
        land_size_m2 = re.sub(r"[^\d]", "", m.group(1)) or land_size_m2
    m = re.search(r"Building\s*[Ss]ize[:\s]*([\d.,]+)\s*(?:m2|sqm)?", body_text, re.I)
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
    for tag in soup.find_all(["li", "div", "span"], string=re.compile(
        r"Pool|Furnished|Living|Parking|Kitchen|Garden|AC\b|Wifi|Security|Bedroom|Bathroom", re.I
    )):
        t = (tag.string or tag.get_text(strip=True) or "").strip()
        if 2 < len(t) < 60:
            facilities_parts.append(t.replace(",", ""))
    facilities = " | ".join(list(dict.fromkeys(facilities_parts))[:25]) if facilities_parts else ""

    main_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        main_image = (og["content"] or "").strip()
    if not main_image:
        for img in soup.find_all("img", src=True):
            src = (img.get("src") or "").strip()
            if "balicoconutliving" in src or "property" in src:
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
        "agent_name": "Bali Coconut Living",
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


def main():
    from villa_csv import setup_logging
    setup_logging("INFO", None)  # so standalone run has console logging

    ap = argparse.ArgumentParser(description="Scrape Bali Coconut Living monthly/yearly villa listings")
    ap.add_argument("--pages", type=int, default=50, help="Max listing pages per index (default 50)")
    ap.add_argument("--delay", type=float, default=2.0, help="Base delay between detail requests (seconds)")
    ap.add_argument("--output", "-o", default="villa_listings.csv", help="Output CSV path")
    ap.add_argument("--append", action="store_true", help="Append to existing CSV")
    ap.add_argument("--list-only", action="store_true", help="Only collect property URLs")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    ap.add_argument("--fast", "-f", action="store_true", help="Shorter delays")
    ap.add_argument("--workers", "-w", type=int, default=1, choices=list(range(1, 17)), help="Parallel workers 1-16 (default 1)")
    args = ap.parse_args()

    delay = 1.0 if args.fast else args.delay
    fast_scale = 0.5 if args.fast else 1.0
    jitter_scale = 0.5 if args.fast else 1.0
    workers = max(1, min(args.workers, 16))
    configs = [(base, min(max_p, args.pages)) for base, max_p in LISTING_CONFIGS]

    result = run_balicoconut(args)
    if result is None:
        return
    if isinstance(result, list) and result and isinstance(result[0], str):
        out_path = Path(args.output).with_suffix(".txt")
        out_path.write_text("\n".join(result), encoding="utf-8")
        log.info("balicoconut | wrote %s URLs to %s", len(result), out_path)
        return
    rows = result
    if not rows:
        log.warning("balicoconut | no rows with title and price")
        return
    out_path = Path(args.output)
    file_exists = out_path.exists()
    with open(out_path, "a" if args.append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
        if not args.append or not file_exists:
            w.writeheader()
        w.writerows(rows)
    mode = "appended" if args.append else "wrote"
    log.info("balicoconut | %s %s rows to %s", mode, len(rows), out_path)


def run_balicoconut(args) -> list[dict] | list[str] | None:
    """
    Scrape Bali Coconut Living. Returns list of normalized CSV rows (dicts),
    or list of URLs if args.list_only. Returns None if no links found.
    Caller is responsible for writing CSV.
    """
    delay = 1.0 if getattr(args, "fast", False) else getattr(args, "delay", 2.0)
    fast_scale = 0.5 if getattr(args, "fast", False) else 1.0
    jitter_scale = 0.5 if getattr(args, "fast", False) else 1.0
    workers = max(1, min(getattr(args, "workers", 1), 16))
    pages_attr = getattr(args, "pages", 50)
    configs = [(base, min(max_p, pages_attr)) for base, max_p in LISTING_CONFIGS]

    log.info("balicoconut | launching browser")
    pages, browser, pw = get_browser_pages(headless=getattr(args, "headless", False), num_pages=1 if workers > 1 else workers)
    try:
        links = fetch_listing_links(pages[0], listing_configs=configs, fast_scale=fast_scale)
        log.info("balicoconut | collected %s property URLs", len(links))
        if not links:
            return None
        if getattr(args, "list_only", False):
            return links

        log.info("balicoconut | fetching %s detail pages (workers=%s)", len(links), workers)
        if workers == 1:
            rows = []
            for i, url in enumerate(links, 1):
                log.debug("balicoconut | [%s/%s] %s", i, len(links), url)
                row = parse_detail_page(pages[0], url, delay=delay, jitter_scale=jitter_scale)
                if row and has_title_and_price(row):
                    rows.append(normalize_row(row))
                    log.debug("balicoconut | ok [%s/%s] %s", i, len(links), url)
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


if __name__ == "__main__":
    main()
