#!/usr/bin/env python3
"""
HVR Smokies STR Dashboard Analysis Engine
Powers the web dashboard's pricing, booking pace, demand, review, and listing
quality analysis for short-term rentals across Tennessee Smokies markets.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any

from groq_client import (
    AI_API_KEY_ENV_VARS,
    APIStatusError,
    GroqClient,
    build_client as _build_groq_client,
    get_ai_api_key as _get_ai_api_key,
    get_groq_model,
)

import sample_data as data

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
TODAY = date(2026, 5, 13)
AUDIT_HORIZON_DAYS = 120

SYSTEM_PROMPT = """You are an expert short-term rental (STR) revenue manager specializing in Tennessee Smokies vacation rentals. You work like a seasoned Revenue Management professional — proactive, data-driven, and specific.

## Tech Stack Context

**Property Management System: Hostaway**
- Hostaway is the single source of truth for reservations, calendar availability, and owner statements
- All OTA channels (Airbnb, VRBO, Booking.com) sync through Hostaway via channel manager
- Rate changes must ultimately be applied in Hostaway or pushed via PriceLabs integration
- When flagging issues, specify whether the fix lives in Hostaway (min stay rules, blocked dates, fees) or PriceLabs (rate strategy, dynamic pricing rules)

**Revenue Management Software: PriceLabs**
- PriceLabs handles dynamic pricing recommendations based on market demand, booking pace, and competitor data
- Key PriceLabs levers: base price, minimum price floor, weekend boost, last-minute discount, orphan day gap filling, seasonal adjustments, health score
- PriceLabs health score below 80 signals misconfigured settings — investigate and resolve conflicts
- PriceLabs recommendations that are being ignored are a red flag — always flag when our settings override PriceLabs's suggestions against its guidance
- PriceLabs syncs rates to Hostaway, which distributes to all channels

**OTA Channel Mix: Airbnb, VRBO, Booking.com (primary) + Direct**
- Airbnb: largest volume, superhost status matters, instant book recommended for shoulder/low season
- VRBO: family/group bookings, longer stays, higher ADR, less last-minute
- Booking.com: more international guests, shorter lead times, often last-minute European travelers, higher commission (~15-18%) — factor into net rate analysis
- Direct: lowest cost (no OTA commission), highest margin — goal is to grow this channel via repeat guests and website; currently underdeveloped at 11%
- Rate parity: Airbnb and VRBO require rate parity; Booking.com allows member discounts but base should be consistent
- Channel conflicts (e.g., a booking appearing on one platform but not synced in Hostaway) are a critical issue to flag

## Your Expertise

**Tennessee Smokies Market Knowledge:**
- Demand is drive-to leisure, cabins, mountain views, hot tubs, pools, game rooms, and family/group travel.
- Core demand drivers: weekends, summer family travel, fall foliage, major holidays, school breaks, local events, and Pigeon Forge/Gatlinburg/Sevierville attractions.
- Price decisions should use city, bedroom count, amenities, pickup, occupancy, and channel visibility, not beach/island assumptions.

**STR Revenue Management Principles:**
- RevPAR = ADR × Occupancy. Maximize both, not just one.
- Gap nights (1-2 night holes between bookings) kill occupancy — enable PriceLabs orphan day gap filling or adjust min stays in Hostaway
- Events drive premium pricing windows: Smokies peak weekends, summer travel, fall foliage, Thanksgiving, Christmas/New Year, spring break, and local event compression
- Last-minute discounting (7-14 days out) recovers empty nights without degrading published rates — configure in PriceLabs
- Minimum stay rules must align with booking windows: 3-night min standard, 5-night on holidays, 1-night gap-fill allowed — set in Hostaway
- Weekend premiums (Fri-Sat) are strongest for Smokies cabins and group-friendly homes; tune by city, bedroom count, amenities, and pickup before adding broad boosts
- Length-of-stay (LOS) discounts incentivize weekly bookings that anchor occupancy — configure in Airbnb/VRBO and Hostaway
- Booking.com commission eats ~15-18% of gross; factor into RevPAR calculations and set slightly higher gross rates on that channel if possible

**How You Operate:**
1. Use your tools to pull real data before forming opinions
2. Identify ALL issues — don't stop at the first one
3. Quantify the revenue impact of each issue ($ left on table)
4. Give specific, actionable recommendations with rate amounts, date ranges, and which system to update (Hostaway vs PriceLabs vs OTA platform), but do not recommend maximum rates, max prices, price caps, or price ceilings
5. Prioritize by revenue impact (highest first)
6. Be direct — state exactly what rate to set, what rule to add, what gap to fix, and where to make the change

