#!/usr/bin/env python3
"""
Portfolio-wide high-demand date calendar — national/regional holidays and
Smoky Mountains seasonal peaks that apply across ALL customization groups
(Gatlinburg, Pigeon Forge, Sevierville, luxury cabins, etc.), as opposed to
kcity_surge_dso.py's Knoxville-specific UT Football / Big Ears events.

Used by the Demand Radar (/api/demand-radar) to answer: for each upcoming
high-demand window, is a given group's forward occupancy strong enough to
raise rates, or soft enough that LOS/rate should come down to drive bookings.

Dates are computed exactly (nth-weekday-of-month rules), not guessed, and
verified against Python's calendar module before being hardcoded here.
"""

from __future__ import annotations

# Each entry: (start_iso, end_iso, label, demand_level)
# demand_level: "high" or "medium"
HIGH_DEMAND_DATES: list[tuple[str, str, str, str]] = [
    # ── 2026 (remainder of year) ──────────────────────────────────────────
    ("2026-09-04", "2026-09-07", "Labor Day Weekend 2026", "high"),
    ("2026-10-10", "2026-10-31", "Fall Foliage Peak 2026", "high"),
    ("2026-11-25", "2026-11-29", "Thanksgiving 2026", "high"),
    ("2026-12-20", "2027-01-02", "Christmas / New Year's 2026", "high"),

    # ── 2027 ───────────────────────────────────────────────────────────────
    ("2027-01-15", "2027-01-19", "MLK Weekend 2027", "medium"),
    ("2027-02-12", "2027-02-16", "Presidents Day Weekend 2027", "medium"),
    ("2027-03-01", "2027-03-31", "Spring Break 2027", "medium"),
    ("2027-05-28", "2027-05-31", "Memorial Day Weekend 2027", "high"),
    ("2027-07-02", "2027-07-05", "July 4th Weekend 2027", "high"),
    ("2027-09-03", "2027-09-06", "Labor Day Weekend 2027", "high"),
    ("2027-10-10", "2027-10-31", "Fall Foliage Peak 2027", "high"),
    ("2027-11-24", "2027-11-28", "Thanksgiving 2027", "high"),
    ("2027-12-20", "2028-01-02", "Christmas / New Year's 2027", "high"),
]
