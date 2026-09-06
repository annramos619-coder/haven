#!/usr/bin/env python3
"""Reservation data from PriceLabs, shaped like Hostaway's reservation records.

The dashboard originally read reservations from the Hostaway API. Haven's
Hostaway token returns 403 ("The resource owner or authorization server denied
the request"), and PriceLabs already holds the same reservations for every
listing it syncs, so this module replaces that source.

Records are returned in Hostaway's field shape so existing callers do not need
to change how they read them.

The Customer API reservation shape carries cleaning fee and guest count as
well as arrival, departure, booking timestamp, rental and total revenue,
channel commission and booking channel. The Report Builder bookings-report
shape omits cleaning fee and guest count, which then come back 0.0 and None.

Guest reviews have no PriceLabs equivalent at all.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

from pricelabs_api import PriceLabsAPIError, client_from_env

# PriceLabs Customer API path for reservation-level data. Overridable because
# the Customer API surface differs by account tier.
RESERVATION_PATH = os.environ.get("PRICELABS_RESERVATION_PATH", "/reservation_data")

DEFAULT_PMS = os.environ.get("PRICELABS_PMS_NAME", "hostaway")

# PriceLabs paginates; it caps page size server-side.
PAGE_LIMIT = 500
MAX_PAGES = 40

# CSV fallback, same pattern as the Report Builder cache: written after a
# successful live pull, and used when the Customer API is unreachable or
# returns nothing. Lets reservation data be supplied out-of-band when the
# account key cannot reach the reservation endpoint.
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE_PATH = HERE / "reservations_cache.csv"


def _epoch_ms_to_date(value: Any) -> str:
    """PriceLabs returns dates as epoch milliseconds. Return YYYY-MM-DD, or ''."""
    if value in (None, "", 0):
        return ""
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _epoch_ms_to_iso(value: Any) -> str:
    """Full timestamp, kept because booking time-of-day is analytically useful."""
    if value in (None, "", 0):
        return ""
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso_date(value: Any) -> str:
    """Accept epoch ms, ISO timestamps, or "15 Sep 2026". Return YYYY-MM-DD."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)):
        return _epoch_ms_to_date(value)
    text = str(value).strip()
    if not text:
        return ""
    if text[:4].isdigit() and "-" in text:
        return text[:10]
    for fmt in ("%d %b %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _iso_timestamp(value: Any) -> str:
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)):
        return _epoch_ms_to_iso(value)
    return str(value).strip()


def to_hostaway_shape(row: dict[str, Any]) -> dict[str, Any]:
    """Map one PriceLabs reservation onto Hostaway's field names.

    Handles both PriceLabs reservation shapes, which do not share field names:

      Customer API /reservation_data : check_in, check_out, booked_date (ISO),
          booking_channel, booking_status, cleaning_fees, guest_count
      Report Builder bookings report : start_date, end_date, booked_time
          (epoch ms), booking_source, status

    Reading only the second shape is what left the booking calendar empty -
    every date came back blank and the caller skipped the row.
    """
    arrival = _iso_date(row.get("check_in") or row.get("start_date_parsed") or row.get("start_date"))
    departure = _iso_date(row.get("check_out") or row.get("end_date_parsed") or row.get("end_date"))
    booked = _iso_timestamp(row.get("booked_date") or row.get("booked_time"))

    rental = _num(row.get("rental_revenue"))
    total = _num(row.get("total_cost")) or rental

    return {
        "id": row.get("reservation_id") or "",
        "listingMapId": row.get("listing_id") or "",
        "arrivalDate": arrival,
        "departureDate": departure,
        # Hostaway callers read createdAt as the booking timestamp.
        "createdAt": booked,
        "channelName": row.get("booking_channel") or row.get("booking_source") or "direct",
        "status": row.get("booking_status") or row.get("status") or "booked",
        "money": {
            "rentalRevenue": rental,
            "totalPrice": total,
            # Present on the Customer API shape, absent from the bookings report.
            "cleaningFee": _num(row.get("cleaning_fees")),
            "channelFee": _num(row.get("ota_commission")),
        },
        "guestCount": row.get("guest_count"),
        # Passed through so callers can use them without re-deriving.
        "_nights": row.get("no_of_days"),
        "_lead_time": row.get("lead_time"),
        "_bedroom_count": row.get("bedroom_count"),
        "_group_name": row.get("group_name"),
        "_city": row.get("city"),
        "_listing_name": row.get("listing_name"),
        "_cancelled_on": row.get("cancelled_on"),
    }