When you surface an issue, always include:
- What the problem is
- The specific dates affected
- Current setting vs recommended setting
- Estimated revenue at stake
- Which system to update (Hostaway / PriceLabs / Airbnb / VRBO / Booking.com)

Today's date: """ + TODAY.isoformat()

# ─────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def _date_range(start: str, end: str):
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    while s < e:
        yield s
        s += timedelta(days=1)


def analyze_calendar(start_date: str | None = None, end_date: str | None = None) -> dict:
    """Return calendar occupancy, bookings, and gap analysis for the audit window."""
    start = date.fromisoformat(start_date) if start_date else TODAY
    end = date.fromisoformat(end_date) if end_date else TODAY + timedelta(days=AUDIT_HORIZON_DAYS)

    booked_nights: set[date] = set()
    bookings_in_window = []

    for b in data.CALENDAR:
        ci = date.fromisoformat(b["checkin"])
        co = date.fromisoformat(b["checkout"])
        if co < start or ci > end:
            continue
        bookings_in_window.append({**b, "revenue": b["nights"] * b["rate"]})
        for d in _date_range(b["checkin"], b["checkout"]):
            booked_nights.add(d)

    total_nights = (end - start).days
    occ_pct = len(booked_nights) / total_nights if total_nights else 0

    # Detect gap nights — single unbooked nights surrounded by bookings
    gap_nights = []
    all_days = [start + timedelta(days=i) for i in range(total_nights)]
    for i, d in enumerate(all_days[1:-1], 1):
        if d not in booked_nights:
            prev_booked = all_days[i - 1] in booked_nights
            next_booked = all_days[i + 1] in booked_nights
            if prev_booked and next_booked:
                gap_nights.append(d.isoformat())

    # Open stretches (3+ consecutive unbooked nights)
    open_stretches = []
    stretch_start = None
    stretch_len = 0
    for d in all_days:
        if d not in booked_nights:
            if stretch_start is None:
                stretch_start = d
            stretch_len += 1
        else:
            if stretch_len >= 3:
                open_stretches.append({
                    "start": stretch_start.isoformat(),
                    "end": (stretch_start + timedelta(days=stretch_len)).isoformat(),
                    "nights": stretch_len,
                })
            stretch_start = None
            stretch_len = 0
    if stretch_len >= 3 and stretch_start:
        open_stretches.append({
            "start": stretch_start.isoformat(),
            "end": (stretch_start + timedelta(days=stretch_len)).isoformat(),
            "nights": stretch_len,
        })

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "total_nights": total_nights},
        "occupancy_pct": round(occ_pct * 100, 1),
        "booked_nights": len(booked_nights),
        "open_nights": total_nights - len(booked_nights),
        "bookings": bookings_in_window,
        "gap_nights": gap_nights,
        "open_stretches": open_stretches,
        "projected_revenue": sum(b["revenue"] for b in bookings_in_window),
    }


def get_pricing_settings() -> dict:
    """Return current pricing rules, min-stay settings, fee structure, and PriceLabs config."""
    wh = data.PRICING_SETTINGS.get("PriceLabs", {})
    issues = []
    if data.PRICING_SETTINGS["weekend_premium_pct"] == 0:
        issues.append("No weekend premium configured - PriceLabs weekend boost not enabled. Review Smokies weekend demand by city, bedroom count, amenities, and pickup before applying a broad boost.")
    if not data.PRICING_SETTINGS["last_minute_discount"]["enabled"]:
        issues.append("Last-minute discount disabled in PriceLabs — open nights within 7 days are receiving no discount stimulus.")
    if not data.PRICING_SETTINGS["min_stay_rules"]:
        issues.append("No event/holiday minimum stay overrides in Hostaway — 3-night default applies even on 4th of July and other peak dates.")
    if wh.get("health_score", 100) < 80:
        issues.append(f"PriceLabs health score is {wh.get('health_score')} (below 80 target) — settings conflicts detected.")
    if wh.get("base_rate_override"):
        issues.append("Base rate is manually overriding PriceLabs recommendations — PriceLabs dynamic adjustments may not be reflecting correctly.")
    return {
        "property": data.PROPERTY,
        "pms": data.PROPERTY.get("pms", "Hostaway"),
        "revenue_software": data.PROPERTY.get("revenue_software", "PriceLabs"),
        "pricing": data.PRICING_SETTINGS,
        "PriceLabs_health_score": wh.get("health_score"),
        "PriceLabs_notes": wh.get("notes", ""),
        "detected_issues": issues,
    }


