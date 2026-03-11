#!/usr/bin/env python3
"""
Scraper for Rumah123 Bali villa rentals.
Collects listing URLs from https://www.rumah123.com/sewa/bali/villa/
then fetches each property detail page and extracts all specs and details.
Output: CSV (and optional JSON).
"""

import argparse
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.rumah123.com"
LISTING_URL = f"{BASE}/sewa/bali/villa/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SESSION_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"}


def get_session():
    s = requests.Session()
    s.headers.update(SESSION_HEADERS)
    return s


def _is_script_or_style(el) -> bool:
    if not getattr(el, "name", None):
        return True
    return el.name in ("script", "style", "noscript")


def _find_text_in_body(soup: BeautifulSoup, pattern):
    """Find first element matching pattern, ignoring script/style."""
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


def fetch_listing_links(session: requests.Session, page: int = 1) -> list[str]:
    """Get all property detail page paths from one listing page."""
    url = LISTING_URL if page <= 1 else f"{LISTING_URL}?page={page}"
    r = _fetch_with_retry(session, url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href.startswith("/properti/"):
            continue
        if "/agent-" in href or "/independent-property-agent/" in href:
            continue
        # Property detail: /properti/{location}/{slug-id}/ (id = hor... or vlr...)
        if re.search(r"-(?:hor|vlr)\d+/?$", href):
            path = href.rstrip("/") + "/" if not href.endswith("/") else href
            full = urljoin(BASE, path)
            if full not in links:
                links.append(full)
    return links


def parse_spec_table(soup: BeautifulSoup) -> dict:
    """Extract key-value specs from 'Spesifikasi' / 'Informasi Rumah Disewa' section."""
    out = {}
    # Common patterns: label in one element, value in next (e.g. dt/dd or div/div)
    for block in soup.find_all(["section", "div"], class_=re.compile(r"spec|info|detail", re.I)):
        texts = block.get_text(separator="|", strip=True)
        if "Luas Tanah" not in texts and "Kamar Tidur" not in texts:
            continue
        # Look for label-value pairs: often "Luas Tanah" followed by "600 m²"
        parts = [p.strip() for p in texts.replace("\n", "|").split("|") if p.strip()]
        for i, p in enumerate(parts):
            if p in (
                "Luas Tanah",
                "Luas Bangunan",
                "Kondisi / Tahun Renovasi",
                "Kamar Tidur",
                "Kamar Mandi",
                "Carport",
                "Sertifikat",
                "Daya Listrik",
            ):
                if i + 1 < len(parts):
                    val = parts[i + 1].strip()
                    if val and val != p and val not in (
                        "Luas Tanah", "Luas Bangunan", "Kamar Tidur", "Kamar Mandi",
                        "Carport", "Sertifikat", "Daya Listrik", "Kondisi / Tahun Renovasi",
                    ):
                        out[p] = val
    # Fallback: find by text then sibling
    labels = (
        "Luas Tanah",
        "Luas Bangunan",
        "Kondisi / Tahun Renovasi",
        "Kamar Tidur",
        "Kamar Mandi",
        "Carport",
        "Sertifikat",
        "Daya Listrik",
    )
    for label in labels:
        if label in out:
            continue
        el = soup.find(string=re.compile(re.escape(label), re.I))
        if el:
            parent = el.parent
            while parent and getattr(parent, "name", None) in ("script", "style"):
                parent = parent.parent
            if parent:
                n = parent.find_next_sibling()
                if n:
                    v = n.get_text(strip=True)
                    if v and v != label:
                        out[label] = v
                else:
                    for s in parent.find_next_siblings():
                        v = s.get_text(strip=True)
                        if v and v != label and len(v) < 100:
                            out[label] = v
                            break
    # Fallback: regex on body text for "Label\nValue" pattern
    if not out and soup.body:
        body_text = soup.body.get_text(separator="\n", strip=True)
        for label in labels:
            if label in out:
                continue
            m = re.search(re.escape(label) + r"\s*\n\s*([^\n]{1,50})", body_text)
            if m:
                out[label] = m.group(1).strip()
    return out


def parse_facilities(soup: BeautifulSoup) -> dict:
    """Extract facility groups: Fasilitas Rumah, Fasilitas Perumahan, Perabotan."""
    out = {"Fasilitas Rumah": [], "Fasilitas Perumahan": [], "Perabotan": []}
    section = _find_text_in_body(soup, re.compile(r"Fasilitas", re.I))
    if not section:
        return out
    root = section.parent
    while root and getattr(root, "name", None) not in ("section", "div"):
        root = getattr(root, "parent", None)
        if not root or getattr(root, "name", None) in ("script", "style", "noscript"):
            return out
    if not root or getattr(root, "name", None) in ("script", "style", "noscript"):
        return out
    # Find subsection headers and list items below them
    for h in root.find_all(["h3", "h4", "strong"], string=re.compile(r"Fasilitas Rumah|Fasilitas Perumahan|Perabotan", re.I)):
        key = h.get_text(strip=True)
        if key not in out:
            continue
        container = h.find_parent("div") or h
        next_ = container.find_next_sibling() or container.find_next()
        while next_ and getattr(next_, "name", None) in ("script", "style"):
            next_ = next_.find_next_sibling() or next_.find_next()
        if next_ and getattr(next_, "name", None) not in ("script", "style") and getattr(next_, "find_all", None):
            items = next_.find_all(["li", "span", "div"], recursive=True)
            texts = []
            for node in items[:50]:
                t = node.get_text(strip=True)
                if t and len(t) < 80 and t not in texts:
                    texts.append(t)
            if not texts:
                texts = re.split(r"[\n|]", next_.get_text(separator=" ", strip=True))
                texts = [x.strip() for x in texts if 2 <= len(x.strip()) <= 80][:30]
            out[key] = texts
    return out


def _fetch_with_retry(session: requests.Session, url: str, max_retries: int = 2, retry_wait: float = 12.0) -> requests.Response:
    """GET with retry on 429 Too Many Requests."""
    last_err = None
    for attempt in range(max_retries + 1):
        r = session.get(url, timeout=30)
        if r.status_code != 429:
            return r
        last_err = r
        if attempt < max_retries:
            time.sleep(retry_wait)
    last_err.raise_for_status()
    return last_err


def parse_detail_page(session: requests.Session, url: str, delay: float = 1.0) -> dict | None:
    """Fetch one property detail page and return structured data."""
    time.sleep(delay)
    r = _fetch_with_retry(session, url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Title – often h1 (strip commas for CSV)
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True).replace(",", "")

    # Price – look for "Rp" pattern (skip script/style)
    price_text = ""
    el = _find_text_in_body(soup, re.compile(r"Rp\s*[\d,\.]+", re.I))
    if el:
        price_text = el.strip()

    # Location – often near title or in breadcrumb (skip script)
    location = ""
    for el in soup.find_all(string=re.compile(r"(Canggu|Ubud|Badung|Denpasar|Gianyar|Kerobokan|Seminyak|Sanur|Jimbaran|Tabanan|Buleleng|Karangasem)", re.I)):
        if el.parent and getattr(el.parent, "name", None) in ("script", "style", "noscript"):
            continue
        t = el.strip()
        # Prefer "Area, Regency" format (e.g. "Canggu, Badung")
        if 5 <= len(t) <= 120 and any(x in t for x in ("Badung", "Gianyar", "Denpasar", "Tabanan", "Canggu", "Ubud", "Kerobokan", "Sanur", "Jimbaran")):
            if "," in t:
                location = t
                break
            elif not location:
                location = t
    if not location:
        # Fallback: first comma-separated line under title
        if h1:
            p = h1.find_next(["p", "div", "span"])
            if p:
                location = p.get_text(strip=True).split(",")[0].strip() or p.get_text(strip=True)[:80]

    # Description (skip script)
    desc = ""
    d = _find_text_in_body(soup, re.compile(r"Deskripsi", re.I))
    if d:
        parent = d.parent
        while parent and parent.name not in ("section", "div"):
            parent = parent.parent
        if parent:
            for s in parent.find_all(["p", "div"]):
                if getattr(s, "name", None) in ("script", "style"):
                    continue
                t = s.get_text(strip=True)
                if len(t) > 50 and "Deskripsi" not in t and "push(" not in t and "formats" not in t:
                    desc = t[:5000]  # cap length
                    break

    # Updated / agent (skip script)
    updated_by = ""
    agent_name = ""
    for el in soup.find_all(string=re.compile(r"Diperbarui.*oleh", re.I)):
        if el.parent and getattr(el.parent, "name", None) in ("script", "style", "noscript"):
            continue
        updated_by = el.strip()
        break
    agent_el = soup.find("a", href=re.compile(r"agen-properti|independent-property-agent"))
    if agent_el:
        agent_name = agent_el.get_text(strip=True)

    specs = parse_spec_table(soup)
    facilities = parse_facilities(soup)

    # Main image: og:image or first picture.rumah123.com URL (prefer direct image, not og proxy)
    main_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        content = og["content"].strip()
        if "picture.rumah123.com" in content:
            if "portal-api/image/og" in content:
                m = re.search(r"src=([^&\s]+)", content)
                if m:
                    main_image = unquote(m.group(1).strip())
                    if main_image.startswith("https:/") and not main_image.startswith("https://"):
                        main_image = "https://" + main_image[7:]
                    if not main_image.startswith("http"):
                        main_image = "https://" + main_image
            else:
                main_image = content
    if not main_image:
        for a in soup.find_all("img", src=True):
            src = (a.get("src") or "").strip()
            if "picture.rumah123.com" in src and ".jpg" in src:
                if "720x420" in src or "720x420-crop" in src:
                    main_image = src if src.startswith("http") else "https://" + src.lstrip("/")
                    break
        if not main_image:
            for a in soup.find_all(href=re.compile(r"picture\.rumah123\.com.*\.(jpg|jpeg|png|webp)")):
                main_image = (a.get("href") or "").strip()
                if main_image:
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
        "facilities_rumah": " | ".join(facilities["Fasilitas Rumah"]) if isinstance(facilities["Fasilitas Rumah"], list) else facilities["Fasilitas Rumah"],
        "facilities_perumahan": " | ".join(facilities["Fasilitas Perumahan"]) if isinstance(facilities["Fasilitas Perumahan"], list) else facilities["Fasilitas Perumahan"],
        "perabotan": " | ".join(facilities["Perabotan"]) if isinstance(facilities["Perabotan"], list) else facilities["Perabotan"],
    }


