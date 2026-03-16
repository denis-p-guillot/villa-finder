"""
AI-driven discovery: search the web freely for Bali villas (rent/sale), then extract
listing and detail URLs with AI, and scrape each detail page into a unified CSV.

Flow:
  1. OpenAI suggests search queries for Bali villas rent/sale.
  2. Web search (DDGS) runs those queries → raw search results (title, url, snippet).
  3. OpenAI classifies each result as listing_page (many properties) or detail_page (single villa).
  4. For each listing URL: Playwright fetches page → OpenAI extracts detail_urls from HTML.
  5. All detail URLs (from search + from listing pages) are deduped.
  6. For each detail URL: Playwright fetches → OpenAI extracts structured row → CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Set

from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright, Page, Browser

from villa_csv import MAIN_CSV_COLUMNS, normalize_row

EXTRA_COLUMNS = ["agent_phone", "agent_email"]

# Approximate exchange rates to IDR (for normalizing price_idr)
USD_TO_IDR = 16_000
EUR_TO_IDR = 17_500
AUD_TO_IDR = 10_500
SGD_TO_IDR = 12_000
# Villa listings below this (IDR) are implausible; re-interpret or drop
MIN_IDR_PLAUSIBLE = 1_000_000


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


# --- Prompts ---
QUERIES_SYSTEM = """You suggest web search queries to find Bali villa listings (for rent or sale).

Given the goal "find Bali villas for rent or sale", output a JSON object with one key:
- queries: array of 6–10 search query strings in English, e.g. "Bali villas for rent", "villa for sale Seminyak", "long term villa rental Canggu Bali".
Diverse queries help discover more listing sites and property pages. No explanation, only valid JSON."""

CLASSIFY_SYSTEM = """You classify web search results about Bali villas.

You receive a list of search results: each has title, url, and body (snippet).

Your task: decide which URLs are relevant to Bali villa listings (rent or sale) and classify them:
- listing_page: URL likely shows a list/catalog of multiple villas (category, search results, "villas for rent", etc.)
- detail_page: URL likely shows a single villa/property detail page.

Output JSON with exactly two keys:
- listing_urls: array of URLs (strings) that are listing/category pages.
- detail_urls: array of URLs (strings) that are single-property detail pages.

Only include URLs that look relevant to Bali villas (rent or sale). Exclude news, blogs, social media, unless clearly a listing. Skip URLs that are clearly filters, login, or homepages with no listing focus."""

LISTING_EXTRACT_SYSTEM = """You find villa/property detail page links on a real estate listing page.

You receive: the page URL and its full HTML.

Your task: extract ALL links that point to INDIVIDUAL villa/property detail pages (one property per page). Do not include links to other listing pages, filters, or pagination.

Output JSON with one key:
- detail_urls: array of absolute URLs (strings) of the villa detail pages."""

DETAIL_EXTRACT_SYSTEM = """You are a data extractor for villa listings in Bali and Indonesia.

You receive the URL and full HTML of a property detail page.

Output ONE JSON object with these exact keys (all strings; use "" if unknown):
- title, price_idr, duration, location, bedrooms, bathrooms, land_size_m2, building_size_m2, certificate, description, facilities, agent_name, updated_by, main_image, image_urls, url, agent_phone, agent_email

Rules:
- If the page language is NOT English (e.g. French or Indonesian), TRANSLATE all textual fields you output (title, description, location, facilities labels if present, agent_name, duration) into clear, natural English.
- For facilities: comma-separated, chosen ONLY from: Air conditioning, WiFi, TV, Kitchen, Fully equipped kitchen, Washing machine, Dishwasher, Office / workspace, Pool, Private pool, Garden, BBQ, Beach access, Balcony, Terrace, Lounge, Gym, Spa, Pet friendly, Parking, Daily cleaning, Staff, Maid service, Security, Sea view, Rice field view.
- price_idr: human-readable IDR or USD string (do not translate currency codes). duration: e.g. "Rental monthly". url: the page URL you received. The JSON must be a single object."""


CONTACT_SYSTEM = """You are extracting DEFAULT public contact info for a real estate / villa agency website.

