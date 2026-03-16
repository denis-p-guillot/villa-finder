from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Dict, Tuple

from dotenv import load_dotenv
from openai import OpenAI


FACILITIES_MASTER = [
    "Air conditioning",
    "WiFi",
    "TV",
    "Kitchen",
    "Fully equipped kitchen",
    "Washing machine",
    "Dishwasher",
    "Office / workspace",
    "Pool",
    "Private pool",
    "Garden",
    "BBQ",
    "Beach access",
    "Balcony",
    "Terrace",
    "Lounge",
    "Gym",
    "Spa",
    "Pet friendly",
    "Parking",
    "Daily cleaning",
    "Staff",
    "Maid service",
    "Security",
    "Sea view",
    "Rice field view",
]

SYSTEM_PROMPT = """
You help clean property listing data for a CSV.

Goals:
- Fix language in the description: correct grammar and spelling, keep the same meaning, keep it concise.
- From the title + description + location, infer which facilities apply from this EXACT list only:
  Air conditioning, WiFi, TV, Kitchen, Fully equipped kitchen, Washing machine, Dishwasher,
  Office / workspace, Pool, Private pool, Garden, BBQ, Beach access, Balcony, Terrace,
  Lounge, Gym, Spa, Pet friendly, Parking, Daily cleaning, Staff, Maid service,
  Security, Sea view, Rice field view.

Rules:
- Only use facilities from the allowed list, nothing else.
- If you are not reasonably sure a facility is present, do not include it.
- Output JSON with keys:
  - cleaned_description: string
  - facilities: array of strings (each must be one of the allowed facilities)
"""


def get_client() -> OpenAI:
    # Load variables from .env if present
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in environment or .env")
    return OpenAI(api_key=api_key)


def enrich_listing(
    client: OpenAI,
    title: str,
    description: str,
    location: str,
) -> Tuple[str, str]:
    user_payload: Dict[str, str] = {
        "title": title or "",
        "description": description or "",
        "location": location or "",
    }

    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Clean this listing and infer facilities from the allowed list.\n"
                + json.dumps(user_payload, ensure_ascii=False),
            },
        ],
    )
    content = completion.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: keep original description, empty facilities
        return description, ""

    cleaned_desc = str(data.get("cleaned_description") or description or "").strip()
    facilities_list = data.get("facilities") or []
    if not isinstance(facilities_list, list):
        facilities_list = []

    # Keep only valid, preserve FACILITIES_MASTER order
    normalized = {str(f).strip() for f in facilities_list if str(f).strip()}
    ordered = [f for f in FACILITIES_MASTER if f in normalized]
    facilities_str = ", ".join(ordered)
    return cleaned_desc, facilities_str


def process_csv(input_path: Path, output_path: Path, delay: float = 0.5) -> None:
    client = get_client()

    with input_path.open("r", encoding="utf-8", newline="") as f_in:
        reader = csv.DictReader(f_in, delimiter=";")
        fieldnames = reader.fieldnames or []
        if "description" not in fieldnames or "facilities" not in fieldnames:
            raise SystemExit("CSV must contain 'description' and 'facilities' columns")

        total = 0
        # Peek total rows for nicer progress (optional, resets file handle)
        rows_cache = list(reader)
        total = len(rows_cache)
        f_in.seek(0)
        reader = csv.DictReader(f_in, delimiter=";")

        with output_path.open("w", encoding="utf-8", newline="") as f_out:
            writer = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()

            for idx, row in enumerate(reader, start=1):
                title = (row.get("title") or "").strip()
                desc = (row.get("description") or "").strip()
                loc = (row.get("location") or "").strip()

                try:
                    cleaned_desc, facilities = enrich_listing(client, title, desc, loc)
                except Exception as e:
                    print(f"[row {idx}] OpenAI error: {e} (keeping original row)")
                    cleaned_desc, facilities = desc, (row.get("facilities") or "").strip()

                row["description"] = cleaned_desc
                # If facilities already present, keep them; otherwise use AI-derived
                if not (row.get("facilities") or "").strip():
                    row["facilities"] = facilities

                writer.writerow(row)

                if idx == 1 or idx % 10 == 0 or idx == total:
                    if total:
                        print(f"[{idx}/{total}] processed: {title[:60]!r}")
                    else:
                        print(f"[{idx}] processed: {title[:60]!r}")

                if delay > 0:
                    time.sleep(delay)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Enrich villa CSV using OpenAI (clean descriptions, infer facilities)."
    )
    ap.add_argument(
        "input",
        help="Input CSV file (e.g. URLS.csv or villa_listings.csv)",
    )
    ap.add_argument(
        "-o",
        "--output",
        help="Output CSV file (default: <input> with _ai_enriched suffix)",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between OpenAI calls in seconds (default: 0.5, increase if you hit rate limits).",
    )
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"Input file not found: {in_path}")

    out_path = Path(args.output) if args.output else in_path.with_name(
        in_path.stem + "_ai_enriched" + in_path.suffix
    )

    process_csv(in_path, out_path, delay=args.delay)
    print(f"Enriched CSV written to {out_path}")


if __name__ == "__main__":
    main()