# Main columns (English names); url last, main_image before it
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

# Duration keywords on site (Indonesian) → English
DURATION_MAP = {
    "/hari": "day",
    "per hari": "day",
    "/minggu": "week",
    "per minggu": "week",
    "/bulan": "month",
    "per bulan": "month",
    "/tahun": "year",
    "per tahun": "year",
    "/year": "year",
    "/month": "month",
    "/day": "day",
}


def _parse_price_idr(price_text: str) -> tuple[str, str]:
    """Parse price string (e.g. 'Rp 1,5 Juta /hari') → (price_idr, duration). IDR uses dot as thousand separator."""
    price_text = (price_text or "").strip()
    duration = ""
    for key, eng in DURATION_MAP.items():
        if key in price_text.lower():
            duration = eng
            price_text = price_text.lower().split(key)[0].strip()
            break

    # Extract number: allow "1,5" (comma decimal), "500", "2,2"
    num_str = re.sub(r"[^\d,\.]", "", price_text).replace(",", ".")
    if not num_str:
        return "", duration

    try:
        amount = float(num_str)
    except ValueError:
        return "", duration

    # Scale by Juta (million), Miliar (billion), Ribu (thousand)
    lower = price_text.lower()
    if "miliar" in lower or "milliar" in lower:
        amount *= 1_000_000_000
    elif "juta" in lower:
        amount *= 1_000_000
    elif "ribu" in lower:
        amount *= 1_000
    else:
        # Assume millions if large number
        if amount < 10_000:
            amount *= 1_000_000
        # else treat as raw (e.g. 500000000)
        pass

    # Format IDR: Rp 1.500.000 (dot as thousand separator)
    amount_int = int(round(amount))
    formatted = f"Rp {amount_int:,}".replace(",", ".")
    return formatted, duration


def _csv_cell(s: str) -> str:
    """Normalize string for CSV: one line per cell, no broken columns."""
    if not isinstance(s, str):
        return ""
    # Remove script junk
    if "push(" in s or '"formats"' in s or '"locale"' in s:
        return ""
    # Replace newlines/carriage returns with space so one record = one line
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Collapse multiple spaces
    s = re.sub(r" +", " ", s).strip()
    if len(s) > 8000:
        s = s[:8000] + "..."
    return s


def flatten_row(row: dict, main_only: bool = True) -> dict:
    """One-level dict for CSV. main_only: keep only MAIN_CSV_COLUMNS. All cells normalized for one line per row."""
    raw = {}
    for k, v in row.items():
        if isinstance(v, (list, dict)):
            raw[k] = _csv_cell(json.dumps(v, ensure_ascii=False)) if v else ""
        else:
            raw[k] = _csv_cell(str(v or ""))

    if not main_only:
        return raw

    # Map full row keys to main column names and merge facility columns
    facilities_parts = [
        raw.get("facilities_rumah") or "",
        raw.get("facilities_perumahan") or "",
        raw.get("perabotan") or "",
    ]
    facilities_str = " | ".join(p for p in facilities_parts if p).strip() or ""

    price_raw = raw.get("price", "")
    price_idr, duration = _parse_price_idr(price_raw)

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
    return {k: _csv_cell(str(v)) for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser(description="Scrape Rumah123 Bali villa listings and details")
    ap.add_argument("--pages", type=int, default=1, help="Number of listing pages to crawl (default 1)")
    ap.add_argument("--delay", type=float, default=3.0, help="Delay between detail requests (seconds)")
    ap.add_argument("--output", "-o", default="villa_listings.csv", help="Output CSV path")
    ap.add_argument("--json", action="store_true", help="Also write JSON file")
    ap.add_argument("--list-only", action="store_true", help="Only collect listing URLs, do not fetch details")
    ap.add_argument("--workers", "-w", type=int, default=2, help="Number of parallel workers for detail pages (default 2)")
    args = ap.parse_args()

    session = get_session()
    all_links = []
    listing_delay = 2.0  # delay between listing pages to avoid 429
    for p in range(1, args.pages + 1):
        if p > 1:
            time.sleep(listing_delay)
        links = fetch_listing_links(session, page=p)
        for link in links:
            if link not in all_links:
                all_links.append(link)

    print(f"Collected {len(all_links)} unique property URLs from {args.pages} page(s)")

    if args.list_only:
        out_path = Path(args.output).with_suffix(".txt")
        out_path.write_text("\n".join(all_links), encoding="utf-8")
        print(f"Wrote {out_path}")
        return

    workers = max(1, min(args.workers, 32))
    # Minimum delay per request to avoid 429; delay is shared across workers
    per_request_delay = max(0.5, args.delay / workers) if workers > 1 else args.delay
    print(f"Fetching {len(all_links)} detail pages with {workers} workers (delay {per_request_delay:.1f}s per request) ...")

    def fetch_one(url: str):
        try:
            s = get_session()
            return parse_detail_page(s, url, delay=per_request_delay)
        except Exception as e:
            print(f"  Error {url}: {e}")
            return None

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(fetch_one, all_links))
    for row in results:
        if row:
            rows.append(flatten_row(row, main_only=True))

    if not rows:
        print("No data collected.")
        return

    out_path = Path(args.output)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MAIN_CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out_path}")

    if args.json:
        json_path = out_path.with_suffix(".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