def fetch_raw(
    listing_id: str | None = None,
    pms: str = DEFAULT_PMS,
    booked_start: str | None = None,
    booked_end: str | None = None,
    stay_start: str | None = None,
    stay_end: str | None = None,
) -> list[dict[str, Any]]:
    """Page through PriceLabs reservation data. Returns raw PriceLabs rows.

    At least one date range must be given — PriceLabs rejects an unbounded
    query. Booked-date and stay-date ranges may be combined.
    """
    if not any((booked_start, booked_end, stay_start, stay_end)):
        raise PriceLabsAPIError(
            "A date range is required: pass booked_start/booked_end or stay_start/stay_end."
        )

    client = client_from_env()
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page in range(MAX_PAGES):
        params: dict[str, Any] = {
            "pms": pms,
            "limit": PAGE_LIMIT,
            "offset": page * PAGE_LIMIT,
        }
        if listing_id:
            params["listing_id"] = str(listing_id)
        if booked_start:
            params["booked_start_date"] = booked_start
        if booked_end:
            params["booked_end_date"] = booked_end
        if stay_start:
            params["start_date"] = stay_start
        if stay_end:
            params["end_date"] = stay_end

        body = client.request("GET", RESERVATION_PATH, params=params)

        # PriceLabs nests the payload differently across endpoints.
        rows = body
        for key in ("data", "reservations"):
            if isinstance(rows, dict) and key in rows:
                rows = rows[key]
        if isinstance(rows, dict):
            rows = rows.get("reservations") or []
        if not isinstance(rows, list):
            raise PriceLabsAPIError(
                f"Unexpected reservation payload from PriceLabs at {RESERVATION_PATH}: "
                f"got {type(rows).__name__}. Set PRICELABS_RESERVATION_PATH if your "
                "account uses a different endpoint."
            )

        new = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("reservation_id") or "")
            # Reservation IDs repeat across pages when the server re-sorts;
            # de-duplicate rather than double-count revenue.
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            collected.append(row)
            new += 1

        # The Customer API signals continuation with next_page; the bookings
        # report just returns a short page.
        more = body.get("next_page") if isinstance(body, dict) else None
        if more is False or len(rows) < PAGE_LIMIT or new == 0:
            break

    return collected


def reservations_for_listing(
    listing_id: str,
    pms: str = DEFAULT_PMS,
    years_back: int = 2,
) -> list[dict[str, Any]]:
    """Reservations for one listing, in Hostaway shape.

    Queried by stay date so year-over-year comparisons see completed stays,
    covering this year and the previous `years_back` years.
    """
    today = dt.date.today()
    start = today.replace(year=today.year - years_back, month=1, day=1)
    end = today.replace(year=today.year + 1, month=12, day=31)
    try:
        rows = fetch_raw(
            listing_id=listing_id,
            pms=pms,
            stay_start=start.isoformat(),
            stay_end=end.isoformat(),
        )
    except Exception:
        rows = []
    if not rows:
        # The live call failed or returned nothing. Fall back to the cache
        # rather than rendering an empty calendar as if the listing genuinely
        # had no reservations.
        rows = _cached_rows_for(listing_id)
    return [to_hostaway_shape(r) for r in rows]


def all_reservations(
    pms: str = DEFAULT_PMS,
    years_back: int = 2,
) -> list[dict[str, Any]]:
    """Every reservation across the portfolio, in Hostaway shape."""
    today = dt.date.today()
    start = today.replace(year=today.year - years_back, month=1, day=1)
    end = today.replace(year=today.year + 1, month=12, day=31)
    try:
        rows = fetch_raw(pms=pms, stay_start=start.isoformat(), stay_end=end.isoformat())
    except Exception:
        rows = []
    if not rows:
        rows = _cached_rows_for()
    return [to_hostaway_shape(r) for r in rows]


