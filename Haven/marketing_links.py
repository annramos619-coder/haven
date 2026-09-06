#!/usr/bin/env python3
"""
Parses marketing_links.csv (exported from Hostaway) and matches Airbnb/VRBO
listing IDs and URLs to PriceLabs portfolio properties by normalized name.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CSV_PATH = Path(__file__).parent / "marketing_links.csv"
LISTING_MAPS_CSV_PATH = Path(__file__).parent / "listing_maps.csv"

AIRBNB_BASE = "https://www.airbnb.com/rooms/{}"
VRBO_BASE   = "https://www.vrbo.com/{}"
BOOKING_BASE = "https://www.booking.com/searchresults.html?dest_id={0}&dest_type=hotel&highlighted_hotels={0}"


@dataclass
class ListingLinks:
    property_name: str          # raw name from marketing CSV
    airbnb_headline: str
    vrbo_headline: str
    streamline_id: Optional[int]
    vrbo_id: Optional[str]
    tripadvisor_id: Optional[str]
    airbnb_id: Optional[str]
    airbnb_id_issue: str = ""
    booking_id: Optional[str] = None
    booking_hotel_id: Optional[str] = None
    booking_room_ids: str = ""
    booking_parent_rate_ids: str = ""
    airbnb_url_override: str = ""
    vrbo_url_override: str = ""
    booking_url_override: str = ""
    airbnb_rating: Optional[float] = None
    airbnb_reviews: Optional[int] = None
    vrbo_rating: Optional[float] = None
    vrbo_reviews: Optional[int] = None
    airbnb_photos: Optional[int] = None
    airbnb_thumb_url: str = ""
    vrbo_photos: Optional[int] = None
    vrbo_thumb_url: str = ""
    vrbo_review_label: str = ""
    vrbo_cleanliness: Optional[float] = None
    vrbo_checkin: Optional[float] = None
    vrbo_communication: Optional[float] = None
    vrbo_location: Optional[float] = None

    @property
    def airbnb_url(self) -> Optional[str]:
        if self.airbnb_url_override:
            return self.airbnb_url_override
        return AIRBNB_BASE.format(self.airbnb_id) if self.airbnb_id else None

    @property
    def vrbo_url(self) -> Optional[str]:
        if self.vrbo_url_override:
            return self.vrbo_url_override
        return VRBO_BASE.format(self.vrbo_id) if self.vrbo_id else None

    @property
    def booking_url(self) -> Optional[str]:
        if self.booking_url_override:
            return self.booking_url_override
        return BOOKING_BASE.format(self.booking_id) if self.booking_id else None

    @property
    def has_airbnb(self) -> bool:
        return bool(self.airbnb_id or self.airbnb_url_override)

    @property
    def has_vrbo(self) -> bool:
        return bool(self.vrbo_id or self.vrbo_url_override)

    @property
    def has_booking(self) -> bool:
        return bool(self.booking_id or self.booking_url_override)

    @staticmethod
    def _rating_status(rating: Optional[float], reviews: Optional[int] = None) -> str:
        if rating is None:
            return "unknown"
        if rating > 5:
            if rating < 8:
                status = "bad"
            elif rating < 9:
                status = "needs attention"
            elif rating < 9.5:
                status = "good"
            else:
                status = "strong"
            if reviews is not None and reviews < 10:
                status += " (limited review count)"
            return status
        if rating < 4.5:
            status = "bad"
        elif rating < 4.7:
            status = "needs attention"
        elif rating < 4.8:
            status = "good, below top-tier"
        else:
            status = "strong"
        if reviews is not None and reviews < 10:
            status += " (limited review count)"
        return status

    @property
    def airbnb_rating_status(self) -> str:
        return self._rating_status(self.airbnb_rating, self.airbnb_reviews)

    @property
    def vrbo_rating_status(self) -> str:
        return self._rating_status(self.vrbo_rating, self.vrbo_reviews)

    @staticmethod
    def _photo_grade(photos: Optional[int], thumb_url: str = "") -> str:
        if photos is None:
            return "unknown"
        if photos < 15:
            grade = "D - too few photos"
        elif photos < 25:
            grade = "C - needs more coverage"
        elif photos < 35:
            grade = "B - adequate"
        else:
            grade = "A - strong coverage"
        if not thumb_url:
            grade += " (thumbnail missing)"
        return grade

    @property
    def airbnb_photo_grade(self) -> str:
        return self._photo_grade(self.airbnb_photos, self.airbnb_thumb_url)

    @property
    def vrbo_photo_grade(self) -> str:
        return self._photo_grade(self.vrbo_photos, self.vrbo_thumb_url)


def _normalize(name: str) -> str:
    """Normalize a property name for fuzzy matching across both CSVs."""
    # Strip inactive prefixes (.LT, .ONB, .OFF, .FEMA, etc.)
    name = re.sub(r'^\.[A-Z]+\s+', '', name.strip(), flags=re.I)
    # PriceLabs exports often append the public listing title after "--".
    # Hostaway listing maps usually keep only the owner/unit key before it.
    name = re.sub(r'\s+--\s+.*$', '', name)
    # Strip ': <Type>' suffix (": Default", ": 2 Bedroom", ": Studio", etc.)
    name = re.sub(r':\s*.+$', '', name)
    return name.strip().lower()


def _int(val: str) -> Optional[int]:
    val = val.strip()
    try:
        return int(float(val)) if val else None
    except ValueError:
        return None


def _id(val: str) -> Optional[str]:
    val = val.strip()
    if not val:
        return None
    # Excel often damages long Airbnb IDs into values like 1.15181E+18.
    # That loses precision, so any generated /rooms/<id> URL would be wrong.
    if re.fullmatch(r"\d+(?:\.\d+)?[eE][+-]?\d+", val):
        return None
    room_match = re.search(r"airbnb\.[^/]+/rooms/(\d+)", val, flags=re.I)
    if room_match:
        return room_match.group(1)
    vrbo_match = re.search(r"vrbo\.[^/]+/(?:[^/?#]+-)?(\d+)", val, flags=re.I)
    if vrbo_match:
        return vrbo_match.group(1)
    if re.fullmatch(r"\d+(\.0+)?", val):
        return val.split(".", 1)[0]
    return re.sub(r"\D+", "", val) or None


def _url(val: str, platform: str = "") -> str:
    val = val.strip()
    if not val:
        return ""
    if val.startswith("//"):
        val = "https:" + val
    elif val.startswith("www."):
        val = "https://" + val
    if not re.match(r"https?://", val, flags=re.I):
        return ""
    if platform == "airbnb" and "airbnb." not in val.lower():
        return ""
    if platform == "vrbo" and "vrbo." not in val.lower():
        return ""
    if platform == "booking" and "booking." not in val.lower():
        return ""
    return val


def _float(val: str) -> Optional[float]:
    val = val.strip()
    try:
        return float(val) if val else None
    except ValueError:
        return None


def load_links() -> dict[str, ListingLinks]:
    """
    Returns a dict keyed by normalized property name → ListingLinks.
    Also includes raw name as a secondary key for direct lookups.
    """

    def _col(header: list[str], *names: str) -> Optional[int]:
        normalized = {
            re.sub(r"[^a-z0-9]+", "", name.lower()): idx
            for idx, name in enumerate(header)
        }
        for name in names:
            idx = normalized.get(re.sub(r"[^a-z0-9]+", "", name.lower()))
            if idx is not None:
                return idx
        return None

    def _value(row: list[str], idx: Optional[int]) -> str:
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    links: dict[str, ListingLinks] = {}
    if not CSV_PATH.exists():
        return _merge_listing_maps(links)

    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)  # skip header
        airbnb_rating_idx = _col(header, "Airbnb Rating", "Airbnb Star Rating", "Airbnb Stars")
        airbnb_reviews_idx = _col(header, "Airbnb Reviews", "Airbnb Review Count", "Airbnb Total Reviews")
        vrbo_rating_idx = _col(header, "Vrbo Rating", "VRBO Rating", "Vrbo Star Rating", "VRBO Stars")
        vrbo_reviews_idx = _col(header, "Vrbo Reviews", "VRBO Reviews", "Vrbo Review Count", "VRBO Review Count")
        airbnb_photos_idx = _col(header, "Airbnb Photos", "Airbnb Photo Count", "Airbnb Num Photos")
        airbnb_url_idx = _col(header, "Airbnb URL", "Airbnb Link", "Airbnb Public URL", "Airbnb Listing URL")
        airbnb_thumb_idx = _col(header, "Airbnb Thumb URL", "Airbnb Thumbnail", "Airbnb Thumbnail URL")
        airbnb_id_issue_idx = _col(header, "Airbnb ID Issue", "Airbnb Link Issue")
        vrbo_photos_idx = _col(header, "Vrbo Photos", "VRBO Photos", "Vrbo Photo Count", "VRBO Photo Count")
        vrbo_url_idx = _col(header, "Vrbo URL", "VRBO URL", "Vrbo Link", "VRBO Link", "Vrbo Listing URL", "VRBO Listing URL")
        vrbo_thumb_idx = _col(header, "Vrbo Thumb URL", "VRBO Thumb URL", "Vrbo Thumbnail", "VRBO Thumbnail URL")
        booking_idx = _col(header, "Booking", "Booking.com", "Booking Id", "Booking.com Id", "Booking Listing Id", "Booking.com Listing Id")
        booking_hotel_idx = _col(header, "Booking Hotel ID", "Booking.com Hotel ID", "Booking Property ID", "Booking.com Property ID")
        booking_room_ids_idx = _col(header, "Booking Room IDs", "Booking.com Room IDs", "Booking Room Type IDs")
        booking_parent_rate_ids_idx = _col(header, "Booking Parent Rate IDs", "Booking.com Parent Rate IDs", "Booking Rate IDs", "Booking Rate Plan IDs")
        booking_url_idx = _col(header, "Booking URL", "Booking.com URL", "Booking Link", "Booking.com Link", "Booking Listing URL", "Booking.com Listing URL")
        vrbo_review_label_idx = _col(header, "Vrbo Review Label", "VRBO Review Label")
        vrbo_cleanliness_idx = _col(header, "Vrbo Cleanliness", "VRBO Cleanliness")
        vrbo_checkin_idx = _col(header, "Vrbo Check-in", "VRBO Check-in", "Vrbo Checkin", "VRBO Checkin")
        vrbo_communication_idx = _col(header, "Vrbo Communication", "VRBO Communication")
        vrbo_location_idx = _col(header, "Vrbo Location", "VRBO Location")

        for row in reader:
            if len(row) < 4 or not row[0].strip():
                continue

            raw_name = row[0].strip()
            ll = ListingLinks(
                property_name=raw_name,
                airbnb_headline=row[1].strip() if len(row) > 1 else "",
                vrbo_headline=row[2].strip() if len(row) > 2 else "",
                streamline_id=_int(row[3]) if len(row) > 3 else None,
                vrbo_id=_id(row[4]) if len(row) > 4 else None,
                tripadvisor_id=_id(row[5]) if len(row) > 5 else None,
                airbnb_id=_id(row[6]) if len(row) > 6 else None,
                airbnb_id_issue=_value(row, airbnb_id_issue_idx),
                booking_id=_id(_value(row, booking_idx)),
                booking_hotel_id=_id(_value(row, booking_hotel_idx)) or _id(_value(row, booking_idx)),
                booking_room_ids=_value(row, booking_room_ids_idx),
                booking_parent_rate_ids=_value(row, booking_parent_rate_ids_idx),
                airbnb_url_override=_url(_value(row, airbnb_url_idx), "airbnb") or (_url(row[6], "airbnb") if len(row) > 6 else ""),
                vrbo_url_override=_url(_value(row, vrbo_url_idx), "vrbo") or (_url(row[4], "vrbo") if len(row) > 4 else ""),
                booking_url_override=_url(_value(row, booking_url_idx), "booking") or _url(_value(row, booking_idx), "booking"),
                airbnb_rating=_float(_value(row, airbnb_rating_idx)),
                airbnb_reviews=_int(_value(row, airbnb_reviews_idx)),
                vrbo_rating=_float(_value(row, vrbo_rating_idx)),
                vrbo_reviews=_int(_value(row, vrbo_reviews_idx)),
                airbnb_photos=_int(_value(row, airbnb_photos_idx)),
                airbnb_thumb_url=_value(row, airbnb_thumb_idx),
                vrbo_photos=_int(_value(row, vrbo_photos_idx)),
                vrbo_thumb_url=_value(row, vrbo_thumb_idx),
                vrbo_review_label=_value(row, vrbo_review_label_idx),
                vrbo_cleanliness=_float(_value(row, vrbo_cleanliness_idx)),
                vrbo_checkin=_float(_value(row, vrbo_checkin_idx)),
                vrbo_communication=_float(_value(row, vrbo_communication_idx)),
                vrbo_location=_float(_value(row, vrbo_location_idx)),
            )
            key = _normalize(raw_name)
            if key in links:
                # Two different raw names normalized to the same key — e.g. sibling
                # units disambiguated only by a ": Default"/": 2 Bedroom" suffix
                # that _normalize strips. Silently overwriting would attach one
                # unit's ratings/photos/links to a different unit with no signal
                # that it happened, so surface it instead of hiding it.
                import sys
                print(
                    f"WARNING marketing_links: '{raw_name}' normalizes to the same key as "
                    f"'{links[key].property_name}' — the second row is overwriting the first. "
                    "Check for a naming collision in marketing_links.csv.",
                    file=sys.stderr,
                )
            links[key] = ll

    return _merge_listing_maps(links)


def _merge_listing_maps(links: dict[str, "ListingLinks"]) -> dict[str, "ListingLinks"]:
    """Overlay the Hostaway 'Listing Maps' channel export (listing_maps.csv) —
    a much larger, directly-synced export of VRBO/Booking.com/Homeaway channel
    IDs and URLs — on top of whatever marketing_links.csv already has.

    This file does NOT reliably cover Airbnb (only 14/471 rows populated in the
    export this was built against — most listings aren't on Hostaway's direct
    Airbnb channel manager), so it only ever touches VRBO/Booking/Hostaway-ID
    fields; existing Airbnb data, ratings, and photo counts are left alone.
    """
    if not LISTING_MAPS_CSV_PATH.exists():
        return links

    with open(LISTING_MAPS_CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_name = (row.get("Hostaway Listing Name") or "").strip()
            if not raw_name:
                continue
            key = _normalize(raw_name)

            vrbo_id = _id((row.get("Vrbo Listing Id") or "").strip())
            vrbo_url = _url((row.get("Vrbo Listing Url") or "").strip(), "vrbo")
            booking_hotel_id = _id((row.get("Bookingcom Hotel Id") or "").strip())
            booking_room_id = (row.get("Bookingcom Room Id") or "").strip()
            hostaway_id = _int((row.get("Hostaway Listing Id") or "").strip())

            existing = links.get(key)
            if existing is not None:
                # Overlay onto the existing entry — only replace fields this
                # file actually has data for, keep everything else as-is
                # (ratings, photos, Airbnb data aren't in this export).
                if vrbo_id:
                    existing.vrbo_id = vrbo_id
                if vrbo_url:
                    existing.vrbo_url_override = vrbo_url
                if booking_hotel_id:
                    existing.booking_hotel_id = booking_hotel_id
                if booking_room_id:
                    existing.booking_room_ids = booking_room_id
                if hostaway_id:
                    existing.streamline_id = hostaway_id
            else:
                # A listing that only exists in this export, not in
                # marketing_links.csv — still worth adding so its VRBO/Booking
                # links are available, just with no ratings/headline data.
                links[key] = ListingLinks(
                    property_name=raw_name,
                    airbnb_headline="",
                    vrbo_headline="",
                    streamline_id=hostaway_id,
                    vrbo_id=vrbo_id,
                    tripadvisor_id=None,
                    airbnb_id=None,
                    booking_hotel_id=booking_hotel_id,
                    booking_room_ids=booking_room_id,
                    vrbo_url_override=vrbo_url,
                )

    return links


# Singleton — loaded once
_LINKS: dict[str, ListingLinks] | None = None


def get_links() -> dict[str, ListingLinks]:
    global _LINKS
    if _LINKS is None:
        _LINKS = load_links()
    return _LINKS


def lookup(property_name: str) -> Optional[ListingLinks]:
    """Look up a property's listing links by its PriceLabs name."""
    return get_links().get(_normalize(property_name))


if __name__ == "__main__":
    links = get_links()
    print(f"Loaded {len(links)} marketing entries\n")

    # Sample
    samples = [
        "Eldorado A107",
        "Royal Kahana 317: Default",
        "Kaanapali Royal B102: 2 Bedroom",
        ".LT Papakea J107: Default",
    ]
    for name in samples:
        ll = lookup(name)
        if ll:
            print(f"✓ {name}")
            print(f"    Airbnb: {ll.airbnb_url or '—'}")
            print(f"    VRBO:   {ll.vrbo_url or '—'}")
            print(f"    Hostaway ID: {ll.streamline_id}")
        else:
            print(f"✗ {name} — no match")