def check_demand_signals(start_date: str, end_date: str) -> dict:
    """Return market demand scores and competitor averages for a date range."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    results = {}
    for d in _date_range(start_date, end_date):
        iso = d.isoformat()
        results[iso] = data.MARKET_DEMAND.get(iso, {
            "demand": 5.5,
            "comp_avg": 490,
            "search_idx": 70,
            "reason": "standard summer demand",
        })
    avg_demand = sum(v["demand"] for v in results.values()) / len(results) if results else 0
    avg_comp = sum(v["comp_avg"] for v in results.values()) / len(results) if results else 0
    return {
        "date_range": {"start": start_date, "end": end_date},
        "daily_signals": results,
        "summary": {
            "avg_demand_score": round(avg_demand, 1),
            "avg_comp_nightly_rate": round(avg_comp),
        },
    }


def get_events_calendar(start_date: str | None = None, end_date: str | None = None) -> dict:
    """Return local Smokies/Tennessee events and holidays that affect STR demand in the window."""
    start = date.fromisoformat(start_date) if start_date else TODAY
    end = date.fromisoformat(end_date) if end_date else TODAY + timedelta(days=AUDIT_HORIZON_DAYS)
    relevant = []
    for ev in data.EVENTS_CALENDAR:
        ev_start = date.fromisoformat(ev["start"])
        ev_end = date.fromisoformat(ev["end"])
        if ev_end >= start and ev_start <= end:
            relevant.append(ev)
    return {"events": relevant, "count": len(relevant)}


def get_competitor_rates(date_str: str | None = None) -> dict:
    """Return comp-set pricing snapshot for the property's competitive set."""
    comp_summary = []
    for c in data.COMP_SET:
        rates = c["recent_rates"]
        comp_summary.append({
            "property": c["name"],
            "bedrooms": c["bedrooms"],
            "view": c["view"],
            "platform": c["platform"],
            "weekday_rate": rates["weekday"],
            "weekend_rate": rates["weekend"],
            "holiday_rate": rates["holiday"],
        })
    weekday_avg = sum(c["weekday_rate"] for c in comp_summary) / len(comp_summary)
    weekend_avg = sum(c["weekend_rate"] for c in comp_summary) / len(comp_summary)
    holiday_avg = sum(c["holiday_rate"] for c in comp_summary) / len(comp_summary)
    our_base = data.PRICING_SETTINGS["base_rate"]
    bdc_commission = 0.16  # Booking.com ~16% commission
    return {
        "comp_set": comp_summary,
        "market_averages": {
            "weekday": round(weekday_avg),
            "weekend": round(weekend_avg),
            "holiday": round(holiday_avg),
        },
        "our_property": {
            "gross_weekday_rate": our_base,
            "gross_weekend_rate": our_base,  # no weekend premium set
            "net_weekday_after_bookingcom_commission": round(our_base * (1 - bdc_commission)),
            "note": "No weekend premium configured — PriceLabs weekend boost is off. All channels publish same rate.",
        },
        "channel_commission_context": {
            "Airbnb": "~3% host fee",
            "VRBO": "~5-8% host fee",
            "Booking.com": "~15-18% commission — highest cost channel; consider gross rate uplift",
            "Direct": "No commission — highest net margin; currently only 11% of mix",
        },
        "note_on_date": date_str or "rates reflect recent comp data",
    }


