#!/usr/bin/env python3
"""Generate weekly PriceLabs review tasks for synced listings only."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from wheelhouse_portfolio import Property, load_portfolio


TODAY = date.today()
OUT_PATH = Path(__file__).parent / "pricelabs_weekly_action_queue.json"


def money(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    return f"${float(value):.0f}"


def round_to_5(value: float) -> int:
    return max(0, int(round(value / 5) * 5))


def pct_label(pct: float) -> str:
    return f"{pct:+.0%}"


def pct_rate(base: float, pct: float) -> int:
    return round_to_5(base * (1 + pct))


def floor_limited_rate(prop: Property, pct: float) -> tuple[int, bool]:
    proposed = pct_rate(prop.base_price, pct)
    if prop.min_price and prop.min_price < prop.base_price and proposed <= prop.min_price:
        return round_to_5(prop.min_price), True
    return proposed, False


def range_label(start_offset: int, nights: int) -> str:
    start = TODAY + timedelta(days=start_offset)
    end = start + timedelta(days=max(1, nights) - 1)
    return f"{start.strftime('%b %d')}-{end.strftime('%b %d')}"


def owner_note(prop: Property) -> str:
    return "; ".join(prop.owner_restrictions)


def group_label(prop: Property) -> str:
    parts = [
        getattr(prop, "customization_group", ""),
        getattr(prop, "customization_sub_group", ""),
    ]
    return " / ".join(part for part in parts if part) or getattr(prop, "city", "") or "Ungrouped"


def action_base(prop: Property, type_: str, priority: str) -> dict:
    return {
        "property": prop.name,
        "listing_id": getattr(prop, "listing_id", ""),
        "pms_name": getattr(prop, "pms_name", ""),
        "group": getattr(prop, "customization_group", ""),
        "subgroup": getattr(prop, "customization_sub_group", ""),
        "city": getattr(prop, "city", ""),
        "group_label": group_label(prop),
        "type": type_,
        "system": "PriceLabs",
        "priority": priority,
        "owner_note": owner_note(prop),
        "id": str(uuid.uuid4()),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_at": None,
    }


def suggest_action(prop: Property) -> dict | None:
    priority = "high" if prop.urgency == "critical" else "medium"
    occ = prop.adj_occ_60d

    if occ >= 0.70 and prop.booked_14d == 0:
        action = action_base(prop, "hold_rate_check_restrictions", "medium")
        action.update(
            {
                "suggestion": "Hold pricing; check restrictions and orphan gaps",
                "adjustment": "No price decrease",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                "proposed_value": "Hold base; audit min-stay, gaps, and blocked dates",
                "reason": (
                    f"60-day occupancy is healthy at {occ:.0%}. Zero recent pickup is not enough reason to discount; "
                    "protect ADR and check whether remaining open nights are constrained."
                ),
                "implementation": "Do not push pricing. Review remaining open calendar dates, minimum stays, orphan gaps, and channel availability in PriceLabs/Hostaway.",
                "pricelabs_payload": {"kind": "manual_review"},
            }
        )
        return action

    if 0.55 <= occ < 0.70 and prop.booked_14d == 0:
        action = action_base(prop, "hold_rate_monitor_pickup", "medium")
        action.update(
            {
                "suggestion": "Hold base price; monitor pickup before discounting",
                "adjustment": "No price decrease",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                "proposed_value": "Hold base; consider date-specific action only if open high-value dates remain",
                "reason": (
                    f"60-day occupancy is near target at {occ:.0%}. With no pickup, investigate open-date quality first; "
                    "do not reduce the base rate across the listing."
                ),
                "implementation": "Review only the unbooked high-value dates. If they are clean, use a narrow date-level percentage adjustment, not a base decrease.",
                "pricelabs_payload": {"kind": "manual_review"},
            }
        )
        return action

    if prop.urgency == "overperforming":
        pct = 0.05
        new_base = pct_rate(prop.base_price, pct)
        action = action_base(prop, "fast_booking_price_increase", "high" if prop.adj_occ_60d >= 0.90 else "medium")
        action.update(
            {
                "suggestion": f"Review base price increase of {pct_label(pct)}",
                "adjustment": f"{pct_label(pct)} base review",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {prop.adj_occ_60d:.0%}",
                "proposed_value": f"Base {pct_label(pct)} (est. {money(new_base)})",
                "reason": (
                    f"Fast booking signal: {prop.booked_14d} booked nights in the last 15-day pickup window; "
                    f"60-day occupancy is {prop.adj_occ_60d:.0%}."
                ),
                "implementation": "Approve first. If approved, review and update base price in PriceLabs. Do not change max price.",
                "pricelabs_payload": {"kind": "base_percentage_review", "adjustment_pct": pct, "estimated_base_price": new_base},
            }
        )
        return action

    if prop.urgency in {"critical", "warning"} and occ < 0.55:
        if prop.no_promotions:
            pct = -0.03
            new_base, hit_floor = floor_limited_rate(prop, pct)
            action = action_base(prop, "base_price_small_nudge_no_promo", priority)
            action.update(
                {
                    "suggestion": (
                        f"Review base price at minimum floor {money(new_base)}"
                        if hit_floor
                        else f"Review base price decrease of {pct_label(pct)}"
                    ),
                    "adjustment": f"floor-limited to {money(new_base)}" if hit_floor else f"{pct_label(pct)} base review",
                    "target_dates": "Until next weekly review",
                    "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {prop.adj_occ_60d:.0%}",
                    "proposed_value": f"Minimum floor {money(new_base)}" if hit_floor else f"Base {pct_label(pct)} (est. {money(new_base)})",
                    "reason": "Pickup is soft, but owner tags indicate no promotions; use a small percentage review instead of discount language.",
                    "implementation": "Approve first. If approved, review base price in PriceLabs only if owner restrictions allow.",
                    "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
                }
            )
            return action

        if prop.booked_14d == 0 and occ <= 0.15:
            pct = -0.10 if prop.base_price < 250 else -0.12
            target_rate, hit_floor = floor_limited_rate(prop, pct)
            start = TODAY + timedelta(days=19)
            end = TODAY + timedelta(days=23)
            action = action_base(prop, "open_gap_fixed_rate_discount", priority)
            action.update(
                {
                    "suggestion": (
                        f"Review floor-limited rate {money(target_rate)} for {range_label(19, 5)}"
                        if hit_floor
                        else f"Review {pct_label(pct)} adjustment for {range_label(19, 5)}"
                    ),
                    "adjustment": f"floor-limited to {money(target_rate)}" if hit_floor else f"{pct_label(pct)} from base",
                    "target_dates": range_label(19, 5),
                    "current_value": f"Base {money(prop.base_price)}; 15-day pickup {prop.booked_14d}",
                    "proposed_value": f"Minimum floor {money(target_rate)}" if hit_floor else f"{pct_label(pct)} date adjustment (est. {money(target_rate)})",
                    "reason": f"No pickup in the last 15-day window and 60-day occupancy is {prop.adj_occ_60d:.0%}.",
                    "implementation": "Approve first. If approved, use a percentage adjustment in PriceLabs unless the minimum floor is reached.",
                    "pricelabs_payload": {
                        "kind": "custom_fixed_rate" if hit_floor else "custom_percentage_adjustment",
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "adjustment_pct": pct,
                        "suggested_rate": target_rate if hit_floor else None,
                        "estimated_rate": target_rate,
                    },
                }
            )
            return action

        if occ <= 0.20:
            pct = -0.05
            new_base, hit_floor = floor_limited_rate(prop, pct)
            action = action_base(prop, "low_occupancy_base_adjustment", priority)
            action.update(
                {
                    "suggestion": (
                        f"Review base price at minimum floor {money(new_base)}"
                        if hit_floor
                        else f"Review base price decrease of {pct_label(pct)}"
                    ),
                    "adjustment": f"floor-limited to {money(new_base)}" if hit_floor else f"{pct_label(pct)} base review",
                    "target_dates": "Until next weekly review",
                    "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                    "proposed_value": f"Minimum floor {money(new_base)}" if hit_floor else f"Base {pct_label(pct)} (est. {money(new_base)})",
                    "reason": (
                        f"60-day occupancy is weak at {occ:.0%}. Pickup of {prop.booked_14d} nights is not enough "
                        "to offset the pacing gap, so the base needs downward pressure."
                    ),
                    "implementation": "Approve first. If approved, reduce base price in PriceLabs and recheck pickup/ADR after 7 days.",
                    "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
                }
            )
            return action

        if occ < 0.35 and prop.booked_14d <= 5:
            pct = -0.03
            new_base, hit_floor = floor_limited_rate(prop, pct)
            action = action_base(prop, "below_target_base_adjustment", priority)
            action.update(
                {
                    "suggestion": (
                        f"Review base price at minimum floor {money(new_base)}"
                        if hit_floor
                        else f"Review base price decrease of {pct_label(pct)}"
                    ),
                    "adjustment": f"floor-limited to {money(new_base)}" if hit_floor else f"{pct_label(pct)} base review",
                    "target_dates": "Until next weekly review",
                    "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                    "proposed_value": f"Minimum floor {money(new_base)}" if hit_floor else f"Base {pct_label(pct)} (est. {money(new_base)})",
                    "reason": (
                        f"60-day occupancy is below target at {occ:.0%}, and pickup is modest at {prop.booked_14d} nights. "
                        "Use a small base adjustment rather than waiting another week."
                    ),
                    "implementation": "Approve first. If approved, reduce base price in PriceLabs and monitor pickup/ADR after 7 days.",
                    "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
                }
            )
            return action

        if occ >= 0.35:
            action = action_base(prop, "hold_rate_monitor_pickup", priority)
            action.update(
                {
                    "suggestion": "Hold base price; watch pickup and open-date quality",
                    "adjustment": "No price decrease",
                    "target_dates": "Until next weekly review",
                    "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {occ:.0%}; pickup {prop.booked_14d}",
                    "proposed_value": "Hold base; review only weak date pockets",
                    "reason": (
                        f"Occupancy is not low enough for a base decrease ({occ:.0%}). "
                        "A base-rate cut would discount already-booked demand and weaken ADR."
                    ),
                    "implementation": "Check open nights and market compression first. Use date-specific adjustments only where the calendar is exposed.",
                    "pricelabs_payload": {"kind": "manual_review"},
                }
            )
            return action

        pct = -0.03
        new_base, hit_floor = floor_limited_rate(prop, pct)
        action = action_base(prop, "base_price_reduction", priority)
        action.update(
            {
                "suggestion": (
                    f"Review base price at minimum floor {money(new_base)}"
                    if hit_floor
                    else f"Review base price decrease of {pct_label(pct)}"
                ),
                "adjustment": f"floor-limited to {money(new_base)}" if hit_floor else f"{pct_label(pct)} base review",
                "target_dates": "Until next weekly review",
                "current_value": f"Base {money(prop.base_price)}; 60-day occupancy {prop.adj_occ_60d:.0%}",
                "proposed_value": f"Minimum floor {money(new_base)}" if hit_floor else f"Base {pct_label(pct)} (est. {money(new_base)})",
                "reason": f"Soft pacing: 60-day occupancy is {prop.adj_occ_60d:.0%}; pickup window shows {prop.booked_14d} nights.",
                "implementation": "Approve first. If approved, review base price in PriceLabs and recheck after 7 days.",
                "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
            }
        )
        return action

    return None


def main() -> int:
    props = [p for p in load_portfolio() if p.active]
    candidates = [p for p in props if p.urgency in {"critical", "warning", "overperforming"}]
    actions = [a for p in candidates if (a := suggest_action(p))]
    OUT_PATH.write_text(json.dumps(actions, indent=2), encoding="utf-8")
    print(f"Generated {len(actions)} weekly PriceLabs tasks for {len(props)} synced active listings.")
    print(f"Saved to {OUT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
