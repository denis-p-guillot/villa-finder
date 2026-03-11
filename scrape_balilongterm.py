#!/usr/bin/env python3
"""
Scraper for Bali Long Term Rentals (https://www.balilongtermrentals.com/yearly-rentals/).
Outputs the same CSV format as scrape_rumah123.py so you can use the same file.
Uses Playwright to get past the site's verification page.
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
    """Sleep for (base + random jitter) * scale seconds to appear less robotic."""
    if scale <= 0:
        return
    t = (base + random.uniform(0, jitter)) * scale
    if t > 0:
        time.sleep(t)


BASE = "https://www.balilongtermrentals.com"
# (listing_base_url, max_pages) for each index to crawl; 999 = no practical limit
LISTING_CONFIGS = [
    (f"{BASE}/yearly-rentals/", 999),
    (f"{BASE}/monthly-rental/", 999),
]


def _parse_price_idr_balilongterm(price_text: str, min_amount: int = 1_000_000) -> tuple[str, str]:
    """Parse 'IDR 330.000.000 / Year' -> (Rp 330.000.000, 'year').
    Ignores tiny numbers (e.g. 250, 30 from sidebar filters) unless min_amount=0."""
    price_text = (price_text or "").strip()
    duration = "year"
    if "/" in price_text:
        parts = price_text.split("/")
        price_text = parts[0].strip()
        if len(parts) > 1:
            d = parts[1].strip().lower()
            if "year" in d or "yearly" in d:
                duration = "year"
            elif "month" in d:
                duration = "month"
            elif "week" in d:
                duration = "week"
            elif "day" in d:
                duration = "day"
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


def get_browser_pages(headless: bool = False, num_pages: int = 1):
    """Launch browser and return (list of pages, browser, playwright).
    Each page has its own browser context so workers can run in parallel."""
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
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
        )
        context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        pages.append(context.new_page())
    return pages, browser, pw


def _is_verification_page(html: str) -> bool:
    """True if page is 'One moment, please' or similar verification."""
    lower = html.lower()
    return "one moment" in lower or "please wait while your request" in lower or "being verified" in lower


# Paths that are not villa detail pages (nav, contact, etc.)
NON_VILLA_SLUGS = frozenset({
    "contact", "wishlist", "career", "yearly-rentals", "monthly-rentals", "monthly-rental",
    "partner-links", "rental-information", "submit-your-villa", "page",
    "holiday-rentals", "villas-for-sale", "list-your-property", "search",
})


def _is_villa_detail_url(full: str) -> bool:
    """True if URL looks like a single villa detail page (not listing, not nav)."""
    if not full.startswith(BASE):
        return False
    path = urlparse(full).path.strip("/")
    if not path:
        return False
    parts = path.split("/")
    # /yearly-rentals/page/N/ or /monthly-rental/page/N/ -> no
    if len(parts) >= 2 and parts[1] == "page":
        if parts[0] in ("yearly-rentals", "monthly-rental"):
            return False
    # /yearly-rentals/<slug>/ or /monthly-rental/<slug>/
    if len(parts) == 2:
        if parts[0] == "yearly-rentals":
            return parts[1] not in NON_VILLA_SLUGS and not parts[1].isdigit()
        if parts[0] == "monthly-rental":
            return parts[1] not in NON_VILLA_SLUGS and not parts[1].isdigit()
    # /yearly-rentals/ or /monthly-rental/ only (no slug)
    if path in ("yearly-rentals", "monthly-rental"):
        return False
    if path.startswith("yearly-rentals/"):
        slug = parts[-1] if parts else ""
        return slug not in NON_VILLA_SLUGS and not slug.isdigit()
    if path.startswith("monthly-rental/"):
        slug = parts[-1] if parts else ""
        return slug not in NON_VILLA_SLUGS and not slug.isdigit()
    # Single segment: /<slug>/ e.g. /3-bedrooms-brand-new-villa-in-west-sanur/
    if len(parts) == 1:
        slug = parts[0]
        if slug in NON_VILLA_SLUGS:
            return False
        if slug.startswith("rent-") or slug.startswith("page"):
            return False
        if "-" in slug:
            return True
        return False
    return False


def fetch_listing_links(page, listing_configs=None, fast_scale: float = 1.0) -> list[str]:
    """Get all villa detail URLs from yearly-rentals and monthly-rental listings (and pagination)."""
    listing_configs = listing_configs or LISTING_CONFIGS
    seen = set()
    for base_url, max_pages in listing_configs:
        base_path = urlparse(base_url).path.rstrip("/")
        for page_num in range(1, max_pages + 1):
            url = base_url if page_num == 1 else f"{BASE}{base_path}/page/{page_num}/"
            _human_delay(2, jitter=2, scale=fast_scale)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                log.warning("balilongterm | failed to load listing %s: %s", url, e)
                break
            _human_delay(5, jitter=4, scale=fast_scale)
            html = page.content()
            if _is_verification_page(html):
                log.debug("balilongterm | verification page detected, retrying %s", url)
                _human_delay(12, jitter=6, scale=fast_scale)
                try:
                    page.goto(url, wait_until="networkidle", timeout=60000)
                except Exception:
                    pass
                _human_delay(6, jitter=4, scale=fast_scale)
                html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            page_links_pre = [
                urljoin(BASE, a.get("href", "").strip())
                for a in soup.find_all("a", href=True)
                if _is_villa_detail_url(urljoin(BASE, a.get("href", "").strip()))
            ]
            if not page_links_pre and (_is_verification_page(html) or page_num == 1):
                try:
                    page.wait_for_selector('a[href*="-"]', timeout=15000)
                except Exception:
                    pass
                _human_delay(3, jitter=2, scale=fast_scale)
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
            page_links = []
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                full = urljoin(BASE, href)
                if not _is_villa_detail_url(full):
                    continue
                normal = full.rstrip("/") + "/"
                page_links.append(normal)
            if not page_links:
                break
            for link in page_links:
                seen.add(link)
            if page_num == 1:
                label = "yearly" if "yearly" in base_url else "monthly"
                log.info("balilongterm | %s page 1: found %s links", label, len(page_links))
            log.debug("balilongterm | listing page %s: %s links", page_num, len(page_links))
            if len(page_links) < 10:
                break
    return list(seen)


def _get_page_content(page, max_retries: int = 3) -> str:
    """Get page HTML, retrying if page is still navigating."""
    for attempt in range(max_retries):
        try:
            return page.content()
        except Exception as e:
            err_msg = str(e).lower()
            if "navigating" in err_msg:
                log.debug("balilongterm | page still navigating, retry %s/%s", attempt + 1, max_retries)
                time.sleep(1.5)
                continue
            log.warning("balilongterm | get_page_content: %s", e)
            raise
    return page.content()

def parse_detail_page(page, url: str, delay: float = 1.0, jitter_scale: float = 1.0) -> dict | None:
    """Fetch one villa detail page and return row in same format as Rumah123."""
    _human_delay(delay, jitter=3 * jitter_scale, scale=1.0)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:
            log.debug("balilongterm | goto %s attempt %s: %s", url, attempt + 1, e)
            if attempt == 0:
                _human_delay(12, jitter=8, scale=jitter_scale)
            continue
        _human_delay(1.5, jitter=1.5, scale=jitter_scale)
        try:
            html = _get_page_content(page)
        except Exception as e:
            if attempt == 0 and "navigating" in str(e).lower():
                _human_delay(3, jitter=2, scale=jitter_scale)
                continue
            log.warning("balilongterm | detail %s: %s", url, e)
            return None
        if _is_verification_page(html):
            log.debug("balilongterm | verification on detail %s", url)
            if attempt == 0:
                _human_delay(15, jitter=10, scale=jitter_scale)
                continue
            return None
        break
    else:
        log.warning("balilongterm | detail failed after retries: %s", url)
        return None
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True).replace(",", "")

    body_text = soup.get_text(separator=" ", strip=True)

    # Asking price: must get the listing price (e.g. IDR 330.000.000 / Year), not sidebar filters (IDR 250 - IDR 500 M)
    price_idr = ""
    duration = "year"
    # Prefer match that includes "/ Year" or "per year" (main asking price), not "IDR 250 -"
    m_price = re.search(
        r"IDR\s*[\d.,]+\s*(?:/\s*[Yy]ear|per\s*[Yy]ear|/\s*[Mm]onth|per\s*[Mm]onth)",
        body_text,
    )
    if m_price:
        price_idr, duration = _parse_price_idr_balilongterm(m_price.group(0))
    if not price_idr:
        m_price = re.search(
            r"Asking price\s+IDR\s*[\d.,]+(?:\s*/\s*[Yy]ear)?",
            body_text,
            re.I,
        )
        if m_price:
            price_idr, duration = _parse_price_idr_balilongterm(m_price.group(0))
    if not price_idr:
        for el in soup.find_all(string=re.compile(r"IDR\s*[\d\.\,]+\s*/\s*[Yy]ear", re.I)):
            t = (el.strip() if isinstance(el, str) else el) or ""
            if "IDR" in t and "/" in t:
                price_idr, duration = _parse_price_idr_balilongterm(t)
                if price_idr:
                    break
        if not price_idr:
            for el in soup.find_all(string=re.compile(r"Asking price", re.I)):
                parent = el.parent
                for _ in range(5):
                    if not parent:
                        break
                    block = parent.get_text(separator=" ", strip=True)
                    if "IDR" in block and re.search(r"IDR\s*[\d.,]+\s*/\s*[Yy]ear", block, re.I):
                        price_idr, duration = _parse_price_idr_balilongterm(block)
                        break
                    parent = parent.parent
                if price_idr:
                    break

    # Location: prefer spec block "Location Sanur" / "Location Villa Ubud"; reject section headers and CTA
    location = ""
    m = re.search(r"Location\s+([A-Za-z][A-Za-z\s/]{0,40}?)(?=\s*Land\s*size|\s*\d+\s*m2|Bathroom|Bedroom|$)", body_text, re.I)
    if m:
        loc = m.group(1).strip().replace(",", "")[:50]
        if not re.search(r"Surroundings|minutes|Highlights|touch|Configuration|required field", loc, re.I):
            location = loc
    if not location:
        m = re.search(r"Location\s+Villa\s+([A-Za-z]+)", body_text, re.I)
        if m:
            location = f"Villa {m.group(1)}"
    if not location:
        for el in soup.find_all(string=re.compile(r"^Location$", re.I)):
            parent = el.parent
            if not parent:
                continue
            n = parent.find_next_sibling() or parent.find_next()
            if n:
                loc = n.get_text(strip=True).replace(",", "")
                if 2 <= len(loc) <= 50 and "Surroundings" not in loc:
                    location = loc
                    break
    if not location:
        for tag in soup.find_all(["li", "div", "p"], string=re.compile(r"Sanur|Ubud|Canggu|Umalas|Seminyak|Nusa Dua|Kerobokan|Seseh|Berawa|Pererenan|Jimbaran|Uluwatu|Denpasar|Gianyar|Tabanan|Balian|Tabanan", re.I)):
            t = tag.get_text(strip=True)
            if 2 <= len(t) <= 50 and "Location" not in t and "results found" not in t:
                location = t.replace(",", "")
                break

    # Bedrooms/bathrooms: prefer "Bedroom 3 bedrooms" / "Bathroom 3 bathrooms" (spec block), not "1 floor" or "3-Story"
    bedrooms = ""
    bathrooms = ""
    m = re.search(r"Bedroom\s*(\d+)\s*bedrooms?", body_text, re.I)
    if m:
        bedrooms = m.group(1)
    if not bedrooms:
        m = re.search(r"(\d+)\s*bedrooms\b", body_text, re.I)
        if m:
            bedrooms = m.group(1)
    if not bedrooms:
        m = re.search(r"(\d+)\s*bedroom\b", body_text, re.I)
        if m:
            bedrooms = m.group(1)
    m = re.search(r"Bathroom\s*(\d+)\s*bathrooms?", body_text, re.I)
    if m:
        bathrooms = m.group(1)
    if not bathrooms:
        m = re.search(r"(\d+)\s*bathrooms\b", body_text, re.I)
        if m:
            bathrooms = m.group(1)
    if not bathrooms:
        m = re.search(r"(\d+)\s*bathroom\b", body_text, re.I)
        if m:
            bathrooms = m.group(1)

    # Land size: support "4.000" (dot as thousand sep) and "180m2"
    def _parse_size_num(s: str) -> str:
        if not s:
            return ""
        s = s.replace(",", ".").strip()
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) == 3:
            return str(int(parts[0]) * 1000 + int(parts[1]) if parts[1].isdigit() else int(parts[0]))
        num_str = re.sub(r"[^\d.]", "", s).replace(",", ".")
        try:
            if "." in num_str:
                return str(int(float(num_str)))
            return str(int(num_str))
        except ValueError:
            return ""

    land_size_m2 = ""
    building_size_m2 = ""
    m = re.search(r"Land\s*[Ss]ize[:\s]*([\d.,]+)\s*(?:m2|sqm)?", body_text)
    if m:
        land_size_m2 = _parse_size_num(m.group(1))
    if not land_size_m2:
        m = re.search(r"Land\s*size\s*([\d.,]+)\s*m2", body_text, re.I)
        if m:
            land_size_m2 = _parse_size_num(m.group(1))
    if not land_size_m2:
        m = re.search(r"(\d+(?:\.\d{3})?)\s*m2", body_text)
        if m:
            land_size_m2 = _parse_size_num(m.group(1))
    m = re.search(r"Building\s*[Ss]ize[:\s]*([\d.,]+)\s*(?:sqm|m2)?", body_text)
    if m:
        building_size_m2 = _parse_size_num(m.group(1))
    if not building_size_m2:
        m = re.search(r"Building\s*Size[:\s]*([\d.,]+)\s*sqm", body_text, re.I)
        if m:
            building_size_m2 = _parse_size_num(m.group(1))

    description = ""
    for p in soup.find_all("p"):
        t = p.get_text(strip=True)
        if len(t) > 80 and ("This beautifully" in t or "Fully furnished" in t or "The villa" in t or "Designed" in t or "Introduction" in t):
            t = t.replace(",", "")
            if "Location & Surroundings" in t:
                t = t.split("Location & Surroundings")[0].strip()
            if "Enquire This Property" in t:
                t = t.split("Enquire This Property")[0].strip()
            if "results found" in t and "+62" in t:
                t = t.split("results found")[0].strip()
            description = t[:2000]
            break
    if not description:
        desc_el = soup.find(class_=re.compile(r"description|content|the.property", re.I))
        if desc_el:
            description = desc_el.get_text(separator=" ", strip=True).replace(",", "")
            for stop in ("Location & Surroundings", "Enquire This Property", "required field"):
                if stop in description:
                    description = description.split(stop)[0].strip()
            if "results found" in description:
                description = description.split("results found")[0].strip()
            description = description[:2000]

    # Facilities: exclude nav/footer (Contact Us, area names, country names)
    FACILITIES_JUNK = re.compile(
        r"Contact Us|Batu Bolong|Echo Beach|Seminyak Beach Side|Sanur Beach Side|Macao SAR|Pacific Islands|CONTACT US|North Macedonia|Macedonia|Monaco|Macau|required field|Swimming Pool:\s*$|Car Access:\s*$|Local Attractions:\s*$",
        re.I,
    )
    facilities_parts = []
    for section in soup.find_all(["ul", "div"], class_=re.compile(r"amenit|facilit|feature", re.I)):
        for li in section.find_all("li"):
            t = li.get_text(strip=True).replace(",", "")
            if 2 < len(t) < 60 and not FACILITIES_JUNK.search(t):
                facilities_parts.append(t)
    for el in soup.find_all(string=re.compile(r"Car Access|Closed Living|Furnished|Swimming Pool|Private Parking|Bathtub|AC\b|Gardener|Housekeeping|Pool maintenance|Storage Room|Wifi Installed|Walking Distance to Beach|Rice Field view|Maid House|Security\b", re.I)):
        t = (el.strip() if isinstance(el, str) else el) or ""
        if 2 < len(t) < 50 and not FACILITIES_JUNK.search(t):
            facilities_parts.append(t.replace(",", ""))
    facilities = " | ".join(list(dict.fromkeys(facilities_parts))[:25]) if facilities_parts else ""

    main_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        main_image = (og["content"] or "").strip()
    if not main_image:
        for img in soup.find_all("img", src=True):
            src = (img.get("src") or "").strip()
            if "balilongtermrentals" in src or "wp-content" in src:
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
        "agent_name": "Bali Long Term Rentals",
        "updated_by": "",
    }


def _fetch_chunk_process(args):
    """Run in a separate process: own browser, fetch chunk of URLs, return (index, row) list.
    Playwright sync API is not thread-safe; multiprocessing gives each worker its own Chrome."""
    chunk, index_offset, delay, jitter_scale, headless = args
    pages, browser, pw = get_browser_pages(headless=headless, num_pages=1)
    try:
        results = []
        for i, url in enumerate(chunk):
            row = parse_detail_page(pages[0], url, delay=delay, jitter_scale=jitter_scale)
            if row and has_title_and_price(row):
                results.append((index_offset + i, normalize_row(row)))
        return results
    finally:
        browser.close()
        pw.stop()


def main():
    from villa_csv import setup_logging
    setup_logging("INFO", None)  # so standalone run has console logging

    ap = argparse.ArgumentParser(description="Scrape Bali Long Term Rentals yearly-rentals into same CSV as Rumah123")
    ap.add_argument("--pages", type=int, default=25, help="Max listing pages to crawl (default 25)")
    ap.add_argument("--delay", type=float, default=3.0, help="Base delay between detail requests (seconds)")
    ap.add_argument("--output", "-o", default="villa_listings.csv", help="Output CSV path (same as Rumah123)")
    ap.add_argument("--append", action="store_true", help="Append to existing CSV instead of overwriting")
    ap.add_argument("--list-only", action="store_true", help="Only collect villa URLs")
    ap.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    ap.add_argument("--fast", "-f", action="store_true", help="Shorter delays (faster; higher block risk)")
    ap.add_argument("--workers", "-w", type=int, default=1, choices=list(range(1, 17)), help="Parallel browser processes for detail fetch, 1-16 (default 1)")
    args = ap.parse_args()

    delay = 1.2 if args.fast else args.delay
    fast_scale = 0.4 if args.fast else 1.0
    jitter_scale = 0.4 if args.fast else 1.0
    workers = max(1, min(args.workers, 16))

    result = run_balilongterm(args)
    if result is None:
        return
    if isinstance(result, list) and result and isinstance(result[0], str):
        out_path = Path(args.output).with_suffix(".txt")
        out_path.write_text("\n".join(result), encoding="utf-8")
        log.info("balilongterm | wrote %s URLs to %s", len(result), out_path)
        return
    rows = result
    if not rows:
        log.warning("balilongterm | no data collected")
        return
    out_path = Path(args.output)
    file_exists = out_path.exists()
    with open(out_path, "a" if args.append else "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
        if not args.append or not file_exists:
            w.writeheader()
        w.writerows(rows)
    mode = "appended" if args.append else "wrote"
    log.info("balilongterm | %s %s rows to %s", mode, len(rows), out_path)


def run_balilongterm(args) -> list[dict] | list[str] | None:
    """
    Scrape Bali Long Term Rentals. Returns list of normalized CSV rows (dicts),
    or list of URLs if args.list_only. Returns None if no links found.
    Caller is responsible for writing CSV.
    """
    delay = 1.2 if getattr(args, "fast", False) else getattr(args, "delay", 3.0)
    fast_scale = 0.4 if getattr(args, "fast", False) else 1.0
    jitter_scale = 0.4 if getattr(args, "fast", False) else 1.0
    workers = max(1, min(getattr(args, "workers", 1), 16))
    pages_attr = getattr(args, "pages", 25)
    configs = [(base, min(max_p, pages_attr)) for base, max_p in LISTING_CONFIGS]

    log.info("balilongterm | launching browser (site may show verification)")
    pages, browser, pw = get_browser_pages(headless=getattr(args, "headless", False), num_pages=1 if workers > 1 else workers)
    try:
        links = fetch_listing_links(pages[0], listing_configs=configs, fast_scale=fast_scale)
        log.info("balilongterm | collected %s villa URLs (yearly + monthly)", len(links))
        if not links:
            return None
        if getattr(args, "list_only", False):
            return links

        log.info("balilongterm | fetching %s detail pages (delay ~%ss, workers=%s)", len(links), round(delay, 1), workers)
        if workers == 1:
            rows = []
            for i, url in enumerate(links, 1):
                log.debug("balilongterm | [%s/%s] %s", i, len(links), url)
                row = parse_detail_page(pages[0], url, delay=delay, jitter_scale=jitter_scale)
                if row and has_title_and_price(row):
                    rows.append(normalize_row(row))
                    log.debug("balilongterm | ok [%s/%s] %s", i, len(links), url)
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
