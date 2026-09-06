#!/usr/bin/env python3
"""
KCity (Knoxville City) surge date DSO task generator.

Generates Date Specific Override tasks for UT Football weekends and
holiday surge dates across all active KCity listings, segmented by
bedroom count and demand level.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

SOURCE = "kcity_surge_dso"

# ---------------------------------------------------------------------------
# Surge calendar — all dates are 2025/2026 season
# ---------------------------------------------------------------------------

# Each entry: (start_iso, end_iso, label, demand_level)
# demand_level: "high" or "medium"
# 2025 season (historical reference — may already be in PriceLabs for 3BR 349115)
SURGE_DATES_2025: list[tuple[str, str, str, str]] = [
    ("2025-09-04", "2025-09-05", "UT Football", "high"),
    ("2025-09-18", "2025-09-19", "UT Football", "high"),
    ("2025-09-24", "2025-09-27", "UT Football", "high"),
    ("2025-10-01", "2025-10-04", "UT Football", "high"),
    ("2025-10-09", "2025-10-11", "UT Football", "high"),
    ("2025-10-15", "2025-10-17", "UT Football (BIGGEST)", "high"),
    ("2025-10-23", "2025-10-25", "UT Football", "high"),
    ("2025-10-29", "2025-11-01", "UT Football", "high"),
    ("2025-11-05", "2025-11-08", "UT Football", "high"),
    ("2025-11-13", "2025-11-15", "UT Football", "high"),
    ("2025-11-19", "2025-11-22", "UT Football – Rivalry", "high"),
    ("2025-12-11", "2025-12-12", "Holiday Weekend", "high"),
    ("2025-12-18", "2025-12-19", "Holiday Weekend", "high"),
    ("2025-12-24", "2025-12-27", "Christmas 2025", "high"),
    ("2025-09-06", "2025-09-07", "Post-UT Football", "medium"),
    ("2025-09-11", "2025-09-13", "UT Football Weekend", "medium"),
    ("2025-11-26", "2025-11-28", "Thanksgiving 2025", "medium"),
]

# 2026 season — UT Football schedule TBD; holiday dates are confirmed
# Football weekends follow the same Sept–Nov Saturday pattern each year.
# Update exact game dates once the 2026 schedule is released.
SURGE_DATES_2026: list[tuple[str, str, str, str]] = [
    # HIGH — UT Football weekends (approximate — update when schedule is released)
    # Sept windows trimmed to Fri-Sat only: Thu/Sun nights don't convert at surge floors
    ("2026-09-04", "2026-09-05", "UT Football Opening Weekend", "high"),
    ("2026-09-18", "2026-09-19", "UT Football", "high"),
    ("2026-09-25", "2026-09-26", "UT Football", "high"),
    ("2026-10-01", "2026-10-03", "UT Football", "high"),
    ("2026-10-08", "2026-10-10", "UT Football", "high"),
    ("2026-10-15", "2026-10-17", "UT Football (BIGGEST)", "high"),
    ("2026-10-22", "2026-10-24", "UT Football", "high"),
    ("2026-10-29", "2026-10-31", "UT Football", "high"),
    ("2026-11-05", "2026-11-07", "UT Football", "high"),
    ("2026-11-12", "2026-11-14", "UT Football", "high"),
    ("2026-11-19", "2026-11-21", "UT Football – Rivalry", "high"),
    ("2026-12-10", "2026-12-12", "Holiday Weekend", "high"),
    ("2026-12-17", "2026-12-19", "Holiday Weekend", "high"),
    ("2026-12-24", "2026-12-27", "Christmas 2026", "high"),
    # HIGH — New Year
    ("2026-12-31", "2027-01-01", "New Year's Eve 2026", "high"),
    # MEDIUM (Post-UT Sunday window removed; Sept weekend trimmed to Fri-Sat)
    ("2026-09-11", "2026-09-12", "UT Football Weekend", "medium"),
    ("2026-11-26", "2026-11-28", "Thanksgiving 2026", "medium"),
]

# 2027 season — UT Football schedule TBD (approximate Saturday pattern).
# IMPORTANT: PriceLabs has a blanket Mar 1–31 2027 DSO at $1,400 that must be
# DELETED before applying these event-specific DSOs, otherwise they will conflict.
# In PriceLabs: go to the listing calendar → find the Mar 1–31 override → delete it.
SURGE_DATES_2027: list[tuple[str, str, str, str]] = [
    # HIGH — Spring events (specific dates only — do NOT use broad monthly overrides)
    ("2027-03-31", "2027-04-04", "Big Ears Knoxville 2027", "high"),
    ("2027-04-08", "2027-04-11", "Knoxville Marathon 2027", "high"),
    # HIGH — UT Football weekends (approximate — update when 2027 schedule releases)
    ("2027-09-02", "2027-09-04", "UT Football Opening Weekend", "high"),
    ("2027-09-16", "2027-09-18", "UT Football", "high"),
    ("2027-09-23", "2027-09-25", "UT Football", "high"),
    ("2027-09-30", "2027-10-02", "UT Football", "high"),
    ("2027-10-07", "2027-10-09", "UT Football", "high"),
    ("2027-10-14", "2027-10-16", "UT Football (BIGGEST)", "high"),
    ("2027-10-21", "2027-10-23", "UT Football", "high"),
    ("2027-10-28", "2027-10-30", "UT Football", "high"),
    ("2027-11-04", "2027-11-06", "UT Football", "high"),
    ("2027-11-11", "2027-11-13", "UT Football", "high"),
    ("2027-11-18", "2027-11-20", "UT Football – Rivalry", "high"),
    # HIGH — Holiday season
    ("2027-12-09", "2027-12-11", "Holiday Weekend", "high"),
    ("2027-12-16", "2027-12-18", "Holiday Weekend", "high"),
    ("2027-12-24", "2027-12-27", "Christmas 2027", "high"),
    ("2027-12-31", "2028-01-01", "New Year's Eve 2027", "high"),
    # MEDIUM
    ("2027-09-05", "2027-09-06", "Post-UT Football", "medium"),
    ("2027-11-25", "2027-11-27", "Thanksgiving 2027", "medium"),
]

SURGE_DATES = SURGE_DATES_2025 + SURGE_DATES_2026 + SURGE_DATES_2027

# Per-event premium added on top of the bedroom threshold ($ per night).
# Matched by exact event label.
EVENT_PREMIUMS: dict[str, int] = {
    "Big Ears Knoxville 2027": 200,
}

# Fail LOUDLY at import time if an EVENT_PREMIUMS key no longer matches any
# event label in SURGE_DATES — a silent mismatch (typo, capitalization edit,
# renamed label) would otherwise just zero out the premium with no error.
_surge_event_labels = {event for _, _, event, _ in SURGE_DATES}
_unmatched_premium_keys = set(EVENT_PREMIUMS) - _surge_event_labels
if _unmatched_premium_keys:
    raise AssertionError(
        f"EVENT_PREMIUMS key(s) {sorted(_unmatched_premium_keys)} don't match any event label "
        f"in SURGE_DATES — the premium would silently apply to nothing. Fix the label to match."
    )

# ---------------------------------------------------------------------------
# Thresholds by bedroom count
# ---------------------------------------------------------------------------

# {bedrooms: {"medium": min_price, "high": min_price}}
THRESHOLDS: dict[int, dict[str, int]] = {
    1: {"medium": 590,  "high": 755},
    2: {"medium": 740,  "high": 890},
    3: {"medium": 850,  "high": 960},
    4: {"medium": 920,  "high": 1225},
}

# 3BR and 4BR medium dates don't reliably hit their thresholds per the analysis
SKIP_MEDIUM: set[int] = {3, 4}


DECAY_WINDOW_DAYS = 60    # start reducing price when event is this many days away
DECAY_OUTER_DAYS = 45     # outer band: 60–45 days out → $5/week
DECAY_PER_WEEK_OUTER = 5  # dollars/week in outer band (60–45 days)
DECAY_PER_WEEK_INNER = 10 # dollars/week in inner weekly band (45–30 days)
DECAY_DAILY_WINDOW = 30   # switch to per-day reduction inside this many days
DECAY_PER_DAY = 10        # dollars to reduce per day (within 30 days)


def _norm(value: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tokens(value: str) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _is_kcity(prop: Any) -> bool:
    """True if this listing belongs to the Knoxville KCity surge-pricing group.

    Matches on whole WORD tokens, not raw substrings — a plain substring test
    (e.g. "kcity" in "rockcity") would false-positive on unrelated listings
    like "Rock City View Cabin" (a real Gatlinburg-area attraction name whose
    normalized form "rockcity" contains "kcity"), or a person/pet name like
    "Knox" that isn't a place reference. Requiring a full token named "kcity"
    or starting with "knox" (Knox/Knoxville) avoids that while still matching
    every real KCity listing in the current portfolio (verified against the
    live CSV during this fix).
    """
    def _matches(tokens: set[str]) -> bool:
        return "kcity" in tokens or any(t.startswith("knox") for t in tokens)

    return (
        _matches(_tokens(getattr(prop, "name", "")))
        or _matches(_tokens(getattr(prop, "customization_group", "")))
        or _matches(_tokens(getattr(prop, "customization_sub_group", "")))
    )


def _beds(prop: Any) -> int:
    try:
        return int(getattr(prop, "bedrooms", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _decayed_price(base_price: int, start: date, today: date) -> tuple[int, int, int]:
    """Return (effective_price, reduction, periods_elapsed).

    60–45 days out: $5/week (outer band, gentle discount).
    45–30 days out: $10/week (inner weekly band, accelerating).
    <30 days out:   $10/day (daily, most aggressive).
    Outside window or past event: no reduction.
    """
    days_until = (start - today).days
    if days_until > DECAY_WINDOW_DAYS or days_until < 0:
        return base_price, 0, 0

    # Accumulated outer-band reduction (60→45 days, $5/week)
    outer_weeks = (DECAY_WINDOW_DAYS - DECAY_OUTER_DAYS) // 7  # = 2 weeks max
    outer_reduction = outer_weeks * DECAY_PER_WEEK_OUTER        # = $10 max

    # Accumulated inner-band weekly reduction (45→30 days, $10/week)
    inner_weeks = (DECAY_OUTER_DAYS - DECAY_DAILY_WINDOW) // 7  # = 2 weeks max
    inner_weekly_reduction = inner_weeks * DECAY_PER_WEEK_INNER  # = $20 max

    if days_until <= DECAY_DAILY_WINDOW:
        # Daily decay: $10/day elapsed into 30-day window, plus all accumulated weekly
        days_elapsed = DECAY_DAILY_WINDOW - days_until
        daily_reduction = (days_elapsed + 1) * DECAY_PER_DAY
        reduction = outer_reduction + inner_weekly_reduction + daily_reduction
        periods_elapsed = days_elapsed + 1

    elif days_until <= DECAY_OUTER_DAYS:
        # Inner weekly band (45–30 days): $10/week, plus full outer reduction
        weeks_elapsed = (DECAY_OUTER_DAYS - days_until) // 7 + 1
        reduction = outer_reduction + weeks_elapsed * DECAY_PER_WEEK_INNER
        periods_elapsed = weeks_elapsed

    else:
        # Outer band (60–45 days): $5/week
        weeks_elapsed = (DECAY_WINDOW_DAYS - days_until) // 7 + 1
        reduction = weeks_elapsed * DECAY_PER_WEEK_OUTER
        periods_elapsed = weeks_elapsed

    effective = max(base_price - reduction, 0)
    return effective, reduction, periods_elapsed


def generate_dso_tasks(portfolio: list[Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    kcity_props = [p for p in portfolio if getattr(p, "active", False) and _is_kcity(p)]

    actions: list[dict[str, Any]] = []
    skipped_past = 0
    skipped_medium_br = 0
    skipped_bedroom_count: dict[int, str] = {}  # unsupported BR count -> a sample listing name

    for start_str, end_str, event, demand in SURGE_DATES:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        if end < today:
            skipped_past += 1
            continue

        days_until = (start - today).days
        in_decay_window = 0 <= days_until <= DECAY_WINDOW_DAYS

        for prop in kcity_props:
            beds = _beds(prop)
            if beds not in THRESHOLDS:
                # No pricing tier defined for this bedroom count (e.g. a studio
                # or 5BR+ KCity listing) — record it instead of silently
                # dropping it with no visibility into what was skipped.
                skipped_bedroom_count.setdefault(beds, getattr(prop, "name", "unknown"))
                continue
            if demand == "medium" and beds in SKIP_MEDIUM:
                skipped_medium_br += 1
                continue

            event_premium = EVENT_PREMIUMS.get(event, 0)
            base_price = THRESHOLDS[beds][demand] + event_premium
            effective_price, reduction, periods_elapsed = _decayed_price(base_price, start, today)
            in_daily_mode = days_until <= DECAY_DAILY_WINDOW
            weeks_elapsed = periods_elapsed  # kept for template compat

            listing_id = str(getattr(prop, "listing_id", "") or "").strip()
            pms_name = str(getattr(prop, "pms_name", "") or "").strip()
            action_id = f"kcity_dso::{listing_id}::{start_str}::{end_str}::{demand}"

            decay_note = ""
            if in_decay_window and reduction > 0:
                if in_daily_mode:
                    decay_note = (
                        f" ⬇ DAILY decay (day {periods_elapsed} of 30-day window): "
                        f"-${reduction} from ${base_price:,} base → ${effective_price:,}"
                    )
                else:
                    decay_note = (
                        f" ⬇ Week {weeks_elapsed} of decay window: "
                        f"-${reduction} from ${base_price:,} base → ${effective_price:,}"
                    )

            actions.append({
                "id": action_id,
                "property": getattr(prop, "name", ""),
                "display_name": getattr(prop, "name", ""),
                "listing_id": listing_id,
                "pms_name": pms_name,
                "group": getattr(prop, "customization_group", "") or "",
                "subgroup": getattr(prop, "customization_sub_group", "") or "",
                "group_label": " / ".join(v for v in [
                    getattr(prop, "customization_group", "") or "",
                    getattr(prop, "customization_sub_group", "") or "",
                ] if v),
                "system": "PriceLabs DSO",
                "source": SOURCE,
                "type": f"kcity_dso_{demand}",
                "priority": "high" if demand == "high" else "medium",
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reviewed_at": None,
                "target_dates": f"{start_str} → {end_str}",
                "days_until_event": days_until,
                "in_decay_window": in_decay_window,
                "decay_weeks_elapsed": weeks_elapsed,
                "decay_reduction": reduction,
                "base_min_price": base_price,
                "effective_min_price": effective_price,
                "event_premium": event_premium,
                "suggestion": (
                    f"Set {demand.upper()} DSO for {event} ({start_str} → {end_str})"
                    + (f" — includes +${event_premium}/night event premium" if event_premium else "")
                    + (f" — decay week {weeks_elapsed}, floor reduced by ${reduction}" if reduction else "")
                ),
                "current_value": f"{beds}BR listing · no DSO set",
                "proposed_value": (
                    f"Min price ${effective_price:,}"
                    + (f" (decayed from ${base_price:,})" if reduction else "")
                    + f" · {start_str} → {end_str}"
                ),
                "adjustment": (
                    f"${effective_price:,} floor · {demand} demand"
                    + (f" · -${reduction} decay wk {weeks_elapsed}" if reduction else "")
                ),
                "reason": (
                    f"{beds}BR KCity listing · {event} · "
                    f"demand={demand.upper()} · base threshold=${base_price:,}"
                    + (
                        f" · {days_until}d until event (decay wk {weeks_elapsed}): "
                        f"-${reduction} → effective floor ${effective_price:,}"
                        if reduction else
                        f" (>{THRESHOLDS[beds]['medium']:,} med / >{THRESHOLDS[beds]['high']:,} high)"
                    )
                ),
                "implementation": (
                    (
                        "⚠️ FIRST: Delete the existing Mar 1–31, 2027 blanket DSO in PriceLabs before setting this override — otherwise it will conflict. "
                        if start_str >= "2027-03-01" and end_str <= "2027-04-30" else ""
                    )
                    + f"In PriceLabs, open this listing's calendar, select {start_str} → {end_str}, "
                    f"and set a minimum price DSO of ${effective_price:,}."
                    + (f" (Includes +${event_premium}/night {event} premium.)" if event_premium else "")
                    + (f" (Base floor ${base_price:,} reduced by ${reduction} — period {weeks_elapsed} of {DECAY_WINDOW_DAYS}-day decay window.)" if reduction else "")
                    + " Do not set a fixed price — allow the algorithm to go higher."
                ),
                "bedrooms": beds,
                "demand_level": demand,
                "event_label": event,
                "pricelabs_payload": {
                    "kind": "kcity_dso_min_price",
                    "start_date": start_str,
                    "end_date": end_str,
                    "min_price": effective_price,
                    "base_min_price": base_price,
                    "decay_reduction": reduction,
                },
            })

    # Sort: high demand first, then by date, then by bedroom count
    rank = {"high": 0, "medium": 1}
    actions.sort(key=lambda a: (rank.get(a["demand_level"], 9), a["target_dates"], a["bedrooms"]))

    future_surges = [s for s in SURGE_DATES if date.fromisoformat(s[1]) >= today]
    in_decay = [a for a in actions if a["in_decay_window"] and a["decay_reduction"] > 0]
    summary = {
        "total_listings": len(kcity_props),
        "total_tasks": len(actions),
        "high_tasks": sum(1 for a in actions if a["demand_level"] == "high"),
        "medium_tasks": sum(1 for a in actions if a["demand_level"] == "medium"),
        "surge_windows": len(future_surges),
        "skipped_past": skipped_past,
        "decay_window_days": DECAY_WINDOW_DAYS,
        "decay_outer_days": DECAY_OUTER_DAYS,
        "decay_daily_window": DECAY_DAILY_WINDOW,
        "decay_per_week_outer": DECAY_PER_WEEK_OUTER,
        "decay_per_week_inner": DECAY_PER_WEEK_INNER,
        "decay_per_day": DECAY_PER_DAY,
        "tasks_in_decay_window": len(in_decay),
        "tasks_in_daily_decay": sum(1 for a in actions if a.get("days_until_event", 999) <= DECAY_DAILY_WINDOW and a.get("decay_reduction", 0) > 0),
        "bedrooms_breakdown": {
            str(br): sum(1 for a in actions if a["bedrooms"] == br)
            for br in sorted(THRESHOLDS)
        },
        "skipped_no_pricing_tier": [
            {"bedrooms": br, "example_listing": name}
            for br, name in sorted(skipped_bedroom_count.items())
        ],
    }
    return {"ok": True, "actions": actions, "summary": summary}
