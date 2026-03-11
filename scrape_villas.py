#!/usr/bin/env python3
"""
Unified villa scraper: all sources into one CSV format.
Usage: --source all | rumah123 | villa-bali | balilongterm | balicoconut
Output: single CSV (villa_listings.csv) with unified columns; only rows with title and price_idr.
"""

import argparse
import csv
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from villa_csv import MAIN_CSV_COLUMNS, get_logger, setup_logging

log = get_logger()

try:
    from scrape_balilongterm import run_balilongterm
except ImportError:
    run_balilongterm = None
try:
    from scrape_balicoconut import run_balicoconut
except ImportError:
    run_balicoconut = None
try:
    from scrape_balihomeimmo import run_balihomeimmo
except ImportError:
    run_balihomeimmo = None

# ---------------------------------------------------------------------------
# Shared CSV format (canonical columns from villa_csv)
# ---------------------------------------------------------------------------


def _csv_cell(s: str) -> str:
    """Sanitize a value for CSV: no commas, no newlines, no double-quotes."""
    if not isinstance(s, str):
        return ""
    if "push(" in s or '"formats"' in s or '"locale"' in s:
        return ""
    s = str(s).replace('"', "'").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r" +", " ", s).strip()
    if len(s) > 8000:
        s = s[:8000] + "..."
    return s.replace(",", "")


# Rumah123: parse "Rp X /hari" -> (price_idr, duration)
DURATION_MAP = {
    "/hari": "day", "per hari": "day", "/minggu": "week", "per minggu": "week",
    "/bulan": "month", "per bulan": "month", "/tahun": "year", "per tahun": "year",
    "/year": "year", "/month": "month", "/day": "day",
}


def _parse_price_idr_rumah123(price_text: str) -> tuple[str, str]:
    price_text = (price_text or "").strip()
    duration = ""
    for key, eng in DURATION_MAP.items():
        if key in price_text.lower():
            duration = eng
            price_text = price_text.lower().split(key)[0].strip()
            break
    num_str = re.sub(r"[^\d,\.]", "", price_text).replace(",", ".")
    if not num_str:
        return "", duration
    try:
        amount = float(num_str)
    except ValueError:
        return "", duration
    lower = price_text.lower()
    if "miliar" in lower or "milliar" in lower:
        amount *= 1_000_000_000
    elif "juta" in lower:
        amount *= 1_000_000
    elif "ribu" in lower:
        amount *= 1_000
    else:
        if amount < 10_000:
            amount *= 1_000_000
    amount_int = int(round(amount))
    return f"Rp {amount_int:,}".replace(",", "."), duration


def flatten_row(row: dict, source: str = "rumah123") -> dict:
    """Normalize row to MAIN_CSV_COLUMNS. Handles Rumah123 (price + ID keys) and Villa-Bali (price_idr + direct keys)."""
    raw = {}
    for k, v in row.items():
        if isinstance(v, (list, dict)):
            raw[k] = _csv_cell(json.dumps(v, ensure_ascii=False)) if v else ""
        else:
            raw[k] = _csv_cell(str(v or ""))

    if source == "rumah123":
        facilities_parts = [
            raw.get("facilities_rumah") or "",
            raw.get("facilities_perumahan") or "",
            raw.get("perabotan") or "",
        ]
        facilities_str = " | ".join(p for p in facilities_parts if p).strip() or ""
        price_raw = raw.get("price", "")
        price_idr, duration = _parse_price_idr_rumah123(price_raw)
        out = {
            "title": (raw.get("title", "") or "").replace(",", ""),
            "price_idr": price_idr,
            "duration": duration,
            "location": raw.get("location", ""),
            "bedrooms": raw.get("Kamar_Tidur", ""),
            "bathrooms": raw.get("Kamar_Mandi", ""),
            "land_size_m2": raw.get("Luas_Tanah", ""),
            "building_size_m2": raw.get("Luas_Bangunan", ""),
            "certificate": raw.get("Sertifikat", ""),
            "description": (raw.get("description", "") or "").replace(",", ""),
            "facilities": facilities_str.replace(",", ""),
            "agent_name": (raw.get("agent_name", "") or "").replace(",", ""),
            "updated_by": raw.get("updated_by", ""),
            "main_image": raw.get("main_image", ""),
            "url": raw.get("url", ""),
        }
    else:
        # villa-bali: already has price_idr, duration, etc.
        out = {
            "title": (raw.get("title", "") or "").replace(",", ""),
            "price_idr": raw.get("price_idr", ""),
            "duration": raw.get("duration", "day"),
            "location": raw.get("location", ""),
            "bedrooms": raw.get("bedrooms", ""),
            "bathrooms": raw.get("bathrooms", ""),
            "land_size_m2": raw.get("land_size_m2", ""),
            "building_size_m2": raw.get("building_size_m2", ""),
            "certificate": raw.get("certificate", ""),
            "description": (raw.get("description", "") or "").replace(",", ""),
            "facilities": (raw.get("facilities", "") or "").replace(",", ""),
            "agent_name": (raw.get("agent_name", "") or "").replace(",", ""),
            "updated_by": raw.get("updated_by", ""),
            "main_image": raw.get("main_image", ""),
            "url": raw.get("url", ""),
        }
    return {k: _csv_cell(str(v)) for k, v in out.items()}


def _row_has_title_and_price(row: dict, source: str) -> bool:
    """True if row has both title and price_idr."""
    if not row:
        return False
    title_ok = bool((row.get("title") or "").strip())
    if source == "rumah123":
        price_idr, _ = _parse_price_idr_rumah123(row.get("price") or "")
    else:
        price_idr = (row.get("price_idr") or "").strip()
    return title_ok and bool(price_idr)