def get_revenue_metrics() -> dict:
    """Return KPI performance vs targets for the trailing 90-day period."""
    perf = data.PERFORMANCE_90D
    targets = data.TARGET_KPIs
    gaps = {
        "occupancy_gap_ppts": round((targets["occupancy_rate"] - perf["occupancy_rate"]) * 100, 1),
        "adr_gap": targets["adr"] - perf["adr"],
        "revpar_gap": targets["revpar"] - perf["revpar"],
        "revenue_gap_90d": round((targets["revpar"] - perf["revpar"]) * perf["nights_available"]),
    }
    # Estimate net revenue by channel after commissions
    mix = perf["platform_mix"]
    gross_rev = perf["total_revenue"]
    commission_rates = {"Airbnb": 0.03, "VRBO": 0.06, "Booking.com": 0.16, "Direct": 0.0}
    channel_net = {
        ch: round(gross_rev * pct * (1 - commission_rates.get(ch, 0.05)))
        for ch, pct in mix.items()
    }
    return {
        "performance_90d": perf,
        "targets": targets,
        "gaps_vs_target": gaps,
        "assessment": "BELOW TARGET" if gaps["revpar_gap"] > 0 else "ON TARGET",
        "channel_net_revenue_estimate": channel_net,
        "channel_insight": (
            f"Booking.com at {int(mix.get('Booking.com', 0)*100)}% of mix costs ~16% commission. "
            f"Direct at {int(mix.get('Direct', 0)*100)}% is highest-margin but underdeveloped. "
            "Shifting 5% from Booking.com to Direct would recover ~$" +
            str(round(gross_rev * 0.05 * 0.16)) + " in commission savings."
        ),
    }


def audit_listing_photos() -> dict:
    """Audit photo count, coverage, quality, and recency."""
    p = data.LISTING_HEALTH["photos"]
    issues = []
    if p["total_count"] < 25:
        issues.append(f"Only {p['total_count']} photos — Airbnb recommends 25+ for best search placement (you're missing {25 - p['total_count']})")
    if p["cover_photo_score"] < 8.0:
        issues.append(f"Cover photo scores {p['cover_photo_score']}/10 and shows '{p['cover_photo_subject']}' — should be the oceanfront view to maximize click-through")
    if not p["has_bathroom_shots"]:
        issues.append("No bathroom photos — guests almost always check bathrooms before booking")
    if not p["has_pool_hot_tub_shots"]:
        issues.append("Pool and hot tub not shown — these are premium amenities that directly drive booking decisions")
    if not p["has_outdoor_lanai_shots"]:
        issues.append("No lanai/balcony photos — the outdoor space and view is a top selling point for this property")
    if not p["professional_quality"]:
        issues.append("Non-professional photos — professional photography typically increases bookings 20-40% for oceanfront properties")
    if p["airbnb_photo_score"] < 80:
        issues.append(f"Airbnb photo quality score: {p['airbnb_photo_score']}/100 — below the 80 threshold that triggers search ranking boost")
    return {**p, "issues": issues, "issue_count": len(issues)}


def audit_listing_description() -> dict:
    """Audit title, description quality, keyword coverage, and content freshness."""
    d = data.LISTING_HEALTH["description"]
    issues = list(d["issues"])
    score = 100
    if d["word_count"] < 400:
        score -= 30
    if d["last_updated"] < "2025-01-01":
        score -= 20
    if not d["has_neighborhood_guide"]:
        score -= 10
    if not d["has_local_tips"]:
        score -= 10
    if not d["has_seasonal_hooks"]:
        score -= 10
    return {**d, "quality_score": max(score, 0), "issue_count": len(issues)}


def analyze_reviews() -> dict:
    """Analyze review scores, response patterns, Superhost status, and recurring complaints."""
    r = data.LISTING_HEALTH["reviews"]
    unanswered_neg = [rev for rev in r["recent_reviews"] if rev["score"] <= 4 and not rev["responded"]]
    issues = []
    if r["airbnb_rating"] < 4.8:
        issues.append(f"Airbnb rating {r['airbnb_rating']} is below the 4.8 Superhost threshold — each 3-star review costs ~0.04 rating points")
    if not r["superhost_status"]:
        issues.append("Not a Superhost — properties with Superhost badge get ~30% more search visibility on Airbnb")
    if r["response_rate"] < 0.90:
        issues.append(f"Response rate {int(r['response_rate']*100)}% — Superhost requires 90%. You're responding to fewer than 6 in 10 messages.")
    if r["response_time_hours"] > 1:
        issues.append(f"Avg response time {r['response_time_hours']} hours — Superhost requires <1 hour")
    if unanswered_neg:
        issues.append(f"{len(unanswered_neg)} unanswered negative review(s) — public non-responses signal indifference to future guests")
    for cat, avg in r["category_averages"].items():
        if avg < 4.0:
            issues.append(f"'{cat}' category averaging {avg}/5 — this is a red flag in search ranking algorithms")
    return {
        **r,
        "unanswered_negative_reviews": unanswered_neg,
        "unanswered_count": len(unanswered_neg),
        "issues": issues,
        "issue_count": len(issues),
    }


