from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, Browser

from villa_csv import get_logger

log = get_logger()

BALIREALTY_BASE = "https://www.balirealty.com"
BALIREALTY_LISTING_URL = "https://www.balirealty.com/bali-villas-for-sale/"

# Approximate USD to IDR for price_idr when site gives USD only
USD_TO_IDR = 16_000


def _abs(url: str) -> str:
    if not url or url.startswith("#"):
        return ""
    return urljoin(BALIREALTY_BASE, url.strip()).split("?")[0].rstrip("/") + "/"


def _price_from_currency_box(soup: BeautifulSoup) -> tuple[str, str]:
    """
    Extract price from .property-currency-box (data-base-currency, data-base-amount).
    Returns (price_idr_str, duration). duration is 'year' for Rent, '' for Sale.
    """
    box = soup.find("div", class_=re.compile(r"property-currency-box"))
    if not box:
        return "", ""
    currency = (box.get("data-base-currency") or "").strip().upper()
    raw = box.get("data-base-amount")
    if raw is None or raw == "":
        return "", ""
    try:
        amount = int(str(raw).replace(",", "").replace(".", ""))
    except ValueError:
        return "", ""
    if currency == "IDR":
        return f"Rp {amount:,}".replace(",", "."), ""
    if currency in ("USD", "AUD", "EUR", "SGD"):
        idr = amount * USD_TO_IDR
        return f"Rp {idr:,}".replace(",", "."), ""
    return "", ""


def _parse_property_container(card) -> dict | None:
    """Parse a .property-container card (large listing box)."""
    content = card.find("div", class_="property-content")
    if not content:
        return None
    h3 = content.find("h3")
    link = h3.find("a", href=True) if h3 else None
    if not link:
        return None
    url = _abs(link.get("href", ""))
    if "/properties/" not in url or "/properties?" in url:
        return None
    title = (link.get_text(strip=True) or "").replace("\u8211", "-").strip()
    small = content.find("small")
    location = ""
    if small:
        location = small.get_text(strip=True).replace("\u8211", "-").strip()
        if location.lower().startswith("flaticon") or len(location) > 200:
            location = ""
    price_idr, _ = _price_from_currency_box(card)
    # Duration from badge: Rent -> year, Sale -> leave empty
    duration = ""
    ribbon = card.find("div", class_="arrow-ribbon")
    if ribbon:
        badge = ribbon.get_text(strip=True).lower()
        if "rent" in badge:
            duration = "year"
    bedrooms = ""
    bathrooms = ""
    attrs = card.find("div", class_="property-attributes")
    if attrs:
        cols = attrs.find_all("div", class_=re.compile(r"col-xs-4"))
        for col in cols:
            h4 = col.find("h4")
            p = col.find("p")
            if not h4:
                continue
            txt = re.sub(r"[^\d]", "", (h4.get_text(strip=True) or ""))
            label = (p.get("title") or p.get_text(strip=True) or "").lower() if p else ""
            if "bedroom" in label or "bed" in str(col):
                bedrooms = txt or bedrooms
            elif "bathroom" in label or "bath" in str(col):
                bathrooms = txt or bathrooms
    desc_el = content.find("p")
    description = (desc_el.get_text(strip=True) or "") if desc_el else ""
    img = card.find("img", class_=re.compile(r"property-box-thumbnail|wp-post-image"))
    main_image = ""
    if img:
        main_image = img.get("data-src") or img.get("data-src-webp") or img.get("src") or ""
        main_image = _abs(main_image) if main_image else ""
    return {
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
        "facilities": "",
        "agent_name": "",
        "updated_by": "balirealty",
        "main_image": main_image,
        "url": url,
    }