def _to_int_or_none_from_idr(price_idr: str) -> int | None:
    """Convert 'Rp 30.000.000' → 30000000 (or None on failure)."""
    if not price_idr:
        return None
    digits = re.sub(r"[^\d]", "", str(price_idr))
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def _row_to_portal_villa(row: dict, source: str) -> dict:
    """
    Convert a unified CSV row into the JSON villa schema requested by the user.

    This uses whatever fields are available in the flattened row and applies
    sensible defaults / derivations for the missing ones.
    """
    price_idr_str = row.get("price_idr") or ""
    price_idr = _to_int_or_none_from_idr(price_idr_str) or 0

    duration = (row.get("duration") or "").strip().lower() or "day"
    if duration in ("bulan", "per bulan", "/bulan"):
        duration = "month"
    elif duration in ("tahun", "per tahun", "/tahun"):
        duration = "year"

    if duration == "month":
        rental_type = "monthly"
        price_monthly = price_idr
        price_yearly = price_idr * 12 if price_idr else 0
    elif duration == "year":
        rental_type = "yearly"
        price_yearly = price_idr
        price_monthly = round(price_idr / 12) if price_idr else 0
    else:
        rental_type = "daily"
        price_monthly = price_idr * 30 if price_idr else 0
        price_yearly = price_idr * 365 if price_idr else 0

    location = (row.get("location") or "").strip()
    title = (row.get("title") or "").strip()

    try:
        bedrooms = int(row.get("bedrooms") or 0)
    except (TypeError, ValueError):
        bedrooms = 0
    try:
        bathrooms = int(row.get("bathrooms") or 0)
    except (TypeError, ValueError):
        bathrooms = 0

    def _to_float_or_zero(v):
        try:
            return float(v) if v not in (None, "") else 0
        except (TypeError, ValueError):
            return 0

    land_size = _to_float_or_zero(row.get("land_size_m2"))
    building_size = _to_float_or_zero(row.get("building_size_m2"))

    description = (row.get("description") or "").strip()
    long_description = description  # we do not have a separate long description

    facilities_raw = (row.get("facilities") or "").strip()
    facilities_list = [f.strip() for f in facilities_raw.split("|") if f.strip()] if facilities_raw else []

    # We don't have a clean split between facilities/amenities/features from this source,
    # so we use facilities for all three buckets and keep the raw string as-is.
    amenities_list = list(facilities_list)
    features_list = []

    main_image_url = (row.get("main_image") or "").strip()

    villa = {
        "source_url": row.get("url") or "",
        "detail_url": row.get("url") or "",
        "title": title,
        "location_name": location,
        "location": location,
        "area_name": location,
        "area": location,
        "managed_by": "agent",
        "rental_type": rental_type,
        "contact_email": "",  # not available in current scraper
        "price_idr": price_idr,
        "price": price_idr,
        "price_monthly": int(price_monthly),
        "price_yearly": int(price_yearly),
        "minimum_stay": "1 " + ("month" if rental_type == "monthly" else "year" if rental_type == "yearly" else "day"),
        "bedrooms": bedrooms,
        "beds": bedrooms,
        "bathrooms": bathrooms,
        "baths": bathrooms,
        "land_size_m2": land_size,
        "land_size": land_size,
        "building_size_m2": building_size,
        "building_size": building_size,
        "description": description,
        "long_description": long_description,
        "facilities": facilities_list,
        "amenities": amenities_list,
        "features": features_list,
        "facilities_raw": facilities_raw,
        "main_image_url": main_image_url,
        "image_urls": [],
        "images": [],
        "image_urls_raw": "",
    }
    return villa


# ---------------------------------------------------------------------------
# User-Agent rotation (new UA for every request)
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def _random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def _rumah123_headers() -> dict:
    return {
        "User-Agent": _random_user_agent(),
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    }


# ---------------------------------------------------------------------------
# Rumah123
# ---------------------------------------------------------------------------

RUMAH123_BASE = "https://www.rumah123.com"
RUMAH123_LISTING = f"{RUMAH123_BASE}/sewa/bali/villa/"


def _rumah123_session():
    s = requests.Session()
    # Do not set fixed User-Agent here; use _rumah123_headers() on every request
    return s


def _find_text_in_body(soup: BeautifulSoup, pattern):
    for el in soup.find_all(string=pattern):
        if not el.parent:
            continue
        p = el.parent
        while p:
            if getattr(p, "name", None) in ("script", "style", "noscript"):
                break
            p = p.parent
        else:
            return el
    return None


def rumah123_fetch_listing_links(session: requests.Session, page: int = 1) -> tuple[list[str], bool]:
    """Fetch one listing page. Returns (links, ok). ok=False on network/429 exhaustion so caller can try next page."""
    url = RUMAH123_LISTING if page <= 1 else f"{RUMAH123_LISTING}?page={page}"
    saw_429 = False
    r = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=45, headers=_rumah123_headers())
            # Retry up to 3 times with backoff when rate-limited
            for retry in range(3):
                if r.status_code != 429:
                    break
                saw_429 = True
                wait = 20 + (retry * 15)  # 20s, then 35s, then 50s
                log.info("rumah123 | 429 rate limit, waiting %ss before retry %s", wait, retry + 1)
                time.sleep(wait)
                r = session.get(url, timeout=45, headers=_rumah123_headers())
            r.raise_for_status()
            break
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
            else:
                log.warning("rumah123 | page %s failed after retries: %s", page, e)
                return ([], False)
    if r is None:
        return ([], False)
    # Cooldown after 429 so next page request doesn't immediately hit rate limit again
    if saw_429:
        cooldown = 12
        log.info("rumah123 | cooldown %ss after 429 before next page", cooldown)
        time.sleep(cooldown)
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href.startswith("/properti/") or "/agent-" in href or "/independent-property-agent/" in href:
            continue
        if re.search(r"-(?:hor|vlr)\d+/?$", href):
            path = href.rstrip("/") + "/" if not href.endswith("/") else href
            full = urljoin(RUMAH123_BASE, path)
            if full not in links:
                links.append(full)
    return (links, True)


def rumah123_parse_spec_table(soup: BeautifulSoup) -> dict:
    out = {}
    for block in soup.find_all(["section", "div"], class_=re.compile(r"spec|info|detail", re.I)):
        texts = block.get_text(separator="|", strip=True)
        if "Luas Tanah" not in texts and "Kamar Tidur" not in texts:
            continue
        parts = [p.strip() for p in texts.replace("\n", "|").split("|") if p.strip()]
        for i, p in enumerate(parts):
            if p in ("Luas Tanah", "Luas Bangunan", "Kamar Tidur", "Kamar Mandi", "Carport", "Sertifikat", "Daya Listrik", "Kondisi / Tahun Renovasi"):
                if i + 1 < len(parts):
                    val = parts[i + 1].strip()
                    if val and val != p:
                        out[p] = val
    labels = ("Luas Tanah", "Luas Bangunan", "Kamar Tidur", "Kamar Mandi", "Carport", "Sertifikat", "Daya Listrik")
    for label in labels:
        if label in out:
            continue
        el = soup.find(string=re.compile(re.escape(label), re.I))
        if el and el.parent:
            parent = el.parent
            n = parent.find_next_sibling()
            if n:
                v = n.get_text(strip=True)
                if v:
                    out[label] = v
    if not out and soup.body:
        body_text = soup.body.get_text(separator="\n", strip=True)
        for label in labels:
            if label in out:
                continue
            m = re.search(re.escape(label) + r"\s*\n\s*([^\n]{1,50})", body_text)
            if m:
                out[label] = m.group(1).strip()
    return out


def rumah123_parse_facilities(soup: BeautifulSoup) -> dict:
    out = {"Fasilitas Rumah": [], "Fasilitas Perumahan": [], "Perabotan": []}
    section = _find_text_in_body(soup, re.compile(r"Fasilitas", re.I))
    if not section:
        return out
    root = section.parent
    while root and getattr(root, "name", None) not in ("section", "div"):
        root = getattr(root, "parent", None)
    if not root:
        return out
    for h in root.find_all(["h3", "h4", "strong"], string=re.compile(r"Fasilitas Rumah|Fasilitas Perumahan|Perabotan", re.I)):
        key = h.get_text(strip=True)
        if key not in out:
            continue
        container = h.find_parent("div") or h
        next_ = container.find_next_sibling() or container.find_next()
        if next_ and getattr(next_, "find_all", None):
            items = next_.find_all(["li", "span", "div"], recursive=True)
            texts = [node.get_text(strip=True) for node in items[:50] if node.get_text(strip=True) and len(node.get_text(strip=True)) < 80]
            out[key] = list(dict.fromkeys(texts))[:30] if texts else []
    return out


def _rumah123_soup_to_row(soup: BeautifulSoup, url: str) -> dict:
    """Parse Rumah123 detail page HTML (from requests or Playwright) into a row dict."""
    h1 = soup.find("h1")
    title = h1.get_text(strip=True).replace(",", "") if h1 else ""
    price_text = ""
    el = _find_text_in_body(soup, re.compile(r"Rp\s*[\d,\.]+", re.I))
    if el:
        price_text = el.strip()
    location = ""
    for el in soup.find_all(string=re.compile(r"(Canggu|Ubud|Badung|Denpasar|Gianyar|Kerobokan|Seminyak|Sanur|Jimbaran|Tabanan)", re.I)):
        if el.parent and getattr(el.parent, "name", None) in ("script", "style", "noscript"):
            continue
        t = el.strip()
        if 5 <= len(t) <= 120:
            location = t
            break
    if not location and h1:
        p = h1.find_next(["p", "div", "span"])
        if p:
            location = p.get_text(strip=True).split(",")[0].strip()[:80]
    desc = ""
    d = _find_text_in_body(soup, re.compile(r"Deskripsi", re.I))
    if d and d.parent:
        parent = d.parent
        while parent and parent.name not in ("section", "div"):
            parent = parent.parent
        if parent:
            for s in parent.find_all(["p", "div"]):
                t = s.get_text(strip=True)
                if len(t) > 50 and "Deskripsi" not in t and "push(" not in t:
                    desc = t[:5000]
                    break
    updated_by = ""
    agent_name = ""
    for el in soup.find_all(string=re.compile(r"Diperbarui.*oleh", re.I)):
        if el.parent and getattr(el.parent, "name", None) not in ("script", "style"):
            updated_by = el.strip()
            break
    agent_el = soup.find("a", href=re.compile(r"agen-properti|independent-property-agent"))
    if agent_el:
        agent_name = agent_el.get_text(strip=True)
    specs = rumah123_parse_spec_table(soup)
    facilities = rumah123_parse_facilities(soup)
    main_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content") and "picture.rumah123.com" in (og.get("content") or ""):
        main_image = (og["content"] or "").strip()
        if "portal-api/image/og" in main_image:
            m = re.search(r"src=([^&\s]+)", main_image)
            if m:
                main_image = unquote(m.group(1).strip())
                if not main_image.startswith("http"):
                    main_image = "https://" + main_image
    if not main_image:
        for img in soup.find_all("img", src=True):
            src = (img.get("src") or "").strip()
            if "picture.rumah123.com" in src and ".jpg" in src:
                main_image = src if src.startswith("http") else "https://" + src.lstrip("/")
                break
    return {
        "url": url,
        "main_image": main_image,
        "title": title,
        "price": price_text,
        "location": location,
        "updated_by": updated_by,
        "agent_name": agent_name,
        "description": desc,
        **{k.replace(" ", "_").replace("/", "_"): v for k, v in specs.items()},
        "facilities_rumah": " | ".join(facilities["Fasilitas Rumah"]) if isinstance(facilities["Fasilitas Rumah"], list) else "",
        "facilities_perumahan": " | ".join(facilities["Fasilitas Perumahan"]) if isinstance(facilities["Fasilitas Perumahan"], list) else "",
        "perabotan": " | ".join(facilities["Perabotan"]) if isinstance(facilities["Perabotan"], list) else "",
    }


