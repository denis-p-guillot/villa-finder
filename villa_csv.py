"""
Shared CSV schema and normalization for all villa scrapers.
All sources produce rows that are normalized to this schema and written to one CSV.
"""

import logging
import re

LOG_NAME = "villa_finder"


def get_logger():
    """Return the shared logger for all scrapers."""
    return logging.getLogger(LOG_NAME)


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
) -> None:
    """
    Configure the villa_finder logger. Call once at process start (e.g. from scrape_villas main).
    level: DEBUG, INFO, WARNING, ERROR
    log_file: if set, also write logs to this file (utf-8).
    """
    log = logging.getLogger(LOG_NAME)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not log.handlers:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        log.addHandler(h)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger(LOG_NAME).addHandler(fh)

# Single canonical schema for the unified villa inventory
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
    "image_urls",
    "url",
]


def _csv_cell(s: str) -> str:
    """Normalize a string for CSV: no commas, no runaway length, no script junk."""
    if not isinstance(s, str):
        return ""
    if "push(" in s or '"formats"' in s or '"locale"' in s:
        return ""
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r" +", " ", s).strip()
    if len(s) > 8000:
        s = s[:8000] + "..."
    return s.replace(",", "")


# Text fields that must have commas stripped before CSV write (avoid breaking column alignment)
CSV_TEXT_FIELDS_NO_COMMA = ("description", "facilities", "agent_name")


def normalize_row(row: dict) -> dict:
    """
    Turn a canonical row (keys = MAIN_CSV_COLUMNS or subset) into a CSV-ready row.
    All sources that output canonical shape use this for a unified data strategy.
    description, facilities, and agent_name are always stripped of commas.
    """
    out = {}
    for k in MAIN_CSV_COLUMNS:
        v = row.get(k, "")
        if v is None:
            v = ""
        if isinstance(v, (int, float)):
            v = str(v)
        out[k] = _csv_cell(str(v))
    # Guarantee no commas in long text fields that could break CSV parsing
    for key in CSV_TEXT_FIELDS_NO_COMMA:
        if key in out and isinstance(out[key], str):
            out[key] = (out[key] or "").replace(",", "")
    return out


def has_title_and_price(row: dict) -> bool:
    """True if row has both title and price_idr (canonical keys). Used to filter before writing."""
    if not row:
        return False
    title_ok = bool((row.get("title") or "").strip())
    price_ok = bool((row.get("price_idr") or "").strip())
    return title_ok and price_ok