def _parse_property_small(card) -> dict | None:
    """Parse a .property-small card (compact listing)."""
    head = card.find("h5", class_="property-small-head")
    link = head.find("a", href=True) if head else None
    if not link:
        link = card.find("a", class_="property-btn", href=True)
    if not link:
        return None
    url = _abs(link.get("href", ""))
    if "/properties/" not in url or "/properties?" in url:
        return None
    title = (head.get_text(strip=True) if head else link.get_text(strip=True) or "").replace("\u8211", "-").strip()
    if not title:
        return None
    price_idr, _ = _price_from_currency_box(card)
    loc_el = card.find("p", class_="property-small-location")
    location = ""
    if loc_el:
        a = loc_el.find("a")
        location = (a.get_text(strip=True) if a else loc_el.get_text(strip=True)) or ""
    img = card.find("img")
    main_image = ""
    if img:
        main_image = img.get("data-src") or img.get("data-src-webp") or img.get("src") or ""
        main_image = _abs(main_image) if main_image else ""
    return {
        "title": title,
        "price_idr": price_idr,
        "duration": "year",
        "location": location,
        "bedrooms": "",
        "bathrooms": "",
        "land_size_m2": "",
        "building_size_m2": "",
        "certificate": "",
        "description": "",
        "facilities": "",
        "agent_name": "",
        "updated_by": "balirealty",
        "main_image": main_image,
        "url": url,
    }


def parse_balirealty_listing_html(html: str) -> list[dict]:
    """
    Parse Bali Realty listing page HTML (homepage or /properties/) into unified rows.
    Collects from .property-container and .property-small; deduplicates by url.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen_urls: set[str] = set()
    rows: list[dict] = []
    for card in soup.find_all("div", class_="property-container"):
        row = _parse_property_container(card)
        if row and row["url"] and row["url"] not in seen_urls:
            seen_urls.add(row["url"])
            rows.append(row)
    for card in soup.find_all("div", class_="property-small"):
        row = _parse_property_small(card)
        if row and row["url"] and row["url"] not in seen_urls:
            seen_urls.add(row["url"])
            rows.append(row)
    return rows


def _open_balirealty_listing(headless: bool = True, wait_ms: int = 10000) -> tuple[Page, Browser, Any]:
    """
    Open the Bali Realty site in a Chromium browser.
    Waits so you can clear Cloudflare / cookie challenges in visible mode.
    """
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.new_page()
    log.info("balirealty | opening listing page in %s mode", "headless" if headless else "headed")
    page.goto(BALIREALTY_LISTING_URL)
    if not headless and wait_ms > 0:
        log.info("balirealty | waiting %sms for you to clear any challenge in the visible browser", wait_ms)
        page.wait_for_timeout(wait_ms)
    return page, browser, pw


def run_balirealty(
    args,
    csv_handle: tuple | None = None,
    existing_urls: set[str] | None = None,
) -> list[dict] | list[str] | None:
    """
    Open Bali Realty in Chromium, wait for challenge, parse listing cards from the page,
    and return unified rows (or write via csv_handle). Uses villa-bali style flatten_row.
    """
    from scrape_villas import (
        _normalize_listing_url,
        _row_has_title_and_price,
        _write_row_to_csv,
        flatten_row,
    )

    headless = getattr(args, "headless", True)
    wait_ms = 10000
    seen = existing_urls if existing_urls is not None else set()

    page = None
    browser = None
    pw = None
    rows: list[dict] = []
    try:
        page, browser, pw = _open_balirealty_listing(headless=headless, wait_ms=wait_ms)
        log.info("balirealty | page opened; parsing after challenge wait.")
        if not headless:
            page.wait_for_timeout(10000)
        html = page.content()
        log.info("balirealty | captured listing HTML of length %s characters", len(html))
        raw_rows = parse_balirealty_listing_html(html)
        log.info("balirealty | parsed %s listing cards", len(raw_rows))
        for row in raw_rows:
            url_norm = _normalize_listing_url(row.get("url") or "")
            if url_norm and url_norm in seen:
                continue
            if not _row_has_title_and_price(row, "villa-bali"):
                continue
            flat = flatten_row(row, source="villa-bali")
            if csv_handle:
                if _write_row_to_csv(csv_handle, flat, seen):
                    rows.append(flat)
            else:
                rows.append(flat)
                if url_norm:
                    seen.add(url_norm)
        return rows
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass

