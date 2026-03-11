# Villa Finder – Bali villa scrapers

One engine, one CSV. **`scrape_villas.py`** collects from all configured sources with the **same data strategy** and writes a single **unified inventory** (`villa_listings.csv`). Column schema and normalization live in **`villa_csv.py`**.

**Sources (these 5 URLs are used when you run `--source all`):**

1. [Rumah123 – sewa/bali/villa](https://www.rumah123.com/sewa/bali/villa/)
2. [Bali Long Term Rentals – yearly-rentals](https://www.balilongtermrentals.com/yearly-rentals/)
3. [Bali Long Term Rentals – monthly-rental](https://www.balilongtermrentals.com/monthly-rental/)
4. [Bali Coconut Living – villa-for-yearly-rental](https://balicoconutliving.com/property/villa-for-yearly-rental)
5. [Bali Coconut Living – villa-for-monthly-rental](https://balicoconutliving.com/property/villa-for-monthly-rental)

You can also run **`--source villa-bali`** (Villa-Bali.com) separately if you want that site included; it is not part of the default "all" set.

## Unified script (all sources)

**`scrape_villas.py`** is the main entry point. Use `--source all` (default) to scrape every source into one CSV, or pick a single source.

```bash
# All sources → one CSV (default)
python scrape_villas.py -o villa_listings.csv
python scrape_villas.py --append -o villa_listings.csv

# Single source
python scrape_villas.py --source rumah123 --pages 3 -o villa_listings.csv --json
python scrape_villas.py -s villa-bali -o villa_listings.csv --append
python scrape_villas.py -s balilongterm -w 4 -o villa_listings.csv --append
python scrape_villas.py -s balicoconut -o villa_listings.csv --append

# List URLs only (single source)
python scrape_villas.py -s rumah123 --list-only -o urls.txt
```

Common options: `--output` / `-o`, `--append`, `--delay`, `--workers` / `-w`, `--headless`, `--fast`. Entries without title or price are never written.

**Why there are gaps between log steps:** Scrapers use short delays between listing pages, “load more” clicks, and detail fetches to reduce load and avoid blocks. Use **`--fast`** to shorten those delays when you want a quicker run.

**Faster runs without jeopardizing collection:** Use **`--fast`** (or **`-f`**): it shortens delays for all sources (Rumah123 listing + detail, Bali Home Immo, Bali Long Term, Bali Coconut) and is tuned to keep blocks unlikely. For a bit more speed at slightly higher block risk, you can also pass **`--workers 2`** so each source uses 2 parallel workers for detail fetches (default is 1). Standalone scripts (`scrape_rumah123.py`, `scrape_villa_bali.py`, `scrape_balilongterm.py`, `scrape_balicoconut.py`) remain available and use the same CSV schema.

**Logging (unified script only):** Use `--log-level DEBUG` for per-URL and retry messages, or `--log-file scrape.log` to write the same logs to a file (utf-8). Example: `python scrape_villas.py -o out.csv --log-level DEBUG --log-file scrape.log`

## Setup

```bash
cd villa-finder
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# For Villa-Bali.com only: install Chromium (playwright is in requirements.txt)
python -m playwright install chromium
```

## Usage

```bash
# One command: all sources → one CSV
python scrape_villas.py -o villa_listings.csv

# Single source via unified script
python scrape_villas.py --source rumah123 --pages 1 -o villa_listings.csv --json
python scrape_villas.py --source villa-bali -o villa_listings.csv --append
python scrape_villas.py --source balilongterm -o villa_listings.csv --append
python scrape_villas.py --source balicoconut -o villa_listings.csv --append

# Standalone scripts (same CSV format)
python scrape_rumah123.py --pages 1 -o villa_listings.csv --json

# Scrape 5 listing pages (~100 villas), 2 workers (default), slower pace
python scrape_rumah123.py --pages 5 -o villa_listings.csv --json

# Only collect listing URLs (no detail fetch)
python scrape_rumah123.py --pages 3 --list-only -o urls.txt

# Villa-Bali.com (same CSV columns) – append to existing file
python scrape_villa_bali.py -o villa_listings.csv --append

# Villa-Bali.com only (overwrite CSV)
python scrape_villa_bali.py -o villa_listings.csv

# Bali Long Term Rentals (yearly) – append to same CSV
python scrape_balilongterm.py -o villa_listings.csv --append

# Bali Long Term Rentals only (first 3 listing pages)
python scrape_balilongterm.py --pages 3 -o villa_listings.csv

# Bali Coconut Living (monthly + yearly) – append to same CSV
python scrape_balicoconut.py -o villa_listings.csv --append

# Bali Coconut Living only (list URLs only)
python scrape_balicoconut.py --list-only -o bcl_urls.txt
```

### Rumah123 options

| Option | Description |
|--------|-------------|
| `--pages` | Number of listing pages to crawl (default: 1). ~20 listings per page. |
| `--delay` | Seconds between each detail-page request (default: 3.0). With multiple workers, delay is split across them. |
| `-w`, `--workers` | Number of parallel workers (default: 2). Lower if you get 429 Too Many Requests. |
| `-o`, `--output` | Output CSV path (default: `villa_listings.csv`). With `--list-only`, writes `.txt`. |
| `--json` | Also write a JSON file with the same data. |
| `--list-only` | Only collect property URLs from listing pages; do not fetch details. |

### Villa-Bali.com options

| Option | Description |
|--------|-------------|
| `-o`, `--output` | Same as Rumah123 (default: `villa_listings.csv`). |
| `--append` | Append to existing CSV instead of overwriting (combine with Rumah123 data). |
| `--delay` | Base delay between detail requests (default: 6.0); random jitter is added to appear more human. |
| `-w`, `--workers` | Ignored (Playwright runs single-threaded); kept for CLI compatibility. |
| `--list-only` | Only collect villa URLs to a `.txt` file. |
| `--headless` | Run browser in headless mode (no window). |

### Bali Long Term Rentals options

| Option | Description |
|--------|-------------|
| `-o`, `--output` | Same as others (default: `villa_listings.csv`). |
| `--append` | Append to existing CSV instead of overwriting. |
| `--pages` | Max listing pages to crawl (default: 25). |
| `--delay` | Base delay between detail requests (default: 3.0); random jitter added. |
| `--fast` | Shorter delays (≈1.2s) and faster listing phase; higher block risk. |
| `-w`, `--workers` | Parallel browser processes for detail fetch: 1–16 (default: 1). Speeds up collection. |
| `--list-only` | Only collect villa URLs to a `.txt` file. |
| `--headless` | Run browser in headless mode. |

### Bali Coconut Living options

| Option | Description |
|--------|-------------|
| `-o`, `--output` | Same as others (default: `villa_listings.csv`). |
| `--append` | Append to existing CSV instead of overwriting. |
| `--pages` | Max listing pages per index – monthly and yearly (default: 50). |
| `--delay` | Base delay between detail requests (default: 2.0). |
| `--fast` | Shorter delays; higher block risk. |
| `-w`, `--workers` | Parallel browser processes for detail fetch: 1–16 (default: 1). |
| `--list-only` | Only collect property URLs to a `.txt` file. |
| `--headless` | Run browser in headless mode. |

Villa-Bali.com uses Playwright (Chromium) with a single page; prices are converted from USD to IDR for the same `price_idr` column. If you see "sorry, you have been blocked": use a higher `--delay` (e.g. 10), run less often, or try a different network/VPN.

Bali Long Term Rentals uses Playwright (the site may show a short "One moment, please" verification). Prices are already in IDR; duration is typically "year".

Bali Coconut Living uses Playwright for both listing and detail pages. Prices are in IDR; duration is inferred from the listing (monthly or yearly). Rows without title or price are skipped.

## Data strategy

- **One schema:** `villa_csv.MAIN_CSV_COLUMNS` defines the 15 columns; all sources normalize to this.
- **One file:** With `--source all`, each source is scraped in sequence and rows are appended to the same CSV (header written once).
- **Filter:** Only rows that have both `title` and `price_idr` are written (`villa_csv.has_title_and_price`).

## CSV columns (English, main information only)

| Column           | Description |
|------------------|-------------|
| title            | Listing title |
| price_idr        | Price in IDR format (e.g. Rp 1.500.000) |
| duration         | Rent duration: `day`, `week`, `month`, or `year` |
| location         | Area, Regency (e.g. Canggu, Badung) |
| bedrooms         | Number of bedrooms |
| bathrooms        | Number of bathrooms |
| land_size_m2     | Land size (e.g. 600 m²) |
| building_size_m2 | Building size |
| certificate      | Certificate type (e.g. SHM) |
| description      | Property description |
| facilities       | All facilities merged |
| agent_name       | Agent or listing owner |
| updated_by       | Last updated text |
| main_image       | URL of the main listing image |
| url              | Property detail page URL (last column) |

Note: Spec fields may be empty when the site renders them only with JavaScript.