You receive:
- The base URL of the site (e.g. https://www.example.com)
- The full HTML of that site's homepage or contact page.

Your task:
- Find the main public phone number and email address used for enquiries on this site (from header, footer, contact page, etc.).

Output a JSON object with exactly these keys:
- agent_phone: main public phone number in international format if possible (or "" if none)
- agent_email: main public email address (or "" if none)

If there are multiple, pick the one that looks like the generic sales/enquiries contact, not a random staff email."""



def get_client() -> OpenAI:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in environment or .env")
    return OpenAI(api_key=api_key)


def ai_suggest_queries(client: OpenAI) -> List[str]:
    log("OpenAI: suggesting search queries for Bali villas (rent/sale)...")
    c = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": QUERIES_SYSTEM},
            {"role": "user", "content": "Suggest search queries to find Bali villas for rent or sale."},
        ],
    )
    content = c.choices[0].message.content or "{}"
    data = json.loads(content)
    queries = data.get("queries") or []
    if not isinstance(queries, list):
        queries = []
    return [str(q).strip() for q in queries if str(q).strip()][:12]


def web_search(query: str, max_results: int = 15) -> List[Dict[str, str]]:
    try:
        from ddgs import DDGS
        results = list(DDGS().text(query, max_results=max_results))
        out = []
        for r in results:
            href = r.get("href") or r.get("url") or ""
            if not href:
                continue
            out.append({
                "title": str(r.get("title") or ""),
                "url": href,
                "body": str(r.get("body") or ""),
            })
        return out
    except Exception as e:
        log(f"Web search failed for {query!r}: {e}")
        return []


def ai_classify_search_results(client: OpenAI, results: List[Dict[str, str]]) -> tuple[List[str], List[str]]:
    if not results:
        return [], []
    log(f"OpenAI: classifying {len(results)} search results (listing vs detail)...")
    c = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": json.dumps({"search_results": results}, ensure_ascii=False)},
        ],
    )
    content = c.choices[0].message.content or "{}"
    data = json.loads(content)
    listing = data.get("listing_urls") or []
    detail = data.get("detail_urls") or []
    if not isinstance(listing, list):
        listing = []
    if not isinstance(detail, list):
        detail = []
    return [u for u in listing if isinstance(u, str) and u.strip()], [u for u in detail if isinstance(u, str) and u.strip()]


PRECINCT_SYSTEM = """You are a Bali geography expert. Given a raw location string from a villa listing (e.g. "Tibubeneng, Canggu", "near Echo Beach", "Munggu"), respond with a single proper Bali precinct or area name.

Use standard Bali area names: Canggu, Berawa, Pererenan, Echo Beach, Seminyak, Kerobokan, Umalas, Sanur, Ubud, Uluwatu, Bukit, Balangan, Bingin, Padang Padang, Jimbaran, Nusa Dua, Nusa Lembongan, Tabanan, Seseh, Cemagi, Tanah Lot, Denpasar, Kuta, Legian, Lovina, Amed, Bukit Peninsula, etc. Prefer the most specific precinct that fits (e.g. Canggu over Badung). One or two words, proper capitalization. Output only the precinct name, no quotes or explanation."""


def ai_suggest_precinct(client: OpenAI, raw_location: str) -> str:
    if not (raw_location or "").strip():
        return ""
    log(f"OpenAI: suggesting precinct for location {raw_location[:60]!r}...")
    try:
        c = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": PRECINCT_SYSTEM},
                {"role": "user", "content": raw_location.strip()},
            ],
        )
        out = (c.choices[0].message.content or "").strip()
        return out if out else ""
    except Exception as e:
        log(f"  Precinct suggestion failed: {e}")
        parts = raw_location.split(",")[0].split()
        return " ".join(parts[:3]).title() if parts else ""


def ai_extract_detail_urls_from_listing(client: OpenAI, listing_url: str, html: str) -> List[str]:
    log(f"OpenAI: extracting detail URLs from listing page (html_len={len(html)})...")
    # Truncate very large HTML to stay within context
    if len(html) > 120_000:
        html = html[:120_000] + "\n... [truncated]"
    c = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": LISTING_EXTRACT_SYSTEM},
            {"role": "user", "content": json.dumps({"url": listing_url, "html": html}, ensure_ascii=False)},
        ],
    )
    content = c.choices[0].message.content or "{}"
    data = json.loads(content)
    urls = data.get("detail_urls") or []
    return [u for u in urls if isinstance(u, str) and u.strip()]


def ai_extract_site_contact(client: OpenAI, base_url: str, html: str) -> dict:
    """
    Ask OpenAI for default site-wide contact info (phone/email) from homepage/contact HTML.
    """
    if not html:
        return {"agent_phone": "", "agent_email": ""}
    # Truncate large pages
    if len(html) > 80_000:
        html = html[:80_000] + "\n... [truncated]"
    log(f"OpenAI: extracting site-wide contact for {base_url} (html_len={len(html)})")
    try:
        c = client.chat.completions.create(
            model="gpt-4.1-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CONTACT_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps({"base_url": base_url, "html": html}, ensure_ascii=False),
                },
            ],
        )
        content = c.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception as e:
        log(f"  Site contact extraction failed for {base_url}: {e}")
        data = {}
    phone = str(data.get("agent_phone") or "").strip()
    email = str(data.get("agent_email") or "").strip()
    return {"agent_phone": phone, "agent_email": email}


def normalize_price_to_idr(raw: str) -> str:
    """
    Normalize price string to IDR (Indonesian Rupiah) in price_idr column.
    - If already IDR (Rp, IDR, rupiah): clean and format as "Rp X.XXX.XXX".
    - If USD/EUR/AUD/SGD: convert to IDR and format as "Rp X.XXX.XXX".
    - Prices below MIN_IDR_PLAUSIBLE (1M IDR) are re-interpreted or dropped for coherence.
    """
    if not raw or not raw.strip():
        return ""
    s = raw.strip()
    num_match = re.search(r"[\d.,]+", s.replace(" ", ""))
    if not num_match:
        return s
    num_str = num_match.group(0).replace(",", "")
    parts = num_str.split(".")
    if len(parts) > 2:
        num_str = "".join(parts)
    else:
        num_str = num_str.replace(".", "")
    try:
        amount = float(num_str)
    except ValueError:
        return s
    s_lower = s.lower()
    currency_detected = True
    if "usd" in s_lower or "$" in s or "dollar" in s_lower:
        amount_idr = int(amount * USD_TO_IDR)
    elif "eur" in s_lower or "€" in s or "euro" in s_lower:
        amount_idr = int(amount * EUR_TO_IDR)
    elif "aud" in s_lower or "a$" in s_lower:
        amount_idr = int(amount * AUD_TO_IDR)
    elif "sgd" in s_lower or "s$" in s_lower:
        amount_idr = int(amount * SGD_TO_IDR)
    elif "rp" in s_lower or "idr" in s_lower or "rupiah" in s_lower:
        amount_idr = int(amount)
    else:
        currency_detected = False
        amount_idr = int(amount)

    # Coherence: villa prices under 1M IDR are implausible; re-interpret or drop
    if amount_idr < MIN_IDR_PLAUSIBLE:
        if not currency_detected:
            # Often raw number is USD (e.g. "500" = 500 USD)
            amount_idr = int(amount * USD_TO_IDR)
        if amount_idr < MIN_IDR_PLAUSIBLE and amount < 1000 and amount > 0:
            # Small number could be "in millions" (e.g. "5" = 5 million IDR)
            amount_idr = int(amount * 1_000_000)
        if amount_idr < MIN_IDR_PLAUSIBLE:
            return ""

    n = int(amount_idr)
    parts_fmt = []
    while n >= 1000:
        parts_fmt.append(f"{n % 1000:03d}")
        n //= 1000
    parts_fmt.append(str(n))
    formatted = ".".join(reversed(parts_fmt))
    return f"Rp {formatted}"


def ai_extract_row(client: OpenAI, url: str, html: str) -> Dict[str, str]:
    if len(html) > 100_000:
        html = html[:100_000] + "\n... [truncated]"
    log(f"OpenAI DETAIL call | url={url[:80]}...")
    c = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": DETAIL_EXTRACT_SYSTEM},
            {"role": "user", "content": json.dumps({"url": url, "html": html}, ensure_ascii=False)},
        ],
    )
    content = c.choices[0].message.content or "{}"
    data = json.loads(content)
    for k in MAIN_CSV_COLUMNS + EXTRA_COLUMNS:
        if k not in data:
            data[k] = ""

    # Force numeric-ish fields to clean integer-like strings or empty
    def to_int_str(v: object) -> str:
        if v is None:
            return ""
        s = str(v)
        # Keep digits, dot, comma, space
        cleaned = "".join(ch for ch in s if ch.isdigit() or ch in "., ")
        cleaned = cleaned.replace(",", " ").strip()
        parts = cleaned.split()
        if not parts:
            return ""
        # Try first numeric token
        token = parts[0].replace(",", "").strip()
        try:
            n = float(token)
        except ValueError:
            return ""
        return str(int(n))

    for key in ("bedrooms", "bathrooms", "land_size_m2", "building_size_m2"):
        data[key] = to_int_str(data.get(key, ""))

    # Normalize price_idr to IDR (Rp) with consistent formatting
    data["price_idr"] = normalize_price_to_idr(str(data.get("price_idr") or "").strip())

    # Normalize location into a canonical Bali precinct name where possible
    raw_loc = str(data.get("location") or "").lower()
    canonical_locations = {
        "canggu": ["canggu"],
        "berawa": ["berawa"],
        "echo beach": ["echo beach"],
        "pererenan": ["pererenan"],
        "seminyak": ["seminyak", "kay u aya", "petitenget"],
        "kerobokan": ["kerobokan", "umalas"],
        "umalas": ["umalas"],
        "sanur": ["sanur"],
        "ubud": ["ubud"],
        "uluwatu": ["uluwatu"],
        "bukit": ["bukit"],
        "balangan": ["balangan"],
        "bingin": ["bingin"],
        "padang padang": ["padang padang"],
        "jimbaran": ["jimbaran"],
        "nusa dua": ["nusa dua"],
        "nusa lembongan": ["nusa lembongan"],
        "tabanan": ["tabanan", "kedungu", "balian"],
        "seseh": ["seseh"],
        "cemagi": ["cemagi"],
        "tanah lot": ["tanah lot"],
        "denpasar": ["denpasar"],
        "kuta": ["kuta", "legian"],
        "lovina": ["lovina"],
        "amed": ["amed"],
        "bukit peninsula": ["bukit peninsula"],
    }

    def normalize_location(raw: str) -> str:
        if not raw:
            return ""
        for canon, keys in canonical_locations.items():
            for k in keys:
                if k in raw:
                    return " ".join(part.capitalize() for part in canon.split())
        # No match: ask OpenAI to suggest the proper Bali precinct
        return ai_suggest_precinct(client, raw)

    data["location"] = normalize_location(raw_loc)

    # Normalize image_urls: separator " | " (survives CSV comma-stripping in villa_csv)
    imgs = data.get("image_urls")
    if isinstance(imgs, list):
        cleaned_list = [str(u).strip() for u in imgs if str(u).strip()]
        data["image_urls"] = " | ".join(cleaned_list)
    elif isinstance(imgs, str):
        s = imgs.strip()
        if "," in s:
            parts = [p.strip() for p in s.split(",") if p.strip()]
            data["image_urls"] = " | ".join(parts)
        elif " | " in s:
            parts = [p.strip() for p in s.split("|") if p.strip()]
            data["image_urls"] = " | ".join(parts)
        else:
            data["image_urls"] = s

    row = normalize_row(data)
    for k in EXTRA_COLUMNS:
        row[k] = "" if data.get(k) is None else str(data.get(k, ""))
    return row


def open_browser(headless: bool, wait_ms: int) -> tuple[Browser, Page, object]:
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.new_page()
    # Block images/fonts/styles to speed up detail and listing loads
    try:
        page.context.route(
            "**/*",
            lambda route, request: route.abort()
            if request.resource_type in ("image", "stylesheet", "font", "media")
            else route.continue_(),
        )
        log("Playwright: blocking images/styles/fonts/media to speed up scraping")
    except Exception:
        pass
    if not headless and wait_ms > 0:
        log(f"Visible browser: waiting {wait_ms} ms for you to clear any challenge...")
        page.wait_for_timeout(wait_ms)
    return browser, page, pw


def fetch_html(page: Page, url: str, wait_until: str = "domcontentloaded", expand_listing: bool = False) -> str:
    """
    Navigate to a URL and return HTML.
    If expand_listing is True, try to scroll and click common "see more"/"load more" buttons
    to trigger lazy-loaded results before capturing HTML.
    """
    try:
        page.goto(url, wait_until=wait_until, timeout=25000)
        if expand_listing:
            # Heuristic: scroll and click typical "load more" controls a few times
            log("  Expanding listing page: scrolling and clicking 'see more' style buttons...")
            try:
                for _ in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    page.wait_for_timeout(1500)
                    # Try clicking common patterns
                    selectors = [
                        "text=/see more/i",
                        "text=/load more/i",
                        "text=/show more/i",
                        "text=/more results/i",
                        "text=/view more/i",
                    ]
                    for sel in selectors:
                        try:
                            btn = page.query_selector(sel)
                            if btn:
                                log(f"  Clicking lazy-load control matching {sel!r}")
                                btn.click()
                                page.wait_for_timeout(1500)
                        except Exception:
                            continue
            except Exception as e:
                log(f"  Listing expand heuristic failed: {e}")
        return page.content()
    except Exception as e:
        log(f"  Fetch failed for {url}: {e}")
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Discover Bali villas via AI: suggest queries → web search → classify → extract listing/detail URLs → scrape details to CSV.",
    )
    ap.add_argument("-o", "--output", default="balivillas_discovered.csv", help="Output CSV path")
    ap.add_argument("--max-queries", type=int, default=8, help="Max search queries to run (default 8)")
    ap.add_argument("--max-results-per-query", type=int, default=12, help="Max search results per query (default 12)")
    ap.add_argument("--max-listing-pages", type=int, default=15, help="Max listing pages to visit for detail URLs (default 15)")
    ap.add_argument("--max-detail-pages", type=int, default=100, help="Max detail pages to scrape into CSV (default 100)")
    ap.add_argument("--no-headless", action="store_true", help="Run browser visible (e.g. to clear Cloudflare)")
    ap.add_argument("--wait-ms", type=int, default=8000, help="Wait (ms) in visible browser for manual challenge (default 8000)")
    ap.add_argument("--delay", type=float, default=0.0, help="Extra delay (s) between requests (default 0)")
    args = ap.parse_args()

    client = get_client()
    headless = not args.no_headless

    # 1) AI suggests search queries
    queries = ai_suggest_queries(client)
    queries = queries[: args.max_queries]
    if not queries:
        log("No search queries from OpenAI; using defaults.")
        queries = ["Bali villas for rent", "Bali villas for sale", "villa rental Canggu Bali", "villa for sale Seminyak"]
    log(f"Using {len(queries)} search queries: {queries[:5]}...")

    # 2) Web search for each query; collect raw results
    all_results: List[Dict[str, str]] = []
    seen_urls: Set[str] = set()
    for q in queries:
        log(f"Search: {q!r}")
        results = web_search(q, max_results=args.max_results_per_query)
        for r in results:
            u = (r.get("url") or "").strip()
            if u and u not in seen_urls:
                seen_urls.add(u)
                all_results.append(r)
        if args.delay > 0:
            time.sleep(args.delay)
    log(f"Total unique search result URLs: {len(all_results)}")

    # 3) AI classifies: listing_urls vs detail_urls (in batches)
    listing_urls: List[str] = []
    detail_urls: List[str] = []
    batch_size = 20
    for i in range(0, len(all_results), batch_size):
        batch = all_results[i : i + batch_size]
        L, D = ai_classify_search_results(client, batch)
        listing_urls.extend(L)
        detail_urls.extend(D)
        if args.delay > 0:
            time.sleep(args.delay)
    listing_urls = list(dict.fromkeys(listing_urls))
    detail_urls = list(dict.fromkeys(detail_urls))
    log(f"Classified: {len(listing_urls)} listing URLs, {len(detail_urls)} detail URLs")

    # 4) Open browser; for each listing URL, fetch and extract detail URLs
    log("Opening browser (Playwright)...")
    browser, page, pw = open_browser(headless=headless, wait_ms=args.wait_ms)
    all_detail: Set[str] = set(detail_urls)
    try:
        for i, url in enumerate(listing_urls[: args.max_listing_pages]):
            log(f"[Listing {i+1}/{min(len(listing_urls), args.max_listing_pages)}] Fetching {url[:70]}...")
            html = fetch_html(page, url, expand_listing=True)
            if not html or len(html) < 500:
                continue
            extracted = ai_extract_detail_urls_from_listing(client, url, html)
            for u in extracted:
                all_detail.add(u)
            if args.delay > 0:
                time.sleep(args.delay)
    finally:
        pass  # keep browser open for detail fetches

    all_detail_list = list(all_detail)[: args.max_detail_pages]
    log(f"Total detail URLs to scrape: {len(all_detail_list)} (capped at {args.max_detail_pages})")

    # 5) For each detail URL: fetch → AI extract row → write CSV
    out_path = Path(args.output)
    fieldnames = MAIN_CSV_COLUMNS + EXTRA_COLUMNS
    written = 0
    try:
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for idx, url in enumerate(all_detail_list, 1):
                log(f"[Detail {idx}/{len(all_detail_list)}] {url[:75]}...")
                html = fetch_html(page, url, wait_until="networkidle", expand_listing=False)
                if not html or len(html) < 300:
                    continue
                try:
                    row = ai_extract_row(client, url, html)
                    phone = (row.get("agent_phone") or "").strip()
                    email = (row.get("agent_email") or "").strip()
                    # Business rule: drop entries that have neither phone nor email
                    if not phone and not email:
                        log("  ! Skipping row: missing both agent_phone and agent_email")
                        continue
                    writer.writerow(row)
                    written += 1
                    log(f"  ✓ Wrote: {row.get('title', '')[:60]!r}")
                except Exception as e:
                    log(f"  ! OpenAI extract failed: {e}")
                if args.delay > 0:
                    time.sleep(args.delay)
    finally:
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
