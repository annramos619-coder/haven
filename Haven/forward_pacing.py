#!/usr/bin/env python3
"""
Forward pacing price-position logic.

This module makes pace actions less blunt by comparing occupancy/pacing against
price position. It supports rich records with posted percentile, market booked
price, and last-year booked price, while also enriching older action JSON that
only has "ADR $x vs market $y" in the reason text.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE = Path(__file__).parent
ACTIVE_QUEUE = BASE / "pricelabs_weekly_action_queue.json"
LEGACY_QUEUE = BASE / "weekly_action_queue.json"
FORWARD_PACING_CSV = BASE / "forward_pacing.csv"
PACE_SOURCE = "Pace 2025 MauiP 05.17.26.xlsm"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick(row: dict[str, Any], *names: str) -> Any:
    normalized = {
        re.sub(r"[^a-z0-9]+", "", str(key).lower()): value
        for key, value in row.items()
    }
    for name in names:
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        if key in normalized:
            return normalized[key]
    return None


@dataclass
class ForwardPriceCheck:
    signal: str
    price_signal: str
    recommended_pct: float
    target_rate: int | None
    rationale: str
    missing_fields: list[str]
    posted_vs_market_booked_pct: float | None = None
    posted_vs_last_year_booked_pct: float | None = None
    posted_vs_market_posted_pct: float | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "price_signal": self.price_signal,
            "recommended_pct": self.recommended_pct,
            "target_rate": self.target_rate,
            "rationale": self.rationale,
            "missing_fields": self.missing_fields,
            "posted_vs_market_booked_pct": self.posted_vs_market_booked_pct,
            "posted_vs_last_year_booked_pct": self.posted_vs_last_year_booked_pct,
            "posted_vs_market_posted_pct": self.posted_vs_market_posted_pct,
        }


def forward_price_check(
    *,
    pace_gap_pct_points: float | None,
    stly_gap_pct_points: float | None,
    posted_price: float | None,
    posted_percentile: float | None,
    market_posted_price: float | None,
    market_booked_price: float | None,
    last_year_booked_price: float | None,
) -> ForwardPriceCheck:
    missing = []
    if posted_price is None:
        missing.append("posted_price")
    if posted_percentile is None:
        missing.append("posted_percentile")
    if market_booked_price is None:
        missing.append("market_booked_price")
    if last_year_booked_price is None:
        missing.append("last_year_booked_price")

    def diff(base: float | None) -> float | None:
        if posted_price is None or not base:
            return None
        return (posted_price - base) / base

    vs_market_booked = diff(market_booked_price)
    vs_last_year = diff(last_year_booked_price)
    vs_market_posted = diff(market_posted_price)

    behind = pace_gap_pct_points is not None and pace_gap_pct_points <= -5
    badly_behind = pace_gap_pct_points is not None and pace_gap_pct_points <= -12
    ahead = pace_gap_pct_points is not None and pace_gap_pct_points >= 8
    last_year_behind = stly_gap_pct_points is not None and stly_gap_pct_points <= -8
    last_year_ahead = stly_gap_pct_points is not None and stly_gap_pct_points >= 8
    expensive_to_booked = any(v is not None and v >= 0.08 for v in (vs_market_booked, vs_last_year))
    cheap_to_booked = all(v is not None and v <= -0.06 for v in (vs_market_booked, vs_last_year))
    high_percentile = posted_percentile is not None and posted_percentile >= 75
    low_percentile = posted_percentile is not None and posted_percentile <= 35

    target_sources = [v for v in (market_booked_price, last_year_booked_price, market_posted_price) if v]
    blended_target = round(sum(target_sources) / len(target_sources)) if target_sources else None

    if badly_behind and (expensive_to_booked or high_percentile):
        pct = -0.10
        signal = "decrease_aggressive"
        price_signal = "behind_and_overpriced"
        rationale = "Behind market pace and posted price is high versus booked demand or percentile."
    elif behind and (expensive_to_booked or high_percentile):
        pct = -0.05
        signal = "decrease_small"
        price_signal = "behind_and_price_high"
        rationale = "Behind market pace; price appears above demand-clearing references."
    elif behind and cheap_to_booked:
        pct = 0.0
        signal = "hold_check_conversion"
        price_signal = "behind_but_already_cheap"
        rationale = "Behind pace, but posted price is already below booked market and last-year references; check visibility, restrictions, channel availability, and listing conversion before discounting."
    elif ahead and (low_percentile or cheap_to_booked or last_year_ahead):
        pct = 0.05
        signal = "increase_small"
        price_signal = "ahead_and_underpriced"
        rationale = "Ahead of market pace while price sits low versus booked-demand references."
    elif ahead and high_percentile:
        pct = 0.0
        signal = "hold_protect_adr"
        price_signal = "ahead_and_price_high"
        rationale = "Ahead of pace, but price is already high; protect ADR and avoid extra discounts."
    elif last_year_behind and (expensive_to_booked or high_percentile):
        pct = -0.03
        signal = "decrease_small"
        price_signal = "behind_last_year_and_price_high"
        rationale = "Current pace trails same time last year and pricing is not demand-clearing."
    else:
        pct = 0.0
        signal = "hold_monitor"
        price_signal = "mixed_or_insufficient_price_signal"
        rationale = "Pace and price signals are mixed or missing; hold broad changes and inspect date-level demand."

    target_rate = None
    if posted_price and pct:
        target_rate = max(0, int(round((posted_price * (1 + pct)) / 5) * 5))
    elif blended_target and signal.startswith("decrease"):
        target_rate = max(0, int(round(blended_target / 5) * 5))

    return ForwardPriceCheck(
        signal=signal,
        price_signal=price_signal,
        recommended_pct=pct,
        target_rate=target_rate,
        rationale=rationale,
        missing_fields=missing,
        posted_vs_market_booked_pct=vs_market_booked,
        posted_vs_last_year_booked_pct=vs_last_year,
        posted_vs_market_posted_pct=vs_market_posted,
    )


def _parse_legacy_reason(action: dict[str, Any]) -> dict[str, float | None]:
    reason = action.get("reason", "")
    adr_match = re.search(r"ADR\s+\$?([\d,.]+)\s+vs\s+market\s+\$?([\d,.]+)", reason, re.I)
    return {
        "posted_price": _num(adr_match.group(1)) if adr_match else _num(action.get("posted_price")),
        "market_posted_price": _num(adr_match.group(2)) if adr_match else _num(action.get("market_posted_price")),
        "market_booked_price": _num(action.get("market_booked_price") or action.get("booked_market_price")),
        "last_year_booked_price": _num(action.get("last_year_booked_price") or action.get("ly_booked_price")),
        "posted_percentile": _num(action.get("posted_percentile") or action.get("posted_price_percentile")),
    }


def enrich_action(action: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_legacy_reason(action)
    check = forward_price_check(
        pace_gap_pct_points=_num(action.get("market_gap_pct_points")),
        stly_gap_pct_points=_num(action.get("stly_gap_pct_points")),
        posted_price=parsed["posted_price"],
        posted_percentile=parsed["posted_percentile"],
        market_posted_price=parsed["market_posted_price"],
        market_booked_price=parsed["market_booked_price"],
        last_year_booked_price=parsed["last_year_booked_price"],
    )
    enriched = dict(action)
    enriched["pace_price_check"] = check.asdict()
    enriched["price_signal"] = check.price_signal
    enriched["pace_signal"] = check.signal
    if check.recommended_pct == 0:
        enriched["type"] = f"pace_year_{check.signal}"
        enriched["suggestion"] = f"{check.signal.replace('_', ' ').title()} for {action.get('target_dates', 'target dates')}"
        enriched["adjustment"] = "No automatic rate change"
        enriched["proposed_value"] = "Hold price; inspect restrictions, visibility, channel availability, and conversion"
        enriched["wheelhouse_payload"] = {"kind": "manual_review"}
    elif check.target_rate:
        pct_text = f"{check.recommended_pct:+.0%}"
        enriched["type"] = f"pace_year_{check.signal}"
        enriched["suggestion"] = f"Review {pct_text} price-position adjustment for {action.get('target_dates', 'target dates')}; target ${check.target_rate}"
        enriched["adjustment"] = f"{pct_text} forward price-position review"
        enriched["proposed_value"] = f"Target fixed nightly rate ${check.target_rate}"
        payload = dict(enriched.get("wheelhouse_payload") or {})
        payload.update({"kind": "custom_fixed_rate", "suggested_rate": check.target_rate})
        enriched["wheelhouse_payload"] = payload
    details = []
    if parsed["posted_price"] is not None:
        details.append(f"posted ${parsed['posted_price']:.0f}")
    if parsed["posted_percentile"] is not None:
        details.append(f"posted percentile {parsed['posted_percentile']:.0f}")
    if parsed["market_booked_price"] is not None:
        details.append(f"market booked ${parsed['market_booked_price']:.0f}")
    if parsed["last_year_booked_price"] is not None:
        details.append(f"LY booked ${parsed['last_year_booked_price']:.0f}")
    if parsed["market_posted_price"] is not None:
        details.append(f"market posted ${parsed['market_posted_price']:.0f}")
    missing = ", ".join(check.missing_fields)
    enriched["reason"] = (
        f"{action.get('reason', '').rstrip()} Price-position check: {check.rationale}"
        + (f" Inputs: {', '.join(details)}." if details else "")
        + (f" Missing: {missing}." if missing else "")
    )
    return enriched


def load_pace_actions() -> list[dict[str, Any]]:
    for path in (ACTIVE_QUEUE, LEGACY_QUEUE):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        actions = [
            enrich_action(action)
            for action in data
            if action.get("source") == PACE_SOURCE and str(action.get("type", "")).startswith("pace")
        ]
        if actions:
            return actions
    return []


def load_forward_pacing_csv() -> list[dict[str, Any]]:
    if not FORWARD_PACING_CSV.exists():
        return []
    actions = []
    with FORWARD_PACING_CSV.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            check = forward_price_check(
                pace_gap_pct_points=_num(_pick(row, "Market Gap Pct Points", "Pace Gap", "Occ Gap")),
                stly_gap_pct_points=_num(_pick(row, "STLY Gap Pct Points", "Last Year Gap")),
                posted_price=_num(_pick(row, "Posted Price", "Our Posted Price", "ADR")),
                posted_percentile=_num(_pick(row, "Posted Percentile", "Posted Price Percentile")),
                market_posted_price=_num(_pick(row, "Market Posted Price", "Market ADR")),
                market_booked_price=_num(_pick(row, "Market Booked Price", "Booked Market Price")),
                last_year_booked_price=_num(_pick(row, "Last Year Booked Price", "LY Booked Price")),
            )
            actions.append({
                "property": _pick(row, "Property", "Listing", "Listing Name") or "Segment pacing",
                "type": f"pace_forward_{check.signal}",
                "system": "Forward Pacing",
                "priority": "high" if check.signal in {"decrease_aggressive", "hold_check_conversion"} else "medium",
                "suggestion": check.rationale,
                "adjustment": f"{check.recommended_pct:+.0%}" if check.recommended_pct else "No automatic rate change",
                "target_dates": _pick(row, "Target Dates", "Week", "Date Range") or "",
                "current_value": "",
                "proposed_value": f"Target ${check.target_rate}" if check.target_rate else "Manual review",
                "reason": check.rationale,
                "implementation": "Approve first. Apply only the selected forward date range after checking stacking, restrictions, and channel visibility.",
                "source": "forward_pacing.csv",
                "source_sheet": _pick(row, "Segment", "Source Sheet") or "",
                "pace_signal": check.signal,
                "price_signal": check.price_signal,
                "pace_price_check": check.asdict(),
                "status": "pending",
            })
    return actions
