#!/usr/bin/env python3
"""
Repair villa_listings.csv: one line per record, English column names,
price as IDR and duration column. Converts old column names if present.
"""
import csv
import re
import sys
from pathlib import Path

# English column order (url last, main_image before it)
OUTPUT_COLUMNS = [
    "title", "price_idr", "duration", "location",
    "bedrooms", "bathrooms", "land_size_m2", "building_size_m2",
    "certificate", "description", "facilities", "agent_name", "updated_by",
    "main_image", "url",
]

DURATION_MAP = {
    "/hari": "day", "per hari": "day",
    "/minggu": "week", "per minggu": "week",
    "/bulan": "month", "per bulan": "month",
    "/tahun": "year", "per tahun": "year",
    "/year": "year", "/month": "month", "/day": "day",
}


def normalize_cell(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r" +", " ", s).strip()


def parse_price_idr(price_text: str) -> tuple[str, str]:
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
    formatted = f"Rp {amount_int:,}".replace(",", ".")
    return formatted, duration


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("villa_listings.csv")
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            row = {k: normalize_cell(str(v or "")) for k, v in row.items()}
            price_idr, duration = parse_price_idr(row.get("price", ""))
            new_row = {
                "title": (row.get("title", "") or "").replace(",", ""),
                "price_idr": price_idr or row.get("price_idr", ""),
                "duration": duration or row.get("duration", ""),
                "location": row.get("location", ""),
                "bedrooms": row.get("bedrooms", "") or row.get("kamar_tidur", ""),
                "bathrooms": row.get("bathrooms", "") or row.get("kamar_mandi", ""),
                "land_size_m2": row.get("land_size_m2", "") or row.get("luas_tanah", ""),
                "building_size_m2": row.get("building_size_m2", "") or row.get("luas_bangunan", ""),
                "certificate": row.get("certificate", "") or row.get("sertifikat", ""),
                "description": (row.get("description", "") or "").replace(",", ""),
                "facilities": (row.get("facilities", "") or "").replace(",", ""),
                "agent_name": (row.get("agent_name", "") or "").replace(",", ""),
                "updated_by": row.get("updated_by", ""),
                "main_image": row.get("main_image", ""),
                "url": row.get("url", ""),
            }
            rows.append(new_row)

    out_path = path.with_name(path.stem + "_fixed.csv") if path.suffix == ".csv" else path.with_suffix(".csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {len(rows)} rows to {out_path} (English columns, price_idr, duration)")


if __name__ == "__main__":
    main()