def last_booked_by_listing(
    pms: str = DEFAULT_PMS,
    days_back: int = 365,
) -> dict[str, int]:
    """Days since each listing's most recent booking, keyed by listing ID.

    Replaces the Hostaway call of the same name. A listing with no booking in
    the window is absent from the result rather than present with a wrong value.
    """
    today = dt.date.today()
    rows = fetch_raw(
        pms=pms,
        booked_start=(today - dt.timedelta(days=days_back)).isoformat(),
        booked_end=today.isoformat(),
    )

    latest: dict[str, dt.date] = {}
    for row in rows:
        lid = str(row.get("listing_id") or "").strip()
        booked = _epoch_ms_to_date(row.get("booked_time"))
        if not lid or not booked:
            continue
        try:
            booked_date = dt.date.fromisoformat(booked)
        except ValueError:
            continue
        if lid not in latest or booked_date > latest[lid]:
            latest[lid] = booked_date

    return {lid: (today - d).days for lid, d in latest.items()}


def booking_stats_by_listing(
    pms: str = DEFAULT_PMS,
    windows: tuple[int, ...] = (7, 14),
) -> dict[str, dict[str, Any]]:
    """Recent booking activity per listing, derived from PriceLabs booking times.

    Replaces the Hostaway reservation-stats cache. For each window (in days
    back from today) returns the number of reservations created and a
    breakdown by booking source, keyed by listing ID:

        {"187612": {"pickup_7d_reservations": 3,
                    "sources_7d": {"airbnbOfficial": 2, "bookingcom": 1},
                    "pickup_14d_reservations": 5,
                    "sources_14d": {...}}}
    """
    today = dt.date.today()
    longest = max(windows)
    rows = fetch_raw(
        pms=pms,
        booked_start=(today - dt.timedelta(days=longest)).isoformat(),
        booked_end=today.isoformat(),
    )

    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        lid = str(row.get("listing_id") or "").strip()
        booked = _epoch_ms_to_date(row.get("booked_time"))
        if not lid or not booked:
            continue
        try:
            age = (today - dt.date.fromisoformat(booked)).days
        except ValueError:
            continue
        source = str(row.get("booking_source") or "direct")
        entry = stats.setdefault(lid, {})
        for window in windows:
            if age > window:
                continue
            count_key = f"pickup_{window}d_reservations"
            src_key = f"sources_{window}d"
            entry[count_key] = entry.get(count_key, 0) + 1
            sources = entry.setdefault(src_key, {})
            sources[source] = sources.get(source, 0) + 1

    return stats


# --------------------------------------------------------------------------- cache

_CACHE_COLUMNS = [
    "reservation_id", "listing_id", "start_date", "end_date", "booked_time",
    "rental_revenue", "total_cost", "ota_commission", "booking_source",
    "status", "no_of_days", "lead_time", "bedroom_count", "group_name",
    "city", "listing_name",
]


def write_cache(rows: list[dict[str, Any]], path: Path | None = None) -> Path:
    """Persist raw PriceLabs reservation rows as CSV."""
    import csv

    target = path or CACHE_PATH
    with open(target, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CACHE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in _CACHE_COLUMNS})
    return target


def read_cache(path: Path | None = None) -> list[dict[str, Any]]:
    """Load cached raw rows, restoring numeric types CSV loses. [] when absent."""
    import csv

    target = path or CACHE_PATH
    if not target.exists():
        return []
    with open(target, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    numeric = {
        "start_date", "end_date", "booked_time", "rental_revenue", "total_cost",
        "ota_commission", "no_of_days", "lead_time", "bedroom_count",
    }
    for row in rows:
        for key in numeric:
            value = row.get(key)
            if value in ("", None):
                row[key] = None
                continue
            try:
                row[key] = int(value)
            except (TypeError, ValueError):
                try:
                    row[key] = float(value)
                except (TypeError, ValueError):
                    pass
    return rows


def _cached_rows_for(listing_id: str | None = None) -> list[dict[str, Any]]:
    rows = read_cache()
    if listing_id is None:
        return rows
    wanted = str(listing_id).strip()
    return [r for r in rows if str(r.get("listing_id") or "").strip() == wanted]


def source_label() -> str:
    """Where reservation data would currently come from - for display."""
    if CACHE_PATH.exists():
        import datetime as _dt
        age = (_dt.datetime.now().timestamp() - CACHE_PATH.stat().st_mtime) / 86400
        return f"cache {CACHE_PATH.name}, {age:.1f} days old"
    return "live PriceLabs API"
