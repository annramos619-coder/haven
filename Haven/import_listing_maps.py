#!/usr/bin/env python3
"""Convert a Hostaway listingMaps export into dashboard marketing_links.csv."""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path


OUTPUT_PATH = Path(__file__).parent / "marketing_links.csv"


FIELDNAMES = [
    "Property Name",
    "Airbnb Headline",
    "Vrbo Headline",
    "PMS Property ID",
    "Vrbo",
    "TripAdvisor",
    "Airbnb",
    "Airbnb ID Issue",
    "Booking",
    "Booking URL",
    "Airbnb Rating",
    "Airbnb Reviews",
    "Vrbo Rating",
    "Vrbo Reviews",
    "Vrbo Review Label",
    "Vrbo Cleanliness",
    "Vrbo Check-in",
    "Vrbo Communication",
    "Vrbo Location",
    "Airbnb Photos",
    "Airbnb Thumb URL",
    "Vrbo Photos",
    "Vrbo Thumb URL",
]


def clean(value: str | None) -> str:
    return (value or "").strip()


def truthy(value: str | None) -> bool:
    return clean(value).lower() in {"true", "yes", "y", "1", "active", "exported", "approved"}


def active_url(value: str | None) -> str:
    value = clean(value)
    return value if value.startswith(("http://", "https://")) else ""


def clean_airbnb_id(value: str | None) -> str:
    value = clean(value)
    if re.fullmatch(r"\d+(?:\.\d+)?[eE][+-]?\d+", value):
        return ""
    if re.fullmatch(r"\d+(\.0+)?", value):
        return value.split(".", 1)[0]
    return value


def airbnb_id_issue(value: str | None) -> str:
    value = clean(value)
    if re.fullmatch(r"\d+(?:\.\d+)?[eE][+-]?\d+", value):
        return "Airbnb ID exported in scientific notation; re-export as text or provide Airbnb URL"
    return ""


def import_listing_maps(input_path: Path) -> tuple[int, int]:
    imported = 0
    skipped = 0

    with input_path.open(newline="", encoding="utf-8-sig") as f_in, OUTPUT_PATH.open(
        "w", newline="", encoding="utf-8"
    ) as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=FIELDNAMES)
        writer.writeheader()

        for row in reader:
            status = clean(row.get("Special Status")).lower()
            if status == "archived":
                skipped += 1
                continue

            hostaway_id = clean(row.get("Hostaway Listing Id"))
            name = clean(row.get("Hostaway Listing Name"))
            if not hostaway_id or not name:
                skipped += 1
                continue

            raw_airbnb_id = row.get("Airbnb Official Listing Id Str") or row.get("Airbnb Official Listing Id")
            airbnb_id = clean_airbnb_id(row.get("Airbnb Official Listing Id Str")) or clean_airbnb_id(row.get("Airbnb Official Listing Id"))
            airbnb_issue = airbnb_id_issue(raw_airbnb_id)
            if not truthy(row.get("Airbnb Official Listing Active")):
                airbnb_id = ""
                airbnb_issue = ""

            vrbo_url = active_url(row.get("Vrbo Listing Url")) or active_url(row.get("Homeaway Api Listing Url"))
            vrbo_id = clean(row.get("Vrbo Listing Id")) or clean(row.get("Homeaway Property Id"))
            if not truthy(row.get("Vrbo Listing Active")) and not truthy(row.get("Homeaway Listing Active")):
                vrbo_url = ""
                vrbo_id = ""

            booking_url = (
                active_url(row.get("Booking.com Listing Url"))
                or active_url(row.get("Booking Listing Url"))
                or active_url(row.get("Booking.com URL"))
                or active_url(row.get("Booking URL"))
            )
            booking_id = (
                clean(row.get("Bookingcom Hotel Id"))
                or clean(row.get("Booking Com Hotel Id"))
                or clean(row.get("Booking.com Listing Id"))
                or clean(row.get("Booking Listing Id"))
                or clean(row.get("Booking.com Property Id"))
                or clean(row.get("Booking Property Id"))
            )
            booking_active = (
                truthy(row.get("Booking.com Listing Active"))
                or truthy(row.get("Booking Listing Active"))
                or truthy(row.get("Bookingcom Room Active"))
                or truthy(row.get("Booking Com Room Active"))
                or bool(booking_url)
                or bool(booking_id)
            )
            if not booking_active:
                booking_url = ""
                booking_id = ""

            writer.writerow(
                {
                    "Property Name": name,
                    "Airbnb Headline": name,
                    "Vrbo Headline": name,
                    "PMS Property ID": hostaway_id,
                    "Vrbo": vrbo_url or vrbo_id,
                    "TripAdvisor": "",
                    "Airbnb": airbnb_id,
                    "Airbnb ID Issue": airbnb_issue,
                    "Booking": booking_id,
                    "Booking URL": booking_url,
                    "Airbnb Rating": "",
                    "Airbnb Reviews": "",
                    "Vrbo Rating": "",
                    "Vrbo Reviews": "",
                    "Vrbo Review Label": "",
                    "Vrbo Cleanliness": "",
                    "Vrbo Check-in": "",
                    "Vrbo Communication": "",
                    "Vrbo Location": "",
                    "Airbnb Photos": "",
                    "Airbnb Thumb URL": "",
                    "Vrbo Photos": "",
                    "Vrbo Thumb URL": "",
                }
            )
            imported += 1

    return imported, skipped


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: py import_listing_maps.py \"C:\\path\\to\\listingMaps.csv\"")
        return 2
    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"ERROR: file not found: {input_path}")
        return 1
    imported, skipped = import_listing_maps(input_path)
    print(f"Imported {imported} active listing map rows to {OUTPUT_PATH.name}.")
    print(f"Skipped {skipped} archived/invalid rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
