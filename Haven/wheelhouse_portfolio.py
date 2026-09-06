#!/usr/bin/env python3
"""
Wheelhouse portfolio loader — parses the exported settings CSV and
computes per-property urgency scores for the portfolio dashboard.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

CSV_PATH = Path(__file__).parent / "pricelabs_portfolio.csv"

INACTIVE_PREFIXES = (".LT", ".ONB", ".lt", ".onb", ".")
ONBOARDING_PREFIXES = (".ONB", ".onb")


@dataclass
class Property:
    name: str
    tags: str
    bedrooms: int
    automation: bool
    base_price: float
    last_booked_days: Optional[int]   # None = never / blank
    adj_occ_60d: float                # 0.0–1.0
    adj_occ_90d: float
    min_price_occ_60d: float
    min_price_occ_90d: float
    booked_7d: int
    booked_14d: int
    min_price: float
    max_price: Optional[float]
    min_stay: int
    last_minute: str
    far_future: str
    day_of_week: str
    seasonality: str
    demand_sensitivity: int
    historical_anchoring: int
    long_term_pricing: str
    gaps_adjacencies: str
    checkin_checkout: str
    occupancy_pacing: str
    events_seasons: str
    city: str = ""
    customization_group: str = ""
    customization_sub_group: str = ""
    listing_id: str = ""
    pms_name: str = ""
    adj_occ_30d: float = 0.0
    adj_occ_45d: float = 0.0
    adj_occ_120d: float = 0.0
    adj_occ_180d: float = 0.0
    reservations_30d: int = 0
    reservations_15d: int = 0

    # Derived
    active: bool = True
    onboarding: bool = False
    urgency: str = "ok"          # "critical" | "warning" | "ok" | "overperforming" | "onboarding"
    urgency_score: int = 0       # 0 = worst, 100 = best
    issues: list = field(default_factory=list)
    calendar_rates: list[tuple[str, float]] = field(default_factory=list)

    # Location tags
    @property
    def side(self) -> str:
        return self.city or self.customization_group or "Other"

    @property
    def area(self) -> str:
        if self.customization_sub_group:
            return self.customization_sub_group
        if self.customization_group:
            return self.customization_group
        if self.city:
            return self.city
        for tag in self.tags.split(";"):
            tag = tag.strip()
            if tag.startswith("RL "):
                return tag[3:]
            if tag in ("Lahaina 0B", "Lahaina 1B", "Lahaina 2B", "Lahaina 3B"):
                return "Lahaina"
        return self.tags.split(";")[0].strip() if self.tags else ""

    @property
    def property_name(self) -> str:
        """Clean name without 'Default' suffix."""
        return self.name.replace(": Default", "").strip()

    @property
    def owner_restrictions(self) -> list[str]:
        tags = [t.strip() for t in self.tags.split(";") if t.strip()]
        restrictions = []
        for tag in tags:
            low = tag.lower()
            if any(token in low for token in ("no promotion", "no promo", "fixed min", "owner", "stice unit")):
                restrictions.append(tag)
        return restrictions

    @property
    def no_promotions(self) -> bool:
        return any("no promo" in r.lower() or "no promotion" in r.lower() for r in self.owner_restrictions)

    @property
    def fixed_min_rate(self) -> bool:
        return any("fixed min" in r.lower() for r in self.owner_restrictions)


def _parse_float(val: str, default: float = 0.0) -> float:
    val = val.replace("%", "").replace(",", "").strip()
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _parse_int(val: str, default: int = 0) -> int:
    try:
        return int(float(val.strip())) if val.strip() else default
    except ValueError:
        return default


def _parse_optional_int(val: str) -> Optional[int]:
    try:
        return int(float(val.strip())) if val.strip() else None
    except ValueError:
        return None


def _parse_last_booked_days(val: str) -> Optional[int]:
    """Parse a date string like '27 May 2026' into days since today."""
    raw = val.strip()
    if not raw:
        return None
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return (date.today() - date(*__import__('time').strptime(raw, fmt)[:3])).days
        except (ValueError, TypeError):
            pass
    return None


def _parse_rate(val: str) -> float:
    """Parse rate values that may be exported as 0.18, 18, or "18 %" for 18%.

    When a literal percent sign is present (confirmed to always be the case
    in PriceLabs' occupancy export columns) the value is unambiguously a
    whole-number percentage — no guessing needed. Without a percent sign,
    fall back to the >1 heuristic for other numeric-only sources.
    """
    raw = str(val or "")
    if "%" in raw:
        return _parse_float(raw) / 100
    rate = _parse_float(raw)
    return rate / 100 if rate > 1 else rate


def _truthy(val: str) -> bool:
    return str(val or "").strip().lower() in {"true", "yes", "y", "1", "active", "enabled"}


def _pick(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row:
            return row.get(name, "")
    return ""


def _is_date_header(val: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", val.strip()))


def _date_rate_pairs(header: list[str], row: list[str]) -> list[tuple[str, float]]:
    pairs: list[tuple[str, float]] = []
    for idx, label in enumerate(header):
        if idx >= len(row) or not _is_date_header(label):
            continue
        raw = row[idx].strip()
        if not raw:
            continue
        rate = _parse_float(raw)
        if rate > 0:
            pairs.append((label.strip(), rate))
    return pairs


def _score_property(prop: Property) -> Property:
    issues = []
    score = 100

    if prop.onboarding:
        prop.issues = ["New/onboarding listing"]
        prop.urgency = "onboarding"
        prop.urgency_score = 70
        return prop

    # ── Tiers based SOLELY on occupancy next 60 days ──────────────────────────
    #   ≥85% → overperforming (FAST) · <20% → critical · <40% → warning · else OK
    # urgency_score = occupancy percentage, so sorting ascending = emptiest first.
    occ_fwd = prop.adj_occ_60d
    score = round(occ_fwd * 100)

    if occ_fwd >= 0.85:
        prop.issues = ["Overperforming: review for underpricing or overly aggressive discounts"]
        prop.urgency = "overperforming"
        prop.urgency_score = score
        return prop

    if occ_fwd == 0.0:
        issues.append("0% occupancy next 60 days")
    elif occ_fwd < 0.20:
        issues.append(f"Very low occupancy next 60 days: {occ_fwd:.0%}")
    elif occ_fwd < 0.40:
        issues.append(f"Below-target occupancy next 60 days: {occ_fwd:.0%}")

    # Informational context only — does NOT affect tier or ranking
    if prop.booked_7d == 0 and prop.booked_14d == 0:
        issues.append("No bookings in next 14 days")
    if prop.last_booked_days is None:
        issues.append("No booking on record")
    elif prop.last_booked_days > 30:
        issues.append(f"Last booked {prop.last_booked_days} days ago")
    if prop.long_term_pricing == "Disabled":
        issues.append("Long-term pricing disabled")
    if prop.occupancy_pacing == "Disabled":
        issues.append("Occupancy pacing disabled")
    if prop.gaps_adjacencies == "Disabled":
        issues.append("Gap/adjacency filling disabled")

    if occ_fwd < 0.20:
        urgency = "critical"
    elif occ_fwd < 0.40:
        urgency = "warning"
    else:
        urgency = "ok"

    prop.issues = issues
    prop.urgency = urgency
    prop.urgency_score = score
    return prop


def _load_pricelabs_portfolio(header: list[str], rows) -> list[Property]:
    properties: list[Property] = []

    for values in rows:
        row = {name: values[idx] if idx < len(values) else "" for idx, name in enumerate(header)}
        name = _pick(row, "Listing Name").strip()
        if not name:
            continue

        # User requirement: never include listings that are not syncing in PriceLabs.
        if not _truthy(_pick(row, "Listing Sync")):
            continue

        # Hide rows PriceLabs marks as hidden/offboarded from the dashboard list.
        if "Show Listing" in row and not _truthy(_pick(row, "Show Listing")):
            continue

        status = _pick(row, "Listing Status").strip().lower()
        if status and status not in {"available", "active", "listed"}:
            continue

        tags = _pick(row, "Tags")
        city = _pick(row, "City")
        group = _pick(row, "Customization Group")
        subgroup = _pick(row, "Customization Sub Group")
        tags_joined = "; ".join(v for v in (city, group, subgroup, tags) if v)

        base_price = _parse_float(_pick(row, "Base Price"))
        recommended_base = _parse_float(_pick(row, "Recommended Base Price"))
        if not base_price and recommended_base:
            base_price = recommended_base

        # Same onboarding-prefix convention as the legacy parser below — PriceLabs
        # has no dedicated "onboarding" column, so this is the only available signal.
        onboarding = any(name.strip().startswith(p) for p in ONBOARDING_PREFIXES)

        prop = Property(
            name=name,
            tags=tags_joined,
            bedrooms=_parse_int(_pick(row, "Bedroom Count")),
            automation=True,
            base_price=base_price,
            last_booked_days=_parse_last_booked_days(_pick(row, "Last Booked Date")) or _parse_optional_int(_pick(row, "Last Booked Days")),
            adj_occ_60d=_parse_rate(_pick(row, "Total Occupancy ( Next 60 Days )") or _pick(row, "Total Occupancy (Next 60 Days)")),
            adj_occ_90d=_parse_rate(_pick(row, "Total Occupancy ( Next 90 Days )") or _pick(row, "Total Occupancy (Next 90 Days)")),
            adj_occ_30d=_parse_rate(_pick(row, "Total Occupancy ( Next 30 Days )") or _pick(row, "Total Occupancy (Next 30 Days)")),
            adj_occ_45d=_parse_rate(_pick(row, "Adjusted Occupancy ( Next 45 Days )") or _pick(row, "Adjusted Occupancy (Next 45 Days)")),
            reservations_15d=_parse_int(_pick(row, "Reservations ( Past 15 Days )") or _pick(row, "Reservations (Past 15 Days)")),
            adj_occ_120d=_parse_rate(_pick(row, "Total Occupancy ( Next 120 Days )") or _pick(row, "Total Occupancy (Next 120 Days)")),
            adj_occ_180d=_parse_rate(_pick(row, "Total Occupancy ( Next 180 Days )") or _pick(row, "Total Occupancy (Next 180 Days)")),
            reservations_30d=_parse_int(_pick(row, "Reservations ( Past 30 Days )") or _pick(row, "Reservations (Past 30 Days)") or _pick(row, "Bookings Last 30 Days")),
            min_price_occ_60d=0,
            min_price_occ_90d=0,
            booked_7d=_parse_int(_pick(row, "Nights Booked ( Past 7 Days )")),
            booked_14d=_parse_int(_pick(row, "Nights Booked ( Past 15 Days )")),
            min_price=_parse_float(_pick(row, "Min Price")),
            max_price=None,
            min_stay=0,
            last_minute="Unknown",
            far_future="Unknown",
            day_of_week="Unknown",
            seasonality="Unknown",
            demand_sensitivity=0,
            historical_anchoring=0,
            long_term_pricing="Unknown",
            gaps_adjacencies="Unknown",
            checkin_checkout="Unknown",
            occupancy_pacing="Unknown",
            events_seasons="Unknown",
            city=city,
            customization_group=group,
            customization_sub_group=subgroup,
            listing_id=_pick(row, "Listing ID").strip(),
            pms_name=_pick(row, "PMS Name").strip(),
            active=True,
            onboarding=onboarding,
            calendar_rates=[],
        )
        # Estimate 120d/180d from 60d→90d trend if CSV doesn't supply them
        if prop.adj_occ_120d == 0.0 and prop.adj_occ_90d > 0:
            trend = prop.adj_occ_90d - prop.adj_occ_60d  # change per 30 days
            prop.adj_occ_120d = max(0.0, min(1.0, prop.adj_occ_90d + trend))
            prop.adj_occ_180d = max(0.0, min(1.0, prop.adj_occ_90d + trend * 3))
        _score_property(prop)
        properties.append(prop)

    tier_order = {"critical": 0, "warning": 1, "onboarding": 2, "overperforming": 3, "ok": 4}
    properties.sort(key=lambda p: (tier_order.get(p.urgency, 4), p.urgency_score, p.property_name.lower()))
    return properties


def load_portfolio(csv_path: Path | None = None) -> list[Property]:
    """
    Parse pricelabs_portfolio.csv and return active properties sorted by urgency
    (critical first, then warning, then ok), with lowest score first within each tier.
    """
    properties: list[Property] = []

    source = csv_path or CSV_PATH
    if not source.exists():
        return properties

    with open(source, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        first_row = next(reader, [])
        if "Listing ID" in first_row and "Listing Sync" in first_row:
            return _load_pricelabs_portfolio(first_row, reader)

        header = next(reader)  # legacy row 2: column names
        has_legacy_settings = len(header) > 26 and not _is_date_header(header[12])

        for row in reader:
            if len(row) < 26:
                continue

            name = row[0].strip()
            if not name:
                continue

            # Inactive check
            onboarding = any(name.startswith(p) for p in ONBOARDING_PREFIXES)
            active = not any(name.startswith(p) for p in INACTIVE_PREFIXES) or onboarding

            prop = Property(
                name=name,
                tags=row[1].strip(),
                bedrooms=_parse_int(row[2]),
                automation=row[3].strip().lower() == "true",
                base_price=_parse_float(row[4]),
                last_booked_days=_parse_optional_int(row[5]),
                adj_occ_60d=_parse_rate(row[6]),
                adj_occ_90d=_parse_rate(row[7]),
                min_price_occ_60d=_parse_rate(row[8]),
                min_price_occ_90d=_parse_rate(row[9]),
                booked_7d=_parse_int(row[10]),
                booked_14d=_parse_int(row[11]),
                min_price=_parse_float(row[13]) if has_legacy_settings and len(row) > 13 else 0,
                max_price=_parse_float(row[14]) if has_legacy_settings and len(row) > 14 and row[14].strip() else None,
                min_stay=_parse_int(row[15], default=0) if has_legacy_settings and len(row) > 15 else 0,
                last_minute=row[16].strip() if has_legacy_settings and len(row) > 16 else "Unknown",
                far_future=row[17].strip() if has_legacy_settings and len(row) > 17 else "Unknown",
                day_of_week=row[18].strip() if has_legacy_settings and len(row) > 18 else "Unknown",
                seasonality=row[19].strip() if has_legacy_settings and len(row) > 19 else "Unknown",
                demand_sensitivity=_parse_int(row[20], default=100) if has_legacy_settings and len(row) > 20 else 0,
                historical_anchoring=_parse_int(row[21], default=0) if has_legacy_settings and len(row) > 21 else 0,
                long_term_pricing=row[22].strip() if has_legacy_settings and len(row) > 22 else "Unknown",
                gaps_adjacencies=row[23].strip() if has_legacy_settings and len(row) > 23 else "Unknown",
                checkin_checkout=row[24].strip() if has_legacy_settings and len(row) > 24 else "Unknown",
                occupancy_pacing=row[25].strip() if has_legacy_settings and len(row) > 25 else "Unknown",
                events_seasons=row[26].strip() if has_legacy_settings and len(row) > 26 else "Unknown",
                active=active,
                onboarding=onboarding,
                calendar_rates=_date_rate_pairs(header, row),
            )

            if active:
                _score_property(prop)

            properties.append(prop)

    # Sort: active criticals first, then warnings, onboarding, overperforming, ok; inactive last
    tier_order = {"critical": 0, "warning": 1, "onboarding": 2, "overperforming": 3, "ok": 4}
    properties.sort(
        key=lambda p: (
            0 if p.active else 1,
            tier_order.get(p.urgency, 2) if p.active else 3,
            p.urgency_score if p.active else 100,
        )
    )
    return properties


def portfolio_summary(properties: list[Property]) -> dict:
    active = [p for p in properties if p.active]
    inactive = [p for p in properties if not p.active]

    critical = [p for p in active if p.urgency == "critical"]
    warning = [p for p in active if p.urgency == "warning"]
    onboarding = [p for p in active if p.urgency == "onboarding"]
    overperforming = [p for p in active if p.urgency == "overperforming"]
    ok = [p for p in active if p.urgency == "ok"]

    zero_14d = [p for p in active if p.booked_14d == 0]
    zero_7d = [p for p in active if p.booked_7d == 0]

    avg_occ_60d = (
        sum(p.adj_occ_60d for p in active) / len(active) if active else 0
    )

    no_last_booked = [p for p in active if p.last_booked_days is None]
    stale_60d = [p for p in active if p.last_booked_days is not None and p.last_booked_days > 60]

    lt_disabled = sum(1 for p in active if p.long_term_pricing == "Disabled")
    pacing_disabled = sum(1 for p in active if p.occupancy_pacing == "Disabled")

    west_count = sum(1 for p in active if p.side == "West")
    south_count = sum(1 for p in active if p.side == "South")

    return {
        "total_active": len(active),
        "total_inactive": len(inactive),
        "critical_count": len(critical),
        "warning_count": len(warning),
        "onboarding_count": len(onboarding),
        "overperforming_count": len(overperforming),
        "ok_count": len(ok),
        "zero_bookings_7d": len(zero_7d),
        "zero_bookings_14d": len(zero_14d),
        "avg_occ_60d": round(avg_occ_60d * 100, 1),
        "no_booking_record": len(no_last_booked),
        "stale_60d_plus": len(stale_60d),
        "long_term_pricing_disabled": lt_disabled,
        "occupancy_pacing_disabled": pacing_disabled,
        "west_side_count": west_count,
        "south_side_count": south_count,
    }


if __name__ == "__main__":
    props = load_portfolio()
    summary = portfolio_summary(props)
    print(f"\n{'='*60}")
    print(f"PORTFOLIO SUMMARY — {summary['total_active']} active properties")
    print(f"{'='*60}")
    print(f"  Critical : {summary['critical_count']}")
    print(f"  Warning  : {summary['warning_count']}")
    print(f"  OK       : {summary['ok_count']}")
    print(f"  Inactive : {summary['total_inactive']}")
    print(f"\n  0 bookings next 7d  : {summary['zero_bookings_7d']}")
    print(f"  0 bookings next 14d : {summary['zero_bookings_14d']}")
    print(f"  Avg occ 60d         : {summary['avg_occ_60d']}%")
    print(f"\n  LT pricing disabled : {summary['long_term_pricing_disabled']}")
    print(f"  Occ pacing disabled : {summary['occupancy_pacing_disabled']}")
    print(f"\nTop 10 most urgent:")
    for p in props[:10]:
        print(f"  [{p.urgency.upper():<8}] {p.property_name:<45} score={p.urgency_score}")