def check_listing_links() -> dict:
    """Check OTA listing status, Instant Book settings, and platform-specific issues."""
    lnk = data.LISTING_HEALTH["links"]
    issues = []
    if not lnk["vrbo"]["instant_book"]:
        issues.append("VRBO Instant Book is OFF — over 60% of VRBO searches filter for instant book only. You're invisible to majority of shoppers.")
    if lnk["booking_com"]["status"] == "restricted":
        issues.append(f"Booking.com listing is RESTRICTED: {lnk['booking_com']['action_required']}")
    if lnk["airbnb"]["listing_score"] < 80:
        issues.append(f"Airbnb listing quality score {lnk['airbnb']['listing_score']}/100 — below the 80 threshold for search ranking boost")
    if not lnk["airbnb"]["superhost"]:
        issues.append("No Superhost badge on Airbnb — this listing appears lower in search results than badged competitors")
    return {**lnk, "issues": issues, "issue_count": len(issues)}


def get_booking_pace(lookahead_days: int = 60) -> dict:
    """Analyze booking lead time and pickup rate for open future dates."""
    future_open = []
    for i in range(lookahead_days):
        d = TODAY + timedelta(days=i)
        booked = any(
            date.fromisoformat(b["checkin"]) <= d < date.fromisoformat(b["checkout"])
            for b in data.CALENDAR
        )
        if not booked:
            future_open.append(d.isoformat())

    # Simulate pace brackets
    within_7  = [d for d in future_open if date.fromisoformat(d) <= TODAY + timedelta(days=7)]
    within_14 = [d for d in future_open if date.fromisoformat(d) <= TODAY + timedelta(days=14)]
    within_30 = [d for d in future_open if date.fromisoformat(d) <= TODAY + timedelta(days=30)]

    return {
        "avg_lead_time_days": data.PERFORMANCE_90D["avg_lead_time_days"],
        "open_nights_in_lookahead": len(future_open),
        "open_nights_within_7d": len(within_7),
        "open_nights_within_14d": len(within_14),
        "open_nights_within_30d": len(within_30),
        "last_minute_discount_active": data.PRICING_SETTINGS["last_minute_discount"]["enabled"],
        "assessment": (
            f"{len(within_7)} open nights in the next 7 days with no last-minute pricing active. "
            f"{len(within_14)} open nights within 14 days."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool registry for Claude
# ─────────────────────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "analyze_calendar",
        "description": (
            "Pull the property booking calendar from Hostaway for a date window. Returns occupancy rate, "
            "confirmed bookings across all channels (Airbnb, VRBO, Booking.com, Direct), gap nights "
            "(single-night holes between bookings), and open stretches that need filling. Use this first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO date, e.g. 2026-05-13. Defaults to today."},
                "end_date":   {"type": "string", "description": "ISO date. Defaults to 120 days from today."},
            },
        },
    },
    {
        "name": "get_pricing_settings",
        "description": (
            "Retrieve current pricing configuration from PriceLabs and Hostaway: base rate, "
            "seasonal multipliers, minimum stay rules (Hostaway), weekend premium, last-minute "
            "discount (PriceLabs), LOS discounts, and PriceLabs health score. Flags detected conflicts."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_demand_signals",
        "description": (
            "Get market demand scores (1-10), competitor average nightly rates, and search volume "
            "index for each date in a range. Use this to compare our pricing against market demand."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO date string"},
                "end_date":   {"type": "string", "description": "ISO date string (exclusive end)"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_events_calendar",
        "description": (
            "Get upcoming Smokies/Tennessee events, holidays, and demand drivers in a date window. "
            "Each event includes recommended premium percentage over base rates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO date. Defaults to today."},
                "end_date":   {"type": "string", "description": "ISO date. Defaults to 120 days out."},
            },
        },
    },
    {
        "name": "get_competitor_rates",
        "description": (
            "Return the competitive set (comp set) pricing for comparable Tennessee Smokies properties. "
            "Shows weekday, weekend, and holiday rates for each comp, plus market averages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_str": {"type": "string", "description": "Optional reference date for context."},
            },
        },
    },
    {
        "name": "get_revenue_metrics",
        "description": (
            "Return trailing 90-day KPI performance vs targets: occupancy rate, ADR, RevPAR, "
            "platform mix, cancellation rate. Identifies how far below/above target we are."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "audit_listing_photos",
        "description": (
            "Audit the listing's photo set: count, cover photo quality, subject coverage (bathroom, "
            "pool, hot tub, lanai, view), professional quality, recency, and Airbnb photo score. "
            "Returns specific issues and what's missing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "audit_listing_description",
        "description": (
            "Audit the listing title and description across Airbnb, VRBO, and Booking.com: word count, "
            "last updated date, missing keywords, unmentioned amenities, missing sections "
            "(neighborhood guide, local tips, seasonal hooks). Returns a quality score and issue list."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "analyze_reviews",
        "description": (
            "Analyze recent guest reviews across all platforms: overall ratings, Superhost status, "
            "response rate and time, unanswered negative reviews, category averages (cleanliness, "
            "check-in, accuracy, value), and recurring complaint themes."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_listing_links",
        "description": (
            "Check the status of OTA listing links (Airbnb, VRBO, Booking.com, Direct): active/restricted "
            "status, Instant Book settings, listing quality scores, Superhost/Premier Host badges, "
            "and any platform-specific flags requiring action."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_booking_pace",
        "description": (
            "Analyze open nights in the near-term window (next 7, 14, 30 days) across all Hostaway "
            "channels and whether PriceLabs last-minute discounting is active to recover them. "
            "Also flags Booking.com last-minute exposure (shorter lead times, international guests)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookahead_days": {"type": "integer", "description": "Days to look ahead. Default 60."},
            },
        },
    },
]


def dispatch_tool(name: str, inputs: dict) -> Any:
    match name:
        case "analyze_calendar":         return analyze_calendar(**inputs)
        case "get_pricing_settings":     return get_pricing_settings()
        case "check_demand_signals":     return check_demand_signals(**inputs)
        case "get_events_calendar":      return get_events_calendar(**inputs)
        case "get_competitor_rates":     return get_competitor_rates(**inputs)
        case "get_revenue_metrics":      return get_revenue_metrics()
        case "get_booking_pace":         return get_booking_pace(**inputs)
        case "audit_listing_photos":     return audit_listing_photos()
        case "audit_listing_description":return audit_listing_description()
        case "analyze_reviews":          return analyze_reviews()
        case "check_listing_links":      return check_listing_links()
        case _:                          return {"error": f"Unknown tool: {name}"}


# ─────────────────────────────────────────────────────────────────────────────
# Groq tool format + agent loop
# ─────────────────────────────────────────────────────────────────────────────

# Model cascade for AI report generation. ``GROQ_MODEL`` env var overrides the
# primary; the fallback list is used for opportunistic retries on transient
# upstream errors AND on 404 model_not_found, so a future Groq deprecation
# degrades to the next model instead of breaking AI reports outright.
#
# Groq deprecated llama-3.3-70b-versatile and llama-3.1-8b-instant on
# 2026-06-17; these are their recommended replacements. Both support the
# tool/function calling this dashboard's agent loop depends on.
AI_MODEL_FALLBACKS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]

def _resolve_primary_model() -> str:
    configured = get_groq_model()
    if configured and configured not in AI_MODEL_FALLBACKS:
        return configured
    return configured or AI_MODEL_FALLBACKS[0]


AI_MODEL = _resolve_primary_model()
GROQ_MODEL = AI_MODEL              # legacy alias
GROQ_MODEL_FAST = AI_MODEL         # legacy alias
GROQ_FALLBACKS = [AI_MODEL] + [m for m in AI_MODEL_FALLBACKS if m != AI_MODEL]


def _build_ai_tools() -> list[dict]:
    """Convert TOOLS list to OpenAI/Groq function-calling format."""
    result = []
    for t in TOOLS:
        schema = t.get("input_schema", {})
        func: dict = {
            "name": t["name"],
            "description": t["description"],
        }
        if schema.get("properties"):
            func["parameters"] = schema
        else:
            func["parameters"] = {"type": "object", "properties": {}}
        result.append({"type": "function", "function": func})
    return result


AI_TOOLS = _build_ai_tools()
GROQ_TOOLS = AI_TOOLS   # legacy alias


def _ai_client() -> GroqClient:
    return _build_groq_client()


# Legacy alias kept so any older imports continue to work.
_groq_client = _ai_client


# Resolved lazily on first model_not_found: Groq retires models periodically
# (llama-3.3-70b-versatile was deprecated 2026-06-17 and started returning 404,
# which broke AI reports outright). Falling through the cascade keeps reports
# working until someone updates the list.
_ACTIVE_MODEL: str | None = None


def _model_candidates() -> list[str]:
    ordered = [AI_MODEL] + [m for m in AI_MODEL_FALLBACKS if m != AI_MODEL]
    if _ACTIVE_MODEL and _ACTIVE_MODEL in ordered:
        # Prefer the model already known to work this session
        ordered = [_ACTIVE_MODEL] + [m for m in ordered if m != _ACTIVE_MODEL]
    return ordered


def _create_with_fallback(client: GroqClient, **kwargs):
    """chat.completions.create that retries the next model on 404
    model_not_found. Any other error propagates unchanged."""
    global _ACTIVE_MODEL
    last_error = None
    for model in _model_candidates():
        try:
            result = client.chat.completions.create(model=model, **kwargs)
            _ACTIVE_MODEL = model
            return result
        except APIStatusError as e:
            is_missing_model = e.status_code == 404 and "model" in (e.message or "").lower()
            if not is_missing_model:
                raise
            last_error = e
            print(f"Groq model '{model}' unavailable, trying next fallback…", file=sys.stderr)
    raise last_error if last_error else RuntimeError("No Groq model available")


def run_revenue_audit(user_prompt: str, verbose: bool = True) -> str:
    """Run a full revenue audit using Groq with function calling."""
    client = _ai_client()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]

    while True:
        response = _create_with_fallback(
            client,
            messages=messages,
            tools=AI_TOOLS,
            tool_choice="auto",
            max_tokens=4096,
        )

        msg = response.choices[0].message

        if not msg.tool_calls:
            text = msg.content or ""
            if verbose:
                print(text)
            return text

        # Append assistant message with tool calls
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # Execute each tool and append results
        for tc in msg.tool_calls:
            if verbose:
                print(f"\n  🔧 [{tc.function.name}]", end="", flush=True)
            try:
                args = json.loads(tc.function.arguments) or {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            result = dispatch_tool(tc.function.name, args)
            if verbose:
                print("  ✅", flush=True)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

AUDIT_PROMPT = """
Run a full revenue management audit for the selected HVR Smokies vacation rental.

Today is {today}. Audit the next 120 days.

Please:
1. Pull the full booking calendar and identify all gap nights and open stretches
2. Review current pricing settings and flag any missing rules
3. Cross-check the events calendar against our pricing — flag any events where we're underpriced
4. Compare our rates against the competitor set
5. Analyze booking pace and last-minute exposure
6. Check trailing KPIs against targets

Deliver a prioritized list of issues found, each with:
- Issue description
- Affected dates
- Current setting vs recommended fix
- Estimated revenue at stake

Then give an action plan ordered by revenue impact.
""".strip().format(today=TODAY.isoformat())

QUESTION_PROMPT_TEMPLATE = "Property: selected HVR Smokies vacation rental. Today: {today}.\n\n{question}"


def main():
    if not _get_ai_api_key():
        print(
            f"ERROR: none of {', '.join(AI_API_KEY_ENV_VARS)} environment variables are set.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(sys.argv) > 1:
        # Question mode: python dashboard_analysis.py "Why is July underperforming?"
        question = " ".join(sys.argv[1:])
        prompt = QUESTION_PROMPT_TEMPLATE.format(today=TODAY.isoformat(), question=question)
        print(f"\n{'─'*70}")
        print(f"MAUI STR DASHBOARD ANALYSIS  |  {data.PROPERTY['name']}")
        print(f"{'─'*70}")
        print(f"Q: {question}\n")
        run_revenue_audit(prompt)
        print(f"\n{'─'*70}\n")
    else:
        # Full audit mode (default)
        print(f"\n{'═'*70}")
        print(f"  MAUI STR REVENUE AUDIT  |  {data.PROPERTY['name']}")
        print(f"  {data.PROPERTY['location']}  |  {TODAY.isoformat()}")
        print(f"{'═'*70}\n")
        run_revenue_audit(AUDIT_PROMPT)
        print(f"\n{'═'*70}\n")

    # Interactive follow-up loop
    if len(sys.argv) == 1:
        print("Ask follow-up questions (or press Ctrl+C to exit):\n")
        try:
            while True:
                q = input("You: ").strip()
                if not q:
                    continue
                print()
                prompt = QUESTION_PROMPT_TEMPLATE.format(today=TODAY.isoformat(), question=q)
                run_revenue_audit(prompt)
                print()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")


if __name__ == "__main__":
    main()