def rumah123_parse_detail(session: requests.Session, url: str, delay: float = 1.0) -> dict | None:
    """Fetch detail page via requests and return parsed row (used when Playwright not available)."""
    time.sleep(delay)
    for attempt in range(3):
        try:
            r = session.get(url, timeout=45, headers=_rumah123_headers())
            for _ in range(2):
                if r.status_code != 429:
                    break
                time.sleep(20)
                r = session.get(url, timeout=45, headers=_rumah123_headers())
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            return _rumah123_soup_to_row(soup, url)
        except Exception as e:
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
            else:
                log.debug("rumah123 | error %s: %s", url[:60], e)
                return None
    return None


def _rumah123_browser(headless: bool = False):
    """Launch Chromium for Rumah123 (visible by default). Returns (page, browser, playwright)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        user_agent=_random_user_agent(),
        locale="id-ID",
        viewport={"width": 1920, "height": 1080},
    )
    context.set_extra_http_headers({"Accept-Language": "id-ID,id;q=0.9,en;q=0.8"})
    page = context.new_page()
    return page, browser, pw


def _rumah123_cloudflare_click_if_present(page, timeout_ms: int = 6000) -> bool:
    """If a Cloudflare challenge / Rumah123 security page is visible, try to click the checkbox. Returns True if clicked."""
    try:
        time.sleep(1.5)
        # Advanced Playwright-based handling:
        # 1) Look through all frames for the Cloudflare / Rumah123 challenge.
        for frame in page.frames:
            url = (frame.url or "").lower()
            if "challenges.cloudflare.com" not in url and "captcha-delivery" not in url and "cloudflare" not in url:
                # Still consider the frame if it contains the Indonesian verification text.
                try:
                    if not frame.is_detached():
                        if not frame.locator("body").is_visible(timeout=500):
                            continue
                except Exception:
                    continue
            try:
                # Check for the Indonesian label text inside the frame.
                label = frame.get_by_text("Buktikan bahwa Anda adalah manusia", exact=False).first
                if label.is_visible(timeout=800):
                    # Try Playwright's label/checkbox helpers first.
                    try:
                        checkbox = frame.get_byLabel("Buktikan bahwa Anda adalah manusia", exact=False).first  # type: ignore[attr-defined]
                        if checkbox.is_visible(timeout=800):
                            checkbox.click()
                            log.info("rumah123 | clicked Rumah123 verification checkbox via label in frame %s", url or "<no-url>")
                            time.sleep(3)
                            return True
                    except Exception:
                        pass
                    # Fallback: try common checkbox-like selectors near the label.
                    for cb_sel in (
                        "input[type='checkbox']",
                        "[role='checkbox']",
                        ".mark",
                        ".cf-turnstile",
                        "div[aria-checked]",
                        "label:has-text('Buktikan bahwa Anda adalah manusia')",
                    ):
                        try:
                            el = frame.locator(cb_sel).first
                            if el.is_visible(timeout=800):
                                el.click()
                                log.info("rumah123 | clicked Rumah123 verification checkbox via selector '%s' in frame %s", cb_sel, url or "<no-url>")
                                time.sleep(3)
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        # 2) Fallback: click by visible text on main page (if no iframe/frame match)
        for text in ("Buktikan bahwa Anda adalah manusia", "Verify you are human", "I am human", "I'm not a robot"):
            try:
                btn = page.get_by_text(text, exact=False).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    log.info("rumah123 | clicked Cloudflare challenge (text)")
                    time.sleep(4)
                    return True
            except Exception:
                continue
    except Exception as e:
        log.debug("rumah123 | cloudflare auto-click failed: %s", e)
    return False


def _rumah123_fetch_listing_playwright(page, page_num: int, listing_sleep: float) -> tuple[list[str], bool]:
    """Fetch one listing page via Playwright. Returns (links, ok). Handles Cloudflare/security verification and waits until it is passed."""
    url = RUMAH123_LISTING if page_num <= 1 else f"{RUMAH123_LISTING}?page={page_num}"
    for attempt in range(3):
        try:
            page.set_extra_http_headers(_rumah123_headers())
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Try to auto-click Cloudflare / security verification if present,
            # and wait until either the challenge is gone or we hit a timeout.
            start = time.time()
            while True:
                _rumah123_cloudflare_click_if_present(page)
                html_lower = page.content().lower()
                if "melakukan verifikasi keamanan" not in html_lower:
                    break
                if time.time() - start > 120:
                    log.warning("rumah123 | security verification still present after 120s on listing page %s", page_num)
                    break
                log.info("rumah123 | waiting for security verification to be solved on listing page %s...", page_num)
                time.sleep(5)

            if page_num == 1:
                log.info("rumah123 | waiting for property links after any security verification...")
            page.wait_for_selector("a[href*='/properti/']", timeout=120_000)
            time.sleep(2)
            html = page.content()
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                log.warning("rumah123 | page %s failed after retries: %s", page_num, e)
                return ([], False)
    else:
        return ([], False)
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href.startswith("/properti/") or "/agent-" in href or "/independent-property-agent/" in href:
            continue
        if re.search(r"-(?:hor|vlr)\d+/?$", href):
            path = href.rstrip("/") + "/" if not href.endswith("/") else href
            full = urljoin(RUMAH123_BASE, path)
            if full not in links:
                links.append(full)
    return (links, True)


def _rumah123_fetch_detail_playwright(page, url: str, delay: float) -> dict | None:
    """Fetch one detail page via Playwright and return parsed row. Handles Cloudflare/security verification and waits until it is passed."""
    time.sleep(delay)
    for attempt in range(3):
        try:
            page.set_extra_http_headers(_rumah123_headers())
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Wait for security verification (if any) to be automatically solved
            start = time.time()
            while True:
                _rumah123_cloudflare_click_if_present(page)
                html_lower = page.content().lower()
                if "melakukan verifikasi keamanan" not in html_lower:
                    break
                if time.time() - start > 120:
                    log.warning("rumah123 | security verification still present after 120s on detail page %s", url)
                    break
                log.info("rumah123 | waiting for security verification to be solved on detail page %s...", url)
                time.sleep(5)
            try:
                page.wait_for_selector("h1", timeout=60_000)
            except Exception:
                pass
            time.sleep(1)
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            return _rumah123_soup_to_row(soup, url)
        except Exception as e:
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
            else:
                log.debug("rumah123 | error %s: %s", url[:60], e)
                return None
    return None


# ---------------------------------------------------------------------------
# Villa-Bali
# ---------------------------------------------------------------------------

VILLA_BALI_BASE = "https://www.villa-bali.com"
VILLA_BALI_SEARCH = f"{VILLA_BALI_BASE}/en/search"
USD_TO_IDR = 16_000


def _human_delay(base: float, jitter: float = 2.0):
    time.sleep(base + random.uniform(0, jitter))


def _usd_to_idr(usd: float) -> str:
    return f"Rp {int(round(usd * USD_TO_IDR)):,}".replace(",", ".")


def villa_bali_parse_price(price_text: str) -> tuple[str, str]:
    m = re.search(r"USD\s*[\d,]+(?:\.\d+)?", (price_text or ""), re.I)
    if not m:
        return "", "day"
    num_str = re.sub(r"[^\d.]", "", m.group(0))
    try:
        return _usd_to_idr(float(num_str)), "day"
    except ValueError:
        return "", "day"


def _villa_bali_headers() -> dict:
    return {
        "User-Agent": _random_user_agent(),
        "Accept-Language": "en-US,en;q=0.9",
    }


def villa_bali_get_browser(headless: bool = False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Install playwright: pip install playwright && playwright install chromium")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    context = browser.new_context(
        user_agent=_random_user_agent(),
        locale="en-US",
        viewport={"width": 1920, "height": 1080},
    )
    context.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
    page = context.new_page()
    return page, browser, pw


def villa_bali_fetch_listing_links(page) -> list[str]:
    _human_delay(2, 2)
    page.set_extra_http_headers(_villa_bali_headers())
    page.goto(VILLA_BALI_SEARCH, wait_until="load", timeout=45000)
    _human_delay(5, 4)
    for _ in range(5):
        page.evaluate("window.scrollBy(0, 800)")
        _human_delay(1.2, 1.5)
    page.evaluate("window.scrollTo(0, 0)")
    _human_delay(1.5, 1)
    soup = BeautifulSoup(page.content(), "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if "/en/villa/" in href:
            full = urljoin(VILLA_BALI_BASE, href)
            if full not in links:
                links.append(full)
    return links


def villa_bali_is_blocked(html: str) -> bool:
    lower = html.lower()
    return "you have been blocked" in lower or "captcha" in lower or "access denied" in lower


def villa_bali_parse_detail(page, url: str, delay: float = 1.0) -> dict | None:
    _human_delay(delay, 4)
    for attempt in range(2):
        try:
            page.set_extra_http_headers(_villa_bali_headers())
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            if attempt == 0:
                _human_delay(15, 10)
            continue
        _human_delay(2, 2)
        html = page.content()
        if villa_bali_is_blocked(html):
            if attempt == 0:
                _human_delay(20, 15)
            continue
        break
    else:
        return None
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True).replace(",", "") if h1 else ""
    price_idr = ""
    duration = "day"
    for el in soup.find_all(string=re.compile(r"USD\s*\d|per night|per week", re.I)):
        t = el.strip() if isinstance(el, str) else el
        if "USD" in t:
            price_idr, duration = villa_bali_parse_price(t)
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
        m = re.search(r"(\d+)\s*bedroom", t, re.I)
        if m:
            bedrooms = m.group(1)
        m = re.search(r"(\d+)\s*bathroom", t, re.I)
        if m:
            bathrooms = m.group(1)
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
        parts = [n.get_text(strip=True).replace(",", "") for n in fac_el.find_all(["li", "span", "div"]) if 2 < len(n.get_text(strip=True)) < 60]
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


# ---------------------------------------------------------------------------
# Resume & duplicate detection (load existing URLs from CSV/Excel)
# ---------------------------------------------------------------------------

def _load_existing_urls(path: Path) -> set[str]:
    """
    Load all existing listing URLs from the output file (CSV or Excel) for resume and duplicate detection.
    Returns a set of URL strings (strip, non-empty). Returns empty set if file missing or unreadable.
    """
    if not path.exists():
        return set()
    urls = set()
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames and "url" in reader.fieldnames:
                    for row in reader:
                        u = (row.get("url") or "").strip()
                        if u:
                            urls.add(u)
        elif suffix in (".xlsx", ".xls"):
            try:
                import openpyxl
            except ImportError:
                log.debug("openpyxl not installed; cannot read Excel for existing URLs")
                return set()
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheet = wb.active
            if sheet is None:
                wb.close()
                return set()
            header = None
            url_col = None
            for row in sheet.iter_rows(min_row=1, max_row=1, values_only=True):
                header = row
                break
            if header:
                for i, h in enumerate(header):
                    if (h or "").strip().lower() == "url":
                        url_col = i
                        break
            if url_col is not None:
                for row in sheet.iter_rows(min_row=2, values_only=True):
                    if len(row) > url_col:
                        u = (row[url_col] or "").strip()
                        if isinstance(u, str) and u:
                            urls.add(u)
            wb.close()
    except Exception as e:
        log.warning("Could not load existing URLs from %s: %s", path, e)
    return urls


def _write_row_to_csv(
    csv_handle: tuple | None,
    row: dict,
    existing_urls: set[str] | None = None,
) -> bool:
    """
    If csv_handle is (writer, file), write one row and flush.
    If existing_urls is set and row["url"] is already in it, skip write and return False.
    Otherwise write, add row["url"] to existing_urls, and return True.
    """
    if csv_handle is None:
        return True
    url = (row.get("url") or "").strip()
    if existing_urls is not None and url in existing_urls:
        return False
    writer, f = csv_handle
    writer.writerow(row)
    f.flush()
    if existing_urls is not None and url:
        existing_urls.add(url)
    return True


def run_rumah123(
    args,
    csv_handle: tuple | None = None,
    existing_urls: set[str] | None = None,
) -> list[dict]:
    log.info("rumah123 | fetching listing pages...")
    seen = existing_urls if existing_urls is not None else set()
    listing_sleep = 2.5 if getattr(args, "fast", False) else 3.0
    base_delay = getattr(args, "delay", 3.0)
    if getattr(args, "fast", False):
        base_delay = max(1.0, base_delay * 0.55)

    # Prefer Playwright so a visible Chromium window opens (same as other sources)
    browser_handle = _rumah123_browser(headless=getattr(args, "headless", False))
    if browser_handle is not None:
        page, browser, pw = browser_handle
        try:
            log.info("rumah123 | launching browser (visible)")
            all_links = []
            for p in range(1, args.pages + 1):
                log.info("rumah123 | requesting listing page %s", p)
                if p > 1:
                    time.sleep(listing_sleep)
                links, ok = _rumah123_fetch_listing_playwright(page, p, listing_sleep)
                if not ok:
                    time.sleep(8)
                    continue
                if not links:
                    break
                before = len(all_links)
                for link in links:
                    if link not in all_links:
                        all_links.append(link)
                if len(all_links) == before:
                    break
            log.info("rumah123 | collected %s unique property URLs", len(all_links))
            if not all_links:
                log.warning("rumah123 | no listing links found")
                return []
            if args.list_only:
                return all_links
            links_to_fetch = [u for u in all_links if u not in seen]
            skipped = len(all_links) - len(links_to_fetch)
            if skipped:
                log.info("rumah123 | skipping %s URLs already in output (resume/duplicate detection)", skipped)
            if not links_to_fetch:
                log.info("rumah123 | no new URLs to fetch")
                return []
            delay = base_delay
            log.info("rumah123 | fetching %s detail pages (delay ~%ss)", len(links_to_fetch), round(delay, 1))
            rows = []
            total = len(links_to_fetch)
            for done, url in enumerate(links_to_fetch, 1):
                if done % 50 == 0 or done == total:
                    log.info("rumah123 | detail progress %s/%s", done, total)
                row = _rumah123_fetch_detail_playwright(page, url, delay)
                if row and _row_has_title_and_price(row, "rumah123"):
                    flat = flatten_row(row, source="rumah123")
                    if _write_row_to_csv(csv_handle, flat, seen):
                        rows.append(flat)
            return rows
        finally:
            browser.close()
            pw.stop()

    # Fallback: requests (no browser, e.g. Playwright not installed)
    session = _rumah123_session()
    all_links = []
    pages_fetched = 0
    for p in range(1, args.pages + 1):
        log.info("rumah123 | requesting listing page %s", p)
        if p > 1:
            time.sleep(listing_sleep)
        try:
            links, ok = rumah123_fetch_listing_links(session, page=p)
        except Exception as e:
            log.warning("rumah123 | page %s failed: %s", p, e)
            continue
        if not ok:
            time.sleep(8)
            continue
        if not links:
            break
        before = len(all_links)
        for link in links:
            if link not in all_links:
                all_links.append(link)
        pages_fetched += 1
        if len(all_links) == before:
            break
    log.info("rumah123 | collected %s unique property URLs from %s page(s)", len(all_links), pages_fetched)
    if not all_links:
        log.warning("rumah123 | no listing links found")
        return []
    if args.list_only:
        return all_links
    links_to_fetch = [u for u in all_links if u not in seen]
    skipped = len(all_links) - len(links_to_fetch)
    if skipped:
        log.info("rumah123 | skipping %s URLs already in output (resume/duplicate detection)", skipped)
    if not links_to_fetch:
        log.info("rumah123 | no new URLs to fetch")
        return []
    workers = max(1, min(args.workers, 32))
    delay = max(0.5, base_delay / workers) if workers > 1 else base_delay
    log.info("rumah123 | fetching %s detail pages with %s workers (delay ~%ss)", len(links_to_fetch), workers, round(delay, 1))

    def fetch_one(url: str):
        try:
            s = _rumah123_session()
            out = rumah123_parse_detail(s, url, delay=delay)
            if out and _row_has_title_and_price(out, "rumah123"):
                log.debug("rumah123 | ok %s", url)
            elif out:
                log.debug("rumah123 | skip (no title/price) %s", url)
            return out
        except Exception as e:
            log.warning("rumah123 | error %s: %s", url, e)
            return None

    rows = []
    total = len(links_to_fetch)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for done, row in enumerate(executor.map(fetch_one, links_to_fetch), 1):
            if done % 50 == 0 or done == total:
                log.info("rumah123 | detail progress %s/%s", done, total)
            if row and _row_has_title_and_price(row, "rumah123"):
                flat = flatten_row(row, source="rumah123")
                if _write_row_to_csv(csv_handle, flat, seen):
                    rows.append(flat)
    return rows


def run_villa_bali(
    args,
    csv_handle: tuple | None = None,
    existing_urls: set[str] | None = None,
) -> list[dict] | list[str] | None:
    log.info("villa-bali | launching browser (may hit Cloudflare)")
    seen = existing_urls if existing_urls is not None else set()
    page, browser, pw = villa_bali_get_browser(headless=args.headless)
    try:
        links = villa_bali_fetch_listing_links(page)
        log.info("villa-bali | collected %s villa URLs", len(links))
        if not links:
            log.warning("villa-bali | no listing links found")
            return []
        if args.list_only:
            return links  # caller writes .txt
        links_to_fetch = [u for u in links if u not in seen]
        skipped = len(links) - len(links_to_fetch)
        if skipped:
            log.info("villa-bali | skipping %s URLs already in output (resume/duplicate detection)", skipped)
        if not links_to_fetch:
            log.info("villa-bali | no new URLs to fetch")
            return []
        log.info("villa-bali | fetching %s detail pages (delay %ss)", len(links_to_fetch), args.delay)
        rows = []
        for i, url in enumerate(links_to_fetch, 1):
            log.debug("villa-bali | [%s/%s] %s", i, len(links_to_fetch), url)
            row = villa_bali_parse_detail(page, url, delay=args.delay)
            if row and _row_has_title_and_price(row, "villa-bali"):
                flat = flatten_row(row, source="villa-bali")
                if _write_row_to_csv(csv_handle, flat, seen):
                    rows.append(flat)
                    log.debug("villa-bali | ok [%s/%s] %s", i, len(links_to_fetch), url)
            else:
                log.debug("villa-bali | skip (no title/price) [%s/%s] %s", i, len(links_to_fetch), url)
        return rows
    finally:
        browser.close()
        pw.stop()


def main():
    ap = argparse.ArgumentParser(
        description="Unified villa scraper: all sources → one CSV (same columns, same data strategy)"
    )
    ap.add_argument(
        "--source", "-s",
        choices=["all", "rumah123", "villa-bali", "balilongterm", "balicoconut", "balihomeimmo"],
        default="all",
        help="Source(s) to scrape (default: all)",
    )
    ap.add_argument("--pages", type=int, default=999, help="Max listing pages per source (default 999 = no practical limit)")
    ap.add_argument("--delay", type=float, default=3.0, help="Delay between detail requests (seconds)")
    ap.add_argument("--output", "-o", default="villa_listings.csv", help="Output CSV path")
    ap.add_argument("--append", action="store_true", help="Append to existing CSV")
    ap.add_argument("--list-only", action="store_true", help="Only collect URLs to .txt (single source only)")
    ap.add_argument("--json", action="store_true", help="Also write JSON (Rumah123 only)")
    ap.add_argument("--workers", "-w", type=int, default=1, help="Workers per source for detail fetch (default 1)")
    ap.add_argument("--headless", action="store_true", help="Run browser headless (Playwright sources)")
    ap.add_argument("--fast", "-f", action="store_true", help="Shorter delays for all sources (faster, minimal block risk)")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level (default: INFO)")
    ap.add_argument("--log-file", metavar="PATH", default=None, help="Also write logs to this file (utf-8)")
    ap.add_argument("--resume", action="store_true", default=True, help="Load existing URLs from output file and skip duplicates (default: on)")
    ap.add_argument("--no-resume", action="store_false", dest="resume", help="Disable resume; do not load existing URLs (fetch and write all)")
    args = ap.parse_args()
    args.headless = False  # always use visible browser (non-headless) for all sources

    setup_logging(level=args.log_level, log_file=args.log_file)
    log.info("source=%s output=%s append=%s", args.source, args.output, args.append)
    log.info("Browser sources (rumah123, balilongterm, balicoconut, balihomeimmo) will run in visible window (non-headless)")

    # "all" = Rumah123, BLT, BCL, Bali Home Immo (yearly+monthly)
    sources_to_run = ["rumah123", "balilongterm", "balicoconut", "balihomeimmo"] if args.source == "all" else [args.source]

    if args.source == "all" and args.list_only:
        log.error("--list-only is not supported with --source all; pick a single source")
        return

    all_rows = []
    out_path = Path(args.output)
    file_exists = out_path.exists()
    write_header = not args.append or not file_exists

    # Resume & duplicate detection (default: on). Load existing URLs from CSV/Excel so we skip them.
    if getattr(args, "resume", True):
        existing_urls = _load_existing_urls(out_path)
        if existing_urls:
            log.info("Resume (default): loaded %s existing URLs from %s; will skip duplicates", len(existing_urls), out_path)
    else:
        existing_urls = set()
        log.info("Resume disabled (--no-resume); will not skip existing URLs")

    if args.source == "all":
        # Run sources sequentially to limit resource and bandwidth use
        workers_to_run = [
            s for s in sources_to_run
            if (s != "balilongterm" or run_balilongterm)
            and (s != "balicoconut" or run_balicoconut)
            and (s != "balihomeimmo" or run_balihomeimmo)
        ]
        if "balihomeimmo" in sources_to_run and run_balihomeimmo is None:
            log.warning("skipping balihomeimmo (scrape_balihomeimmo not importable)")
        for src in workers_to_run:
            log.info("--- %s ---", src)
            try:
                if src == "rumah123":
                    result = run_rumah123(args, existing_urls=existing_urls)
                elif src == "villa-bali":
                    result = run_villa_bali(args, existing_urls=existing_urls)
                elif src == "balilongterm":
                    result = run_balilongterm(args)
                elif src == "balicoconut":
                    result = run_balicoconut(args)
                elif src == "balihomeimmo":
                    result = run_balihomeimmo(args)
                else:
                    result = None
            except Exception as e:
                log.warning("%s failed: %s", src, e)
                continue
            if result is None:
                log.warning("%s returned no data", src)
                continue
            if isinstance(result, list) and result and isinstance(result[0], str):
                out_path = Path(args.output).with_suffix(".txt")
                out_path.write_text("\n".join(result), encoding="utf-8")
                log.info("wrote %s URLs to %s", len(result), out_path)
                return
            rows = result if isinstance(result, list) else []
            if rows:
                # Skip duplicates: only write rows whose url is not already in existing_urls
                new_rows = []
                for r in rows:
                    url = (r.get("url") or "").strip()
                    if url and url not in existing_urls:
                        new_rows.append(r)
                        existing_urls.add(url)
                if new_rows:
                    mode = "w" if write_header else "a"
                    with open(out_path, mode, newline="", encoding="utf-8") as f:
                        w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
                        if write_header:
                            w.writeheader()
                            write_header = False
                        for r in new_rows:
                            safe = {k: _csv_cell(str(r.get(k, ""))) for k in MAIN_CSV_COLUMNS}
                            w.writerow(safe)
                    log.info("→ %s: wrote %s new rows (skipped %s duplicates) to %s", src, len(new_rows), len(rows) - len(new_rows), out_path)
                else:
                    log.info("→ %s: all %s rows already in file (duplicates skipped)", src, len(rows))
                all_rows.extend(rows)
            else:
                log.warning("%s returned no data", src)
    else:
        # Single source: open CSV once, write header, then write each row as it is retrieved
        with open(out_path, "a" if args.append else "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                w.writeheader()
                f.flush()
            csv_handle = (w, f)
            for src in sources_to_run:
                log.info("--- %s ---", src)
                if src == "rumah123":
                    result = run_rumah123(args, csv_handle=csv_handle, existing_urls=existing_urls)
                elif src == "villa-bali":
                    result = run_villa_bali(args, csv_handle=csv_handle, existing_urls=existing_urls)
                elif src == "balilongterm":
                    if run_balilongterm is None:
                        log.warning("skipping balilongterm (scrape_balilongterm not importable)")
                        continue
                    result = run_balilongterm(args)
                elif src == "balicoconut":
                    if run_balicoconut is None:
                        log.warning("skipping balicoconut (scrape_balicoconut not importable)")
                        continue
                    result = run_balicoconut(args)
                elif src == "balihomeimmo":
                    if run_balihomeimmo is None:
                        log.warning("skipping balihomeimmo (scrape_balihomeimmo not importable)")
                        continue
                    result = run_balihomeimmo(args)
                else:
                    continue

                if result is None:
                    log.warning("%s returned no data", src)
                    continue
                if isinstance(result, list) and result and isinstance(result[0], str):
                    out_path = Path(args.output).with_suffix(".txt")
                    out_path.write_text("\n".join(result), encoding="utf-8")
                    log.info("wrote %s URLs to %s", len(result), out_path)
                    return
                rows = result if isinstance(result, list) else []
                if rows:
                    all_rows.extend(rows)
                    # BLT/BCL/BHI don't support csv_handle; write only new rows (skip duplicates)
                    for r in rows:
                        url = (r.get("url") or "").strip()
                        if url and url in existing_urls:
                            continue
                        safe = {k: _csv_cell(str(r.get(k, ""))) for k in MAIN_CSV_COLUMNS}
                        w.writerow(safe)
                        f.flush()
                        if url:
                            existing_urls.add(url)

    if args.source != "all":
        if not all_rows:
            log.warning("no data collected")
            return
        # Rows were already written incrementally to CSV; no bulk write needed
        mode = "appended" if args.append else "wrote"
        log.info("%s %s rows to %s", mode.capitalize(), len(all_rows), out_path)

        if args.json and args.source == "rumah123" and all_rows:
            # Write normalized portal-style JSON structure:
            # {"villas": [ { ...canonical villa fields... }, ... ]}
            villas_payload = [_row_to_portal_villa(row, source="rumah123") for row in all_rows]
            json_path = out_path.with_suffix(".json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump({"villas": villas_payload}, f, ensure_ascii=False, indent=2)
            log.info("wrote %s in portal JSON format (villas[])", json_path)
    else:
        log.info("all sources done | total rows in %s: %s", out_path, len(all_rows))


if __name__ == "__main__":
    main()
