#!/usr/bin/env python3
"""
HVR Smokies Dashboard.
Serves the HTML dashboard and streams Groq AI analysis via Server-Sent Events.
"""

import html as _html_lib
import calendar
import csv
import json
import os
import re
import sys
import shutil
import threading
import time
import uuid

import requests
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, ".")
from dashboard_analysis import (
    AI_API_KEY_ENV_VARS,
    AI_MODEL,
    AI_MODEL_FALLBACKS,
    GROQ_FALLBACKS,  # legacy alias, retained for any external import
    _ai_client,
    _get_ai_api_key,
    _groq_client,    # legacy alias for _ai_client
)
from groq_client import APIStatusError as AIAPIStatusError, get_active_provider_info
from wheelhouse_portfolio import load_portfolio, portfolio_summary, Property
from marketing_links import lookup as lookup_links, get_links
from pricelabs_api import PriceLabsAPIError, client_from_env
from booking_api import BookingAPIError, build_promotion_xml, client_from_env as booking_client_from_env
from pricelabs_reservations import (
    RESERVATION_PATH as _PL_RESERVATION_PATH,
    all_reservations as pricelabs_all_reservations,
    booking_stats_by_listing as pricelabs_booking_stats,
    last_booked_by_listing as pricelabs_last_booked_by_listing,
    reservations_for_listing as pricelabs_reservations_for_listing,
    source_label as pricelabs_reservations_source,
)


def pricelabs_reservation_path() -> str:
    return _PL_RESERVATION_PATH
from pricelabs_monthly_pacing import SOURCE as MONTHLY_PACING_SOURCE, load_monthly_pacing
from kcity_surge_dso import generate_dso_tasks, SOURCE as KCITY_DSO_SOURCE
import statistics

import pricelabs_report_builder as pl_report_builder
import supabase_store
import weather_monitor

app = Flask(__name__)
TODAY = date.today()
EARLIEST_RATE_EDIT_DATE = TODAY + timedelta(days=1)

CSV_PATH        = Path(__file__).parent / "pricelabs_portfolio.csv"
MARKETING_PATH  = Path(__file__).parent / "marketing_links.csv"
ACTION_QUEUE_PATH = Path(__file__).parent / "pricelabs_weekly_action_queue.json"
BOOKING_PROMOTIONS_PATH = Path(__file__).parent / "booking_promotion_lab.json"
MONTHLY_PACING_PATH = Path(__file__).parent / "pricelabs_report_builder_monthly.csv"
SCORECARD_PATH      = Path(__file__).parent / "scorecard_upload.xlsx"
SCORECARD_SSS_PATH  = Path(__file__).parent / "scorecard_sss.xlsx"


def _sss_roster() -> set[str] | None:
    """Listing names from the curated SSS scorecard, or None if not uploaded."""
    if not SCORECARD_SSS_PATH.exists():
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(SCORECARD_SSS_PATH), read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        headers = [str(h or "").strip() for h in next(it)]
        idx = headers.index("Listing Name") if "Listing Name" in headers else 0
        names = {str(r[idx]).strip() for r in it if r and r[idx]}
        wb.close()
        return names or None
    except Exception:
        return None
PRICELABS_API_SNAPSHOT_PATH = Path(__file__).parent / "pricelabs_api_snapshot.json"
LISTING_QUALITY_RULES_PATH = Path(__file__).parent / "listing_quality_rules.md"
ACTION_REPEAT_COOLDOWN_DAYS = 14
_portfolio_lock = threading.Lock()
_actions_lock = threading.Lock()


class PricingApplyError(RuntimeError):
    pass

# Load portfolio once at startup
_PORTFOLIO: list[Property] = load_portfolio()
_PORTFOLIO_INDEX: dict[str, Property] = {p.name: p for p in _PORTFOLIO}
_SUMMARY = portfolio_summary(_PORTFOLIO)


def _resolve_property(name: str) -> Property | None:
    if not name:
        return None
    if name in _PORTFOLIO_INDEX:
        return _PORTFOLIO_INDEX[name]
    clean = name.strip().lower()
    for prop in _PORTFOLIO:
        if prop.name.strip().lower() == clean or prop.property_name.strip().lower() == clean:
            return prop
    # Substring fallback: only safe when exactly ONE property matches. Unit
    # names commonly differ by a short numeric suffix (e.g. "Aspen 1" vs
    # "Aspen 10"), so a non-unique substring match must not silently pick
    # either one — callers use this result to validate/apply live price pushes.
    candidates = [
        prop for prop in _PORTFOLIO
        if clean and (clean in prop.name.strip().lower() or prop.name.strip().lower() in clean)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _load_actions_from_json() -> list[dict]:
    if not ACTION_QUEUE_PATH.exists():
        return []
    try:
        return json.loads(ACTION_QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_actions_to_json(actions: list[dict]) -> None:
    try:
        ACTION_QUEUE_PATH.write_text(json.dumps(actions, indent=2), encoding="utf-8")
    except OSError:
        # Vercel filesystem is read-only outside /tmp; Supabase is the source of
        # truth when configured, so swallow this and rely on the table.
        pass


def _load_actions() -> list[dict]:
    if supabase_store.is_enabled():
        remote = supabase_store.load_pricing_actions()
        if remote is None:
            return _load_actions_from_json()
        if remote:
            return remote
        # Empty table on first read — backfill from the bundled JSON snapshot.
        seed = _load_actions_from_json()
        if seed and supabase_store.save_pricing_actions(seed):
            return seed
        return seed
    return _load_actions_from_json()


def _save_actions(actions: list[dict]) -> None:
    if supabase_store.is_enabled():
        if supabase_store.save_pricing_actions(actions):
            return
    _save_actions_to_json(actions)


def _load_booking_promotions_from_json() -> list[dict]:
    if not BOOKING_PROMOTIONS_PATH.exists():
        return []
    try:
        return json.loads(BOOKING_PROMOTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_booking_promotions_to_json(promotions: list[dict]) -> None:
    try:
        BOOKING_PROMOTIONS_PATH.write_text(json.dumps(promotions, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_booking_promotions() -> list[dict]:
    if supabase_store.is_enabled():
        remote = supabase_store.load_booking_promotions()
        if remote is None:
            return _load_booking_promotions_from_json()
        if remote:
            return remote
        seed = _load_booking_promotions_from_json()
        if seed and supabase_store.save_booking_promotions(seed):
            return seed
        return seed
    return _load_booking_promotions_from_json()


def _save_booking_promotions(promotions: list[dict]) -> None:
    if supabase_store.is_enabled():
        if supabase_store.save_booking_promotions(promotions):
            return
    _save_booking_promotions_to_json(promotions)


def _listing_quality_rules() -> str:
    try:
        return LISTING_QUALITY_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _money(value: float | int | None) -> str:
    if value is None:
        return "unknown"
    return f"${float(value):.0f}"


def _round_to_5(value: float) -> int:
    return max(0, int(round(value / 5) * 5))


def _pct_label(pct: float) -> str:
    return f"{pct:+.0%}"


def _pct_rate(base: float, pct: float) -> int:
    return _round_to_5(base * (1 + pct))


def _floor_limited_rate(prop: Property, pct: float, base: float | None = None) -> tuple[int, bool]:
    starting_rate = base if base is not None else prop.base_price
    proposed = _pct_rate(starting_rate, pct)
    if prop.min_price and prop.min_price < starting_rate and proposed <= prop.min_price:
        return _round_to_5(prop.min_price), True
    return proposed, False


def _range_label(start_offset: int, nights: int) -> str:
    start = TODAY + timedelta(days=start_offset)
    end = start + timedelta(days=max(1, nights) - 1)
    fmt = lambda d: d.strftime("%b ") + str(d.day)
    return f"{fmt(start)}–{fmt(end)}"


def _base_delta(prop: Property, pct: float, floor: int = 10, cap: int = 35) -> int:
    return _round_to_5(min(cap, max(floor, prop.base_price * pct)))


def _upcoming_rates(prop: Property, days: int = 45) -> list[tuple[date, float]]:
    end = TODAY + timedelta(days=days)
    out: list[tuple[date, float]] = []
    for day_text, rate in prop.calendar_rates:
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if EARLIEST_RATE_EDIT_DATE <= day <= end:
            out.append((day, rate))
    return out


def _stagnant_rate_window(prop: Property, min_nights: int = 4) -> dict | None:
    rates = _upcoming_rates(prop, 45)
    if not rates:
        return None
    best: list[tuple[date, float]] = []
    current: list[tuple[date, float]] = []
    for item in rates:
        if not current:
            current = [item]
            continue
        prev_day, prev_rate = current[-1]
        day, rate = item
        if rate == prev_rate and day == prev_day + timedelta(days=1):
            current.append(item)
        else:
            if len(current) > len(best):
                best = current
            current = [item]
    if len(current) > len(best):
        best = current
    if len(best) < min_nights:
        return None
    start, rate = best[0]
    end = best[-1][0]
    suggested_rate, hit_floor = _floor_limited_rate(prop, -0.03, rate)
    return {
        "start": start,
        "end": end,
        "nights": len(best),
        "rate": rate,
        "adjustment_pct": -0.03,
        "suggested_rate": suggested_rate,
        "hit_floor": hit_floor,
        "label": f"{start.strftime('%b ') + str(start.day)}-{end.strftime('%b ') + str(end.day)}",
    }


def _owner_note(prop: Property) -> str:
    return "; ".join(prop.owner_restrictions)


# ─────────────────────────────────────────────────────────────────────────────
# Listing Optimizer — rich HTML generator
# ─────────────────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return _html_lib.escape(str(v) if v is not None else "", quote=True)


def _lo_grade_color(grade: str) -> str:
    if not grade or grade == "N/A":
        return "text-slate-400"
    if grade.startswith("A"):
        return "text-green-700"
    if grade.startswith("B"):
        return "text-blue-700"
    if grade.startswith("C"):
        return "text-amber-600"
    return "text-red-600"


def _lo_analyze_title(title: str, char_limit: int = 50) -> dict:
    if not title or title in ("Not available", "Not synced", ""):
        return {"grade": "N/A", "issues": [], "passes": [], "char_count": 0, "mobile_preview": ""}
    issues = []
    passes = []
    char_count = len(title)
    title_lower = title.lower()
    if char_count > char_limit:
        issues.append(f"Title is {char_count}/{char_limit} chars — exceeds {char_limit}-char hard limit")
    else:
        passes.append(f"Character count: {char_count}/{char_limit} — within limit")
    if re.search(r"\bnew[!,\s]", title_lower) or title_lower.startswith("new "):
        issues.append('"New" in title is redundant per Airbnb guidelines — wastes characters')
    if "cozy" in title_lower:
        issues.append('"Cozy" is the most overused STR adjective — replace with a specific feature')
    if re.search(r"\bsleeps\s+\d+\b", title_lower):
        issues.append('"Sleeps X" is redundant — guest capacity is shown automatically in search results')
    IMPROPER_CAP_WORDS = {
        "new", "cozy", "beautiful", "stunning", "spacious", "charming", "modern",
        "luxurious", "easy", "perfect", "amazing", "great", "best", "private",
        "quiet", "comfortable", "peaceful", "relaxing", "sleeps", "with",
        "and", "the", "for", "near", "by", "in", "at", "on",
    }
    cap_violations = sum(
        1 for w in title.split()[1:]
        if w and w[0].isupper() and w.lower() in IMPROPER_CAP_WORDS
    )
    if cap_violations >= 2:
        issues.append("Title case violation — Airbnb requires sentence case: only first word and proper nouns capitalized")
    n = len(issues)
    grade = "A" if n == 0 and char_count <= 45 else "B+" if n == 0 else "B" if n == 1 else "C" if n == 2 else "D"
    return {"grade": grade, "issues": issues, "passes": passes, "char_count": char_count, "mobile_preview": title[:32]}


def _lo_photo_grade(count, existing_grade=None) -> str:
    if existing_grade and existing_grade not in ("unknown", "N/A", "", None):
        return str(existing_grade)
    if count is None:
        return "N/A"
    count = int(count)
    if count < 10:
        return "D"
    if count < 15:
        return "C"
    if count < 25:
        return "B"
    return "A"


def _lo_review_grade(rating, reviews=None) -> str:
    if rating is None:
        return "N/A"
    r = float(rating)
    if reviews is not None and int(reviews) < 3:
        return "C"
    if r >= 4.9:
        return "A"
    if r >= 4.8:
        return "A−"
    if r >= 4.7:
        return "B+"
    if r >= 4.5:
        return "B"
    if r >= 4.3:
        return "C"
    return "D"


def _lo_pricing_grade(prop) -> str:
    if prop.urgency == "overperforming":
        return "B+"
    if prop.urgency in ("ok", "onboarding"):
        return "B"
    if prop.adj_occ_60d < 0.10 and prop.booked_14d == 0:
        return "D"
    if prop.urgency == "warning":
        return "C"
    if prop.urgency == "critical":
        return "D"
    return "B"


def _lo_occ_grade(prop) -> str:
    occ = prop.adj_occ_60d
    if occ >= 0.75:
        return "A"
    if occ >= 0.50:
        return "B"
    if occ >= 0.30:
        return "C"
    return "D"




def _listing_optimizer_html(prop) -> str:  # noqa: C901
    ll = lookup_links(prop.name)
    benchmark = _benchmark_for(prop)
    # ── Raw data ───────────────────────────────────────────────────────────────
    airbnb_title   = (ll.airbnb_headline or "")  if ll else ""
    vrbo_title     = (ll.vrbo_headline   or "")  if ll else ""
    airbnb_photos  = ll.airbnb_photos            if ll else None
    vrbo_photos    = ll.vrbo_photos              if ll else None
    # Ratings come from marketing_links.csv - the only live source now.
    airbnb_rating  = ll.airbnb_rating  if ll else None
    airbnb_reviews = ll.airbnb_reviews if ll else None
    vrbo_rating    = ll.vrbo_rating    if ll else None
    vrbo_reviews   = ll.vrbo_reviews   if ll else None
    airbnb_url     = (ll.airbnb_url or "")       if ll else ""
    vrbo_url       = (ll.vrbo_url   or "")       if ll else ""
    booking_url    = (ll.booking_url or "")      if ll else ""
    vrbo_clean   = getattr(ll, "vrbo_cleanliness",   None) if ll else None
    vrbo_checkin = getattr(ll, "vrbo_checkin",       None) if ll else None
    vrbo_comm    = getattr(ll, "vrbo_communication", None) if ll else None
    vrbo_loc     = getattr(ll, "vrbo_location",      None) if ll else None

    ab_ta = _lo_analyze_title(airbnb_title, 50)
    vr_ta = _lo_analyze_title(vrbo_title, 70)

    title_grade   = ab_ta["grade"] if airbnb_title else (vr_ta["grade"] if vrbo_title else "N/A")
    photo_grade   = _lo_photo_grade(airbnb_photos, ll.airbnb_photo_grade if ll else None)
    vrbo_pg       = _lo_photo_grade(vrbo_photos, ll.vrbo_photo_grade if ll else None)
    review_grade  = _lo_review_grade(airbnb_rating, airbnb_reviews)
    if review_grade == "N/A" and vrbo_rating is not None:
        review_grade = _lo_review_grade(vrbo_rating, vrbo_reviews)
    pricing_grade = _lo_pricing_grade(prop)
    occ_grade     = _lo_occ_grade(prop)

    def _gn(g: str) -> float:
        if not g or g == "N/A": return 5.0
        if g.startswith("A"):   return 9.0
        if g.startswith("B"):   return 7.0
        if g.startswith("C"):   return 5.0
        return 3.0

    quality_score = round((_gn(title_grade) + _gn(photo_grade) + 7.0 + 7.0 + _gn(review_grade) + _gn(occ_grade)) / 6, 1)
    quality_letter = ("A" if quality_score >= 8.5 else "B+" if quality_score >= 7.5 else
                      "B" if quality_score >= 6.5 else "C+" if quality_score >= 5.5 else
                      "C" if quality_score >= 4.5 else "D")

    def _gc(g: str) -> str:
        if not g or g == "N/A": return "rgb(var(--muted-foreground))"
        if g.startswith("A"):   return "#16A34A"
        if g.startswith("B"):   return "#2563EB"
        if g.startswith("C"):   return "#D97706"
        return "#DC2626"

    # ── Status ─────────────────────────────────────────────────────────────────
    urgency_map = {
        "critical":       ("CRITICAL",   "red",   "Immediate action needed"),
        "warning":        ("WARNING",    "amber", "Fixable issues found"),
        "overperforming": ("STRONG",     "green", "Above benchmark pace"),
        "onboarding":     ("ONBOARDING", "amber", "New listing"),
    }
    status_val, status_cls, status_sub = urgency_map.get(prop.urgency, ("MODERATE", "amber", "On track"))

    # ── Issue list ─────────────────────────────────────────────────────────────
    issue_list = []
    if ab_ta["issues"]:               issue_list.append("Title")
    if airbnb_photos is not None and int(airbnb_photos) < 20: issue_list.append("Photos")
    if airbnb_rating is not None and float(airbnb_rating) < 4.7: issue_list.append("Reviews")
    if prop.urgency in ("critical", "warning"): issue_list.append("Pricing")
    issues_count = len(issue_list)
    issues_cls   = "red" if issues_count >= 3 else ("amber" if issues_count >= 1 else "green")

    primary_rating  = airbnb_rating  if airbnb_rating  is not None else vrbo_rating
    primary_reviews = airbnb_reviews if airbnb_rating  is not None else vrbo_reviews

    occ_gap   = benchmark.get("occ_gap", 0)
    bench_cls = "green" if occ_gap >= 0 else ""
    bench_parts: list[str] = []
    if airbnb_photos is not None and int(airbnb_photos) < 20: bench_parts.append("Photo count is primary drag")
    if title_grade.startswith(("C", "D")):                    bench_parts.append("title needs work")
    if primary_rating and float(primary_rating) >= 4.75:      bench_parts.append("Rating above average")
    bench_sub = " · ".join(bench_parts) or f"vs {benchmark.get('basis', 'portfolio benchmark')}"

    # ── Priority fixes ─────────────────────────────────────────────────────────
    pf_today: list[str] = []
    pf_week:  list[str] = []
    pf_month: list[str] = []
    if airbnb_photos is not None and int(airbnb_photos) < 10:
        pf_today.append(f"Add photos immediately — only {airbnb_photos} Airbnb photos. Target 20+. Shoot bedrooms, exterior, key amenities.")
    elif airbnb_photos is not None and int(airbnb_photos) < 20:
        pf_week.append(f"Expand Airbnb photo library from {airbnb_photos} → 20+ photos. Add bedrooms, amenities, and exterior shot.")
    if len(ab_ta["issues"]) >= 2:
        pf_today.append(f"Fix Airbnb title — {len(ab_ta['issues'])} violations: {'; '.join(ab_ta['issues'][:2])}")
    elif len(ab_ta["issues"]) == 1:
        pf_week.append(f"Fix Airbnb title: {ab_ta['issues'][0]}")
    if vrbo_title and len(vr_ta["issues"]) >= 1:
        pf_week.append(f"Fix VRBO title: {vr_ta['issues'][0]}")
    if airbnb_rating is not None and float(airbnb_rating) < 4.7:
        pf_week.append(f"Rating {float(airbnb_rating):.2f}★ — add pre-arrival message and 2-hour post-check-in follow-up")
    if prop.urgency == "critical" and prop.booked_14d == 0:
        pf_today.append("Critical status + 0 pickups — verify listing is live and amenity filters are complete")
    pf_month.append("Verify amenity filter completeness in Hostaway and all OTAs (parking, pool, kitchen, washer)")
    pf_month.append("Review description first 295 chars — lead with top USP (key amenities, location, capacity)")
    if prop.urgency in ("ok", "overperforming"):
        pf_month.append("Review pricing for upcoming high-demand dates and local events")

    top3 = ([(t, "r") for t in pf_today] + [(t, "a") for t in pf_week] + [(t, "b") for t in pf_month])[:3]

    # ── CSS (all rules scoped to .lo-wrap) ────────────────────────────────────
    css = (
        ".lo-wrap,.lo-wrap *{box-sizing:border-box}"
        ".lo-wrap{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:rgb(var(--foreground));font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased}"
        ".lo-wrap .platform-row{display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap}"
        ".lo-wrap .plat{display:inline-flex;align-items:center;gap:6px;border-radius:20px;padding:5px 12px;font-size:12px;font-weight:600;border:1.5px solid;text-decoration:none;cursor:pointer}"
        ".lo-wrap .plat:hover{opacity:.82}"
        ".lo-wrap .plat-airbnb{background:#FFF1F2;border-color:#FECDD3;color:#BE123C}"
        ".lo-wrap .plat-vrbo{background:#EFF6FF;border-color:#BFDBFE;color:#1D4ED8}"
        ".lo-wrap .plat-booking{background:#F0FDF4;border-color:#BBF7D0;color:#166534}"
        ".lo-wrap .plat-pms{background:#F5F3FF;border-color:#DDD6FE;color:#5B21B6}"
        ".lo-wrap .plat-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}"
        ".lo-wrap .plat-dim{opacity:.35;filter:grayscale(.7)}"
        ".lo-wrap .metric-row{display:flex;gap:0;background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:10px;overflow:hidden;margin-bottom:16px}"
        ".lo-wrap .metric-card{flex:1;padding:14px 18px;border-right:1px solid rgb(var(--border));min-width:0}"
        ".lo-wrap .metric-card:last-child{border-right:none}"
        ".lo-wrap .metric-card.status-card{border-left:3px solid #EF4444}"
        ".lo-wrap .metric-card.issues-card{border-left:3px solid #EF4444}"
        ".lo-wrap .mc-label{font-size:11px;color:rgb(var(--muted-foreground));font-weight:500;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}"
        ".lo-wrap .mc-value{font-size:18px;font-weight:700;color:rgb(var(--foreground));line-height:1.2}"
        ".lo-wrap .mc-value.red{color:#EF4444}.lo-wrap .mc-value.green{color:#16A34A}.lo-wrap .mc-value.amber{color:#D97706}"
        ".lo-wrap .mc-sub{font-size:10px;color:rgb(var(--muted-foreground));margin-top:2px}"
        ".lo-wrap .benchmark-card{background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:10px;padding:14px 18px;margin-bottom:20px;display:inline-block;min-width:220px}"
        ".lo-wrap .bc-label{font-size:11px;color:rgb(var(--muted-foreground));font-weight:500;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}"
        ".lo-wrap .bc-value{font-size:20px;font-weight:700;color:#EF4444;margin-bottom:2px}"
        ".lo-wrap .bc-value.green{color:#16A34A}"
        ".lo-wrap .bc-sub{font-size:11px;color:rgb(var(--muted-foreground))}"
        ".lo-wrap .main-card{background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:10px;padding:24px 28px;margin-bottom:16px}"
        ".lo-wrap .main-card-title{font-size:16px;font-weight:700;color:rgb(var(--foreground));margin-bottom:4px}"
        ".lo-wrap .main-card-sub{font-size:12px;color:rgb(var(--muted-foreground));margin-bottom:18px}"
        ".lo-wrap .quality-banner{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:12px 16px;margin-bottom:20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}"
        ".lo-wrap .qb-score{font-size:26px;font-weight:800;color:#D97706}"
        ".lo-wrap .qb-label{font-size:12px;font-weight:600;color:rgb(var(--ink-alt))}"
        ".lo-wrap .qb-sub{font-size:11px;color:rgb(var(--muted-foreground));margin-top:1px}"
        ".lo-wrap .qb-grades{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}"
        ".lo-wrap .qb-grade{display:flex;flex-direction:column;align-items:center;background:rgb(var(--surface));border:1px solid rgb(var(--border));border-radius:6px;padding:5px 10px;min-width:52px}"
        ".lo-wrap .qbg-label{font-size:9px;color:rgb(var(--muted-foreground));text-transform:uppercase;letter-spacing:.04em;margin-bottom:2px}"
        ".lo-wrap .qbg-val{font-size:16px;font-weight:800;line-height:1}"
        ".lo-wrap .sec-title{font-size:14px;font-weight:700;color:rgb(var(--foreground));margin-bottom:2px}"
        ".lo-wrap .sec-sub{font-size:12px;color:rgb(var(--muted-foreground));margin-bottom:14px}"
        ".lo-wrap .fix-item{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid rgb(var(--muted))}"
        ".lo-wrap .fix-item:last-child{border-bottom:none}"
        ".lo-wrap .fix-num{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;margin-top:1px}"
        ".lo-wrap .fn-1{background:#FEE2E2;color:#991B1B}.lo-wrap .fn-2{background:#FEF3C7;color:#92400E}.lo-wrap .fn-3{background:#DBEAFE;color:#1E40AF}"
        ".lo-wrap .fix-text{font-size:13px;color:rgb(var(--ink-alt));line-height:1.55}"
        ".lo-wrap .fix-text strong{color:rgb(var(--foreground))}"
        ".lo-wrap .rule{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid rgb(var(--surface-alt));align-items:flex-start}"
        ".lo-wrap .rule:last-child{border-bottom:none}"
        ".lo-wrap .rdot{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;flex-shrink:0;margin-top:2px;font-weight:700}"
        ".lo-wrap .rd-r{background:#FEE2E2;color:#991B1B}.lo-wrap .rd-a{background:#FEF3C7;color:#92400E}.lo-wrap .rd-g{background:#F0FDF4;color:#166534}.lo-wrap .rd-b{background:#EFF6FF;color:#1E40AF}"
        ".lo-wrap .rtxt{font-size:13px;color:rgb(var(--ink-alt));line-height:1.6;flex:1}"
        ".lo-wrap .rtxt strong{color:rgb(var(--foreground));font-weight:600}"
        ".lo-wrap .title-box{font-family:SFMono-Regular,Consolas,monospace;border-radius:6px;padding:10px 14px;font-size:13px;margin-bottom:6px}"
        ".lo-wrap .tb-bad{background:#FFF5F5;border:1px solid #FECACA;color:#991B1B;font-weight:600}"
        ".lo-wrap .tb-good{background:#F0FDF4;border:1px solid #BBF7D0;color:#166534;font-weight:600}"
        ".lo-wrap .tb-neutral{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));color:rgb(var(--ink-alt))}"
        ".lo-wrap .mob-prev{background:#1E293B;border-radius:6px;padding:8px 12px;display:flex;align-items:center;gap:10px;margin:6px 0;font-family:SFMono-Regular,Consolas,monospace;font-size:12px}"
        ".lo-wrap .mp-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:#475569;flex-shrink:0;min-width:52px}"
        ".lo-wrap .mp-v{color:#F1F5F9}.lo-wrap .mp-c{color:#334155}"
        ".lo-wrap .cbar-wrap{margin-bottom:6px}"
        ".lo-wrap .cbar{height:3px;background:rgb(var(--border));border-radius:2px;overflow:hidden}"
        ".lo-wrap .cf{height:3px;border-radius:2px;display:block}"
        ".lo-wrap .cf-g{background:#22C55E}.lo-wrap .cf-a{background:#F59E0B}.lo-wrap .cf-r{background:#EF4444}"
        ".lo-wrap .topt{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:12px 16px;margin-bottom:8px}"
        ".lo-wrap .topt.rec{border-color:#22C55E;background:#F0FDF4}"
        ".lo-wrap .to-lbl{font-size:10px;color:rgb(var(--muted-foreground));text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}"
        ".lo-wrap .to-val{font-family:SFMono-Regular,Consolas,monospace;font-size:14px;font-weight:700;color:rgb(var(--foreground));margin-bottom:6px}"
        ".lo-wrap .to-meta{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.5}"
        ".lo-wrap .photo-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}"
        ".lo-wrap .pc{border-radius:8px;padding:12px;border:1px solid rgb(var(--border))}"
        ".lo-wrap .pc-ok{background:rgb(var(--surface-alt))}.lo-wrap .pc-warn{background:#FFFBEB;border-color:#FDE68A}.lo-wrap .pc-bad{background:#FFF5F5;border-color:#FECACA}.lo-wrap .pc-miss{background:#F5F3FF;border-color:#DDD6FE}"
        ".lo-wrap .pc-num{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:5px}"
        ".lo-wrap .pc-grade{display:inline-flex;border-radius:20px;padding:2px 8px;font-size:11px;font-weight:700;margin-bottom:6px;border:1px solid}"
        ".lo-wrap .pcg-a{background:#F0FDF4;color:#166534;border-color:#BBF7D0}.lo-wrap .pcg-b{background:#EFF6FF;color:#1E40AF;border-color:#BFDBFE}.lo-wrap .pcg-c{background:#FFFBEB;color:#92400E;border-color:#FDE68A}.lo-wrap .pcg-d{background:#FEF2F2;color:#991B1B;border-color:#FECACA}.lo-wrap .pcg-m{background:#F5F3FF;color:#5B21B6;border-color:#DDD6FE}"
        ".lo-wrap .pc-txt{font-size:12px;color:rgb(var(--ink-alt));line-height:1.5}"
        ".lo-wrap .pc-txt strong{color:rgb(var(--foreground))}"
        ".lo-wrap .cov-wrap{margin-bottom:16px}"
        ".lo-wrap .cov-lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:6px}"
        ".lo-wrap .cov-track{height:8px;background:rgb(var(--muted));border-radius:4px;overflow:hidden;display:flex;gap:2px}"
        ".lo-wrap .cov-leg{display:flex;gap:14px;margin-top:5px;flex-wrap:wrap}"
        ".lo-wrap .cov-li{display:flex;align-items:center;gap:5px;font-size:10px;color:rgb(var(--muted-foreground))}"
        ".lo-wrap .cov-dot{width:8px;height:8px;border-radius:2px}"
        ".lo-wrap .rv-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}"
        ".lo-wrap .rv-card{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:6px;padding:10px;text-align:center}"
        ".lo-wrap .rv-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:4px}"
        ".lo-wrap .rv-num{font-size:20px;font-weight:800;margin-bottom:4px}"
        ".lo-wrap .rv-bar{height:3px;background:rgb(var(--border));border-radius:2px;overflow:hidden;max-width:44px;margin:0 auto}"
        ".lo-wrap .rv-fill{height:3px;border-radius:2px}"
        ".lo-wrap .desc-lbl{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:5px}"
        ".lo-wrap .desc{font-family:SFMono-Regular,Consolas,monospace;font-size:12px;border-radius:6px;padding:12px 14px;line-height:1.8;margin-bottom:5px}"
        ".lo-wrap .desc-b{background:#FFF5F5;border:1px solid #FECACA;color:#991B1B}"
        ".lo-wrap .desc-g{background:#F0FDF4;border:1px solid #BBF7D0;color:#166534}"
        ".lo-wrap .char-ct{font-size:10px;color:rgb(var(--muted-foreground));text-align:right;margin-bottom:10px}"
        ".lo-wrap .atags{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:12px}"
        ".lo-wrap .atag{padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid}"
        ".lo-wrap .at-g{background:#F0FDF4;color:#166534;border-color:#BBF7D0}.lo-wrap .at-b{background:#EFF6FF;color:#1E40AF;border-color:#BFDBFE}.lo-wrap .at-a{background:#FFFBEB;color:#92400E;border-color:#FDE68A}.lo-wrap .at-r{background:#FEF2F2;color:#991B1B;border-color:#FECACA}"
        ".lo-wrap .tier-lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}"
        ".lo-wrap .tbl{width:100%;border-collapse:collapse;font-size:12px}"
        ".lo-wrap .tbl th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));font-weight:700;padding:7px 10px;background:rgb(var(--surface-alt));border-bottom:1px solid rgb(var(--border))}"
        ".lo-wrap .tbl td{padding:8px 10px;border-bottom:1px solid rgb(var(--muted));color:rgb(var(--ink-alt));vertical-align:middle}"
        ".lo-wrap .tbl tr:last-child td{border-bottom:none}"
        ".lo-wrap .tbl tr.this td{background:#F0FDF4;color:rgb(var(--foreground));font-weight:600}"
        ".lo-wrap .tbl tr.this td:first-child{border-left:3px solid #22C55E;padding-left:7px}"
        ".lo-wrap .better{color:#166534;font-weight:700}.lo-wrap .worse{color:#DC2626;font-weight:700}.lo-wrap .neut{color:rgb(var(--muted-foreground))}"
        ".lo-wrap .cl-sect{margin-bottom:16px}"
        ".lo-wrap .cl-hd{display:flex;align-items:center;gap:8px;margin-bottom:8px}"
        ".lo-wrap .cl-dot{width:10px;height:10px;border-radius:50%}"
        ".lo-wrap .cl-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}"
        ".lo-wrap .ci{display:flex;gap:10px;align-items:flex-start;padding:8px 12px;border-radius:6px;margin-bottom:4px;border:1px solid}"
        ".lo-wrap .ci-r{background:#FEF2F2;border-color:#FECACA}.lo-wrap .ci-a{background:#FFFBEB;border-color:#FDE68A}.lo-wrap .ci-g{background:#F0FDF4;border-color:#BBF7D0}"
        ".lo-wrap .checkbox{width:14px;height:14px;border-radius:3px;flex-shrink:0;margin-top:2px;border:1.5px solid}"
        ".lo-wrap .cb-r{border-color:#EF4444}.lo-wrap .cb-a{border-color:#F59E0B}.lo-wrap .cb-g{border-color:#22C55E}"
        ".lo-wrap .ci-txt{font-size:12px;color:rgb(var(--ink-alt));line-height:1.5}"
        ".lo-wrap .ci-txt strong{color:rgb(var(--foreground))}"
        ".lo-wrap .rev-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}"
        ".lo-wrap .rev-card{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:12px}"
        ".lo-wrap .rev-card.hl{background:#F0FDF4;border-color:#BBF7D0}"
        ".lo-wrap .rev-lbl{font-size:10px;color:rgb(var(--muted-foreground));text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}"
        ".lo-wrap .rev-val{font-size:20px;font-weight:800;margin-bottom:3px}"
        ".lo-wrap .rv-pos{color:#166534}"
        ".lo-wrap .rev-sub{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.5}"
        ".lo-wrap .roadmap{position:relative;padding-left:24px}"
        ".lo-wrap .roadmap::before{content:'';position:absolute;left:6px;top:4px;bottom:4px;width:2px;background:linear-gradient(180deg,#EF4444 0%,#F59E0B 40%,#3B82F6 100%);border-radius:2px}"
        ".lo-wrap .rm-item{position:relative;margin-bottom:16px}"
        ".lo-wrap .rm-item:last-child{margin-bottom:0}"
        ".lo-wrap .rm-dot{position:absolute;left:-20px;top:4px;width:10px;height:10px;border-radius:50%;border:2px solid rgb(var(--surface));box-shadow:0 0 0 2px currentColor}"
        ".lo-wrap .rmd-r{color:#EF4444;background:#EF4444}.lo-wrap .rmd-a{color:#F59E0B;background:#F59E0B}.lo-wrap .rmd-b{color:#3B82F6;background:#3B82F6}"
        ".lo-wrap .rm-ph{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}"
        ".lo-wrap .rmp-r{color:#DC2626}.lo-wrap .rmp-a{color:#D97706}.lo-wrap .rmp-b{color:#1D4ED8}"
        ".lo-wrap .rm-ul{list-style:none;padding:0;margin:0}"
        ".lo-wrap .rm-ul li{font-size:12px;color:rgb(var(--muted-foreground));padding:2px 0;display:flex;align-items:flex-start;gap:6px}"
        ".lo-wrap .rm-ul li::before{content:'\\2192';color:rgb(var(--muted-foreground));flex-shrink:0;font-size:11px;margin-top:1px}"
        ".lo-wrap .two-col{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}"
        ".lo-wrap .usp-card{background:rgb(var(--surface-alt));border:1px solid rgb(var(--border));border-radius:8px;padding:10px}"
        ".lo-wrap .usp-name{font-size:12px;font-weight:700;margin-bottom:3px}"
        ".lo-wrap .usp-desc{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.5}"
        ".lo-wrap .seg-card{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px}"
        ".lo-wrap .seg-name{font-size:12px;font-weight:700;color:#1E40AF;margin-bottom:3px}"
        ".lo-wrap .seg-desc{font-size:11px;color:rgb(var(--ink-alt));line-height:1.5}"
        ".lo-wrap .gf-block{background:linear-gradient(135deg,#FFFBEB,#FEF9E7);border:1.5px solid #FDE68A;border-radius:8px;padding:14px 16px;margin-bottom:12px;display:flex;gap:12px;align-items:flex-start}"
        ".lo-wrap .gf-icon{font-size:24px;flex-shrink:0}"
        ".lo-wrap .gf-title{font-size:13px;font-weight:700;color:#92400E;margin-bottom:3px}"
        ".lo-wrap .gf-body{font-size:12px;color:rgb(var(--ink-alt));line-height:1.6}"
        ".lo-wrap .gf-body strong{color:rgb(var(--foreground))}"
        ".lo-wrap .consistency-note{background:#FFF5F5;border:1px solid #FECACA;border-radius:6px;padding:10px 14px;font-size:12px;color:#991B1B;margin-bottom:10px}"
        ".lo-wrap .divider{height:1px;background:rgb(var(--muted));margin:18px 0}"
        ".lo-wrap .note{font-size:11px;color:rgb(var(--muted-foreground));line-height:1.6;margin-top:8px}"
        "@media(max-width:680px){.lo-wrap .metric-row{flex-wrap:wrap}.lo-wrap .metric-card{min-width:45%}.lo-wrap .photo-grid,.lo-wrap .two-col,.lo-wrap .rev-grid{grid-template-columns:1fr}.lo-wrap .rv-grid{grid-template-columns:1fr 1fr}.lo-wrap .qb-grades{margin-left:0;margin-top:10px}}"
    )

    # ── Platform badges ────────────────────────────────────────────────────────
    def _plat_badge(cls, dot_color, label, href):
        tag = "a" if href else "div"
        attrs = f' href="{_esc(href)}" target="_blank" rel="noopener"' if href else ""
        dim = "" if href else " plat-dim"
        return (f'<{tag} class="plat {cls}{dim}"{attrs}>'
                f'<div class="plat-dot" style="background:{dot_color}"></div>{_esc(label)}</{tag}>')

    pms_label = getattr(prop, "customization_group", "") or getattr(prop, "city", "") or "PMS"
    platform_row = (
        '<div class="platform-row">'
        + _plat_badge("plat-airbnb",  "#FF5A5F", "Airbnb",       airbnb_url)
        + _plat_badge("plat-vrbo",    "#1C5BD9", "VRBO",         vrbo_url)
        + _plat_badge("plat-booking", "#003580", "Booking.com",  booking_url)
        + _plat_badge("plat-pms",     "#7C3AED", _esc(pms_label), "")
        + '</div>'
    )

    # ── Metric cards ───────────────────────────────────────────────────────────
    rating_disp = (f"{float(primary_rating):.2f} ★" if primary_rating is not None else "N/A")
    rating_cls  = ("green" if primary_rating and float(primary_rating) >= 4.85
                   else "" if primary_rating and float(primary_rating) >= 4.7 else "amber")
    reviews_sub = f"{primary_reviews} reviews" if primary_reviews else "No reviews synced"
    photos_disp = (f"{airbnb_photos} / 20+" if airbnb_photos is not None else "N/A")
    photos_cls  = "red" if (airbnb_photos is None or int(airbnb_photos) < 10) else ("amber" if int(airbnb_photos) < 20 else "green")

    def _mc(label, value, sub, extra_cls=""):
        return (f'<div class="metric-card{" " + extra_cls if extra_cls else ""}">'
                f'<div class="mc-label">{label}</div>'
                f'<div class="mc-value {_esc(value[1])}">{_esc(value[0])}</div>'
                f'<div class="mc-sub">{_esc(sub)}</div></div>')

    metric_row = (
        '<div class="metric-row">'
        + _mc("Status",         (status_val, status_cls),   status_sub,     "status-card")
        + _mc("Overall Rating", (rating_disp, rating_cls),  reviews_sub)
        + _mc("5-Star Rate",    ("N/A", ""),                "Not synced — check host dashboard")
        + _mc("Guest Favorite", ("Active ✓" if (primary_rating and float(primary_rating) >= 4.8 and primary_reviews and int(primary_reviews) >= 5) else "Check", "green" if primary_rating and float(primary_rating) >= 4.8 else "amber"),
              "Review in Airbnb host dashboard")
        + _mc("Photos",         (photos_disp, photos_cls),  "Below benchmark" if airbnb_photos and int(airbnb_photos) < 20 else "Competitive count")
        + _mc("Issues",         (str(issues_count), issues_cls), " · ".join(issue_list[:4]) or "None flagged", "issues-card")
        + '</div>'
    )

    # ── Benchmark card ─────────────────────────────────────────────────────────
    benchmark_card = (
        f'<div class="benchmark-card">'
        f'<div class="bc-label">Vs {_esc(benchmark.get("basis", "portfolio"))} Benchmark</div>'
        f'<div class="bc-value {bench_cls}">{occ_gap:+.1f} pts occ</div>'
        f'<div class="bc-sub">{_esc(bench_sub)}</div>'
        f'</div>'
    )

    # ── Quality banner ─────────────────────────────────────────────────────────
    grades_html = "".join(
        f'<div class="qb-grade"><div class="qbg-label">{lbl}</div>'
        f'<div class="qbg-val" style="color:{_gc(g)}">{_esc(g)}</div></div>'
        for lbl, g in [("Title", title_grade), ("Images", photo_grade),
                       ("Reviews", review_grade), ("Pricing", pricing_grade),
                       ("Occ", occ_grade), ("Amenities", "B")]
    )
    quality_banner = (
        f'<div class="quality-banner">'
        f'<div><div class="qb-score">{quality_score} / 10</div>'
        f'<div class="qb-label">Evidence-Based Quality Score</div>'
        f'<div class="qb-sub">Grade {_esc(quality_letter)} · {issues_count} issue(s) flagged · AI analysis below</div></div>'
        f'<div class="qb-grades">{grades_html}</div>'
        f'</div>'
    )

    # ── Top 3 fixes ────────────────────────────────────────────────────────────
    fn_cls = ["fn-1", "fn-2", "fn-3"]
    fix_rows = "".join(
        f'<div class="fix-item"><div class="fix-num {fn_cls[i]}">{i+1}</div>'
        f'<div class="fix-text">{_esc(txt)}</div></div>'
        for i, (txt, _) in enumerate(top3)
    )
    top_fixes = (
        f'<div class="sec-title">Top Fixes</div>'
        f'<div class="sec-sub" style="margin-bottom:10px">Highest-impact actions across the entire listing</div>'
        + fix_rows
    )

    # ── Title optimization ─────────────────────────────────────────────────────
    if airbnb_title:
        tb_cls  = "tb-bad" if ab_ta["issues"] else "tb-good"
        issue_count_txt = f'{len(ab_ta["issues"])} violation{"s" if len(ab_ta["issues"]) != 1 else ""} found' if ab_ta["issues"] else "No violations found"
        mobile_current  = _esc(airbnb_title[:32])
        mobile_rest     = _esc(airbnb_title[32:]) if len(airbnb_title) > 32 else ""
        mobile_rest_html = f'<span class="mp-c">{mobile_rest}…</span>' if mobile_rest else ""
        char_pct = min(100, int(ab_ta["char_count"] / 50 * 100))
        bar_cls  = "cf-g" if ab_ta["char_count"] <= 40 else ("cf-a" if ab_ta["char_count"] <= 50 else "cf-r")
        rule_rows = ""
        for iss in ab_ta["issues"]:
            rule_rows += f'<div class="rule"><div class="rdot rd-r">✕</div><div class="rtxt"><strong>Issue:</strong> {_esc(iss)}</div></div>'
        for pas in ab_ta["passes"]:
            rule_rows += f'<div class="rule"><div class="rdot rd-g">✓</div><div class="rtxt">{_esc(pas)}</div></div>'
        if vrbo_title and vr_ta["issues"]:
            for iss in vr_ta["issues"]:
                rule_rows += f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt"><strong>VRBO:</strong> {_esc(iss)}</div></div>'
        title_section = (
            f'<div class="sec-title">Title Optimization</div>'
            f'<div class="sec-sub">{_esc(issue_count_txt)} · Platform rules applied</div>'
            f'<div class="title-box {tb_cls}">"{_esc(airbnb_title)}" — {ab_ta["char_count"]} chars</div>'
            f'<div style="margin-bottom:14px">'
            f'<div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:5px">Mobile truncation (32 chars visible)</div>'
            f'<div class="mob-prev"><span class="mp-lbl">Current</span><span class="mp-v">{mobile_current}</span>{mobile_rest_html}</div>'
            f'</div>'
            f'<div class="cbar-wrap"><div class="cbar"><div class="cf {bar_cls}" style="width:{char_pct}%"></div></div></div>'
            + rule_rows
            + f'<div style="margin-top:14px"><div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:rgb(var(--muted-foreground));margin-bottom:8px">Recommended rewrites</div>'
            f'<div class="topt rec"><div class="to-lbl">★ Recommended — remove violations, lead with top USP</div>'
            f'<div class="to-val">{_esc(prop.property_name)}, {prop.bedrooms}BR — [your top amenity]</div>'
            f'<div class="cbar-wrap"><div class="cbar"><div class="cf cf-g" style="width:72%"></div></div></div>'
            f'<div class="to-meta">Sentence case ✓ · No redundant terms · Add your #1 Smokies amenity (hot tub, view, indoor pool, game room, theater, pet-friendly) before 32-char mobile cut</div></div>'
            f'<div class="topt"><div class="to-lbl">Option 2 — location-first</div>'
            f'<div class="to-val">{_esc(prop.property_name)}, {_esc(prop.area or "")}, {prop.bedrooms}BR</div>'
            f'<div class="to-meta">Area lead targets guests searching by location · Sentence case ✓</div></div>'
            f'</div>'
        )
    else:
        title_section = (
            f'<div class="sec-title">Title Optimization</div>'
            f'<div class="sec-sub">Title not synced</div>'
            f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt">Title not synced — check in Hostaway or the OTA host dashboard to run analysis.</div></div>'
        )

    # ── Image analysis ─────────────────────────────────────────────────────────
    def _photo_pcls(g: str) -> str:
        return {"A": "pcg-a", "B": "pcg-b", "C": "pcg-c", "D": "pcg-d"}.get(g[0] if g else "", "pcg-m")

    def _pc_block(platform, count, grade, card_cls):
        if count is None:
            return (f'<div class="pc pc-miss"><div class="pc-num">{_esc(platform)}</div>'
                    f'<span class="pc-grade pcg-m">Not synced</span>'
                    f'<div class="pc-txt">Photo count not in marketing data — update marketing_links.csv.</div></div>')
        cnt = int(count)
        note = (f"<strong>{cnt} photos</strong> — target 20+ minimum" if cnt < 20 else f"<strong>{cnt} photos</strong> — competitive count")
        return (f'<div class="pc {card_cls}"><div class="pc-num">{_esc(platform)}</div>'
                f'<span class="pc-grade {_photo_pcls(grade)}">{_esc(grade)}</span>'
                f'<div class="pc-txt">{note}</div></div>')

    ab_card_cls = "pc-bad" if photo_grade.startswith("D") else ("pc-warn" if photo_grade.startswith("C") else "pc-ok")
    vr_card_cls = "pc-bad" if vrbo_pg.startswith("D") else ("pc-warn" if vrbo_pg.startswith("C") else "pc-ok")
    total_photos = (airbnb_photos or 0)
    cov_pct = min(100, int(total_photos / 30 * 100))
    photo_section = (
        f'<div class="sec-title">Image Analysis</div>'
        f'<div class="sec-sub">{total_photos if airbnb_photos else "?"} Airbnb photos total</div>'
        f'<div class="cov-wrap">'
        f'<div class="cov-lbl">Photo count vs 30-photo benchmark</div>'
        f'<div class="cov-track"><div style="background:#3B82F6;width:{cov_pct}%;height:8px"></div>'
        f'<div style="background:rgb(var(--border));width:{100-cov_pct}%;height:8px"></div></div>'
        f'<div class="cov-leg">'
        f'<div class="cov-li"><div class="cov-dot" style="background:#3B82F6"></div>Current: {total_photos} photos ({cov_pct}%)</div>'
        f'<div class="cov-li"><div class="cov-dot" style="background:rgb(var(--border))"></div>Gap to 30-photo benchmark</div>'
        f'</div></div>'
        f'<div class="photo-grid">'
        + _pc_block("Airbnb", airbnb_photos, photo_grade, ab_card_cls)
        + _pc_block("VRBO",   vrbo_photos,   vrbo_pg,     vr_card_cls)
        + f'<div class="pc pc-miss"><div class="pc-num" style="color:#5B21B6">Manual audit — required</div>'
        f'<span class="pc-grade pcg-m">Checklist</span>'
        f'<div class="pc-txt">Cover photo sells the primary reason to book? · Hot tub/view/pool/game room documented? · Every bedroom shown individually? · Exterior shot included?</div></div>'
        f'<div class="pc pc-ok"><div class="pc-num">Photo order</div>'
        f'<span class="pc-grade pcg-b">Review</span>'
        f'<div class="pc-txt">Lead with the WOW shot. Secondary spaces (laundry, garage) should be last. Avoid 3 angles of the same room.</div></div>'
        f'</div>'
        f'<div class="rule"><div class="rdot rd-b">→</div><div class="rtxt"><strong>Fastest win:</strong> Delete any redundant angles of the same room and replace with a bedroom or amenity photo — zero cost, immediate CTR improvement.</div></div>'
    )

    # ── Description guidance ───────────────────────────────────────────────────
    desc_section = (
        f'<div class="sec-title">Description Optimization</div>'
        f'<div class="sec-sub">First 295 chars = only text visible before "show more" on mobile</div>'
        f'<div class="rule"><div class="rdot rd-r">✕</div><div class="rtxt"><strong>★★ and ✹ symbols in description body</strong> violate Airbnb content policy — may suppress search visibility. Replace with ALL CAPS plain-text headers.</div></div>'
        f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt"><strong>Lead with your #1 Smokies experience amenity in the first sentence.</strong> Guests scan the first 50 words. Put the strongest USP (hot tub, mountain view, indoor pool, game room, theater, pet-friendly setup, fire pit, unique design) before the fold.</div></div>'
        f'<div class="rule"><div class="rdot rd-g">✓</div><div class="rtxt"><strong>Optimized 295-char preview template:</strong> "[Top amenity] at [property name] — [#2 USP]. [Bedrooms/beds] at [location context]. [#3 USP], [#4 USP], sleeps [capacity]."</div></div>'
        f'<div style="margin-top:12px">'
        f'<div class="desc-lbl">Description data not synced — review in Airbnb host dashboard</div>'
        f'<div class="note">Check: does your mobile preview lead with key amenities or start with generic phrases like "Welcome to" or "Discover comfort"? If yes, rewrite the first paragraph.</div>'
        f'</div>'
    )

    # ── Amenity audit ──────────────────────────────────────────────────────────
    beds_br = f"{prop.bedrooms}BR"
    amenity_section = (
        f'<div class="sec-title">Amenity Audit</div>'
        f'<div class="sec-sub">Review filter completeness in Airbnb host dashboard and Hostaway</div>'
        f'<div class="tier-lbl" style="color:#166534">Tier 1 — Smokies conversion drivers (verify when present)</div>'
        f'<div class="atags">'
        f'<span class="atag at-g">Hot Tub</span><span class="atag at-g">Mountain View</span>'
        f'<span class="atag at-g">Fast Wi-Fi</span><span class="atag at-g">Full Kitchen</span>'
        f'<span class="atag at-g">Free Parking</span><span class="atag at-g">Keyless Entry</span>'
        f'</div>'
        f'<div class="tier-lbl" style="color:#1E40AF">Tier 2 — Premium experience filters (verify enabled + photographed)</div>'
        f'<div class="atags">'
        f'<span class="atag at-b">Indoor Pool</span><span class="atag at-b">Game Room</span>'
        f'<span class="atag at-b">Theater Room</span><span class="atag at-b">Fire Pit</span>'
        f'<span class="atag at-b">Covered Deck + Grill</span><span class="atag at-b">Pet Friendly</span>'
        f'</div>'
        f'<div class="tier-lbl" style="color:#92400E">Premium gaps to investigate</div>'
        f'<div class="atags">'
        f'<span class="atag at-a">EV charger?</span><span class="atag at-a">Sauna / cold plunge?</span>'
        f'<span class="atag at-a">Bunk room / kid amenities?</span><span class="atag at-a">Coffee bar / standout design?</span>'
        f'<span class="atag at-r">Any high-impact amenity listed with zero photo evidence?</span>'
        f'</div>'
    )

    # ── Star rating analysis ───────────────────────────────────────────────────
    def _rv_card(label, val_str, color, pct):
        return (f'<div class="rv-card"><div class="rv-lbl">{_esc(label)}</div>'
                f'<div class="rv-num" style="color:{color}">{_esc(val_str)}</div>'
                f'<div class="rv-bar"><div class="rv-fill" style="width:{pct}%;background:{color}"></div></div>'
                f'</div>')

    rv_cards = ""
    if airbnb_rating is not None:
        r = float(airbnb_rating)
        rc = "#16A34A" if r >= 4.9 else ("#2563EB" if r >= 4.7 else "#D97706")
        rv_cards += _rv_card("Airbnb", f"{r:.2f}", rc, r / 5 * 100)
    if vrbo_rating is not None:
        vr = float(vrbo_rating)
        vc = "#16A34A" if vr >= 4.9 else ("#2563EB" if vr >= 4.7 else "#D97706")
        rv_cards += _rv_card("VRBO", f"{vr:.2f}", vc, vr / 5 * 100)
    for lbl, val in [("Cleanliness", vrbo_clean), ("Check-in", vrbo_checkin),
                     ("Comm.", vrbo_comm), ("Location", vrbo_loc)]:
        if val is not None:
            v = float(val)
            vc = "#16A34A" if v >= 4.9 else ("#2563EB" if v >= 4.7 else "#D97706")
            rv_cards += _rv_card(lbl, f"{v:.1f}", vc, v / 5 * 100)
    if not rv_cards:
        rv_cards = '<div class="rv-card" style="grid-column:span 3"><div class="rv-lbl">Ratings</div><div class="rv-num" style="color:rgb(var(--muted-foreground));font-size:14px">Not synced</div></div>'

    rating_section = (
        f'<div class="sec-title">Star Rating Analysis</div>'
        f'<div class="sec-sub">'
        + (f'{primary_reviews} reviews · {float(primary_rating):.2f} overall' if primary_rating else 'Not synced')
        + f'</div>'
        f'<div class="rv-grid">{rv_cards}</div>'
        f'<div class="rule"><div class="rdot rd-a">!</div><div class="rtxt"><strong>4-star gap is the primary lever.</strong> A pre-arrival message with local tips and a 2-hour post-check-in follow-up converts borderline stays into 5-star reviews.</div></div>'
        f'<div class="rule"><div class="rdot rd-g">✓</div><div class="rtxt">'
        + (f'<strong>Rating {float(primary_rating):.2f}★ signals rate headroom.</strong> A modest 8–12% increase on peak weekends is unlikely to move the value score negatively.' if primary_rating and float(primary_rating) >= 4.8 else '<strong>Focus on operational consistency</strong> — cleanliness and check-in are the most actionable levers for rating improvement.')
        + f'</div></div>'
    )

    # ── Guest Favorite ─────────────────────────────────────────────────────────
    gf_active = (primary_rating is not None and float(primary_rating) >= 4.8
                 and primary_reviews is not None and int(primary_reviews) >= 5)
    gf_section = (
        f'<div class="sec-title">Guest Favorite &amp; Review Status</div>'
        f'<div class="gf-block"><div class="gf-icon">★</div><div>'
        f'<div class="gf-title">{"Guest Favorites badge likely active" if gf_active else "Guest Favorites status — verify in host dashboard"}</div>'
        f'<div class="gf-body">'
        + (f'Rating {float(primary_rating):.2f}★ with {primary_reviews} reviews. <strong>Airbnb Guest Favorites badge appears in search results and improves CTR.</strong> At this rating, a single 3-star review could risk the badge — cleanliness and check-in are the most important operational levers.' if gf_active else 'Guest Favorites requires 4.8+ overall with sufficient reviews. Focus on consistent 5-star cleanliness and communication to reach the threshold.')
        + f'</div></div></div>'
    )

    # ── Consistency check ──────────────────────────────────────────────────────
    cons_note = ""
    if airbnb_photos is not None and int(airbnb_photos) < 20:
        cons_note = f'<div class="consistency-note">Only {airbnb_photos} photos — amenities listed in description or filters may not be visually verified for guests. Add photos for every key amenity you list.</div>'
    consistency_section = (
        f'<div class="sec-title">Consistency Check</div>'
        f'<div class="sec-sub">Description ↔ Amenities ↔ Photos</div>'
        + cons_note
        + f'<div style="overflow-x:auto"><table class="tbl">'
        f'<thead><tr><th>Claim</th><th>Description</th><th>Amenities</th><th>Photos</th><th>Status</th></tr></thead>'
        f'<tbody>'
        f'<tr class="this"><td>{prop.bedrooms}BR layout</td><td>—</td><td>✓</td><td>{"✓" if airbnb_photos and int(airbnb_photos) >= 20 else "✕ verify"}</td><td class="{"better" if airbnb_photos and int(airbnb_photos) >= 20 else "worse"}">{"Consistent" if airbnb_photos and int(airbnb_photos) >= 20 else "Add bedroom photos"}</td></tr>'
        f'<tr><td>Key amenities</td><td>—</td><td>✓</td><td>{"✓" if airbnb_photos and int(airbnb_photos) >= 15 else "✕ gaps likely"}</td><td class="neut" style="color:#D97706;font-weight:700">Verify each amenity has a photo</td></tr>'
        f'<tr><td>Title claims</td><td>—</td><td>—</td><td>—</td><td class="neut">Review manually in host dashboard</td></tr>'
        f'</tbody></table></div>'
    )

    # ── Positioning ────────────────────────────────────────────────────────────
    area = _esc(prop.area or "this area")
    positioning_section = (
        f'<div class="sec-title">Positioning &amp; Target Segments</div>'
        f'<div class="two-col" style="margin-top:10px">'
        f'<div class="usp-card"><div class="usp-name">Location — {area}</div><div class="usp-desc">Highlight proximity to top attractions, restaurants, or transport. Should appear in the first sentence of the description.</div></div>'
        f'<div class="usp-card"><div class="usp-name">{prop.bedrooms}BR layout</div><div class="usp-desc">Group-size signal. Should appear in the title and in the first 50 words of the description.</div></div>'
        f'<div class="usp-card"><div class="usp-name">Top Smokies experience amenity (verify)</div><div class="usp-desc">Your highest-converting amenity (hot tub, mountain view, indoor pool, game room, theater, fire pit, pet friendly, EV charger, sauna, unique design) should appear in the title, first sentence, and cover photo when present.</div></div>'
        f'<div class="usp-card"><div class="usp-name">Review strength</div><div class="usp-desc">'
        + (f'Rating {float(primary_rating):.2f}★ is a competitive advantage — mention "highly-rated" or "guest favorite" in your description.' if primary_rating and float(primary_rating) >= 4.8 else 'Build reviews through proactive guest communication before and after check-in.')
        + f'</div></div></div>'
        f'<div class="two-col">'
        f'<div class="seg-card"><div class="seg-name">Primary segment</div><div class="seg-desc">Identify and optimize for your highest-ADR guest type (couples, families, large groups, remote workers, pet owners, luxury travelers) — verify amenity filters match their searches.</div></div>'
        f'<div class="seg-card"><div class="seg-name">Seasonal premium (verify)</div><div class="seg-desc">Identify peak events in {area} (festivals, holidays, sports) and set custom pricing windows before competitors.</div></div>'
        f'</div>'
    )

    # ── Top performer comparison ───────────────────────────────────────────────
    bench_basis   = _esc(benchmark.get("basis", "portfolio"))
    bench_occ     = benchmark.get("occ_60d", 0)
    bench_price   = benchmark.get("base_price")
    bench_size    = benchmark.get("sample_size", 0)
    my_occ_pct    = f"{prop.adj_occ_60d:.0%}"
    bench_occ_pct = f"{bench_occ:.1f}%"
    occ_pos       = "better" if occ_gap >= 0 else "worse"
    price_pos     = ("better" if bench_price and prop.base_price > bench_price else
                     "worse"  if bench_price and prop.base_price < bench_price else "neut")
    comparison_section = (
        f'<div class="sec-title">Top Performer Comparison</div>'
        f'<div class="sec-sub">{bench_basis} benchmark ({bench_size} listings)</div>'
        f'<div style="overflow-x:auto;margin-top:10px"><table class="tbl">'
        f'<thead><tr><th>Signal</th><th>{_esc(prop.property_name)}</th><th>{bench_basis} Average</th><th>Position</th></tr></thead>'
        f'<tbody>'
        f'<tr class="this"><td>60-day occupancy</td><td>{my_occ_pct}</td><td>{bench_occ_pct}</td>'
        f'<td class="{occ_pos}">{"Above avg" if occ_gap >= 0 else "Below avg"}</td></tr>'
        f'<tr><td>Base price</td><td>${prop.base_price:.0f}</td>'
        f'<td>{"$" + str(int(bench_price)) if bench_price else "N/A"}</td>'
        f'<td class="{price_pos}">{"Above avg" if price_pos == "better" else "Below avg" if price_pos == "worse" else "—"}</td></tr>'
        f'<tr><td>Overall rating</td><td>{"★ " + str(float(primary_rating)) if primary_rating else "N/A"}</td>'
        f'<td>Benchmark N/A</td>'
        f'<td class="{"better" if primary_rating and float(primary_rating) >= 4.75 else "neut"}">{"Strong" if primary_rating and float(primary_rating) >= 4.75 else "Verify"}</td></tr>'
        f'<tr><td>Photo count</td><td>{airbnb_photos or "N/A"}</td><td>20–35 photos</td>'
        f'<td class="{"better" if airbnb_photos and int(airbnb_photos) >= 20 else "worse"}">{"Competitive" if airbnb_photos and int(airbnb_photos) >= 20 else "Critical gap"}</td></tr>'
        f'<tr><td>Title violations</td><td>{len(ab_ta["issues"])} violation{"s" if len(ab_ta["issues"]) != 1 else ""}</td><td>0–1 typical</td>'
        f'<td class="{"better" if not ab_ta["issues"] else "worse"}">{"Clean" if not ab_ta["issues"] else "Below avg"}</td></tr>'
        f'</tbody></table></div>'
    )

    # ── Pricing strategy ───────────────────────────────────────────────────────
    pricing_section = (
        f'<div class="sec-title">Pricing &amp; Promotion Strategy</div>'
        f'<div class="sec-sub">Urgency: {_esc(prop.urgency.upper())} · 60-day occ: {prop.adj_occ_60d:.0%} · Booked next 14d: {prop.booked_14d}</div>'
        f'<div class="rev-grid" style="margin-top:10px">'
        + (f'<div class="rev-card hl"><div class="rev-lbl">Peak / Event Windows</div><div class="rev-val rv-pos">+20–40%</div><div class="rev-sub">Identify top 3 demand spikes in {area} — set custom pricing 90 days ahead.</div></div>'
           if prop.urgency in ("ok", "overperforming") else
           f'<div class="rev-card"><div class="rev-lbl">Price Position</div><div class="rev-val" style="color:#EF4444">{_esc(prop.urgency.upper())}</div><div class="rev-sub">Check posted rate vs market booked price before discounting. Use date-level adjustments, not broad base cuts.</div></div>')
        + f'<div class="rev-card"><div class="rev-lbl">Weekly Discount (7+ nights)</div><div class="rev-val">10%</div><div class="rev-sub">Visible in Airbnb search — targets long-stay and multi-family segments.</div></div>'
        f'<div class="rev-card"><div class="rev-lbl">Early Bird (60–90 days out)</div><div class="rev-val">10–15%</div><div class="rev-sub">Lock in peak-week bookings before competitors fill those dates.</div></div>'
        f'<div class="rev-card"><div class="rev-lbl">Shoulder Season</div><div class="rev-val">15–20%</div><div class="rev-sub">3+ consecutive mid-week nights at a custom rate fills gaps without discounting weekends.</div></div>'
        f'</div>'
    )

    # ── Ranked checklist ───────────────────────────────────────────────────────
    def _ci(txt, tier):
        cls_map = {"r": ("ci-r", "cb-r"), "a": ("ci-a", "cb-a"), "g": ("ci-g", "cb-g")}
        ci_c, cb_c = cls_map.get(tier, ("ci-g", "cb-g"))
        return (f'<div class="ci {ci_c}"><div class="checkbox {cb_c}"></div>'
                f'<div class="ci-txt">{txt}</div></div>')

    checklist_section = (
        f'<div class="sec-title">Ranked Fix Checklist</div>'
        f'<div style="margin-top:12px">'
        f'<div class="cl-sect"><div class="cl-hd"><div class="cl-dot" style="background:#EF4444"></div>'
        f'<div class="cl-title" style="color:#991B1B">Critical — Do Today</div></div>'
        + "".join(_ci(f"<strong>{_esc(t)}</strong>", "r") for t in pf_today[:4])
        + (f'<div class="ci ci-r"><div class="checkbox cb-r"></div><div class="ci-txt"><strong>Verify listing is active on all channels</strong> — check Hostaway channel manager sync status.</div></div>' if not pf_today else "")
        + f'</div>'
        f'<div class="cl-sect"><div class="cl-hd"><div class="cl-dot" style="background:#F59E0B"></div>'
        f'<div class="cl-title" style="color:#92400E">High Priority — This Week</div></div>'
        + "".join(_ci(f"<strong>{_esc(t)}</strong>", "a") for t in pf_week[:5])
        + f'<div class="ci ci-a"><div class="checkbox cb-a"></div><div class="ci-txt"><strong>Rewrite description 295-char preview</strong> — replace generic opener with king-beds, top amenity, and location lead.</div></div>'
        + f'</div>'
        f'<div class="cl-sect"><div class="cl-hd"><div class="cl-dot" style="background:#22C55E"></div>'
        f'<div class="cl-title" style="color:#166534">Optimization — This Month</div></div>'
        + "".join(_ci(f"<strong>{_esc(t)}</strong>", "g") for t in pf_month[:4])
        + f'</div></div>'
    )

    # ── 90-day roadmap ─────────────────────────────────────────────────────────
    rm_w1 = (["Fix Airbnb title — remove all violations"] if ab_ta["issues"] else []) + \
            (["Add missing photos — start with bedrooms, then key amenities"] if airbnb_photos and int(airbnb_photos) < 20 else []) + \
            ["Rewrite description 295-char preview with top USP lead",
             "Verify amenity filters match all listed features in Hostaway"]
    rm_w2 = ["Complete photo library to 20+ images (bedrooms, amenities, exterior)",
             "Remove ★★ and special symbols from description body",
             "Set weekly discount (10%) and early bird (10–15%) in pricing tool",
             "Add building exterior and key amenity photos to gallery"]
    rm_m2 = ["Implement pre-arrival message template with local tips",
             "Add 2-hour post-check-in follow-up message",
             "Identify peak event windows and set custom pricing",
             "Target 4.85+ overall to lock in Guest Favorites badge"]

    def _rm_li(items):
        return "".join(f"<li>{_esc(i)}</li>" for i in items)

    roadmap_section = (
        f'<div class="sec-title">90-Day Action Roadmap</div>'
        f'<div style="margin-top:14px"><div class="roadmap">'
        f'<div class="rm-item"><div class="rm-dot rmd-r"></div>'
        f'<div class="rm-ph rmp-r">Week 1 — Quick wins (no-cost, immediate impact)</div>'
        f'<ul class="rm-ul">{_rm_li(rm_w1)}</ul></div>'
        f'<div class="rm-item"><div class="rm-dot rmd-a"></div>'
        f'<div class="rm-ph rmp-a">Weeks 2–4 — Photo build-out &amp; listing polish</div>'
        f'<ul class="rm-ul">{_rm_li(rm_w2)}</ul></div>'
        f'<div class="rm-item"><div class="rm-dot rmd-b"></div>'
        f'<div class="rm-ph rmp-b">Month 2–3 — Guest experience &amp; revenue optimization</div>'
        f'<ul class="rm-ul">{_rm_li(rm_m2)}</ul></div>'
        f'</div></div>'
        f'<div style="margin-top:16px;padding:12px 14px;background:rgb(var(--surface-alt));border-radius:6px;border:1px solid rgb(var(--border));font-size:11px;color:rgb(var(--muted-foreground));line-height:1.6">'
        f'<strong style="color:rgb(var(--ink-alt))">Protecting the review score while building the photo set to 20+ images are the two highest-impact moves over the next 60 days.</strong> '
        f'Base price changes should only follow after listing quality improvements are in place.'
        f'</div>'
    )

    # ── Assemble ───────────────────────────────────────────────────────────────
    divider = '<div class="divider"></div>'
    return (
        f'<style>{css}</style>'
        f'<div class="lo-wrap">'
        + platform_row
        + metric_row
        + benchmark_card
        + f'<div class="main-card">'
        + f'<div class="main-card-title">Listing Optimizer</div>'
        + f'<div class="main-card-sub">{_esc(prop.property_name)} · {_esc(prop.area or "")} · {prop.bedrooms}BR · {_esc(prop.urgency.upper())} status</div>'
        + quality_banner
        + top_fixes
        + divider
        + title_section
        + divider
        + photo_section
        + divider
        + desc_section
        + divider
        + amenity_section
        + divider
        + rating_section
        + divider
        + gf_section
        + divider
        + consistency_section
        + divider
        + positioning_section
        + divider
        + comparison_section
        + divider
        + pricing_section
        + divider
        + checklist_section
        + divider
        + roadmap_section
        + divider
        + f'<div class="sec-title">AI Deep Analysis</div>'
        + f'<div class="sec-sub">Streaming analysis based on your full property context</div>'
        + f'<div id="lo-ai-body" class="note" style="font-size:13px;color:rgb(var(--muted-foreground))">'
        + f'<p style="color:rgb(var(--muted-foreground));font-style:italic">Loading AI analysis…</p>'
        + f'</div>'
        + f'</div>'  # /main-card
        + f'</div>'  # /lo-wrap
    )


def _group_label(prop: Property) -> str:
    parts = [
        getattr(prop, "customization_group", ""),
        getattr(prop, "customization_sub_group", ""),
    ]
    return " / ".join(part for part in parts if part) or getattr(prop, "city", "") or "Ungrouped"


def _action_context(prop: Property) -> dict:
    return {
        "property": prop.name,
        "listing_id": getattr(prop, "listing_id", ""),
        "pms_name": getattr(prop, "pms_name", ""),
        "group": getattr(prop, "customization_group", ""),
        "subgroup": getattr(prop, "customization_sub_group", ""),
        "city": getattr(prop, "city", ""),
        "group_label": _group_label(prop),
        "system": "PriceLabs",
    }


def _action_dedupe_key(action: dict) -> tuple:
    payload = action.get("pricelabs_payload") or {}
    return (
        action.get("property"),
        action.get("type"),
        action.get("target_dates"),
        payload.get("kind"),
        payload.get("start_date"),
        payload.get("end_date"),
        payload.get("adjustment_pct"),
        payload.get("suggested_rate"),
        payload.get("suggested_base_price"),
        payload.get("estimated_base_price"),
        payload.get("estimated_rate"),
    )


def _is_pace_year_action(action: dict) -> bool:
    return (
        action.get("source") == "Pace 2025 MauiP 05.17.26.xlsm"
        and str(action.get("type", "")).startswith("pace_year_")
    )


def _is_monthly_pacing_action(action: dict) -> bool:
    return action.get("source") == MONTHLY_PACING_SOURCE or str(action.get("type", "")).startswith("monthly_")


def _recently_reviewed(action: dict, now: datetime) -> bool:
    when = action.get("reviewed_at") or action.get("applied_at") or action.get("created_at")
    if not when:
        return False
    try:
        reviewed = datetime.fromisoformat(str(when))
    except ValueError:
        return False
    if reviewed.tzinfo is None:
        reviewed = reviewed.replace(tzinfo=timezone.utc)
    return now - reviewed <= timedelta(days=ACTION_REPEAT_COOLDOWN_DAYS)


def _suggest_action(prop: Property) -> dict | None:
    """4-criteria scorecard for base rate decisions, occupancy window by bedroom count.

    Occupancy window (booking windows differ by size):
      1-2BR → next 45 days (est. from 30d/60d)
      3-4BR → next 90 days
      5BR+  → next 120 days

    Red flags (each is a decrease signal):
    1. Occ below 40% in the window for this BR count
    2. Fewer than 3 reservations in the last 30 days
    3. Last booked more than 14 days ago
    4. 10+ points below the same-group average occupancy (same window)

    0 red flags → increase base | 1 → small increase | 2 → hold & monitor | 3-4 → decrease
    Override: occ ≥ 85% always increase; occ < 10% always decrease.
    """
    benchmark = _benchmark_for(prop)
    occ_gap = benchmark.get("occ_gap", 0)
    owner_note = _owner_note(prop)
    stagnant = _stagnant_rate_window(prop)

    # ── Bedroom-based occupancy window ───────────────────────────────────────
    beds = int(prop.bedrooms or 0)
    def _window_occ(p) -> float:
        if beds <= 2:
            # Real 45d occupancy from PriceLabs API when synced; else estimate
            if p.adj_occ_45d > 0:
                return p.adj_occ_45d
            if p.adj_occ_30d > 0 and p.adj_occ_60d > 0:
                return (p.adj_occ_30d + p.adj_occ_60d) / 2
            return p.adj_occ_60d
        if beds <= 4:
            # Fall back to 60d (always populated) if 90d is missing from this
            # week's export — matches the fallback pattern used by the other
            # two bands instead of silently reading as 0% occupancy.
            return p.adj_occ_90d if p.adj_occ_90d > 0 else p.adj_occ_60d
        if p.adj_occ_120d > 0:
            return p.adj_occ_120d
        return p.adj_occ_90d if p.adj_occ_90d > 0 else p.adj_occ_60d

    window_label = "45d" if beds <= 2 else ("90d" if beds <= 4 else "120d")
    occ = _window_occ(prop)

    # Same customization-group average over the SAME window
    group_peers = [
        p for p in _PORTFOLIO
        if p.active and p.name != prop.name
        and (p.customization_group or "") == (prop.customization_group or "")
        and int(p.bedrooms or 0) == beds
        and p.urgency != "onboarding"
    ]
    if len(group_peers) < 3:  # widen to whole group if BR slice too thin
        group_peers = [
            p for p in _PORTFOLIO
            if p.active and p.name != prop.name
            and (p.customization_group or "") == (prop.customization_group or "")
            and p.urgency != "onboarding"
        ]
    group_occ = (sum(_window_occ(p) for p in group_peers) / len(group_peers)) if group_peers else benchmark.get("occ_90d", 50) / 100
    group_gap_pts = (occ - group_occ) * 100  # negative = below group

    # ── 4 criteria (green = healthy; red flag = inverse) ─────────────────────
    # Pickup: prefer 30d reservation count (Report Builder CSV); else use
    # PriceLabs API unique reservations past 15d doubled as a 30d estimate
    resv_30d = prop.reservations_30d if prop.reservations_30d > 0 else prop.reservations_15d * 2
    c1_above_group  = group_gap_pts > -10                                   # not 10+ pts below group avg
    c2_good_pickup  = resv_30d >= 3                                         # ≥3 reservations last 30d
    c3_above_min    = occ >= 0.40                                           # ≥40% occ in BR window
    c4_recent_book  = (prop.last_booked_days is not None and prop.last_booked_days <= 14)

    score = sum([c1_above_group, c2_good_pickup, c3_above_min, c4_recent_book])

    def _criteria_note() -> str:
        flags = [
            (f"Within 10pts of group ({window_label})", c1_above_group, f"{occ:.0%} vs {group_occ:.0%} avg ({group_gap_pts:+.1f} pts, {len(group_peers)} peers)"),
            ("Pickup ≥3 resv/30d", c2_good_pickup, f"{resv_30d} reservations" + ("" if prop.reservations_30d > 0 else " (est. from 15d API count)")),
            (f"Occ ≥40% next {window_label}", c3_above_min, f"{occ:.0%}"),
            ("Booked last 14d", c4_recent_book, f"{prop.last_booked_days}d ago" if prop.last_booked_days is not None else "no record"),
        ]
        parts = [f"{'✓' if ok else '✗'} {label}: {detail}" for label, ok, detail in flags]
        return f"Score {score}/4 ({beds}BR → {window_label} window) — " + " · ".join(parts)

    # ── Override: hard floor / ceiling ──────────────────────────────────────
    if occ >= 0.85 or (c1_above_group and c2_good_pickup and occ >= 0.70):
        pct = 0.05
        new_base = _pct_rate(prop.base_price, pct)
        return {
            **_action_context(prop),
            "type": "fast_booking_price_increase",
            "priority": "high",
            "suggestion": f"Increase base price {_pct_label(pct)} — strong demand signal",
            "adjustment": f"{_pct_label(pct)} base increase",
            "target_dates": "Next 60 days",
            "current_value": f"Base {_money(prop.base_price)}; {window_label} occ {occ:.0%}",
            "proposed_value": f"Base {_pct_label(pct)} (est. {_money(new_base)})",
            "owner_note": owner_note,
            "reason": _criteria_note(),
            "implementation": "Approve then update PriceLabs base price. Do not change max price.",
            "pricelabs_payload": {"kind": "base_percentage_review", "adjustment_pct": pct, "estimated_base_price": new_base},
        }

    if occ < 0.10 and prop.last_booked_days is not None and prop.last_booked_days > 30:
        pct = -0.10
        new_base, hit_floor = _floor_limited_rate(prop, pct)
        return {
            **_action_context(prop),
            "type": "low_occupancy_base_adjustment",
            "priority": "high",
            "suggestion": f"Decrease base {_pct_label(pct)} — very low occ + stale bookings",
            "adjustment": f"{_pct_label(pct)} base decrease",
            "target_dates": "Until next weekly review",
            "current_value": f"Base {_money(prop.base_price)}; {window_label} occ {occ:.0%}",
            "proposed_value": f"Base est. {_money(new_base)}",
            "owner_note": owner_note,
            "reason": _criteria_note(),
            "implementation": "Approve then reduce base in PriceLabs. Recheck in 7 days.",
            "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
        }

    # ── Score-based decisions ────────────────────────────────────────────────
    if score == 4:
        pct = 0.05
        new_base = _pct_rate(prop.base_price, pct)
        return {
            **_action_context(prop),
            "type": "scorecard_increase_strong",
            "priority": "high",
            "suggestion": f"Increase base price {_pct_label(pct)} — all 4 signals green",
            "adjustment": f"{_pct_label(pct)} base increase",
            "target_dates": "Next 60 days",
            "current_value": f"Base {_money(prop.base_price)}; {window_label} occ {occ:.0%}; {prop.reservations_30d} resv/30d",
            "proposed_value": f"Base {_pct_label(pct)} (est. {_money(new_base)})",
            "owner_note": owner_note,
            "reason": _criteria_note(),
            "implementation": "Approve then update PriceLabs base price. Do not change max price.",
            "pricelabs_payload": {"kind": "base_percentage_review", "adjustment_pct": pct, "estimated_base_price": new_base},
        }

    if score == 3:
        pct = 0.03
        new_base = _pct_rate(prop.base_price, pct)
        return {
            **_action_context(prop),
            "type": "scorecard_increase_moderate",
            "priority": "medium",
            "suggestion": f"Consider small base increase {_pct_label(pct)} — 3/4 signals green",
            "adjustment": f"{_pct_label(pct)} base review",
            "target_dates": "Next 60 days",
            "current_value": f"Base {_money(prop.base_price)}; {window_label} occ {occ:.0%}; {prop.reservations_30d} resv/30d",
            "proposed_value": f"Base {_pct_label(pct)} (est. {_money(new_base)})",
            "owner_note": owner_note,
            "reason": _criteria_note(),
            "implementation": "Approve to nudge base up. Monitor pickup for 7 days before further increases.",
            "pricelabs_payload": {"kind": "base_percentage_review", "adjustment_pct": pct, "estimated_base_price": new_base},
        }

    if score == 2:
        # Check for stagnant calendar dates as a targeted action
        if stagnant and not c3_above_min:
            pct = stagnant["adjustment_pct"]
            hit_floor = stagnant["hit_floor"]
            return {
                **_action_context(prop),
                "type": "stagnant_rate_nudge",
                "priority": "medium",
                "suggestion": f"Hold base; targeted adjustment for stagnant dates {stagnant['label']}",
                "adjustment": f"{_pct_label(pct)} date adjustment",
                "target_dates": stagnant["label"],
                "current_value": f"{stagnant['nights']} nights at {_money(stagnant['rate'])}",
                "proposed_value": f"Minimum floor {_money(stagnant['suggested_rate'])}" if hit_floor else f"Date adjustment {_pct_label(pct)} (est. {_money(stagnant['suggested_rate'])})",
                "owner_note": owner_note,
                "reason": _criteria_note(),
                "implementation": "Do not change base price. Apply a narrow date-level adjustment only for the stagnant window.",
                "pricelabs_payload": {
                    "kind": "custom_fixed_rate" if hit_floor else "custom_percentage_adjustment",
                    "start_date": stagnant["start"].isoformat(),
                    "end_date": stagnant["end"].isoformat(),
                    "adjustment_pct": pct,
                    "suggested_rate": stagnant["suggested_rate"] if hit_floor else None,
                    "estimated_rate": stagnant["suggested_rate"],
                },
            }
        return {
            **_action_context(prop),
            "type": "hold_rate_monitor_pickup",
            "priority": "medium",
            "suggestion": "Hold base price — mixed signals, monitor pickup",
            "adjustment": "No price change",
            "target_dates": "Until next weekly review",
            "current_value": f"Base {_money(prop.base_price)}; {window_label} occ {occ:.0%}; {prop.reservations_30d} resv/30d",
            "proposed_value": "Hold base; reassess next week",
            "owner_note": owner_note,
            "reason": _criteria_note(),
            "implementation": "Do not push pricing this week. Check which 2 signals are red and address the root cause.",
            "pricelabs_payload": {"kind": "manual_review"},
        }

    # score 0 or 1 → decrease
    if score <= 1:
        if prop.no_promotions:
            pct = -0.03
        elif not c3_above_min and not c2_good_pickup:
            pct = -0.07  # both minimum occ AND pickup failing
        else:
            pct = -0.05
        new_base, hit_floor = _floor_limited_rate(prop, pct)
        return {
            **_action_context(prop),
            "type": "scorecard_decrease",
            "priority": "high" if score == 0 else "medium",
            "suggestion": (
                f"Decrease base {_pct_label(pct)} — {score}/4 signals green"
            ),
            "adjustment": f"floor-limited to {_money(new_base)}" if hit_floor else f"{_pct_label(pct)} base decrease",
            "target_dates": "Until next weekly review",
            "current_value": f"Base {_money(prop.base_price)}; {window_label} occ {occ:.0%}; {prop.reservations_30d} resv/30d; last booked {prop.last_booked_days}d ago",
            "proposed_value": f"Base est. {_money(new_base)}",
            "owner_note": owner_note,
            "reason": _criteria_note(),
            "implementation": "Approve then reduce base in PriceLabs. Recheck pickup and reservations in 7 days.",
            "pricelabs_payload": {"kind": "base_price_review" if hit_floor else "base_percentage_review", "adjustment_pct": pct, "suggested_base_price": new_base if hit_floor else None, "estimated_base_price": new_base},
        }

    return None


def _generate_weekly_actions() -> list[dict]:
    existing = _load_actions()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    protected: list[dict] = []
    protected_keys = set()
    for action in existing:
        if _is_pace_year_action(action):
            continue
        status = action.get("status", "pending")
        key = _action_dedupe_key(action)
        if status in {"approved", "applied"}:
            protected.append(action)
            protected_keys.add(key)
        elif status in {"rejected"} and _recently_reviewed(action, now_dt):
            protected.append(action)
            protected_keys.add(key)

    from kcity_surge_dso import _is_kcity

    created_by_key: dict[tuple, dict] = {}
    # KCity/Knoxville listings are handled through the dedicated KCity Surge
    # DSOs page (event-based date-specific overrides), not the general base-
    # rate scorecard — exclude them here so they don't show up twice.
    underperforming = [
        p for p in _PORTFOLIO
        if p.active and p.urgency in {"critical", "warning"} and not p.onboarding and not _is_kcity(p)
    ][:80]
    overperforming = [
        p for p in _PORTFOLIO
        if p.active and p.urgency == "overperforming" and not _is_kcity(p)
    ][:60]
    candidates = underperforming + overperforming
    for prop in candidates:
        action = _suggest_action(prop)
        if not action:
            continue
        key = _action_dedupe_key(action)
        if key in protected_keys:
            continue
        action.update({
            "id": str(uuid.uuid4()),
            "status": "pending",
            "created_at": now,
            "reviewed_at": None,
        })
        created_by_key[key] = action
    actions = protected + list(created_by_key.values())
    _save_actions(actions)
    return actions


def _generate_monthly_pacing_actions() -> tuple[list[dict], dict]:
    result = load_monthly_pacing(MONTHLY_PACING_PATH, _PORTFOLIO, TODAY)
    if not result.get("ok"):
        return [], result.get("summary", {})

    existing = _load_actions()
    generated = result.get("actions", [])
    generated_by_id = {a["id"]: a for a in generated}
    now_dt = datetime.now(timezone.utc)
    kept: list[dict] = []
    for action in existing:
        if _is_pace_year_action(action):
            continue
        if not _is_monthly_pacing_action(action):
            kept.append(action)
            continue
        status = action.get("status", "pending")
        action_id = action.get("id")
        if status in {"approved", "applied"}:
            kept.append(action)
            generated_by_id.pop(action_id, None)
        elif status == "rejected" and _recently_reviewed(action, now_dt):
            kept.append(action)
            generated_by_id.pop(action_id, None)

    actions = kept + list(generated_by_id.values())
    _save_actions(actions)
    monthly_actions = [a for a in actions if _is_monthly_pacing_action(a)]
    return monthly_actions, result.get("summary", {})


def _booking_promo_key(prop: Property) -> str:
    ll = lookup_links(prop.name)
    return str(getattr(ll, "booking_id", "") or prop.name).strip().lower()


def _split_ids(value: str | list | None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [
        part.strip()
        for part in str(value).replace(";", ",").replace("|", ",").split(",")
        if part.strip()
    ]


def _booking_mapping(prop: Property) -> dict:
    ll = lookup_links(prop.name)
    return {
        "booking_hotel_id": getattr(ll, "booking_hotel_id", "") if ll else "",
        "booking_room_ids": _split_ids(getattr(ll, "booking_room_ids", "") if ll else "") or _split_ids(os.environ.get("BOOKING_DEFAULT_ROOM_IDS")),
        "booking_parent_rate_ids": _split_ids(getattr(ll, "booking_parent_rate_ids", "") if ll else "") or _split_ids(os.environ.get("BOOKING_DEFAULT_PARENT_RATE_IDS")),
    }


def _booking_health(prop: Property) -> dict:
    benchmark = _benchmark_for(prop)
    score = 72
    score += min(12, max(-18, benchmark.get("occ_gap", 0) * 0.7))
    score += min(8, max(-12, benchmark.get("booked_14d_gap", 0) * 1.8))
    if prop.urgency == "critical":
        score -= 16
    elif prop.urgency == "warning":
        score -= 8
    elif prop.urgency == "overperforming":
        score += 8
    if prop.booked_14d == 0:
        score -= 8
    score = int(max(0, min(100, round(score))))
    if score < 45:
        label = "critical"
    elif score < 65:
        label = "needs lift"
    elif score > 82:
        label = "protect ADR"
    else:
        label = "stable"
    return {"score": score, "label": label, "benchmark": benchmark}


def _default_booking_promotion(prop: Property) -> dict:
    health = _booking_health(prop)
    ll = lookup_links(prop.name)
    booking_id = getattr(ll, "booking_id", None) if ll else None
    booking_url = getattr(ll, "booking_url", None) if ll else None
    mapping = _booking_mapping(prop)
    occ = prop.adj_occ_60d
    no_promos = bool(getattr(prop, "no_promotions", False))

    if no_promos or prop.urgency == "overperforming" or occ >= 0.70:
        promo_type = "none"
        discount = 0
        reason = "Protect ADR; current demand does not justify a Booking.com discount."
    elif prop.booked_14d == 0 and occ <= 0.20:
        promo_type = "limited_time_deal"
        discount = 12
        reason = "Low occupancy and no near-term pickup; use a narrow visibility lift instead of a broad rate cut."
    elif prop.urgency in {"critical", "warning"}:
        promo_type = "basic_deal"
        discount = 8
        reason = "Soft pacing suggests a modest Booking.com promotion test."
    else:
        promo_type = "mobile_rate"
        discount = 5
        reason = "Use a light audience-specific promotion only if Booking.com ranking is weak."

    start = TODAY + timedelta(days=7)
    end = start + timedelta(days=30)
    stay_start = TODAY + timedelta(days=14)
    stay_end = stay_start + timedelta(days=60)
    expected_rate = _round_to_5(prop.base_price * (1 - discount / 100)) if discount else prop.base_price
    net_adr_note = (
        f"Estimated gross ADR after discount: {_money(expected_rate)} before Booking.com commission, taxes, and stacked discounts."
        if discount else "No gross ADR discount proposed."
    )
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"booking-promo:{_booking_promo_key(prop)}")),
        "property": prop.name,
        "display_name": prop.property_name,
        "booking_id": booking_id or "",
        "booking_hotel_id": mapping["booking_hotel_id"] or booking_id or "",
        "booking_room_ids": mapping["booking_room_ids"],
        "booking_parent_rate_ids": mapping["booking_parent_rate_ids"],
        "booking_url": booking_url or "",
        "group_label": _group_label(prop),
        "health_score": health["score"],
        "health_label": health["label"],
        "promotion_type": promo_type,
        "discount_pct": discount,
        "book_start_date": start.isoformat(),
        "book_end_date": end.isoformat(),
        "stay_start_date": stay_start.isoformat(),
        "stay_end_date": stay_end.isoformat(),
        "audience": "all_travelers" if promo_type in {"basic_deal", "limited_time_deal"} else "mobile",
        "status": "draft",
        "source": "generated",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": None,
        "current_value": f"Base {_money(prop.base_price)}; occ60 {prop.adj_occ_60d:.0%}; booked14 {prop.booked_14d}",
        "expected_adr": expected_rate,
        "net_adr_note": net_adr_note,
        "reason": reason,
        "risk_flags": _booking_promo_risks(prop, discount, promo_type),
        "ai_review": "",
        "api_payload_preview": _booking_payload_preview(prop, promo_type, discount, start, end, stay_start, stay_end),
    }


def _booking_promo_risks(prop: Property, discount: int, promo_type: str) -> list[str]:
    risks = []
    mapping = _booking_mapping(prop)
    if getattr(prop, "no_promotions", False):
        risks.append("Owner note indicates no promotions; use manual approval only.")
    risks.append("Check overlapping Booking.com Genius, mobile, country, and length-of-stay discounts before applying.")
    if not lookup_links(prop.name) or not getattr(lookup_links(prop.name), "booking_url", None):
        risks.append("Booking.com listing ID or URL is missing from marketing links.")
    if not (mapping.get("booking_hotel_id") or (lookup_links(prop.name) and getattr(lookup_links(prop.name), "booking_id", None))):
        risks.append("Booking.com hotel/property ID is missing; API push cannot run.")
    if promo_type in {"basic_deal", "limited_time_deal", "last_minute_deal", "early_booker_deal"} and not mapping.get("booking_room_ids"):
        risks.append("Booking.com room type IDs are missing; API push cannot run for this promo type.")
    if promo_type != "none" and not mapping.get("booking_parent_rate_ids"):
        risks.append("Booking.com parent rate plan IDs are missing; API push cannot run.")
    if promo_type == "mobile_rate" and discount and discount < 10:
        risks.append("Booking.com mobile rates require a minimum 10% discount.")
    if prop.min_price and discount:
        expected = prop.base_price * (1 - discount / 100)
        if expected <= prop.min_price:
            risks.append("Proposed discount may push gross ADR close to the minimum price floor.")
    if promo_type == "none":
        risks.append("No promotion recommended; monitor rank and conversion before discounting.")
    return risks


def _booking_payload_preview(prop: Property, promo_type: str, discount: int, book_start: date, book_end: date, stay_start: date, stay_end: date) -> dict:
    mapping = _booking_mapping(prop)
    return {
        "provider": "Booking.com Connectivity Promotions API",
        "endpoint": "/promotions",
        "note": "Requires Booking.com token-based machine-account credentials and Promotions API permissions.",
        "hotel_id": mapping["booking_hotel_id"] or (lookup_links(prop.name).booking_id if lookup_links(prop.name) else ""),
        "room_ids": mapping["booking_room_ids"],
        "parent_rate_ids": mapping["booking_parent_rate_ids"],
        "promotion": {
            "type": promo_type,
            "discount_percentage": discount,
            "book_dates": {"from": book_start.isoformat(), "to": book_end.isoformat()},
            "stay_dates": {"from": stay_start.isoformat(), "to": stay_end.isoformat()},
        },
    }


def _generate_booking_promotions() -> list[dict]:
    existing = {item.get("id"): item for item in _load_booking_promotions()}
    generated = []
    for prop in [p for p in _PORTFOLIO if p.active][:150]:
        candidate = _default_booking_promotion(prop)
        old = existing.get(candidate["id"])
        if old:
            preserved = {**candidate, **old}
            preserved["health_score"] = candidate["health_score"]
            preserved["health_label"] = candidate["health_label"]
            preserved["current_value"] = candidate["current_value"]
            preserved["risk_flags"] = candidate["risk_flags"]
            preserved["api_payload_preview"] = candidate["api_payload_preview"]
            generated.append(preserved)
        else:
            generated.append(candidate)
    _save_booking_promotions(generated)
    return generated


def _booking_promotion_review(promo: dict) -> str:
    prop = _PORTFOLIO_INDEX.get(promo.get("property", ""))
    ctx = _property_context(prop) if prop else "Property context unavailable."
    prompt = f"""
Today is {TODAY.isoformat()}. Review this draft Booking.com promotion before a revenue manager applies it.

Property context:
{ctx}

Draft promotion:
{json.dumps(promo, indent=2)}

Return concise markdown with:
1. Verdict: approve, revise, or reject
2. Net ADR / stacking risk
3. Ranking and conversion rationale
4. Exact changes to make before applying

Rules:
- Never assume Booking.com discounts do not stack; call out stacking checks.
- Do not say the promotion was pushed or applied.
- If Booking.com ID is missing, say it cannot be pushed by API yet.
""".strip()
    try:
        client = _ai_client()
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "You are an STR revenue manager reviewing Booking.com promotions. Be concise, numeric, and cautious about discount stacking."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        flags = promo.get("risk_flags") or []
        verdict = "revise" if flags or int(promo.get("discount_pct") or 0) >= 10 else "approve"
        return (
            f"**Verdict: {verdict.title()}**\n\n"
            f"AI provider unavailable, so this is a rule-based review: {e}\n\n"
            f"**Checks Before Applying**\n"
            f"- Confirm Booking.com promotion stacking with Genius, mobile, country, and length-of-stay discounts.\n"
            f"- Confirm net ADR after commission/taxes and owner restrictions.\n"
            f"- Confirm Booking.com property ID is mapped.\n\n"
            f"**Risk Flags**\n" + ("\n".join(f"- {flag}" for flag in flags) if flags else "- No major rule-based flags.")
        )


def _benchmark_for(prop: Property) -> dict:
    active = [p for p in _PORTFOLIO if p.active and p.name != prop.name]
    same_segment = [
        p for p in active
        if p.area == prop.area and p.bedrooms == prop.bedrooms and p.urgency != "onboarding"
    ]
    group = same_segment
    basis = f"{prop.area} {prop.bedrooms}BR"
    if len(group) < 4:
        group = [p for p in active if p.area == prop.area and p.urgency != "onboarding"]
        basis = prop.area or "portfolio area"
    if len(group) < 4:
        group = [p for p in active if p.urgency != "onboarding"]
        basis = "portfolio"

    def _avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0

    occ = _avg([p.adj_occ_60d for p in group])
    occ_120d = _avg([p.adj_occ_120d for p in group])
    occ_180d = _avg([p.adj_occ_180d for p in group])
    booked_14d = _avg([p.booked_14d for p in group])
    reservations_30d = _avg([p.reservations_30d for p in group])
    base_price = _avg([p.base_price for p in group if p.base_price])
    return {
        "basis": basis,
        "sample_size": len(group),
        "occ_60d": round(occ * 100, 1),
        "occ_90d": round(_avg([p.adj_occ_90d for p in group]) * 100, 1),
        "occ_120d": round(occ_120d * 100, 1),
        "occ_180d": round(occ_180d * 100, 1),
        "booked_14d": round(booked_14d, 1),
        "reservations_30d": round(reservations_30d, 1),
        "base_price": round(base_price, 0) if base_price else None,
        "occ_gap": round((prop.adj_occ_60d - occ) * 100, 1),
        "occ_gap_pct": round((prop.adj_occ_60d - occ) * 100, 1),
        "occ_120d_gap": round((prop.adj_occ_120d - occ_120d) * 100, 1),
        "occ_180d_gap": round((prop.adj_occ_180d - occ_180d) * 100, 1),
        "booked_14d_gap": round(prop.booked_14d - booked_14d, 1),
        "reservations_30d_gap": round(prop.reservations_30d - reservations_30d, 1),
    }


def _reload_portfolio(csv_path: Path | None = None):
    """Reload portfolio data from CSV — called after a hot-reload upload."""
    global _PORTFOLIO, _PORTFOLIO_INDEX, _SUMMARY
    import marketing_links as _ml
    _ml._LINKS = None          # bust marketing links cache
    with _portfolio_lock:
        _PORTFOLIO       = load_portfolio(csv_path)
        _PORTFOLIO_INDEX = {p.name: p for p in _PORTFOLIO}
        _SUMMARY         = portfolio_summary(_PORTFOLIO)
    print(f"[reload] Portfolio reloaded — {_SUMMARY['total_active']} active, {_SUMMARY['critical_count']} critical")


def _api_pct(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith("%"):
        return text
    try:
        val = float(text)
    except ValueError:
        return text
    if 0 <= val <= 1:
        val *= 100
    return f"{val:.1f}%"


def _api_bool(value: object) -> str:
    return "TRUE" if bool(value) else "FALSE"


def _api_numeric(value: object) -> str:
    """Return numeric string or empty string — strips non-numeric values like 'Unavailable'."""
    text = str(value or "").strip()
    try:
        float(text)
        return text
    except (ValueError, TypeError):
        return ""


def _api_channel_tags(item: dict) -> str:
    parts = []
    for channel in item.get("channel_listing_details") or []:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("channel_name") or "").strip()
        listing_id = str(channel.get("channel_listing_id") or "").strip()
        if name and listing_id:
            parts.append(f"{name}:{listing_id}")
    return "; ".join(parts)


def _derive_last_booked_date(item: dict, ha_last_booked: dict | None = None) -> str:
    from datetime import date, datetime, timezone, timedelta
    # 1. PriceLabs reservations (most accurate)
    listing_id = str(item.get("id") or "")
    if ha_last_booked and listing_id and listing_id in ha_last_booked:
        try:
            d = date.fromisoformat(ha_last_booked[listing_id][:10])
            return d.strftime("%d %b %Y")
        except (ValueError, TypeError):
            pass
    # 2. PriceLabs API field (if ever added)
    for field in ("last_booked_date", "last_booking_date", "last_booked"):
        raw = item.get(field)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt.strftime("%d %b %Y")
            except (ValueError, TypeError):
                try:
                    d = date.fromisoformat(str(raw)[:10])
                    return d.strftime("%d %b %Y")
                except (ValueError, TypeError):
                    pass
    # 3. Estimate from booking pickup windows
    today = date.today()
    for days in (3, 7, 15):
        try:
            if int(item.get(f"booking_pickup_unique_past_{days}") or 0) > 0:
                return (today - timedelta(days=days)).strftime("%d %b %Y")
        except (ValueError, TypeError):
            pass
    return ""


def _write_pricelabs_api_portfolio(listings: list[dict], ha_last_booked: dict | None = None) -> dict:
    header = [
        "Listing ID",
        "Listing Name",
        "Listing Sync",
        "Show Listing",
        "Listing Status",
        "PMS Name",
        "Base Price",
        "Recommended Base Price",
        "Min Price",
        "Max Price",
        "Bedroom Count",
        "City",
        "Customization Group",
        "Customization Sub Group",
        "Tags",
        "Total Occupancy ( Next 60 Days )",
        "Total Occupancy ( Next 90 Days )",
        "Adjusted Occupancy ( Next 45 Days )",
        "Reservations ( Past 15 Days )",
        "Nights Booked ( Past 7 Days )",
        "Nights Booked ( Past 15 Days )",
        "Last Booked Date",
    ]
    rows = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        push_enabled = bool(item.get("push_enabled"))
        hidden = bool(item.get("isHidden"))
        tags = item.get("tags")
        if isinstance(tags, list):
            tag_text = "; ".join(str(t).strip() for t in tags if str(t).strip())
        else:
            tag_text = str(tags or "").strip()
        channel_tags = _api_channel_tags(item)
        if channel_tags:
            tag_text = "; ".join(v for v in [tag_text, channel_tags] if v)
        rows.append([
            item.get("id", ""),
            item.get("name", ""),
            _api_bool(push_enabled),
            _api_bool(not hidden),
            "available" if push_enabled and not hidden else "hidden",
            item.get("pms", ""),
            item.get("base", ""),
            _api_numeric(item.get("recommended_base_price", "")),
            item.get("min", ""),
            item.get("max", ""),
            item.get("no_of_bedrooms", ""),
            item.get("city_name", ""),
            item.get("group", ""),
            item.get("subgroup", ""),
            tag_text,
            _api_pct(item.get("occupancy_next_60")),
            _api_pct(item.get("occupancy_next_90")),
            _api_pct(item.get("adjusted_occupancy_next_45")),
            item.get("booking_pickup_unique_past_15", ""),
            item.get("booking_pickup_past_7", ""),
            item.get("booking_pickup_past_15", ""),
            _derive_last_booked_date(item, ha_last_booked or {}),
        ])

    target_path = CSV_PATH
    write_error: str | None = None
    try:
        with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
    except OSError as exc:
        # Vercel's bundled filesystem is read-only outside /tmp. Fall back to
        # writing the regenerated CSV to /tmp so _reload_portfolio() still has
        # fresh data; Supabase remains the durable copy.
        fallback = Path("/tmp") / CSV_PATH.name
        try:
            with fallback.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)
            target_path = fallback
        except OSError as exc2:
            write_error = f"{type(exc).__name__}: {exc}; fallback failed: {type(exc2).__name__}: {exc2}"

    active_rows = [
        row for row in rows
        if str(row[2]).upper() == "TRUE" and str(row[3]).upper() == "TRUE"
    ]
    return {
        "total": len(rows),
        "active": len(active_rows),
        "path": str(target_path),
        "write_error": write_error,
    }




def _load_pricelabs_snapshot() -> list[dict]:
    try:
        data = json.loads(PRICELABS_API_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return data.get("listings", data) if isinstance(data, dict) else data
    except Exception:
        return []


def _sync_pricelabs_api() -> dict:
    response = client_from_env().request("GET", "/listings")
    listings = response.get("listings") if isinstance(response, dict) else response
    if not isinstance(listings, list):
        raise PriceLabsAPIError("PriceLabs /listings did not return a listings array.")
    PRICELABS_API_SNAPSHOT_PATH.write_text(json.dumps(response, indent=2), encoding="utf-8")
    try:
        last_booked = pricelabs_last_booked_by_listing()
    except Exception as e:
        # Previously this failure was swallowed silently, which left
        # last_booked_days empty for every listing with nothing on screen or in
        # the logs to say why. Log it and carry the reason into the response.
        app.logger.warning("PriceLabs last-booked lookup failed: %s", e)
        last_booked = {}
        last_booked_error = str(e)
    else:
        last_booked_error = None
    written = _write_pricelabs_api_portfolio(listings, last_booked)
    _reload_portfolio()
    return {
        "last_booked_listings": len(last_booked),
        "last_booked_error": last_booked_error,
        "source": "PriceLabs Customer API /listings",
        "listings_total": written["total"],
        "listings_active": written["active"],
        "snapshot": str(PRICELABS_API_SNAPSHOT_PATH),
        "portfolio_csv": written.get("path"),
        "csv_write_error": written.get("write_error"),
    }


def _parse_pricelabs_hook(raw_hook: str) -> tuple[str, str]:
    parts = raw_hook.strip().split(None, 1)
    if len(parts) == 2 and parts[0].upper() in {"GET", "POST", "PUT", "PATCH"}:
        return parts[0].upper(), parts[1].strip()
    return "POST", raw_hook.strip()


def _pricelabs_post_apply_payload(apply_result: dict) -> dict:
    return {
        "listing_id": apply_result.get("listing_id"),
        "pms": apply_result.get("pms"),
        "endpoint": apply_result.get("endpoint"),
        "confirmed_base": apply_result.get("confirmed_base"),
        "confirmed_dates": apply_result.get("confirmed_dates"),
        "adjusted_start_date": apply_result.get("adjusted_start_date"),
        "source": "Haven Dashboard post-apply refresh",
    }


def _run_pricelabs_post_apply_hooks(apply_result: dict) -> dict:
    """Run optional PriceLabs Save/Refresh/Sync endpoints after a successful push.

    PriceLabs documents Save & Refresh/Sync primarily as UI actions. If the
    account has API endpoints for those actions, configure them with
    PRICELABS_POST_APPLY_ENDPOINTS, for example:
      POST /listings/{listing_id}/refresh, POST /listings/{listing_id}/sync
    """
    hooks_text = (
        os.environ.get("PRICELABS_POST_APPLY_ENDPOINTS")
        or os.environ.get("PRICELABS_SAVE_REFRESH_ENDPOINTS")
        or ""
    ).strip()
    if not hooks_text:
        return {
            "configured": False,
            "attempted": False,
            "message": "No PriceLabs Save/Refresh/Sync API hook configured.",
        }

    client = client_from_env()
    listing_id = str(apply_result.get("listing_id") or "")
    pms = str(apply_result.get("pms") or "")
    payload = _pricelabs_post_apply_payload(apply_result)
    calls = []
    for raw_hook in hooks_text.split(","):
        raw_hook = raw_hook.strip()
        if not raw_hook:
            continue
        method, endpoint = _parse_pricelabs_hook(raw_hook)
        endpoint = endpoint.format(listing_id=listing_id, pms=pms)
        response = client.request(method, endpoint, None if method == "GET" else payload)
        calls.append({
            "method": method,
            "endpoint": endpoint,
            "response": response,
        })

    return {
        "configured": True,
        "attempted": bool(calls),
        "calls": calls,
        "message": f"Ran {len(calls)} configured PriceLabs Save/Refresh/Sync hook(s).",
    }


def _verify_pricelabs_push_from_sync(apply_result: dict) -> dict:
    sync_result = _sync_pricelabs_api()
    listing_id = str(apply_result.get("listing_id") or "")
    confirmed_base = apply_result.get("confirmed_base")
    verification = {
        "synced": True,
        "sync": sync_result,
        "verified": None,
        "message": "Dashboard re-synced from PriceLabs Customer API.",
    }
    if confirmed_base is None:
        verification["message"] = (
            "Dashboard re-synced from PriceLabs Customer API; date override read-back "
            "is not available from the current snapshot."
        )
        return verification

    try:
        snapshot = json.loads(PRICELABS_API_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        verification["verified"] = False
        verification["message"] = "Dashboard synced, but the PriceLabs snapshot could not be read for verification."
        return verification

    listings = snapshot.get("listings") if isinstance(snapshot, dict) else snapshot
    match = next(
        (
            item for item in listings or []
            if isinstance(item, dict) and str(item.get("id")) == listing_id
        ),
        None,
    )
    if not match:
        verification["verified"] = False
        verification["message"] = "Dashboard synced, but the updated listing was not found in the PriceLabs snapshot."
        return verification

    try:
        synced_base = int(round(float(match.get("base"))))
    except (TypeError, ValueError):
        verification["verified"] = False
        verification["message"] = "Dashboard synced, but the listing base price was not readable."
        return verification

    verification["synced_base"] = synced_base
    verification["verified"] = synced_base == int(confirmed_base)
    if verification["verified"]:
        verification["message"] = f"Dashboard re-synced and verified PriceLabs base price at ${synced_base}."
    else:
        verification["message"] = (
            f"Dashboard re-synced, but PriceLabs snapshot still shows base ${synced_base} "
            f"instead of ${confirmed_base}."
        )
    return verification


def _finalize_pricelabs_push(apply_result: dict) -> dict:
    post_apply = {"verified_sync": None, "refresh": None}
    try:
        post_apply["refresh"] = _run_pricelabs_post_apply_hooks(apply_result)
        post_apply["verified_sync"] = _verify_pricelabs_push_from_sync(apply_result)
    except PriceLabsAPIError as e:
        raise PricingApplyError(f"PriceLabs push succeeded, but post-push refresh/sync failed: {e}") from e
    apply_result["post_apply"] = post_apply
    return apply_result

# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────

def _property_links_context(prop: Property) -> str:
    ll = lookup_links(prop.name)
    if not ll:
        return "OTA Links: not found in marketing data"
    lines = []
    if ll.airbnb_url:
        lines.append(f"  Airbnb: {ll.airbnb_url}  (headline: \"{ll.airbnb_headline}\")")
        if ll.airbnb_rating is not None:
            lines.append(f"    Rating: {_format_rating(ll.airbnb_rating, ll.airbnb_reviews)} — status: {ll.airbnb_rating_status}")
        if ll.airbnb_photos is not None:
            lines.append(f"    Photos: {_format_count(ll.airbnb_photos)} — grade: {ll.airbnb_photo_grade}")
    else:
        lines.append("  Airbnb: NOT LISTED")
    if ll.vrbo_url:
        lines.append(f"  VRBO:   {ll.vrbo_url}  (headline: \"{ll.vrbo_headline}\")")
        if ll.vrbo_rating is not None:
            lines.append(f"    Rating: {_format_rating(ll.vrbo_rating, ll.vrbo_reviews)} — status: {ll.vrbo_rating_status}")
        if ll.vrbo_photos is not None:
            lines.append(f"    Photos: {_format_count(ll.vrbo_photos)} — grade: {ll.vrbo_photo_grade}")
    else:
        lines.append("  VRBO:   NOT LISTED")
    if getattr(ll, "booking_url", None):
        lines.append(f"  Booking.com: {ll.booking_url}")
    else:
        lines.append("  Booking.com: NOT LISTED")
    lines.append(f"  PMS ID: {ll.streamline_id or 'unknown'}")
    return "OTA Links:\n" + "\n".join(lines)


def _format_rating(rating: float | None, reviews: int | None) -> str:
    if rating is None:
        return "unknown"
    review_text = f" from {reviews} reviews" if reviews is not None else ""
    scale = 10 if rating > 5 else 5
    return f"{rating:.1f}/{scale}{review_text}"


def _format_count(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _property_context(prop: Property) -> str:
    occ_60 = f"{prop.adj_occ_60d:.0%}" if prop.adj_occ_60d else "0%"
    occ_90 = f"{prop.adj_occ_90d:.0%}" if prop.adj_occ_90d else "0%"
    last_bkd = f"{prop.last_booked_days} days ago" if prop.last_booked_days is not None else "no booking on record"
    min_p = f"${prop.min_price:.0f}" if prop.min_price else "unknown"
    min_stay = f"{prop.min_stay} nights" if prop.min_stay else "unknown"
    demand = f"{prop.demand_sensitivity}%" if prop.demand_sensitivity else "unknown"
    safety_minimum = f"{prop.historical_anchoring}%" if prop.historical_anchoring else "not synced"
    links_ctx = _property_links_context(prop)
    benchmark = _benchmark_for(prop)
    return f"""
Property: {prop.name}
Tags/Area: {prop.tags}
Bedrooms: {prop.bedrooms}
Urgency Status: {prop.urgency.upper()} (score {prop.urgency_score}/100)
Known Issues: {"; ".join(prop.issues) if prop.issues else "none flagged"}
Owner Notes / Restrictions: {_owner_note(prop) or "none detected"}

PriceLabs Settings:
  Base Price: ${prop.base_price:.0f}
  Min Price: {min_p}
  Min Stay: {min_stay}
  Last Minute: {prop.last_minute}  |  Far Future: {prop.far_future}
  Day of Week: {prop.day_of_week}  |  Seasonality: {prop.seasonality}
  Demand Sensitivity: {demand}  |  Safety Minimum / Historical Anchoring: {safety_minimum}
  Long-term Pricing: {prop.long_term_pricing}
  Occupancy Pacing: {prop.occupancy_pacing}
  Gaps & Adjacencies: {prop.gaps_adjacencies}
  Events & Seasons: Enabled

{links_ctx}

Tag-Based Neighborhood Benchmark:
  Benchmark Group: {benchmark['basis']} ({benchmark['sample_size']} listings)
  Listing Occupancy 60-day: {prop.adj_occ_60d:.0%}
  Benchmark Occupancy 60-day: {benchmark['occ_60d']}%
  Occupancy Gap vs Benchmark: {benchmark['occ_gap']} percentage points
  Listing Booked Nights 14-day: {prop.booked_14d}
  Benchmark Avg Booked Nights 14-day: {benchmark['booked_14d']}
  Base Price vs Benchmark Avg: ${prop.base_price:.0f} vs ${benchmark['base_price'] if benchmark['base_price'] is not None else 'unknown'}

Occupancy & Booking Data:
  Adjusted Occupancy 60-day: {occ_60}
  Adjusted Occupancy 90-day: {occ_90}
  Booked nights next 7 days: {prop.booked_7d}
  Booked nights next 14 days: {prop.booked_14d}
  Last booked: {last_bkd}
  Min price hit rate 60-day: {prop.min_price_occ_60d:.0%}
  Min price hit rate 90-day: {prop.min_price_occ_90d:.0%}

Important data quality note:
If a setting says "unknown", do not infer or invent it. Treat PriceLabs as the pricing source.

""".strip()


def _apply_weekly_action(action: dict) -> dict:
    payload = action.get("pricelabs_payload") or {}
    kind = payload.get("kind")
    if not kind:
        raise PricingApplyError("This action has no PriceLabs payload. Regenerate weekly suggestions.")
    if kind == "manual_review":
        raise PricingApplyError("This is a manual review item and should not be pushed to PriceLabs.")

    listing_id = str(action.get("listing_id") or "").strip()
    pms_name = str(action.get("pms_name") or "").strip()
    if not listing_id or not pms_name:
        raise PricingApplyError("This action is missing the PriceLabs listing ID or PMS name. Regenerate weekly suggestions from the current PriceLabs export.")

    start_text = payload.get("start_date") or payload.get("override_start")
    end_text = payload.get("end_date") or payload.get("override_end") or start_text
    if not start_text:
        if kind not in {"base_percentage_review", "base_price_review"}:
            raise PricingApplyError(f"Unsupported PriceLabs action kind: {kind}.")

        # Fail CLOSED, not open: if the property can't be uniquely resolved,
        # we cannot validate the live minimum-price floor, so refuse to push
        # rather than silently skipping the safety check.
        prop = _resolve_property(action.get("property", ""))
        if not prop:
            raise PricingApplyError(
                "Could not uniquely resolve this listing against the current PriceLabs data — the "
                "property name didn't match exactly one active listing. Refusing to push a base-price "
                "change without being able to validate it against the live minimum-price floor. "
                "Re-sync PriceLabs and retry."
            )

        if kind == "base_percentage_review":
            # Recompute from the CURRENT live base price rather than trusting the
            # dollar estimate captured when this suggestion was generated — the
            # base price may have changed since (manual edit, an earlier-applied
            # action), which would otherwise silently push a stale/wrong amount.
            pct = payload.get("adjustment_pct")
            if pct is None or not prop.base_price:
                raise PricingApplyError("This percentage action is missing adjustment_pct or a current base price.")
            new_base_value = int(round(float(prop.base_price) * (1 + float(pct))))
        else:
            new_base = payload.get("suggested_base_price") or payload.get("estimated_base_price")
            if not new_base:
                raise PricingApplyError("This base-price action is missing the target base price.")
            new_base_value = int(round(float(new_base)))

        if not (20 <= new_base_value <= 5000):
            raise PricingApplyError(
                f"Refusing to push a ${new_base_value:,} base price — outside the sane $20-$5,000 "
                "per-night range. This looks like a corrupted action; regenerate weekly suggestions."
            )

        if prop.min_price and new_base_value <= int(round(float(prop.min_price))):
            raise PricingApplyError(
                f"Not pushed: target base ${new_base_value} is at or below the current PriceLabs minimum "
                f"price ${int(round(float(prop.min_price)))}. The calendar would stay constrained by the "
                "minimum price floor. Review the floor separately before pushing this base-price decrease."
            )
        # Idempotency: if PriceLabs snapshot already shows the target base, treat as confirmed
        if prop.base_price and int(round(float(prop.base_price))) == new_base_value:
            return {
                "provider": "PriceLabs",
                "listing_id": listing_id,
                "pms": pms_name,
                "endpoint": "/listings",
                "sent": None,
                "response": None,
                "confirmed_base": new_base_value,
                "idempotent": True,
                "message": f"PriceLabs already shows ${new_base_value} as the base price — no update needed.",
            }

        listing_update = {
            "id": listing_id,
            "pms": pms_name,
            "base": new_base_value,
        }
        request_payload = {"listings": [listing_update]}
        try:
            response = client_from_env().request("POST", "/listings", request_payload)
        except PriceLabsAPIError as e:
            raise PricingApplyError(str(e)) from e

        # Require an explicit confirmation from PriceLabs — a missing/wrong
        # response shape must be treated as UNCONFIRMED, not silently passed,
        # since the price push may have partially or fully failed either way.
        returned_listings = response.get("listings") if isinstance(response, dict) else None
        if not isinstance(returned_listings, list):
            raise PricingApplyError(
                "PriceLabs responded, but the response did not include a confirmable listings array. "
                "Treating this base price update as UNCONFIRMED — verify manually in PriceLabs before retrying."
            )
        confirmed = [
            item for item in returned_listings
            if isinstance(item, dict)
            and str(item.get("id")) == listing_id
            and int(float(item.get("base", -1))) == listing_update["base"]
        ]
        if not confirmed:
            raise PricingApplyError(
                "PriceLabs responded, but did not confirm the requested base price update."
            )

        return {
            "provider": "PriceLabs",
            "listing_id": listing_id,
            "pms": pms_name,
            "endpoint": "/listings",
            "sent": request_payload,
            "response": response,
            "confirmed_base": listing_update["base"],
        }

    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except (TypeError, ValueError) as e:
        raise PricingApplyError("This action has invalid PriceLabs override dates.") from e

    adjusted_start = max(start, EARLIEST_RATE_EDIT_DATE)
    if end < adjusted_start:
        raise PricingApplyError("This action only affects same-day or past dates, so it was not pushed to PriceLabs.")

    # Sanity rails before ANY push to a live account. Real portfolio base
    # prices range $85-$1,310/night; these bounds are deliberately wide so no
    # legitimate value ever trips them, but a corrupted/miscalculated action
    # (e.g. a $0 or $500,000 price) gets refused instead of pushed live.
    SANE_MIN_NIGHTLY_PRICE = 20
    SANE_MAX_NIGHTLY_PRICE = 5000
    SANE_MAX_PCT_SWING = 0.50  # ±50%

    override_items = []
    day = adjusted_start
    while day <= end:
        item = {"date": day.isoformat()}
        if kind in {"custom_percentage_adjustment", "monthly_date_override"}:
            pct = payload.get("adjustment_pct")
            if pct is None:
                raise PricingApplyError("This percentage action is missing adjustment_pct.")
            pct = float(pct)
            if not (-SANE_MAX_PCT_SWING <= pct <= SANE_MAX_PCT_SWING):
                raise PricingApplyError(
                    f"Refusing to push a {pct:+.0%} price adjustment — outside the sane "
                    f"±{SANE_MAX_PCT_SWING:.0%} range. This looks like a corrupted or miscalculated "
                    "action; regenerate weekly suggestions."
                )
            item["price"] = round(pct * 100, 2)
            item["price_type"] = "percent"
        elif kind == "kcity_dso_min_price":
            min_price = payload.get("min_price")
            if not min_price:
                raise PricingApplyError("This DSO action is missing min_price.")
            min_price_value = float(min_price)
            if not (SANE_MIN_NIGHTLY_PRICE <= min_price_value <= SANE_MAX_NIGHTLY_PRICE):
                raise PricingApplyError(
                    f"Refusing to push a ${min_price_value:,.0f} minimum price — outside the sane "
                    f"${SANE_MIN_NIGHTLY_PRICE}-${SANE_MAX_NIGHTLY_PRICE:,} per-night range. This looks "
                    "like a corrupted action; regenerate weekly suggestions."
                )
            item["min_price"] = int(round(min_price_value))
            item["min_price_type"] = "fixed"
            item["currency"] = "USD"
        elif kind == "custom_fixed_rate":
            rate = payload.get("suggested_rate")
            if not rate:
                raise PricingApplyError("This fixed-rate action is missing suggested_rate.")
            rate_value = float(rate)
            if not (SANE_MIN_NIGHTLY_PRICE <= rate_value <= SANE_MAX_NIGHTLY_PRICE):
                raise PricingApplyError(
                    f"Refusing to push a ${rate_value:,.0f} fixed rate — outside the sane "
                    f"${SANE_MIN_NIGHTLY_PRICE}-${SANE_MAX_NIGHTLY_PRICE:,} per-night range. This looks "
                    "like a corrupted action; regenerate weekly suggestions."
                )
            item["price"] = int(round(rate_value))
            item["price_type"] = "fixed"
            item["currency"] = payload.get("currency") or "USD"
        else:
            raise PricingApplyError(
                f"PriceLabs push is only enabled for date-level overrides right now. Unsupported action kind: {kind}."
            )
        override_items.append(item)
        day += timedelta(days=1)

    pl_client = client_from_env()
    request_payload = {"pms": pms_name, "overrides": override_items}
    try:
        response = pl_client.request("POST", f"/listings/{listing_id}/overrides", request_payload)
    except PriceLabsAPIError as e:
        raise PricingApplyError(str(e)) from e

    # Require an explicit confirmation from PriceLabs — a missing/wrong response
    # shape must be treated as UNCONFIRMED, not silently passed.
    returned_overrides = response.get("overrides") if isinstance(response, dict) else None
    if not isinstance(returned_overrides, list):
        raise PricingApplyError(
            "PriceLabs responded, but the response did not include a confirmable overrides array. "
            "Treating this push as UNCONFIRMED — verify manually in PriceLabs before retrying."
        )

    # Compare the actual pushed VALUE per date, not just date presence — a
    # partial/clamped apply (dates echoed back with a different price) must
    # not be reported as fully confirmed.
    value_key = "min_price" if kind == "kcity_dso_min_price" else "price"
    requested_by_date = {item["date"]: item.get(value_key) for item in override_items}
    returned_by_date = {
        str(item.get("date")): item.get(value_key)
        for item in returned_overrides
        if isinstance(item, dict) and item.get("date")
    }
    returned_dates = set(returned_by_date)
    requested_dates = set(requested_by_date)

    missing_dates = sorted(requested_dates - returned_dates)
    mismatched_dates = sorted(
        d for d in (requested_dates & returned_dates)
        if returned_by_date[d] is not None
        and abs(float(returned_by_date[d]) - float(requested_by_date[d])) > 0.5
    )
    if missing_dates or mismatched_dates:
        details = []
        if missing_dates:
            details.append(f"missing: {', '.join(missing_dates)}")
        if mismatched_dates:
            details.append(f"confirmed with a different value than requested: {', '.join(mismatched_dates)}")
        raise PricingApplyError(
            "PriceLabs responded, but did not confirm every requested override (" + "; ".join(details) + ")."
        )

    return {
        "provider": "PriceLabs",
        "listing_id": listing_id,
        "pms": pms_name,
        "endpoint": f"/listings/{listing_id}/overrides",
        "sent": request_payload,
        "response": response,
        "confirmed_dates": sorted(returned_dates) if returned_dates else sorted(requested_dates),
        "adjusted_start_date": adjusted_start.isoformat() if adjusted_start != start else None,
    }


def _build_property_prompt(report_type: str, prop: Property) -> str:
    ctx = _property_context(prop)
    today = TODAY.isoformat()
    is_fast = prop.urgency == "overperforming"

    base = f"""
Today is {today}. Analyze this HVR Smokies / Tennessee STR property from the PriceLabs portfolio export:

{ctx}

Pricing recommendation guardrails:
- If status is CRITICAL or WARNING, do not recommend increasing base price, minimum price, or nightly rates.
- Use percentage adjustments for pricing requests, such as -3%, -8%, -10%, or +5%.
- Only recommend a fixed dollar rate when the percentage adjustment would hit or cross the minimum price floor.
- If owner tags include Fixed Min Rate or No Promotions, call that out and avoid promo/discount campaign language.
- Do not recommend matching a benchmark average blindly; use benchmark only as context.
- Never recommend or apply same-day rate edits. Same-day bookings are not allowed.
- Never edit Last Minute adjustment settings. Today's available rate is intentionally protected by a 999% last-minute increase.
- Do not include sections where all inputs are unknown or not synced. Say what data is missing in one sentence only when it affects the recommendation.
- Do not use Maui, Hawaii, beach, or island seasonality. Use Tennessee Smokies demand logic: cabin/leisure drive-to demand, weekends, summer family travel, fall foliage, holidays, events, and city-level context from the listing data.

""".strip()

    why_not_booking_prompt = f"""
{base}

Explain what is working for this fast-paced property and where we may be leaving ADR on the table.

1. **What Is Working** — Why is this property pacing fast? Use occupancy, pickup, benchmark gap, OTA links, city, bedroom count, and group as evidence.
2. **ADR Protection** — Identify whether the base/min setup looks too low, reasonable, or needs only selective future-date review.
3. **Demand & Timing** — Use Tennessee Smokies logic for {prop.city or prop.area}: weekend compression, summer/fall demand, holidays, and drive-to leisure demand.
4. **Opportunity Check** — Give specific opportunities to protect revenue without hurting conversion: selected date increases, restriction review, content/OTA visibility, and channel mix.
5. **Risks To Watch** — Note any risk from underpricing, minimum floors, overly generous discounts, or missing OTA/listing data.
6. **Specific Next Moves** — Give 3-5 concrete actions. Do not recommend broad discounts for a fast-paced listing.
""".strip() if is_fast else f"""
{base}

Diagnose why this property has {prop.booked_7d} bookings in next 7 days and {prop.booked_14d} in next 14 days.

1. **Pricing Diagnosis** — Min-price occurrence is {prop.min_price_occ_60d:.0%}. If current min price is unknown, say the floor must be checked in PriceLabs before recommending changes.
2. **Demand & Timing** — Use Tennessee Smokies logic for {prop.city or prop.area}: weekday/weekend pattern, summer travel, fall foliage, holidays, and local drive-to demand.
3. **Booking Blockers** — Identify only blockers supported by synced data: low pickup, low occupancy, missing OTA link, Airbnb link export issue, weak title/photo/review signal, or channel visibility.
4. **Min Stay / Restrictions Check** — Only analyze minimum stay if the current setting is explicitly known. Otherwise list it as a Hostaway/PriceLabs check, not a finding.
5. **Last-Minute Strategy** — Never edit same-day protection. Recommend only future-date percentage actions where demand and pickup support it.
6. **Specific Fixes** — Give 3-5 concrete fixes tied to this listing's actual data. Avoid generic advice.
""".strip()

    sections = {
        "overview": f"""
{base}

Provide a full revenue management assessment:

1. **Urgency Assessment** — Why is this property at {prop.urgency.upper()} status? What's the revenue impact?
2. **Booking Pace Analysis** — {prop.booked_7d} booked nights next 7 days, {prop.booked_14d} next 14 days. What does this tell us?
3. **Pricing Analysis** — Is the base price of ${prop.base_price:.0f} appropriate for a {prop.bedrooms}BR property in {prop.area}?
4. **Action Plan** — Top 5 specific actions to take this week in PriceLabs, Hostaway, or OTA only where supported by synced data
5. **Revenue Opportunity** — Estimate the 90-day revenue uplift if occupancy and pickup recover
""".strip(),

        "why_not_booking": why_not_booking_prompt,

        "revenue": f"""
{base}

Run a full revenue and pricing analysis:

1. **KPI Status** — Occupancy 60d: {prop.adj_occ_60d:.0%} vs target ~75%. How far off and what's the dollar gap?
2. **Booking Insights / Monthly Performance** — Use PriceLabs Booking Insights, Report Builder, and portfolio pacing data as the source of truth. If OTA reservation source is not present in the synced PriceLabs data, say that PriceLabs needs to expose or export the booking-source columns.
3. **PriceLabs Settings Audit** — Discuss Demand Sensitivity only if synced. Treat Historical Anchoring as PriceLabs Safety Minimum; do not call it a separate setting.
4. **Specific Rate Changes** — Give exact percentage-based pricing actions for this week. Use fixed dollar rates only when the minimum floor is actually reached; do not recommend maximum rates or price ceilings.
5. **Channel Revenue Risk** — Use only known OTA links/channel data. If booking source is not synced, do not estimate channel mix.
6. **90-Day Projection** — If all pricing fixes are implemented, project the revenue uplift
""".strip(),

        "weekly_actions": _build_weekly_actions_prompt(prop),
    }

    if report_type == "listing":
        return _build_listing_quality_prompt(prop)

    return sections.get(report_type, sections["overview"])


def _build_weekly_actions_prompt(prop: Property) -> str:
    return f"""
Today is {TODAY.isoformat()}. Create a weekly revenue-management review for this property using only the data below.

{_property_context(prop)}

Return:
1. Current health summary
2. Top 3 recommended actions
3. Risk level for each action
4. What should be checked before approving
5. Exact proposed action text for an approval queue

Rules:
- Do not claim an action has been applied.
- Do not recommend changing unknown settings.
- If the property is CRITICAL or WARNING, do not recommend increasing base price, minimum floor, or rates.
- Use percentage adjustments for pricing requests, such as -3%, -8%, -10%, or +5%.
- Only switch to a fixed dollar rate when the calculated percentage rate would hit or cross the minimum price floor.
- If owner tags include Fixed Min Rate or No Promotions, call that out and avoid discount/promo language.
- Every proposed action must be approve/decline friendly and include the exact field/date/percentage to review.
- Same-day bookings are not allowed: do not propose or apply rate edits for today.
- Never edit Last Minute adjustment settings; the 999% same-day protection must stay locked.
""".strip()


def _local_property_report(report_type: str, prop: Property, error: Exception | None = None) -> str:
    if report_type == "listing":
        return _local_listing_optimizer_report(prop, error)

    benchmark = _benchmark_for(prop)
    occ_gap = benchmark.get("occ_gap", 0)
    occ = prop.adj_occ_60d
    pickup = prop.booked_14d
    status = prop.urgency.upper()
    issue_text = "; ".join(prop.issues) if prop.issues else "No major PriceLabs issue flags."
    owner_text = _owner_note(prop) or "No owner promo restriction detected."

    if prop.urgency in {"critical", "warning"} and occ < 0.35 and pickup == 0:
        verdict = "Demand is soft. Review date-level price position before using broad promotions."
        action = "Check exposed open dates, compare posted rate to market booked and last-year booked price, then use a narrow date adjustment only where our posted rate is high."
    elif prop.urgency == "overperforming":
        verdict = "Booking pace is strong. Protect ADR and review whether future dates are underpriced."
        action = "Avoid discounts. Review selective increases for high-demand forward windows."
    elif pickup == 0:
        verdict = "No recent pickup, but occupancy is not weak enough for an automatic discount."
        action = "Hold base price and inspect restrictions, availability, channel visibility, and orphan gaps."
    else:
        verdict = "No emergency price move from current synced metrics."
        action = "Monitor pickup and use the forward-pacing price-position check for date-level decisions."

    ai_note = f"\n\n_AI provider unavailable: {error}_" if error else ""
    return f"""
## Local Revenue Review

**Verdict:** {verdict}

**Current Signal**
- Status: {status}
- Base price: {_money(prop.base_price)}
- Minimum price: {_money(prop.min_price) if prop.min_price else "unknown"}
- 60-day adjusted occupancy: {occ:.0%}
- Booked nights, last 15-day pickup window: {pickup}
- Benchmark: {benchmark.get("basis", "portfolio")} ({benchmark.get("sample_size", 0)} listings)
- Occupancy gap vs benchmark: {occ_gap:+.1f} pts

**Issues**
{issue_text}

**Owner / Promo Constraint**
{owner_text}

**Recommended Next Action**
{action}

**Forward Pacing Rule**
Do not discount just because pacing is behind. Compare:
- our posted future price and posted percentile
- market booked price
- last year's booked price
- market posted price

If we are behind pace but already below market booked and last-year booked price, hold price and check conversion, restrictions, visibility, availability, and Booking.com promotion stacking.
{ai_note}
""".strip()


def _local_listing_optimizer_report(prop: Property, error: Exception | None = None) -> str:
    ll = lookup_links(prop.name)
    airbnb_title = ll.airbnb_headline if ll and ll.airbnb_headline else "Not synced"
    vrbo_title = ll.vrbo_headline if ll and ll.vrbo_headline else "Not synced"
    airbnb_rating = _format_rating(ll.airbnb_rating, ll.airbnb_reviews) if ll else "unknown"
    vrbo_rating = _format_rating(ll.vrbo_rating, ll.vrbo_reviews) if ll else "unknown"
    airbnb_photos = _format_count(ll.airbnb_photos) if ll else "unknown"
    vrbo_photos = _format_count(ll.vrbo_photos) if ll else "unknown"
    lid = str(prop.listing_id or "").strip()
    ha_reviews = []  # PriceLabs carries no review data
    pl_listings = _load_pricelabs_snapshot()
    pl_data = next((l for l in pl_listings if str(l.get("id","")) == lid), {})
    cleaning_fee = pl_data.get("cleaning_fees")
    channels = pl_data.get("channel_listing_details") or []
    ota_fee_map = {"airbnb": "3% host fee", "vrbo": "5% host fee", "booking.com": "15% commission", "bookingcom": "15% commission"}
    evidence = []
    if ll and (ll.airbnb_headline or ll.vrbo_headline):
        evidence.append("titles")
    if ll and (ll.airbnb_photos is not None or ll.vrbo_photos is not None):
        evidence.append("photo counts")
    if ll and (ll.airbnb_rating is not None or ll.vrbo_rating is not None):
        evidence.append("ratings/reviews")
    score_note = "provisional" if len(evidence) < 3 else "evidence-based"
    ai_note = f"\n\n_AI provider unavailable: {error}_" if error else ""

    # OTA channels section
    ota_lines = []
    for ch in channels:
        name = str(ch.get("channel_name") or ch.get("channelName") or "").lower()
        ch_id = ch.get("channel_listing_id") or ch.get("channelListingId") or "unknown"
        fee = ota_fee_map.get(name, "fee unknown")
        ota_lines.append(f"- {name.title()}: ID {ch_id} — {fee}")
    ota_section = "\n".join(ota_lines) if ota_lines else "- No OTA channel data synced"

    # Fees section
    fees_section = f"- Cleaning Fee: ${cleaning_fee:.0f}" if cleaning_fee else "- Cleaning fee: not synced"

    # Reviews section
    if ha_reviews:
        review_lines = []
        for r in ha_reviews:
            rating = f"⭐ {r['rating']}/5" if r.get("rating") else ""
            ch = r.get("channel", "").title()
            dt = r.get("date", "")
            comment = r.get("comment") or "No comment"
            review_lines.append(f"**{ch} {dt} {rating}**\n  _{comment}_")
        reviews_section = "\n\n".join(review_lines)
    else:
        reviews_section = "_Review data is not available - PriceLabs does not carry guest reviews._"

    return f"""
## Listing Optimizer

**Evidence-Based Quality Score:** {score_note}

## Title Optimization

**Current**
- Airbnb: {airbnb_title}
- VRBO: {vrbo_title}

**Recommended**
- Airbnb: Keep under 50 characters; lead with location or the clearest guest-facing hook.
- VRBO: Use a slightly fuller title under 70 characters with bedroom count and primary amenity.

## Photo And Visual Check
- Airbnb photos: {airbnb_photos} ({ll.airbnb_photo_grade if ll else "not synced"})
- VRBO photos: {vrbo_photos} ({ll.vrbo_photo_grade if ll else "not synced"})

Manual check: cover image, first 8 photo order, bedroom/bath coverage, hot tub/view/game-room proof, and thumbnail crop.

## Reviews And Trust
- Airbnb: {airbnb_rating}
- VRBO: {vrbo_rating}

### Recent Guest Reviews
{reviews_section}

## OTA Channels & Fees
{ota_section}

### Property Fees
{fees_section}

## Positioning
- Group: {_group_label(prop)}
- Bedrooms: {prop.bedrooms}
- Guest Capacity: {ha.get('personCapacity', 'not synced')}
- Bathrooms: {ha.get('bathroomsNumber', 'not synced')}
- Inferred segment: {"families / groups" if prop.bedrooms >= 3 else "couples / small groups"}


## Action Checklist
1. Open Airbnb, VRBO, and Booking.com links from the dashboard.
2. Confirm the OTA title matches the intended positioning.
3. Check whether the first photo sells the main reason to book.
4. Verify amenity filters that affect search conversion.
5. Confirm bedroom/bath count and sleeping setup consistency.
6. Review recent guest feedback above and address recurring complaints.
{ai_note}
""".strip()


def _has_listing_quality_data(prop: Property) -> bool:
    ll = lookup_links(prop.name)
    if not ll:
        return False
    has_real_title = any(
        title and title.strip().lower() != prop.property_name.strip().lower()
        for title in (ll.airbnb_headline, ll.vrbo_headline)
    )
    has_reviews = ll.airbnb_rating is not None or ll.vrbo_rating is not None
    has_photos = ll.airbnb_photos is not None or ll.vrbo_photos is not None
    return has_real_title or has_reviews or has_photos


def _build_listing_quality_prompt(prop: Property) -> str:
    quality_rules = _listing_quality_rules()
    ll = lookup_links(prop.name)
    airbnb_title = ll.airbnb_headline if ll and ll.airbnb_headline else "Not available"
    vrbo_title   = ll.vrbo_headline   if ll and ll.vrbo_headline   else "Not available"
    airbnb_url   = ll.airbnb_url      if ll and ll.airbnb_url      else "Not listed"
    vrbo_url     = ll.vrbo_url        if ll and ll.vrbo_url        else "Not listed"
    pms_id = ll.streamline_id  if ll else "Unknown"
    airbnb_rating = _format_rating(ll.airbnb_rating, ll.airbnb_reviews) if ll else "unknown"
    airbnb_rating_status = ll.airbnb_rating_status if ll else "unknown"
    vrbo_rating = _format_rating(ll.vrbo_rating, ll.vrbo_reviews) if ll else "unknown"
    vrbo_rating_status = ll.vrbo_rating_status if ll else "unknown"
    airbnb_photos = _format_count(ll.airbnb_photos) if ll else "unknown"
    vrbo_photos = _format_count(ll.vrbo_photos) if ll else "unknown"
    airbnb_photo_grade = ll.airbnb_photo_grade if ll else "unknown"
    vrbo_photo_grade = ll.vrbo_photo_grade if ll else "unknown"
    min_price = f"${prop.min_price:.0f}" if prop.min_price else "unknown"
    min_stay = f"{prop.min_stay} nights" if prop.min_stay else "unknown"
    review_lines = []
    if ll and ll.airbnb_rating is not None:
        review_lines.append(f"  Airbnb Rating: {airbnb_rating} — status: {airbnb_rating_status}")
    if ll and ll.vrbo_rating is not None:
        review_lines.append(f"  VRBO Rating:   {vrbo_rating} — status: {vrbo_rating_status}")
    if ll and ll.vrbo_review_label:
        review_lines.append(f"  VRBO Review Label: {ll.vrbo_review_label}")
    if ll and any(v is not None for v in (ll.vrbo_cleanliness, ll.vrbo_checkin, ll.vrbo_communication, ll.vrbo_location)):
        review_lines.append(
            f"  VRBO Category Scores: Cleanliness {ll.vrbo_cleanliness if ll.vrbo_cleanliness is not None else 'n/a'}, "
            f"Check-in {ll.vrbo_checkin if ll.vrbo_checkin is not None else 'n/a'}, "
            f"Communication {ll.vrbo_communication if ll.vrbo_communication is not None else 'n/a'}, "
            f"Location {ll.vrbo_location if ll.vrbo_location is not None else 'n/a'}"
        )
    photo_lines = []
    if ll and ll.airbnb_photos is not None:
        photo_lines.append(f"  Airbnb Photos: {airbnb_photos} — {airbnb_photo_grade}")
    if ll and ll.vrbo_photos is not None:
        photo_lines.append(f"  VRBO Photos:   {vrbo_photos} — {vrbo_photo_grade}")
    review_photo_context = "\n".join(review_lines + photo_lines) if review_lines or photo_lines else "  No reliable review/rating/photo data synced yet."

    # Infer property type from name
    name_lower = prop.name.lower()
    if "studio" in name_lower or prop.bedrooms == 0:
        prop_type = "Studio"
    elif prop.bedrooms == 1:
        prop_type = "1-Bedroom Condo"
    elif prop.bedrooms == 2:
        prop_type = "2-Bedroom Condo"
    elif prop.bedrooms >= 3:
        prop_type = f"{prop.bedrooms}-Bedroom Home"
    else:
        prop_type = "Condo"

    area_desc = _group_label(prop) or prop.area or prop.city or "HVR Smokies"

    return f"""
You are a listing optimization analyst for HVR Smokies short-term rentals, similar to PriceLabs Listing Optimizer.
Analyze this listing and produce a practical listing-quality report for the dashboard. Today is {TODAY.isoformat()}.

Use the LISTING QUALITY RULEBOOK below as the source of truth. If the rulebook conflicts with a generic instinct,
follow the rulebook. Do not give vague advice. If source data is missing, keep it out of the score and put it under
Manual Review Needed instead of creating long NA sections.

Hard constraints:
- Do not generate a low score just because data is unsynced.
- Do not write repeated NA rows or sections.
- Score only synced evidence: titles, photo counts, thumbnails, ratings, reviews, VRBO category scores, links, and visible metadata.
- Put missing descriptions, amenities, photo order, cover image quality, and guest-favorite status under Manual Review Needed.
- For Smokies listings, prioritize experience amenities and OTA filters: hot tub, mountain views, indoor pool, game room, theater room, pet friendly, fire pit/outdoor lounge, EV charger, sauna/cold plunge, fast Wi-Fi/workstation, kid amenities, covered deck/grill, coffee bar/standout design, and wedding/event-friendly spaces.
- Treat indoor pool, exceptional mountain view, luxury outdoor space, theater + arcade combo, sauna/cold plunge, and unique design as the largest potential pricing-premium signals when they are supported by title/photo/amenity data.
- Treat pet friendly, hot tub, flexible cancellation, competitive cleaning fee, fast Wi-Fi, and game room as occupancy-driver checks. Do not claim they exist unless synced or visible.
- For cover-photo recommendations, favor hot tub with mountain view, indoor pool, sunset deck, dramatic cabin/A-frame exterior, or theater/game room lighting when the listing actually has that feature.
- If fewer than three evidence categories are synced, write "Not scored - insufficient synced listing data" instead of a numeric score.
- Do not recommend adding photos when photo count is unknown. Say "sync or manually check photo count and order" instead.
- Do not recommend rewriting a full description when the description is unsynced. Give a manual-review checklist instead.
- Do not use placeholder text like [Inferred], [NA], or bracketed template labels.
- Do not upgrade claims: "mountain view" is not "panoramic mountain view"; "pool access" is not "private indoor pool"; "near Gatlinburg" is not "walk to downtown".
- Count title characters accurately before criticizing title length.
- Do not claim promotional language unless the title actually contains sale, discount, special, deal, limited time, or excessive punctuation/symbols.
- For target segments, use real segment names such as Couples, Families, Remote Workers, Large Groups, or Leisure Travelers, and label them as inferred when needed.

LISTING QUALITY RULEBOOK:
{quality_rules if quality_rules else "No external rulebook file found; use the explicit rules in this prompt."}

PROPERTY DATA:
  Name: {prop.name}
  Type: {prop_type}
  Location: {area_desc}
  Bedrooms: {prop.bedrooms}
  Base Price: ${prop.base_price:.0f}/night  |  Min Price: {min_price}
  Min Stay: {min_stay}
  PMS ID: {pms_id}

OTA LISTING TITLES:
  Airbnb Title: "{airbnb_title}" ({len(airbnb_title)} characters)
  VRBO Title:   "{vrbo_title}" ({len(vrbo_title)} characters)
  Airbnb URL: {airbnb_url}
  VRBO URL:   {vrbo_url}

REVIEW / PHOTO DATA:
{review_photo_context}

PriceLabs SETTINGS (pricing context):
  Last Minute Discounting: {prop.last_minute}
  Long-term Pricing: {prop.long_term_pricing}
  Occupancy Pacing: {prop.occupancy_pacing}
  Gaps & Adjacencies: {prop.gaps_adjacencies}
  Demand Sensitivity: {prop.demand_sensitivity}%

BOOKING PERFORMANCE:
  Adj. Occupancy 60-day: {prop.adj_occ_60d:.0%}
  Booked nights next 7d / 14d: {prop.booked_7d} / {prop.booked_14d}
  Last booked: {f"{prop.last_booked_days} days ago" if prop.last_booked_days is not None else "no record"}
  Min-price hit rate 60d: {prop.min_price_occ_60d:.0%}

---

Produce this exact concise structure:

## Listing Optimizer

**Evidence-Based Quality Score:** X.X/10 or "Not scored - insufficient synced listing data"
Score only synced evidence. If fewer than three evidence categories are synced, do not give a numeric score.

### Top Fixes
Give 3-5 prioritized fixes. Each fix must be actionable and based on synced data or clearly marked "manual review". Do not call missing data a defect.

## Title Optimization

**Current**
- Airbnb: "{airbnb_title}"
- VRBO: "{vrbo_title}"

**Recommended**
- Airbnb: one improved title, 50 characters or fewer, with character count
- VRBO: one improved title, 70 characters or fewer, with character count

**Why**
Explain what the current title does well and exactly what to change.

## Photo And Visual Check
Use only synced photo counts and thumbnail availability.
- Airbnb photos: {airbnb_photos} ({airbnb_photo_grade})
- VRBO photos: {vrbo_photos} ({vrbo_photo_grade})

Say what the cover image and first 8 photos should be manually checked for. Do not invent photo quality.

## Reviews And Trust
Use only the explicit ratings, review counts, and VRBO category scores provided.
- Airbnb: {airbnb_rating} - {airbnb_rating_status}
- VRBO: {vrbo_rating} - {vrbo_rating_status}

Explain if review score/count is likely hurting conversion, or say it looks healthy if supported.

## Positioning
List likely guest segments and selling points supported by the listing name, bedroom count, group, ratings, or synced titles. Mark inferred items as inferred. For Smokies, explicitly check whether the listing is positioned for couples, families, large groups, pet owners, remote workers, luxury travelers, or event/wedding groups.

## Manual Review Needed
List only missing items that would materially improve the optimizer:
- full description
- amenities
- cover image quality
- photo order
- guest favorite / badge status
- platform consistency
- high-impact Smokies amenity filters and photo proof: hot tub, mountain view, indoor pool, game room, theater room, pet friendly, fire pit, covered deck/grill, EV charger, sauna/cold plunge, fast Wi-Fi/workstation, kid amenities, coffee bar/design feature

## Action Checklist
Return 5-8 checklist items the team can complete in Hostaway/OTA/PriceLabs Listing Optimizer.
""".strip()


def _build_portfolio_prompt(report_type: str) -> str:
    today = TODAY.isoformat()
    active = [p for p in _PORTFOLIO if p.active]
    critical = [p for p in active if p.urgency == "critical"]
    warning = [p for p in active if p.urgency == "warning"]

    # Keep critical list short — top 12 only, condensed format
    critical_summary = "\n".join(
        f"  {p.property_name} | {p.bedrooms}BR | occ60={p.adj_occ_60d:.0%} | bkd14={p.booked_14d} | base=${p.base_price:.0f} | {p.issues[0] if p.issues else ''}"
        for p in critical[:12]
    )

    if report_type == "portfolio":
        return f"""
You are an STR revenue manager. Today is {today}. Portfolio: {_SUMMARY['total_active']} active HVR Smokies / Tennessee vacation rentals.

STATS: Critical={_SUMMARY['critical_count']} | Warning={_SUMMARY['warning_count']} | OK={_SUMMARY['ok_count']}
Zero bookings next 14d: {_SUMMARY['zero_bookings_14d']} properties | Avg occ 60d: {_SUMMARY['avg_occ_60d']}%
LT pricing disabled: {_SUMMARY['long_term_pricing_disabled']}/{_SUMMARY['total_active']} | Occ pacing disabled: {_SUMMARY['occupancy_pacing_disabled']}/{_SUMMARY['total_active']}

TOP CRITICAL PROPERTIES:
{critical_summary}

Provide:
1. **Portfolio Health Score** (0–100) + one-line verdict
2. **Systemic Issues** — problems hitting 10+ properties, portfolio-wide fix for each
3. **Top 5 Critical Properties** — why critical, the single most important fix, revenue at stake
4. **3 Quick Wins** — changes in PriceLabs or Hostaway this week, broadest impact
5. **Revenue Projection** — if critical properties hit 70% occupancy, estimated monthly uplift

Rules:
- This is Tennessee / Smokies inventory, not Maui or Hawaii.
- Do not include a Settings Gaps section when settings are unknown or not synced.
""".strip()

    return _build_portfolio_prompt("portfolio")


def _clean_channel(value: object) -> str:
    text = str(value or "").strip()
    low = text.lower()
    if "airbnb" in low:
        return "Airbnb"
    if "vrbo" in low or "homeaway" in low:
        return "VRBO"
    if "booking" in low:
        return "Booking.com"
    if "direct" in low or "website" in low:
        return "Direct"
    return text or "Unknown"


def _extract_calendar_day(item: dict, fallback_date: date) -> dict:
    day_text = (
        item.get("date")
        or item.get("startDate")
        or item.get("calendarDate")
        or item.get("day")
        or fallback_date.isoformat()
    )
    status = str(item.get("status") or item.get("availability") or "").strip().lower()
    reservation = item.get("reservation") if isinstance(item.get("reservation"), dict) else {}
    reservation_id = (
        item.get("reservationId")
        or item.get("reservation_id")
        or reservation.get("id")
        or item.get("bookingId")
    )
    channel = _clean_channel(
        item.get("channelName")
        or item.get("channel")
        or item.get("source")
        or reservation.get("channelName")
        or reservation.get("channel")
    )
    rate = item.get("price") or item.get("rate") or item.get("nightlyRate") or item.get("amount")
    try:
        rate = round(float(str(rate).replace("$", "").replace(",", "")), 2)
    except (TypeError, ValueError):
        rate = None
    is_booked = bool(reservation_id) or status in {"reserved", "booked", "unavailable"}
    return {
        "date": str(day_text)[:10],
        "status": "booked" if is_booked else "open",
        "channel": channel if is_booked else "",
        "reservation_id": str(reservation_id or ""),
        "rate": rate,
        "guest": reservation.get("guestName") or item.get("guestName") or "",
    }


def _revenue_calendar(prop: Property, days: int = 30) -> dict:
    start = TODAY
    rate_by_date = {}
    for day_text, rate in prop.calendar_rates:
        rate_by_date[day_text] = rate
    fallback_days = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        rate = rate_by_date.get(day.isoformat())
        fallback_days.append({
            "date": day.isoformat(),
            "status": "unknown" if rate is None else "rate_only",
            "channel": "",
            "reservation_id": "",
            "rate": rate,
            "guest": "",
        })
    return {
        "ok": True,
        "source": "PriceLabs calendar export",
        "property": prop.name,
        "days": fallback_days,
        "message": "This data is available in PriceLabs Booking Insights. Export the PriceLabs CSV from this Booking Insights or detailed report view and upload it here so the dashboard can track monthly revenue, occupancy, ADR, and OTA/channel detail when included.",
    }


def _fmt_money_api(value: object) -> str:
    try:
        if value is None:
            return "-"
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct_api(value: object, signed: bool = False) -> str:
    try:
        if value is None:
            return "-"
        val = float(value)
        prefix = "+" if signed and val > 0 else ""
        return f"{prefix}{val * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _match_action_to_property(action: dict, prop: Property) -> bool:
    def key(value: object) -> str:
        text = str(value or "").split(" -- ")[0].split(": Default")[0]
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    action_keys = [key(action.get("property")), key(action.get("display_name"))]
    prop_keys = [key(prop.name), key(prop.property_name)]
    return any(a and p and (a == p or a in p or p in a) for a in action_keys for p in prop_keys)


def _revenue_insights(prop: Property) -> dict:
    actions = [a for a in _load_actions() if _is_monthly_pacing_action(a)]
    if not actions and MONTHLY_PACING_PATH.exists():
        actions, _summary = _generate_monthly_pacing_actions()

    matched = [a for a in actions if _match_action_to_property(a, prop)]
    primary = matched[0] if matched else None
    metrics = primary.get("monthly_metrics", {}) if primary else {}
    revenue = metrics.get("rental_revenue")
    revenue_stly = metrics.get("rental_revenue_stly")
    revenue_yoy = metrics.get("rental_revenue_yoy")
    occ = metrics.get("paid_occupancy")
    occ_stly = metrics.get("paid_occupancy_stly")
    occ_gap = metrics.get("paid_occupancy_gap")
    revpar = metrics.get("revpar")
    market_revpar = metrics.get("market_revpar")
    adr = prop.base_price or None

    if primary:
        signal = primary.get("monthly_signal", "monitor")
        recommendation = primary.get("suggestion") or primary.get("proposed_value") or "Monitor monthly pacing"
        reason = primary.get("reason", "")
        target_dates = primary.get("target_dates") or "Report Builder range"
    elif prop.urgency == "overperforming":
        signal = "overperforming"
        recommendation = "Fast pace: protect ADR and review selective future-date increases"
        reason = f"PriceLabs portfolio shows {prop.adj_occ_60d:.0%} 60-day occupancy and {prop.booked_14d} booked nights in the next 14 days."
        target_dates = "PriceLabs portfolio export"
    elif prop.urgency in {"critical", "warning"}:
        signal = "behind_occupancy"
        recommendation = "Behind occupancy: inspect open dates, OTA visibility, and price position"
        reason = f"PriceLabs portfolio shows {prop.adj_occ_60d:.0%} 60-day occupancy and {prop.booked_14d} booked nights in the next 14 days."
        target_dates = "PriceLabs portfolio export"
    else:
        signal = "monitor"
        recommendation = "Monitor next refresh"
        reason = f"PriceLabs portfolio shows {prop.adj_occ_60d:.0%} 60-day occupancy."
        target_dates = "PriceLabs portfolio export"

    rows = []
    if primary:
        rows.append({
            "period": target_dates,
            "revenue": _fmt_money_api(revenue),
            "revenue_delta": _fmt_pct_api(revenue_yoy, True),
            "occupancy": _fmt_pct_api(occ),
            "occupancy_delta": _fmt_pct_api(occ_gap, True),
            "adr": _fmt_money_api(adr),
            "revpar": _fmt_money_api(revpar),
            "market_revpar": _fmt_money_api(market_revpar),
            "signal": signal,
        })

    return {
        "ok": True,
        "property": prop.name,
        "source": "PriceLabs Report Builder" if primary else "PriceLabs portfolio export",
        "has_report_builder": bool(primary),
        "signal": signal,
        "recommendation": recommendation,
        "reason": reason,
        "target_dates": target_dates,
        "kpis": {
            "revenue": _fmt_money_api(revenue),
            "revenue_stly": _fmt_money_api(revenue_stly),
            "revenue_yoy": _fmt_pct_api(revenue_yoy, True),
            "occupancy": _fmt_pct_api(occ if occ is not None else prop.adj_occ_60d),
            "occupancy_stly": _fmt_pct_api(occ_stly),
            "occupancy_gap": _fmt_pct_api(occ_gap, True),
            "adr": _fmt_money_api(adr),
            "revpar": _fmt_money_api(revpar),
            "market_revpar": _fmt_money_api(market_revpar),
        },
        "rows": rows,
        "message": "" if primary else "PriceLabs Booking Insights is available in the PriceLabs UI, but its CSV has not been loaded for this listing yet. Showing portfolio pacing signals until the PriceLabs export is uploaded.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", summary=_SUMMARY, today=TODAY.isoformat())


def _persist_upload(file_storage, local_path: Path, remote_name: str) -> None:
    """Save an uploaded file locally (best-effort) and mirror to Supabase Storage.

    The local write is wrapped in try/except so the read-only Vercel filesystem
    does not break the request when Supabase Storage is the durable target.
    """
    body = file_storage.read()
    try:
        local_path.write_bytes(body)
    except OSError:
        pass
    if supabase_store.is_enabled():
        supabase_store.upload_csv(remote_name, body)


@app.route("/api/reload", methods=["POST"])
def reload_data():
    """
    Accept uploaded CSV files and hot-reload portfolio data without restart.
    Form fields:  pricelabs_csv (file, optional)
                  marketing_csv  (file, optional)
                  report_builder_csv (file, optional)
    """
    updated = []
    if "pricelabs_csv" in request.files or "wheelhouse_csv" in request.files:
        f = request.files.get("pricelabs_csv") or request.files["wheelhouse_csv"]
        if f.filename:
            _persist_upload(f, CSV_PATH, "pricelabs_portfolio.csv")
            updated.append("pricelabs")

    if "marketing_csv" in request.files:
        f = request.files["marketing_csv"]
        if f.filename:
            _persist_upload(f, MARKETING_PATH, "marketing_links.csv")
            updated.append("marketing")

    if "report_builder_csv" in request.files:
        f = request.files["report_builder_csv"]
        if f.filename:
            _persist_upload(f, MONTHLY_PACING_PATH, "pricelabs_report_builder_monthly.csv")
            updated.append("report_builder")

    if updated:
        _reload_portfolio()
        return jsonify({
            "ok": True,
            "updated": updated,
            "summary": _SUMMARY,
        })
    return jsonify({"ok": False, "error": "No files uploaded"}), 400


@app.route("/api/summary")
def get_summary():
    """Return current portfolio summary stats (for after a reload)."""
    return jsonify(_SUMMARY)


@app.route("/api/actions")
def get_actions():
    actions = _load_actions()
    if request.args.get("generate") == "1":
        actions = _generate_monthly_pacing_actions()[0] if request.args.get("source") == "monthly_pacing" else _generate_weekly_actions()
    source = request.args.get("source")
    if source == "weekly":
        actions = [
            a for a in actions
            if not _is_pace_year_action(a) and not _is_monthly_pacing_action(a)
        ]
    elif source == "monthly_pacing":
        actions = [a for a in actions if _is_monthly_pacing_action(a)]
    elif source == "pace_year":
        actions = []
    status = request.args.get("status")
    if status:
        actions = [a for a in actions if a.get("status") == status]
    else:
        # Always hide rejected/declined unless caller explicitly requests them
        actions = [a for a in actions if a.get("status") not in {"rejected", "declined"}]
    return jsonify({"ok": True, "actions": actions})


@app.route("/api/monthly-pacing")
def monthly_pacing():
    has_csv = MONTHLY_PACING_PATH.exists()
    try:
        if request.args.get("generate") == "1":
            actions, summary = _generate_monthly_pacing_actions()
            return jsonify({"ok": True, "actions": actions, "summary": summary, "has_csv": has_csv})

        result = load_monthly_pacing(MONTHLY_PACING_PATH, _PORTFOLIO, TODAY)
        actions = [a for a in _load_actions() if _is_monthly_pacing_action(a)]
        if not actions and has_csv:
            actions, summary = _generate_monthly_pacing_actions()
            result["summary"] = summary
        if actions:
            result["actions"] = actions
        result["has_csv"] = has_csv
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "has_csv": has_csv, "actions": [], "summary": {}}), 500


@app.route("/api/kcity-surge/test-push")
def kcity_surge_test_push():
    """Test endpoint: sends ONE date override for listing 349115 and returns the raw PriceLabs response."""
    try:
        pl = client_from_env()
        payload = {"pms": "hostaway", "overrides": [{"date": "2026-09-03", "price": 755, "price_type": "fixed", "currency": "USD"}]}
        response = pl.request("POST", "/listings/349115/overrides", payload)
        refresh = None
        try:
            refresh = pl.request("POST", "/listings/349115/refresh", {"pms": "hostaway"})
        except Exception as re:
            refresh = {"error": str(re)}
        return jsonify({"ok": True, "sent": payload, "response": response, "refresh": refresh})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


DEMAND_ACTION_THRESHOLDS = {
    "increase": 0.85,   # occ >= 85% -> raise rate/DSO floor, demand is strong
    "hold": 0.55,       # occ 55-84% -> on pace, no action needed
    "watch": 0.40,      # occ 40-54% -> borderline, keep an eye on pickup
    # occ < 40% -> decrease: loosen LOS / lower rate to drive bookings
}


def _demand_action(occ: float | None) -> dict:
    """Classify an occupancy reading into an action recommendation.
    Thresholds match the ones already used elsewhere (40% base-rate scorecard
    minimum, 85% overperforming cutoff) so this reads consistently with the
    rest of the dashboard."""
    if occ is None:
        return {"action": "unknown", "label": "No data", "detail": "Not enough forward occupancy data yet."}
    if occ >= DEMAND_ACTION_THRESHOLDS["increase"]:
        return {"action": "increase", "label": "INCREASE", "detail": "Demand is strong — raise the rate or DSO floor."}
    if occ >= DEMAND_ACTION_THRESHOLDS["hold"]:
        return {"action": "hold", "label": "HOLD", "detail": "On pace — no action needed."}
    if occ >= DEMAND_ACTION_THRESHOLDS["watch"]:
        return {"action": "watch", "label": "WATCH", "detail": "Borderline — monitor pickup before acting."}
    return {"action": "decrease", "label": "DECREASE", "detail": "Below target — loosen LOS restrictions and/or lower the rate to drive bookings."}


def _is_likely_blocked(p) -> bool:
    """True when a listing's ~100% forward occupancy is almost certainly owner
    blocks / an off-market calendar rather than genuine sold-out demand.

    PriceLabs counts blocked nights as occupied, so a fully-blocked cabin looks
    identical to a fully-booked one on occupancy alone. The distinguishing
    signal is booking ACTIVITY: verified against live data, 26 of 27 listings
    at 100% next-60-day occupancy had zero bookings in 14 days AND no
    last-booked date on record, versus only 32 of 263 listings below 100% —
    a clearly separate population.

    Deliberately conservative: only applies at >=99% occupancy, so a genuinely
    sold-out listing with recent booking activity is never excluded.
    """
    return (
        p.adj_occ_60d >= 0.99
        and p.booked_14d == 0
        and p.last_booked_days is None
    )


def _group_forward_occ(props: list) -> dict:
    """Average forward occupancy per customization_group across 30/60/90/120/180-day windows."""
    by_group: dict[str, dict] = {}
    for p in props:
        if not p.active:
            continue
        g = p.customization_group or "Ungrouped"
        slot = by_group.setdefault(g, {"listings": 0, "occ_30d": [], "occ_60d": [], "occ_90d": [], "occ_120d": [], "occ_180d": []})
        slot["listings"] += 1
        if p.adj_occ_30d:  slot["occ_30d"].append(p.adj_occ_30d)
        if p.adj_occ_60d:  slot["occ_60d"].append(p.adj_occ_60d)
        if p.adj_occ_90d:  slot["occ_90d"].append(p.adj_occ_90d)
        if p.adj_occ_120d: slot["occ_120d"].append(p.adj_occ_120d)
        if p.adj_occ_180d: slot["occ_180d"].append(p.adj_occ_180d)

    def _avg(vals):
        return round(sum(vals) / len(vals), 3) if vals else None

    result = {}
    for g, slot in by_group.items():
        result[g] = {
            "listings": slot["listings"],
            "occ_30d": _avg(slot["occ_30d"]),
            "occ_60d": _avg(slot["occ_60d"]),
            "occ_90d": _avg(slot["occ_90d"]),
            "occ_120d": _avg(slot["occ_120d"]),
            "occ_180d": _avg(slot["occ_180d"]),
        }
    return result


def _nearest_window_occ(group_occ: dict, days_until: int) -> tuple[float | None, str]:
    """Pick the forward-occupancy window closest to how far out an event is."""
    windows = [(30, "occ_30d"), (60, "occ_60d"), (90, "occ_90d"), (120, "occ_120d"), (180, "occ_180d")]
    best_key, best_label = min(windows, key=lambda w: abs(w[0] - days_until))[1], None
    for day_count, key in windows:
        if key == best_key:
            best_label = f"{day_count}d"
    return group_occ.get(best_key), best_label


@app.route("/api/demand-radar")
def demand_radar():
    """Upcoming high-demand events + current group occupancy, so it's clear
    whether a group needs a rate/LOS decrease, hold, or increase."""
    from kcity_surge_dso import _is_kcity, SURGE_DATES
    from high_demand_calendar import HIGH_DEMAND_DATES

    today = TODAY
    group_occ = _group_forward_occ(_PORTFOLIO)

    # KCity/Knox group occupancy — blend every group whose listings match
    # _is_kcity, since surge events target the whole Knoxville metro area.
    kcity_groups = {p.customization_group or "Ungrouped" for p in _PORTFOLIO if p.active and _is_kcity(p)}
    kcity_occ: dict = {"listings": 0, "occ_30d": [], "occ_60d": [], "occ_90d": [], "occ_120d": [], "occ_180d": []}
    for p in _PORTFOLIO:
        if not (p.active and _is_kcity(p)):
            continue
        kcity_occ["listings"] += 1
        for days, attr in ((30, "adj_occ_30d"), (60, "adj_occ_60d"), (90, "adj_occ_90d"), (120, "adj_occ_120d"), (180, "adj_occ_180d")):
            v = getattr(p, attr, 0)
            if v:
                kcity_occ[f"occ_{days}d"].append(v)
    kcity_occ_avg = {
        "listings": kcity_occ["listings"],
        **{f"occ_{d}d": (round(sum(kcity_occ[f"occ_{d}d"]) / len(kcity_occ[f"occ_{d}d"]), 3) if kcity_occ[f"occ_{d}d"] else None)
           for d in (30, 60, 90, 120, 180)},
    }

    # Upcoming events: dedupe SURGE_DATES entries (currently stored once per
    # bedroom/demand tier) down to one row per (start, end, event_label).
    seen_events = {}
    for start_str, end_str, event, demand in SURGE_DATES:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        if end < today:
            continue
        key = (start_str, end_str, event)
        if key in seen_events and seen_events[key]["demand_level"] == "high":
            continue  # keep the high-demand entry if both exist for the same dates
        seen_events[key] = {"start": start_str, "end": end_str, "event": event, "demand_level": demand}

    events = []
    for e in seen_events.values():
        start = date.fromisoformat(e["start"])
        days_until = (start - today).days
        occ, window_label = _nearest_window_occ(kcity_occ_avg, days_until)
        action = _demand_action(occ)
        events.append({
            "event": e["event"],
            "group": "Knoxville / KCity",
            "start_date": e["start"],
            "end_date": e["end"],
            "days_until": days_until,
            "demand_level": e["demand_level"],
            "occ_pct": round(occ * 100, 1) if occ is not None else None,
            "occ_window": window_label,
            **action,
        })
    # General portfolio-wide holidays/seasonal peaks (Christmas, July 4th, fall
    # foliage, etc.) — these apply across EVERY group, not just Knoxville, so
    # generate one row per (holiday, group) using that group's own occupancy.
    seen_holidays = {}
    for start_str, end_str, event, demand in HIGH_DEMAND_DATES:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        if end < today:
            continue
        seen_holidays[(start_str, end_str, event)] = {"start": start_str, "end": end_str, "event": event, "demand_level": demand}

    for e in seen_holidays.values():
        start = date.fromisoformat(e["start"])
        days_until = (start - today).days
        for g, occ in group_occ.items():
            group_occ_pct, window_label = _nearest_window_occ(occ, days_until)
            action = _demand_action(group_occ_pct)
            events.append({
                "event": e["event"],
                "group": g,
                "start_date": e["start"],
                "end_date": e["end"],
                "days_until": days_until,
                "demand_level": e["demand_level"],
                "occ_pct": round(group_occ_pct * 100, 1) if group_occ_pct is not None else None,
                "occ_window": window_label,
                **action,
            })

    events.sort(key=lambda e: (e["days_until"], e["event"], e["group"]))
    events = events[:80]  # nearest ~3-4 holiday windows across every group, plus KCity events

    # General group pacing checkpoints (covers groups with no named event calendar)
    groups = []
    for g, occ in sorted(group_occ.items()):
        primary = occ.get("occ_60d") if occ.get("occ_60d") is not None else occ.get("occ_90d")
        action = _demand_action(primary)
        groups.append({
            "group": g,
            "listings": occ["listings"],
            "occ_30d": round(occ["occ_30d"] * 100, 1) if occ["occ_30d"] is not None else None,
            "occ_60d": round(occ["occ_60d"] * 100, 1) if occ["occ_60d"] is not None else None,
            "occ_90d": round(occ["occ_90d"] * 100, 1) if occ["occ_90d"] is not None else None,
            "occ_120d": round(occ["occ_120d"] * 100, 1) if occ["occ_120d"] is not None else None,
            **action,
        })
    groups.sort(key=lambda g: (g["occ_60d"] if g["occ_60d"] is not None else 999))

    return jsonify({
        "ok": True,
        "as_of": today.isoformat(),
        "events": events,
        "groups": groups,
        "thresholds": DEMAND_ACTION_THRESHOLDS,
    })


@app.route("/api/market-weather")
def market_weather():
    """Live NWS alerts + near-term forecast for each portfolio market, with
    snow-storm/extreme-heat flags. Requires the server to have normal internet
    access — degrades to cached/last-known data (or a clear error) if the
    National Weather Service API is unreachable."""
    force = request.args.get("refresh") == "1"
    try:
        data = weather_monitor.get_market_weather(force_refresh=force)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Weather check failed: {e}"}), 500
    return jsonify({"ok": True, **data})


@app.route("/api/kcity-surge")
def kcity_surge():
    try:
        result = generate_dso_tasks(_PORTFOLIO, TODAY)
        existing = _load_actions()
        existing_map = {a["id"]: a for a in existing}

        # Merge status from saved actions; upsert new ones so apply works
        stale_price_ids = []
        for action in result["actions"]:
            matched = existing_map.get(action["id"])
            if matched:
                # Compare BASE floors only (threshold + event premium). The decayed
                # effective price drifts every week/day by design — that must NOT
                # re-open applied tasks, or Apply All loops forever.
                old_base = (matched.get("pricelabs_payload") or {}).get("base_min_price") or matched.get("base_min_price")
                new_base = (action.get("pricelabs_payload") or {}).get("base_min_price") or action.get("base_min_price")
                if matched.get("status") == "applied" and old_base and new_base and old_base != new_base:
                    # Deliberate floor change (e.g. new event premium) → re-open once
                    action["status"] = "pending"
                    action["reviewed_at"] = None
                    action["apply_result"] = None
                    action["suggestion"] = f"FLOOR UPDATED ${old_base:,} → ${new_base:,} — re-apply. " + action.get("suggestion", "")
                    stale_price_ids.append(action["id"])
                else:
                    action["status"] = matched.get("status", "pending")
                    action["reviewed_at"] = matched.get("reviewed_at")
                    action["apply_result"] = matched.get("apply_result")
        if stale_price_ids:
            stale_set = set(stale_price_ids)
            fresh_by_id = {a["id"]: a for a in result["actions"]}
            existing = [fresh_by_id.get(a["id"], a) if a["id"] in stale_set else a for a in existing]
            _save_actions(existing)
            existing_map = {a["id"]: a for a in existing}

        # Never re-add actions that were already rejected/declined
        SKIP_STATUSES = {"rejected", "declined", "applied"}
        new_actions = [
            a for a in result["actions"]
            if a["id"] not in existing_map and a.get("status") not in SKIP_STATUSES
        ]
        if new_actions:
            _save_actions(existing + new_actions)

        # Filter out rejected/declined from the response so they don't clutter the UI
        result["actions"] = [a for a in result["actions"] if a.get("status") not in {"rejected", "declined"}]
        result["summary"]["total_tasks"] = len(result["actions"])
        result["summary"]["high_tasks"] = sum(1 for a in result["actions"] if a.get("demand_level") == "high")
        result["summary"]["medium_tasks"] = sum(1 for a in result["actions"] if a.get("demand_level") == "medium")

        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "actions": [], "summary": {}}), 500


@app.route("/api/kcity-surge/remove-dates", methods=["POST"])
def kcity_surge_remove_dates():
    """Delete PriceLabs date overrides on specific dates for all KCity listings.
    Body: {"dates": ["2026-09-03", ...]}  Used to clean up trimmed Thu/Sun DSOs."""
    body = request.get_json(silent=True) or {}
    dates = [str(d).strip() for d in (body.get("dates") or []) if str(d).strip()]
    if not dates:
        return jsonify({"ok": False, "error": "Provide dates: [\"YYYY-MM-DD\", ...]"}), 400
    from kcity_surge_dso import _is_kcity
    kcity_props = [p for p in _PORTFOLIO if p.active and _is_kcity(p) and str(p.listing_id or "").strip()]
    pl = client_from_env()
    results, removed, failed = [], 0, 0
    for prop in kcity_props:
        lid = str(prop.listing_id).strip()
        pms = str(getattr(prop, "pms_name", "") or "hostaway").strip() or "hostaway"
        payload = {"pms": pms, "overrides": [{"date": d} for d in dates]}
        try:
            resp = pl.request("DELETE", f"/listings/{lid}/overrides", payload)
            removed += 1
            results.append({"listing": prop.name, "ok": True, "response": resp})
        except PriceLabsAPIError as e:
            failed += 1
            results.append({"listing": prop.name, "ok": False, "error": str(e)})
    return jsonify({"ok": failed == 0, "dates": dates, "listings_cleaned": removed,
                    "listings_failed": failed, "results": results})


@app.route("/api/kcity-surge/verify/<path:action_id>")
def kcity_surge_verify(action_id: str):
    """Read overrides back from PriceLabs and confirm the DSO is really set."""
    actions = _load_actions()
    action = next((a for a in actions if a.get("id") == action_id), None)
    if not action:
        return jsonify({"ok": False, "error": "Action not found in queue."}), 404
    payload = action.get("pricelabs_payload") or {}
    listing_id = str(action.get("listing_id") or "").strip()
    pms_name = str(action.get("pms_name") or "").strip() or "hostaway"
    start_s, end_s = payload.get("start_date"), payload.get("end_date")
    expected = payload.get("min_price")
    if not listing_id or not start_s or not end_s or not expected:
        return jsonify({"ok": False, "error": "Action is missing listing/date/price info."}), 400
    try:
        resp = client_from_env().request("GET", f"/listings/{listing_id}/overrides?pms={pms_name}")
    except PriceLabsAPIError as e:
        return jsonify({"ok": False, "error": f"PriceLabs read failed: {e}"}), 502

    overrides = resp.get("overrides") if isinstance(resp, dict) else resp
    if not isinstance(overrides, list):
        overrides = []
    by_date = {str(o.get("date")): o for o in overrides if isinstance(o, dict)}

    start_d, end_d = date.fromisoformat(start_s), date.fromisoformat(end_s)
    check_start = max(start_d, EARLIEST_RATE_EDIT_DATE)
    results, all_set = [], True
    day = check_start
    while day <= end_d:
        iso = day.isoformat()
        o = by_date.get(iso)
        found_min = None
        if o:
            try:
                found_min = float(o.get("min_price")) if o.get("min_price") not in (None, "") else None
            except (TypeError, ValueError):
                found_min = None
        ok = found_min is not None and abs(found_min - float(expected)) < 1
        if not ok:
            all_set = False
        results.append({"date": iso, "expected_min": expected, "found_min": found_min, "ok": ok})
        day += timedelta(days=1)

    return jsonify({
        "ok": True,
        "listing_id": listing_id,
        "property": action.get("property"),
        "verified": all_set,
        "dates": results,
        "total_overrides_on_listing": len(overrides),
    })


@app.route("/api/actions/<action_id>", methods=["POST"])
def update_action(action_id: str):
    payload = request.get_json(silent=True) or {}
    new_status = payload.get("status")
    if new_status not in {"approved", "rejected", "pending", "applied"}:
        return jsonify({"ok": False, "error": "status must be approved, rejected, pending, or applied"}), 400
    apply_now = bool(payload.get("apply"))

    # Lock the fast load/mutate/save step so two concurrent requests can't
    # read-modify-write the same JSON queue and silently drop each other's
    # change. The lock is deliberately NOT held across the slow PriceLabs
    # network call below — instead we re-load fresh state before writing the
    # apply result, so a concurrent edit made during the network round-trip
    # isn't clobbered.
    with _actions_lock:
        actions = _load_actions()
        action = next((a for a in actions if a.get("id") == action_id), None)
        if action is None:
            return jsonify({"ok": False, "error": "Action not found"}), 404
        action["status"] = new_status
        action["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        if new_status == "applied" and not apply_now:
            action["applied_at"] = datetime.now(timezone.utc).isoformat()
        _save_actions(actions)

    if not (new_status == "approved" and apply_now):
        return jsonify({"ok": True, "action": action})

    try:
        result = _finalize_pricelabs_push(_apply_weekly_action(action))
    except PricingApplyError as e:
        with _actions_lock:
            actions = _load_actions()
            fresh = next((a for a in actions if a.get("id") == action_id), action)
            fresh["status"] = "approved"
            fresh["apply_error"] = str(e)
            _save_actions(actions)
        app.logger.error("PricingApplyError for action %s: %s", action_id, e)
        return jsonify({"ok": False, "error": str(e), "action": fresh}), 400

    with _actions_lock:
        actions = _load_actions()
        fresh = next((a for a in actions if a.get("id") == action_id), action)
        fresh["status"] = "applied"
        fresh["applied_at"] = datetime.now(timezone.utc).isoformat()
        fresh["apply_result"] = result
        adjusted_start = result.get("adjusted_start_date") if isinstance(result, dict) else None
        if adjusted_start:
            wp = fresh.get("pricelabs_payload") or {}
            old_start = wp.get("start_date")
            wp["start_date"] = adjusted_start
            fresh["pricelabs_payload"] = wp
            fresh["target_dates"] = fresh.get("target_dates", "").replace(str(old_start), adjusted_start)
            post_apply = result.get("post_apply") if isinstance(result, dict) else {}
            verified_sync = (post_apply or {}).get("verified_sync") or {}
            refresh = (post_apply or {}).get("refresh") or {}
            sync_message = verified_sync.get("message") or "Dashboard re-sync status unknown."
            refresh_message = refresh.get("message") or ""
            fresh["apply_note"] = (
                f"Start date adjusted from {old_start} to {adjusted_start} "
                f"to preserve same-day booking protection. {sync_message} {refresh_message}"
            ).strip()
        else:
            confirmed_base = result.get("confirmed_base") if isinstance(result, dict) else None
            confirmed = result.get("confirmed_dates") if isinstance(result, dict) else None
            post_apply = result.get("post_apply") if isinstance(result, dict) else {}
            verified_sync = (post_apply or {}).get("verified_sync") or {}
            refresh = (post_apply or {}).get("refresh") or {}
            sync_message = verified_sync.get("message") or "Dashboard re-sync status unknown."
            refresh_message = refresh.get("message") or ""
            if confirmed_base:
                fresh["apply_note"] = f"PriceLabs confirmed base price update to ${confirmed_base}. {sync_message} {refresh_message}".strip()
            elif confirmed:
                fresh["apply_note"] = f"PriceLabs confirmed {len(confirmed)} date override(s): {', '.join(confirmed[:3])}{'...' if len(confirmed) > 3 else ''}. {sync_message} {refresh_message}".strip()
            else:
                fresh["apply_note"] = f"PriceLabs accepted the override request. {sync_message} {refresh_message}".strip()
        _save_actions(actions)

    return jsonify({"ok": True, "action": fresh})


@app.route("/api/revenue-calendar")
def revenue_calendar():
    property_name = request.args.get("property", "")
    prop = _resolve_property(property_name)
    if not prop:
        return jsonify({"ok": False, "error": "Property not found", "days": []}), 404
    return jsonify(_revenue_calendar(prop))


@app.route("/api/revenue-insights")
def revenue_insights():
    property_name = request.args.get("property", "")
    prop = _resolve_property(property_name)
    if not prop:
        return jsonify({"ok": False, "error": "Property not found"}), 404
    return jsonify(_revenue_insights(prop))


@app.route("/api/booking-promotions")
def get_booking_promotions():
    promotions = _generate_booking_promotions() if request.args.get("generate") == "1" else _load_booking_promotions()
    if not promotions:
        promotions = _generate_booking_promotions()
    property_name = request.args.get("property", "")
    if property_name:
        promotions = [p for p in promotions if p.get("property") == property_name]
    status = request.args.get("status", "")
    if status and status != "all":
        promotions = [p for p in promotions if p.get("status", "draft") == status]
    else:
        promotions = [p for p in promotions if p.get("status") not in {"rejected", "declined"}]
    return jsonify({"ok": True, "promotions": promotions})


@app.route("/api/booking-promotions/<promotion_id>", methods=["POST"])
def update_booking_promotion(promotion_id: str):
    payload = request.get_json(silent=True) or {}
    promotions = _load_booking_promotions() or _generate_booking_promotions()
    editable = {
        "promotion_type",
        "discount_pct",
        "booking_hotel_id",
        "booking_room_ids",
        "booking_parent_rate_ids",
        "book_start_date",
        "book_end_date",
        "stay_start_date",
        "stay_end_date",
        "audience",
        "status",
    }
    for promo in promotions:
        if promo.get("id") != promotion_id:
            continue
        for key in editable:
            if key in payload:
                promo[key] = payload[key]
        if "discount_pct" in payload:
            try:
                promo["discount_pct"] = max(0, min(50, int(float(payload["discount_pct"]))))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "discount_pct must be numeric"}), 400
        promo["booking_room_ids"] = _split_ids(promo.get("booking_room_ids"))
        promo["booking_parent_rate_ids"] = _split_ids(promo.get("booking_parent_rate_ids"))
        if promo.get("status") not in {"draft", "needs_review", "approved", "rejected", "applied"}:
            return jsonify({"ok": False, "error": "Invalid promotion status"}), 400
        promo["updated_at"] = datetime.now(timezone.utc).isoformat()
        prop = _PORTFOLIO_INDEX.get(promo.get("property", ""))
        if prop:
            discount = int(promo.get("discount_pct") or 0)
            promo["expected_adr"] = _round_to_5(prop.base_price * (1 - discount / 100)) if discount else prop.base_price
            promo["risk_flags"] = _booking_promo_risks(prop, discount, promo.get("promotion_type") or "none")
            try:
                promo["api_payload_preview"] = _booking_payload_preview(
                    prop,
                    promo.get("promotion_type") or "none",
                    discount,
                    date.fromisoformat(promo["book_start_date"]),
                    date.fromisoformat(promo["book_end_date"]),
                    date.fromisoformat(promo["stay_start_date"]),
                    date.fromisoformat(promo["stay_end_date"]),
                )
            except (KeyError, ValueError):
                return jsonify({"ok": False, "error": "Promotion dates must use YYYY-MM-DD"}), 400
        _save_booking_promotions(promotions)
        return jsonify({"ok": True, "promotion": promo})
    return jsonify({"ok": False, "error": "Promotion not found"}), 404


@app.route("/api/booking-promotions/<promotion_id>/review", methods=["POST"])
def review_booking_promotion(promotion_id: str):
    promotions = _load_booking_promotions() or _generate_booking_promotions()
    for promo in promotions:
        if promo.get("id") == promotion_id:
            promo["ai_review"] = _booking_promotion_review(promo)
            promo["status"] = "needs_review" if promo.get("status") == "draft" else promo.get("status", "needs_review")
            promo["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            _save_booking_promotions(promotions)
            return jsonify({"ok": True, "promotion": promo})
    return jsonify({"ok": False, "error": "Promotion not found"}), 404


@app.route("/api/booking-promotions/<promotion_id>/push", methods=["POST"])
def push_booking_promotion(promotion_id: str):
    promotions = _load_booking_promotions() or _generate_booking_promotions()
    for promo in promotions:
        if promo.get("id") != promotion_id:
            continue
        if promo.get("status") != "approved":
            return jsonify({"ok": False, "error": "Promotion must be approved before pushing to Booking.com."}), 400
        try:
            xml_body = build_promotion_xml(promo)
            result = booking_client_from_env().create_promotion(xml_body)
        except BookingAPIError as e:
            promo["push_error"] = str(e)
            promo["pushed_at"] = None
            promo["api_xml_preview"] = None
            try:
                promo["api_xml_preview"] = build_promotion_xml(promo)
            except BookingAPIError:
                pass
            _save_booking_promotions(promotions)
            return jsonify({"ok": False, "error": str(e), "promotion": promo}), 400
        promo["status"] = "applied"
        promo["pushed_at"] = datetime.now(timezone.utc).isoformat()
        promo["booking_push_result"] = result
        promo["booking_promotion_ids"] = result.get("promotion_ids", [])
        promo["push_error"] = ""
        promo["api_xml_sent"] = xml_body
        _save_booking_promotions(promotions)
        return jsonify({"ok": True, "promotion": promo})
    return jsonify({"ok": False, "error": "Promotion not found"}), 404


@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    """Bulk-refresh dashboard data from PriceLabs Customer API."""
    if not os.environ.get("PRICELABS_API_KEY"):
        return jsonify({"ok": False, "error": "PRICELABS_API_KEY is not set. Add it to your .env file and restart the app.", "summary": _SUMMARY}), 400
    try:
        result = _sync_pricelabs_api()
        return jsonify({
            "ok": True,
            "output": (
                f"Synced {result['listings_total']} PriceLabs listings from the Customer API; "
                f"{result['listings_active']} are active/syncing and loaded into the dashboard."
            ),
            "sync": result,
            "summary": _SUMMARY,
        })
    except PriceLabsAPIError as e:
        return jsonify({"ok": False, "error": str(e), "summary": _SUMMARY}), 400
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, flush=True)
        return jsonify({"ok": False, "error": f"Sync failed: {e}", "traceback": tb, "summary": _SUMMARY}), 500


@app.route("/api/revenue")
def listing_revenue():
    """Return reservations, monthly totals, and YTD stats for a listing from PriceLabs."""
    property_name = request.args.get("property", "")
    prop = _resolve_property(property_name)
    if not prop:
        return jsonify({"ok": False, "error": "Property not found"}), 404
    lid = str(prop.listing_id or "").strip()
    if not lid:
        return jsonify({"ok": False, "error": "No listing ID for this property"}), 400
    try:
        reservations = pricelabs_reservations_for_listing(lid)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if not reservations:
        # An empty list previously rendered as a blank calendar and "-" KPIs,
        # which reads as "this listing has no bookings" rather than "no data
        # reached us". Say which it is.
        return jsonify({
            "ok": False,
            "error": (
                "No reservation data available for this listing. PriceLabs returned "
                "nothing and there is no local cache "
                f"({pricelabs_reservations_source()}). Reservations come from the "
                "PriceLabs Customer API - if your key cannot reach "
                f"{pricelabs_reservation_path()}, set PRICELABS_RESERVATION_PATH or "
                "supply reservations_cache.csv in the Haven folder."
            ),
        })

    today = date.today()
    current_year = today.year
    last_year = current_year - 1
    try:
        ly_today = today.replace(year=last_year).isoformat()
    except ValueError:
        # today is Feb 29 and last_year isn't a leap year — fall back to Feb 28
        ly_today = today.replace(year=last_year, day=28).isoformat()

    # Build reservation rows
    rows = []
    monthly: dict[str, dict] = {}
    monthly_ly: dict[str, dict] = {}  # last year
    ytd_revenue = 0.0
    ytd_nights = 0
    ytd_revenue_ly = 0.0
    ytd_nights_ly = 0

    skipped_malformed = 0
    for r in reservations:
        try:
            arrival = (r.get("arrivalDate") or r.get("checkIn") or "")[:10]
            departure = (r.get("departureDate") or r.get("checkOut") or "")[:10]
            booked = (r.get("createdAt") or r.get("bookingDate") or "")[:10]
            if not arrival:
                continue

            # Financial data - several field names are accepted
            money = r.get("money") or {}
            rental_rev = float(money.get("rentalRevenue") or r.get("rentalRevenue") or r.get("totalPrice") or r.get("totalAmount") or 0)
            total_rev = float(money.get("totalPrice") or money.get("totalAmount") or r.get("totalPrice") or r.get("totalAmount") or rental_rev)
            cleaning = float(money.get("cleaningFee") or r.get("cleaningFee") or 0)
            channel_fee = float(money.get("channelFee") or money.get("channelCommission") or r.get("channelFee") or 0)

            los = 0
            if arrival and departure:
                try:
                    los = (date.fromisoformat(departure) - date.fromisoformat(arrival)).days
                except (ValueError, TypeError):
                    pass
            adr = round(rental_rev / los, 2) if los > 0 and rental_rev > 0 else 0

            source = str(r.get("channelName") or r.get("source") or r.get("channel") or "direct").strip()
            guest_count = r.get("guestCount") or r.get("numberOfGuests") or 1
        except (TypeError, ValueError, AttributeError):
            # One malformed reservation record (unexpected field type/shape)
            # must not 500 the entire revenue response for this listing.
            skipped_malformed += 1
            continue

        rows.append({
            "booked_date": booked,
            "check_in": arrival,
            "check_out": departure,
            "los": los,
            "rental_revenue": rental_rev,
            "total_revenue": total_rev,
            "cleaning_fee": cleaning,
            "channel_fee": channel_fee,
            "adr": adr,
            "source": source,
            "guest_count": guest_count,
            "reservation_id": r.get("id") or r.get("reservationId") or "",
        })

        # Monthly aggregation (by check-in month of current year)
        if arrival.startswith(str(current_year)):
            month_key = arrival[:7]  # YYYY-MM
            if month_key not in monthly:
                monthly[month_key] = {"revenue": 0.0, "nights": 0, "reservations": 0}
            monthly[month_key]["revenue"] += rental_rev
            monthly[month_key]["nights"] += los
            monthly[month_key]["reservations"] += 1

        # Monthly aggregation last year
        if arrival.startswith(str(last_year)):
            month_key_ly = arrival[:7]
            if month_key_ly not in monthly_ly:
                monthly_ly[month_key_ly] = {"revenue": 0.0, "nights": 0, "reservations": 0}
            monthly_ly[month_key_ly]["revenue"] += rental_rev
            monthly_ly[month_key_ly]["nights"] += los
            monthly_ly[month_key_ly]["reservations"] += 1

        # YTD (current year arrivals up to today)
        if arrival.startswith(str(current_year)) and arrival <= today.isoformat():
            ytd_revenue += rental_rev
            ytd_nights += los

        # YTD same period last year
        if arrival.startswith(str(last_year)) and arrival <= ly_today:
            ytd_revenue_ly += rental_rev
            ytd_nights_ly += los
    if skipped_malformed:
        app.logger.warning("Skipped %d malformed reservation record(s)", skipped_malformed)

    # Build monthly table for current year + last year comparison
    monthly_rows = []
    for month_num in range(1, 13):
        key = f"{current_year}-{month_num:02d}"
        key_ly = f"{last_year}-{month_num:02d}"
        m = monthly.get(key, {})
        m_ly = monthly_ly.get(key_ly, {})
        nights = m.get("nights", 0)
        rev = m.get("revenue", 0.0)
        nights_ly = m_ly.get("nights", 0)
        rev_ly = m_ly.get("revenue", 0.0)
        # Use each year's own day count — current/last year can differ in leap
        # status (e.g. 2028 vs 2027), so reusing one year's Feb day-count for
        # both would silently skew the other year's occupancy %.
        days_in_month_cy = calendar.monthrange(current_year, month_num)[1]
        days_in_month_ly = calendar.monthrange(last_year, month_num)[1]
        occ = round(nights / days_in_month_cy * 100) if nights else 0
        adr = round(rev / nights, 2) if nights else 0
        occ_ly = round(nights_ly / days_in_month_ly * 100) if nights_ly else 0
        adr_ly = round(rev_ly / nights_ly, 2) if nights_ly else 0
        rev_delta = round(rev - rev_ly, 2)
        rev_delta_pct = round((rev - rev_ly) / rev_ly * 100, 1) if rev_ly else None
        monthly_rows.append({
            "month": date(current_year, month_num, 1).strftime("%b %Y"),
            "month_ly": date(last_year, month_num, 1).strftime("%b %Y"),
            "revenue": round(rev, 2),
            "nights": nights,
            "reservations": m.get("reservations", 0),
            "occupancy": occ,
            "adr": adr,
            "revenue_ly": round(rev_ly, 2),
            "nights_ly": nights_ly,
            "reservations_ly": m_ly.get("reservations", 0),
            "occupancy_ly": occ_ly,
            "adr_ly": adr_ly,
            "revenue_delta": rev_delta,
            "revenue_delta_pct": rev_delta_pct,
            "is_current": month_num == today.month,
        })

    # Calendar: next 3 months
    calendar_months = []
    for offset in range(3):
        m = (today.month - 1 + offset) % 12 + 1
        y = current_year + (today.month - 1 + offset) // 12
        import calendar as cal_mod
        cal_days = cal_mod.monthcalendar(y, m)
        booked_dates = {r["check_in"] for r in rows if r["check_in"] and r["check_in"] >= today.isoformat()}
        calendar_months.append({
            "year": y, "month": m,
            "name": date(y, m, 1).strftime("%B %Y"),
            "weeks": cal_days,
            "booked": list(booked_dates),
        })

    ytd_delta = round(ytd_revenue - ytd_revenue_ly, 2)
    ytd_delta_pct = round((ytd_revenue - ytd_revenue_ly) / ytd_revenue_ly * 100, 1) if ytd_revenue_ly else None
    return jsonify({
        "ok": True,
        "property": prop.name,
        "listing_id": lid,
        "reservations": rows[:50],
        "monthly": monthly_rows,
        "ytd_revenue": round(ytd_revenue, 2),
        "ytd_nights": ytd_nights,
        "ytd_revenue_ly": round(ytd_revenue_ly, 2),
        "ytd_nights_ly": ytd_nights_ly,
        "ytd_delta": ytd_delta,
        "ytd_delta_pct": ytd_delta_pct,
        "calendar_months": calendar_months,
        "year": current_year,
        "last_year": last_year,
    })


_REPORT_BUILDER_CACHE: dict[int, dict] = {}
_REPORT_BUILDER_TTL_SECONDS = 900

_BOOKING_STATS_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}
_BOOKING_STATS_TTL_SECONDS = 900


def _cached_booking_stats() -> dict:
    """Recent booking activity per listing from PriceLabs, cached for 15 minutes.

    /api/portfolio runs on every page load, so this must not make a live API
    call each time, and must never break the portfolio if PriceLabs is
    unreachable - callers get {} and the affected fields render empty.
    """
    now = time.time()
    if _BOOKING_STATS_CACHE["data"] and now - _BOOKING_STATS_CACHE["at"] < _BOOKING_STATS_TTL_SECONDS:
        return _BOOKING_STATS_CACHE["data"]
    try:
        stats = pricelabs_booking_stats()
    except Exception as e:
        app.logger.warning("PriceLabs booking stats unavailable: %s", e)
        return _BOOKING_STATS_CACHE["data"] or {}
    _BOOKING_STATS_CACHE["at"] = now
    _BOOKING_STATS_CACHE["data"] = stats
    return stats


@app.route("/api/report-builder")
def report_builder_snapshot():
    """Live portfolio snapshot from a PriceLabs Report Builder template.

    Template 1158 is the only one covering all listings. Falls back to the CSV
    cache when the API is unreachable and reports which source was used, so a
    cached number is never presented as live.
    """
    try:
        template_id = int(request.args.get("template") or pl_report_builder.DEFAULT_TEMPLATE_ID)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "template must be a number"}), 400

    now = time.time()
    cached = _REPORT_BUILDER_CACHE.get(template_id)
    if cached and now - cached["at"] < _REPORT_BUILDER_TTL_SECONDS:
        return jsonify(cached["data"])

    rows, source = pl_report_builder.load(template_id)
    if not rows:
        return jsonify({"ok": False, "error": source, "template_id": template_id})

    today = date.today()

    def num(row, key):
        value = row.get(key)
        return value if isinstance(value, (int, float)) else None

    def days_since_booked(row):
        raw = row.get("Last Booked date")
        if not raw:
            return None
        try:
            return (today - date.fromisoformat(str(raw)[:10])).days
        except ValueError:
            return None

    revenue = sum(num(r, "Rental Revenue") or 0 for r in rows)
    occs = [o for o in (num(r, "Occupancy") for r in rows) if o is not None]
    pairs = [(a, b) for a, b in
             ((num(r, "RevPar"), num(r, "Average Market RevPar")) for r in rows)
             if a is not None and b]

    aged = [(days_since_booked(r), r) for r in rows]
    dated = [(d, r) for d, r in aged if d is not None]
    buckets = [(0, 3, "0-3 days"), (4, 7, "4-7 days"), (8, 14, "8-14 days"),
               (15, 30, "15-30 days"), (31, 90, "31-90 days"), (91, 10 ** 6, "over 90 days")]

    stale = sorted((x for x in dated if x[0] > 14), key=lambda x: -x[0])

    groups: dict[str, dict] = {}
    for r in rows:
        name = str(r.get("Group Name") or "(ungrouped)")
        g = groups.setdefault(name, {"listings": 0, "revenue": 0.0, "occ": []})
        g["listings"] += 1
        g["revenue"] += num(r, "Rental Revenue") or 0
        occ = num(r, "Occupancy")
        if occ is not None:
            g["occ"].append(occ)

    payload = {
        "ok": True,
        "template_id": template_id,
        "source": source,
        "as_of": today.isoformat(),
        "window": "trailing 30 days",
        "listings": len(rows),
        "revenue": round(revenue, 2),
        "occupancy_median": round(statistics.median(occs), 1) if occs else None,
        "zero_occupancy": sum(1 for o in occs if o == 0),
        "revpar_index": round(statistics.median(a / b for a, b in pairs) * 100) if pairs else None,
        "beating_market": sum(1 for a, b in pairs if a >= b),
        "benchmarked": len(pairs),
        "zero_pickup_7d": sum(1 for r in rows if not (num(r, "Num Booked Pickup 7") or 0)),
        "zero_pickup_14d": sum(1 for r in rows if not (num(r, "Num Booked Pickup 14") or 0)),
        "last_booked_buckets": [
            {"label": label, "count": sum(1 for d, _ in dated if lo <= d <= hi)}
            for lo, hi, label in buckets
        ],
        "last_booked_missing": len(aged) - len(dated),
        "stale_count": len(stale),
        "stale": [
            {
                "days": d,
                "name": r.get("Listing Name") or "",
                "listing_id": str(r.get("Listing ID") or ""),
                "occupancy": num(r, "Occupancy"),
                "base_price": num(r, "Base Price"),
                "group": r.get("Group Name") or "",
            }
            for d, r in stale[:40]
        ],
        "groups": sorted(
            (
                {
                    "name": name,
                    "listings": g["listings"],
                    "revenue": round(g["revenue"], 2),
                    "occupancy_median": round(statistics.median(g["occ"]), 1) if g["occ"] else None,
                }
                for name, g in groups.items()
            ),
            key=lambda x: -x["listings"],
        ),
    }

    _REPORT_BUILDER_CACHE[template_id] = {"at": now, "data": payload}
    return jsonify(payload)


@app.route("/api/portfolio")
def get_portfolio():
    urgency_filter = request.args.get("urgency", "all")
    props = [p for p in _PORTFOLIO if p.active]

    if urgency_filter == "critical":
        props = [p for p in props if p.urgency == "critical"]
    elif urgency_filter == "warning":
        props = [p for p in props if p.urgency == "warning"]
    elif urgency_filter == "overperforming":
        props = [p for p in props if p.urgency == "overperforming"]
    elif urgency_filter == "onboarding":
        props = [p for p in props if p.urgency == "onboarding"]
    elif urgency_filter == "ok":
        props = [p for p in props if p.urgency == "ok"]

    booking_stats = _cached_booking_stats()

    def _prop_dict(p: Property) -> dict:
        ll = lookup_links(p.name)
        benchmark = _benchmark_for(p)
        ha_stats = booking_stats.get(str(p.listing_id or "").strip(), {})
        return {
            "name": p.name,
            "display_name": p.property_name,
            "area": p.area,
            "side": p.side,
            "city": getattr(p, "city", ""),
            "group": getattr(p, "customization_group", ""),
            "subgroup": getattr(p, "customization_sub_group", ""),
            "group_label": _group_label(p),
            "bedrooms": p.bedrooms,
            "base_price": p.base_price,
            "min_price": p.min_price,
            "booked_7d": p.booked_7d,
            "booked_14d": p.booked_14d,
            "adj_occ_60d": round(p.adj_occ_60d * 100, 1),
            "last_booked_days": p.last_booked_days,
            "urgency": p.urgency,
            "urgency_score": p.urgency_score,
            "issues": p.issues,
            "owner_restrictions": p.owner_restrictions,
            "benchmark": benchmark,
            # OTA links
            "airbnb_url":      ll.airbnb_url if ll else None,
            "vrbo_url":        ll.vrbo_url if ll else None,
            "streamline_id":   ll.streamline_id if ll else None,
            "airbnb_headline": ll.airbnb_headline if ll else None,
            "airbnb_id_issue": ll.airbnb_id_issue if ll else "",
            "vrbo_headline":   ll.vrbo_headline if ll else None,
            "has_airbnb":      bool(ll and ll.has_airbnb),
            "has_vrbo":        bool(ll and ll.has_vrbo),
            "booking_url":     ll.booking_url if ll else None,
            "has_booking":     bool(ll and ll.has_booking),
            "airbnb_rating":   ll.airbnb_rating if ll else None,
            "airbnb_reviews":  ll.airbnb_reviews if ll else None,
            "airbnb_rating_status": ll.airbnb_rating_status if ll else "unknown",
            "vrbo_rating":     ll.vrbo_rating if ll else None,
            "vrbo_reviews":    ll.vrbo_reviews if ll else None,
            "vrbo_rating_status": ll.vrbo_rating_status if ll else "unknown",
            "vrbo_review_label": ll.vrbo_review_label if ll else "",
            "vrbo_cleanliness": ll.vrbo_cleanliness if ll else None,
            "vrbo_checkin": ll.vrbo_checkin if ll else None,
            "vrbo_communication": ll.vrbo_communication if ll else None,
            "vrbo_location": ll.vrbo_location if ll else None,
            "airbnb_photos":   ll.airbnb_photos if ll else None,
            "airbnb_thumb_url": ll.airbnb_thumb_url if ll else "",
            "airbnb_photo_grade": ll.airbnb_photo_grade if ll else "unknown",
            "vrbo_photos":     ll.vrbo_photos if ll else None,
            "vrbo_thumb_url":  ll.vrbo_thumb_url if ll else "",
            "vrbo_photo_grade": ll.vrbo_photo_grade if ll else "unknown",
            "listing_id": getattr(p, "listing_id", "") or "",
            # PriceLabs booking source enrichment
            "booking_sources_7d": ha_stats.get("sources_7d") or {},
            "booking_sources_14d": ha_stats.get("sources_14d") or {},
            "booking_pickup_7d_reservations": ha_stats.get("pickup_7d_reservations") or None,
            "booking_pickup_14d_reservations": ha_stats.get("pickup_14d_reservations") or None,
        }

    return jsonify({
        "summary": _SUMMARY,
        "properties": [_prop_dict(p) for p in props],
    })


@app.route("/api/report/kcity-surge")
def stream_kcity_surge_report():
    question = request.args.get("q", "").strip()
    result = generate_dso_tasks(_PORTFOLIO, TODAY)
    actions = result.get("actions", [])
    summary = result.get("summary", {})

    applied = [a for a in actions if a.get("status") == "applied"]
    pending = [a for a in actions if a.get("status") == "pending"]
    high = [a for a in actions if a["demand_level"] == "high"]
    medium = [a for a in actions if a["demand_level"] == "medium"]

    # Build context
    br_breakdown = "\n".join(
        f"  - {br}BR: {count} tasks" for br, count in summary.get("bedrooms_breakdown", {}).items()
    )
    sample_windows = {}
    for a in actions[:100]:
        k = a["target_dates"]
        if k not in sample_windows:
            sample_windows[k] = {"event": a["event_label"], "demand": a["demand_level"], "count": 0}
        sample_windows[k]["count"] += 1

    windows_text = "\n".join(
        f"  - {w['event']} ({dates}) [{w['demand'].upper()}] — {w['count']} listings"
        for dates, w in list(sample_windows.items())[:10]
    )

    context = f"""
KCity (Knoxville City) Market — Surge DSO Analysis
Today: {TODAY}

PORTFOLIO SUMMARY:
- 33 active KCity listings (1BR×9, 2BR×14, 3BR×8, 4BR×2)
- Primary demand driver: UT Vols football home games (Sep–Nov) + holiday weekends

DSO TASK SUMMARY:
- Total tasks: {summary.get('total_tasks', 0)}
- HIGH demand tasks: {summary.get('high_tasks', 0)}
- MEDIUM demand tasks: {summary.get('medium_tasks', 0)}
- Surge windows: {summary.get('surge_windows', 0)}
- Applied: {len(applied)}
- Pending: {len(pending)}

BEDROOM BREAKDOWN:
{br_breakdown}

SURGE WINDOWS (upcoming):
{windows_text}

PRICING THRESHOLDS:
- 1BR: HIGH ≥$755, MEDIUM ≥$590
- 2BR: HIGH ≥$890, MEDIUM ≥$740
- 3BR: HIGH ≥$960 (medium dates skipped — below $850 threshold)
- 4BR: HIGH ≥$1,225 (medium dates skipped)

KEY EVENTS:
- UT Football Opening Weekend: Sep 3–5, 2026
- UT Football (BIGGEST game): Oct 15–17, 2026
- UT Football Rivalry: Nov 19–21, 2026
- Christmas: Dec 24–27, 2026
- New Year's Eve: Dec 31 – Jan 1

EXISTING OVERRIDES: Some listings (e.g., Alex Cameron 1502 / 3BR) already have
comprehensive DSOs set from Dec 2025 covering football weekends at $1,359 with
2-night minimums and check-in restrictions.
"""

    user_message = question if question else (
        "Analyze the KCity surge pricing strategy. Which surge windows are highest priority? "
        "Are the threshold floors appropriate for each bedroom count? "
        "What revenue impact can we expect from applying all DSOs? "
        "Are there any risks or gaps in the current approach?"
    )

    def generate():
        try:
            ai = _ai_client()
            system = (
                "You are an expert STR revenue manager specializing in the Knoxville, Tennessee market. "
                "You understand UT Vols football demand patterns, KCity downtown loft performance, "
                "and PriceLabs date-specific override strategy. Be specific, data-driven, and actionable. "
                "Format in clean markdown with headers. Focus on highest-impact insights."
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"{context}\n\nQuestion: {user_message}"},
            ]
            response = ai.chat.completions.create(model=AI_MODEL_FALLBACKS[0], messages=messages, max_tokens=1400, temperature=0.3)
            text = response.choices[0].message.content or ""
            chunk = 80
            for i in range(0, len(text), chunk):
                yield f"data: {json.dumps({'type': 'ai_text', 'text': text[i:i+chunk]})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'ai_text', 'text': f'Error: {e}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/report")
def stream_report():
    report_type = request.args.get("type", "portfolio")
    property_name = request.args.get("property", "")
    resolved_prop = _resolve_property(property_name)

    # Resolve property and build prompt
    if resolved_prop:
        prop = resolved_prop
        prompt = _build_property_prompt(report_type, prop)
    else:
        prompt = _build_portfolio_prompt(report_type)

    def generate():
        try:
            client = _ai_client()

            SHORT_SYSTEM = (
                "You are an expert STR revenue manager and listing optimizer for HVR Smokies vacation rentals. "
                "Be specific, data-driven, and actionable. Use the exact data provided. "
                "Do not call tools or functions. Format responses in clean markdown. "
                "Do not recommend maximum rates, max prices, price caps, or price ceilings. "
                "Do not recommend same-day rate edits, and never recommend editing Last Minute adjustment settings. "
                "Never use Maui or Hawaii market assumptions; this portfolio is Tennessee Smokies and nearby cities. "
                "Skip sections where synced data is unknown instead of filling them with unknowns. "
                "Keep the answer concise: focus on the highest-impact issues and actions."
            )

            messages: list[dict] = [
                {"role": "system", "content": SHORT_SYSTEM},
                {"role": "user",   "content": prompt},
            ]

            def call_with_fallback(msgs):
                """Try each model in the cascade until one succeeds."""
                models = AI_MODEL_FALLBACKS
                for model in models:
                    try:
                        kwargs = dict(
                            model=model,
                            messages=msgs,
                            max_tokens=2200 if report_type == "listing" else 1200,
                            temperature=0.2 if report_type == "listing" else 0.4,
                        )
                        return client.chat.completions.create(**kwargs), model
                    except AIAPIStatusError as e:
                        if e.status_code in (429, 413):
                            continue   # rate limit or too large — try next model
                        # Groq retires models periodically; a 404 model_not_found
                        # should fall through to the next candidate rather than
                        # breaking AI reports outright.
                        if e.status_code == 404 and "model" in (e.message or "").lower():
                            continue
                        raise
                raise RuntimeError("No available AI model succeeded — all candidates were rate limited or unavailable")

            response, used_model = call_with_fallback(messages)
            text = response.choices[0].message.content or ""
            if resolved_prop and resolved_prop.urgency in {"critical", "warning"}:
                risky = re.search(r"\bincrease\b.*\b(base|min(?:imum)?|rate|price)\b", text, flags=re.I | re.S)
                if risky:
                    text += (
                        "\n\n## Guardrail Correction\n\n"
                        f"This listing is {resolved_prop.urgency.upper()}, so do not increase base price, "
                        "minimum price, or nightly rates from this report. Treat any earlier increase language "
                        "as rejected. Use forward price-position checks first: posted percentile, market booked "
                        "price, last-year booked price, market posted price, restrictions, and channel visibility."
                    )
            if resolved_prop and re.search(r"\b(decrease|increase|change|adjust|reduce|raise)\b.*\b(minimum price|min price|price floor|floor)\b", text, flags=re.I | re.S):
                text += (
                    "\n\n## Minimum Price Guardrail\n\n"
                    "Do not change minimum price or floor from this report. Floor changes require a separate "
                    "owner/portfolio review. For revenue recovery, use date-level forward pacing: compare posted "
                    "price percentile, market booked price, last-year booked price, and market posted price before "
                    "choosing hold, narrow rate adjustment, Booking.com promo, or channel visibility work."
                )
            chunk = 80
            if report_type == "listing" and resolved_prop:
                listing_html = _listing_optimizer_html(resolved_prop)
                yield f"data: {json.dumps({'type': 'html', 'html': listing_html})}\n\n"
                for i in range(0, len(text), chunk):
                    yield f"data: {json.dumps({'type': 'ai_text', 'text': text[i:i+chunk]})}\n\n"
            else:
                for i in range(0, len(text), chunk):
                    yield f"data: {json.dumps({'type': 'text', 'text': text[i:i+chunk]})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            if resolved_prop:
                fallback = _local_property_report(report_type, resolved_prop, e)
                chunk = 80
                if report_type == "listing":
                    listing_html = _listing_optimizer_html(resolved_prop)
                    yield f"data: {json.dumps({'type': 'html', 'html': listing_html})}\n\n"
                    for i in range(0, len(fallback), chunk):
                        yield f"data: {json.dumps({'type': 'ai_text', 'text': fallback[i:i+chunk]})}\n\n"
                else:
                    for i in range(0, len(fallback), chunk):
                        yield f"data: {json.dumps({'type': 'text', 'text': fallback[i:i+chunk]})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/revenue-scorecard")
def revenue_scorecard():
    try:
        year_param = request.args.get("year", "")
        group_filter = request.args.get("group", "").strip()
        bedrooms_filter = request.args.get("bedrooms", "").strip()

        current_year = int(year_param) if year_param.isdigit() else TODAY.year
        last_year = current_year - 1

        # Build a portfolio index by listing_id for fast lookup
        portfolio_by_lid: dict[str, Property] = {}
        for p in _PORTFOLIO:
            if p.listing_id:
                portfolio_by_lid[str(p.listing_id).strip()] = p

        # Aggregate revenue by listingMapId
        agg: dict[str, dict] = {}

        today_str = TODAY.strftime("%Y-%m-%d")
        # Cutoff: same calendar date last year
        cutoff_ly = f"{last_year}-{TODAY.month:02d}-{TODAY.day:02d}"

        # PriceLabs pages internally and returns the whole set in one call.
        for page in (pricelabs_all_reservations(),):
            for r in page:
                lid = str(r.get("listingMapId") or r.get("listingId") or "").strip()
                if not lid or lid not in portfolio_by_lid:
                    continue
                arrival = (r.get("arrivalDate") or r.get("checkIn") or "")[:10]
                if not arrival:
                    continue
                money = r.get("money") or {}
                rental_rev = float(
                    money.get("rentalRevenue")
                    or r.get("rentalRevenue")
                    or r.get("totalPrice")
                    or r.get("totalAmount")
                    or 0
                )
                departure = (r.get("departureDate") or r.get("checkOut") or "")[:10]
                los = 0
                if arrival and departure:
                    try:
                        los = (date.fromisoformat(departure) - date.fromisoformat(arrival)).days
                    except (ValueError, TypeError):
                        pass

                if lid not in agg:
                    agg[lid] = {"revenue_cy": 0.0, "revenue_ly": 0.0, "nights_cy": 0, "nights_ly": 0}

                # Current year: arrivals Jan 1 – today of current_year
                if arrival.startswith(str(current_year)) and arrival <= today_str:
                    agg[lid]["revenue_cy"] += rental_rev
                    agg[lid]["nights_cy"] += los
                # Last year: arrivals Jan 1 – same date last year
                elif arrival.startswith(str(last_year)) and arrival <= cutoff_ly:
                    agg[lid]["revenue_ly"] += rental_rev
                    agg[lid]["nights_ly"] += los

        # Build rows for portfolio listings
        rows = []
        for p in _PORTFOLIO:
            if not p.listing_id:
                continue
            lid = str(p.listing_id).strip()

            # Apply filters
            if group_filter and (p.customization_group or "") != group_filter:
                continue
            if bedrooms_filter and bedrooms_filter.isdigit() and p.bedrooms != int(bedrooms_filter):
                continue

            data = agg.get(lid, {"revenue_cy": 0.0, "revenue_ly": 0.0, "nights_cy": 0, "nights_ly": 0})
            rev_cy = round(data["revenue_cy"], 2)
            rev_ly = round(data["revenue_ly"], 2)
            delta = round(rev_cy - rev_ly, 2)
            delta_pct = round((delta / rev_ly * 100), 1) if rev_ly else None

            rows.append({
                "listing_id": lid,
                "name": p.name,
                "group": p.customization_group or "",
                "subgroup": p.customization_sub_group or "",
                "bedrooms": p.bedrooms,
                "revenue_cy": rev_cy,
                "revenue_ly": rev_ly,
                "delta": delta,
                "delta_pct": delta_pct,
                "nights_cy": data["nights_cy"],
                "nights_ly": data["nights_ly"],
            })

        rows.sort(key=lambda r: r["revenue_cy"], reverse=True)

        return jsonify({
            "ok": True,
            "rows": rows,
            "year": current_year,
            "last_year": last_year,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/scorecard")
def get_scorecard():
    """Read the weekly uploaded scorecard Excel and return per-listing KPIs."""
    if not SCORECARD_PATH.exists():
        return jsonify({"ok": False, "error": "No scorecard file uploaded yet. Upload scorecard_upload.xlsx to the Haven folder."}), 404
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(SCORECARD_PATH), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h or "").strip() for h in next(rows_iter)]
        group_filter = request.args.get("group", "").strip().lower()
        br_filter = request.args.get("bedrooms", "").strip()

        # Build prop lookup by name for group/bedroom enrichment
        prop_map = {p.name: p for p in _PORTFOLIO}

        rows = []
        for row in rows_iter:
            if not row or not row[0]:
                continue
            rec = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
            name = str(rec.get("Listing Name") or "").strip()
            if not name:
                continue
            prop = prop_map.get(name)
            group = (prop.customization_group if prop else "") or ""
            subgroup = (prop.customization_sub_group if prop else "") or ""
            bedrooms = int(prop.bedrooms or 0) if prop else 0

            if group_filter and group_filter not in group.lower():
                continue
            if br_filter and br_filter.isdigit() and bedrooms != int(br_filter):
                continue

            def _f(key):
                v = rec.get(key)
                if v is None or v == "":
                    return None
                try:
                    return round(float(v), 2)
                except (TypeError, ValueError):
                    # A blank/"N/A"/text cell anywhere in the weekly export must
                    # not 500 the entire scorecard response — just skip that field.
                    return None

            rows.append({
                "name": name,
                "group": group,
                "subgroup": subgroup,
                "bedrooms": bedrooms,
                "rental_revenue": _f("Rental Revenue"),
                "rental_revenue_ly": _f("Rental Revenue STLY"),
                "rental_revenue_full_ly": _f("Rental Revenue LY"),
                "rental_revenue_yoy_pct": _f("Rental Revenue STLY YoY %"),
                "revpar": _f("Rental RevPAR"),
                "revpar_ly": _f("Rental RevPAR STLY"),
                "revpar_yoy_pct": _f("Rental RevPAR STLY YoY %"),
                "market_revpar": _f("Market RevPAR"),
                "market_revpar_ly": _f("Market RevPAR STLY"),
                "market_revpar_yoy_pct": _f("Market RevPAR STLY YoY %"),
                "occ_pct": _f("Paid Occupancy %"),
                "occ_pct_ly": _f("Paid Occupancy % STLY"),
                "occ_yoy_diff": _f("Paid Occupancy STLY YoY Difference"),
                "market_occ_pct": _f("Market Occupancy %"),
                "market_occ_pct_ly": _f("Market Occupancy % STLY"),
                "market_occ_yoy_diff": _f("Market Occupancy STLY YoY Difference"),
                "adr": _f("Rental ADR"),
                "adr_ly": _f("Rental ADR STLY"),
                "adr_yoy_pct": _f("Rental ADR STLY YoY %"),
                "market_adr": _f("Market ADR"),
                "market_adr_ly": _f("Market ADR STLY"),
                "market_adr_yoy_pct": _f("Market ADR STLY YoY %"),
                "mpi": _f("Market Penetration Index %"),
                "booking_window": _f("Median Booking Window"),
                "market_booking_window": _f("Average Market Booking Window"),
                "pickup_30d": _f("Booked Nights Pickup (30 Days)"),
            })

        # Same-Store Sales filter — curated SSS roster if uploaded, else STLY > 0
        sss = request.args.get("sss", "").strip() in {"1", "true", "yes"}
        total_before_sss = len(rows)
        if sss:
            roster = _sss_roster()
            if roster:
                rows = [r for r in rows if r["name"] in roster]
            else:
                rows = [r for r in rows if (r.get("rental_revenue_ly") or 0) > 0]

        rows.sort(key=lambda r: (r.get("rental_revenue") or 0), reverse=True)
        groups = sorted(set(r["group"] for r in rows if r["group"]))
        wb.close()
        import os
        mtime = os.path.getmtime(str(SCORECARD_PATH))
        from datetime import datetime as dt2
        uploaded_at = dt2.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        return jsonify({"ok": True, "rows": rows, "groups": groups, "uploaded_at": uploaded_at, "total": len(rows),
                        "sss": sss, "sss_excluded": total_before_sss - len(rows), "total_listings_all": total_before_sss})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Field names for the executive report, per source. The uploaded workbook and
# each Report Builder template use different names for the same metric, so
# every source needs its own map.
#
#   3089 "Noeline Daily Tracking Scorecard" - rich metrics incl. market
#        absolutes and pickup, but filtered to one group (188 listings).
#    119 "Leaderboard" (PriceLabs canned) - all 296 syncing listings across
#        every group, with revenue/ADR/RevPAR/occupancy vs STLY and MPI.
#        Carries no market absolutes; those are derived from the penetration
#        indices below.
_EXEC_FIELDS_XLSX = {
    "rental_revenue": "Rental Revenue",
    "rental_revenue_ly": "Rental Revenue STLY",
    "rental_revenue_full_ly": "Rental Revenue LY",
    "rental_revenue_yoy_pct": "Rental Revenue STLY YoY %",
    "revpar": "Rental RevPAR",
    "revpar_ly": "Rental RevPAR STLY",
    "revpar_yoy_pct": "Rental RevPAR STLY YoY %",
    "market_revpar": "Market RevPAR",
    "market_revpar_ly": "Market RevPAR STLY",
    "market_revpar_yoy_pct": "Market RevPAR STLY YoY %",
    "occ_pct": "Paid Occupancy %",
    "occ_pct_ly": "Paid Occupancy % STLY",
    "occ_yoy_diff": "Paid Occupancy STLY YoY Difference",
    "market_occ_pct": "Market Occupancy %",
    "market_occ_pct_ly": "Market Occupancy % STLY",
    "market_occ_yoy_diff": "Market Occupancy STLY YoY Difference",
    "adr": "Rental ADR",
    "adr_ly": "Rental ADR STLY",
    "adr_yoy_pct": "Rental ADR STLY YoY %",
    "market_adr": "Market ADR",
    "market_adr_ly": "Market ADR STLY",
    "market_adr_yoy_pct": "Market ADR STLY YoY %",
    "mpi": "Market Penetration Index %",
    "booking_window": "Median Booking Window",
    "market_booking_window": "Average Market Booking Window",
    "pickup_30d": "Booked Nights Pickup (30 Days)",
}

_EXEC_FIELDS_3089 = {
    "rental_revenue": "Rental Revenue",
    "rental_revenue_ly": "Rental Revenue STLY",
    "rental_revenue_full_ly": "Rental Revenue LY",
    "rental_revenue_yoy_pct": "Rental Revenue YoY %",
    "revpar": "RevPar",
    "revpar_ly": "RevPar STLY",
    "revpar_yoy_pct": "RevPar STLY YoY %",
    "market_revpar": "Average Market RevPar",
    "market_revpar_ly": "Average Market RevPar STLY",
    "market_revpar_yoy_pct": "Average Market RevPar STLY YoY %",
    "occ_pct": "Paid Occupancy",
    "occ_pct_ly": "Paid Occupancy STLY",
    "occ_yoy_diff": "Paid Occupancy STLY YoY difference",
    "market_occ_pct": "Average Market Occupancy",
    "market_occ_pct_ly": "Average Market Occupancy STLY",
    "market_occ_yoy_diff": "Average Market Occupancy STLY YoY difference",
    "adr": "ADR",
    "adr_ly": "ADR STLY",
    "adr_yoy_pct": "ADR STLY YoY %",
    "market_adr": "Average Market ADR",
    "market_adr_ly": "Average Market ADR STLY",
    "market_adr_yoy_pct": "Average Market ADR STLY YoY %",
    "mpi": "Market Penetration Index",
    "booking_window": "Booking Window",
    "market_booking_window": "Average Market Booking Window",
    "pickup_30d": "Num Booked Pickup 30",
}

# Leaderboard reports Total Revenue (rental plus fees), not rental revenue
# alone. The UI labels the source so the two are not silently compared.
_EXEC_FIELDS_119 = {
    "rental_revenue": "Total Revenue",
    "rental_revenue_ly": "Total Revenue STLY",
    "rental_revenue_full_ly": "Total Revenue LY",
    "rental_revenue_yoy_pct": "Total Revenue STLY YoY %",
    "revpar": "RevPar",
    "revpar_ly": "RevPar STLY",
    "revpar_yoy_pct": "RevPar STLY YoY %",
    "occ_pct": "Occupancy",
    "occ_pct_ly": "Occupancy STLY",
    "occ_yoy_diff": "Occupancy STLY YoY difference",
    "adr": "ADR",
    "adr_ly": "ADR STLY",
    "adr_yoy_pct": "ADR STLY YoY %",
    "mpi": "Market Penetration Index",
}

_EXEC_FIELD_MAPS = {119: _EXEC_FIELDS_119, 3089: _EXEC_FIELDS_3089}

EXEC_REPORT_TEMPLATE_ID = int(os.environ.get("PRICELABS_EXEC_TEMPLATE_ID", "119"))


def _exec_rows_from_report_builder(template_id: int = EXEC_REPORT_TEMPLATE_ID):
    """Executive-report rows from Report Builder, so no upload is required.

    Returns (rows, source). Template 119 is the default: it is the only source
    covering every group. Non-syncing listings are dropped - they are
    offboarded units that still carry last-year revenue, and including them
    turns portfolio YoY sharply negative for no real reason.
    """
    raw, source = pl_report_builder.load(template_id)
    if not raw:
        return [], source

    fields = _EXEC_FIELD_MAPS.get(template_id, _EXEC_FIELDS_119)
    portfolio_idx = {p.name: p for p in _PORTFOLIO}

    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _from_index(value, index):
        """Recover a market figure from a penetration index (own / market * 100)."""
        if value is None or not index:
            return None
        try:
            return round(value / (float(index) / 100.0), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    rows = []
    skipped_not_syncing = 0
    for r in raw:
        name = str(r.get("Listing Name") or "").strip()
        if not name:
            continue
        sync = r.get("Sync ON/OFF")
        if sync in (0, "0", False):
            skipped_not_syncing += 1
            continue

        prop = portfolio_idx.get(name)
        # Group by customization group only - never the sub-group.
        group = (prop.customization_group if prop else None) or str(r.get("Group Name") or "")
        row = {
            "name": name,
            "group": group.strip(),
            "subgroup": "",
            "bedrooms": (prop.bedrooms if prop else None) or int(r.get("Bedroom Count") or 0),
        }
        for field, api_name in fields.items():
            row[field] = _f(r.get(api_name))

        # Leaderboard carries no market absolutes, but its penetration indices
        # are own-versus-market ratios, so the market side can be recovered.
        row.setdefault("market_adr", None)
        row.setdefault("market_revpar", None)
        row.setdefault("market_occ_pct", None)
        if row.get("market_adr") is None:
            row["market_adr"] = _from_index(row.get("adr"), r.get("Market Penetration ADR Index"))
        if row.get("market_revpar") is None:
            row["market_revpar"] = _from_index(row.get("revpar"), r.get("Market Penetration RevPar Index"))
        if row.get("market_occ_pct") is None:
            row["market_occ_pct"] = _from_index(row.get("occ_pct"), r.get("Market Penetration Index"))
        for missing in ("market_adr_ly", "market_revpar_ly", "market_occ_pct_ly",
                        "market_adr_yoy_pct", "market_revpar_yoy_pct", "market_occ_yoy_diff",
                        "booking_window", "market_booking_window", "pickup_30d"):
            row.setdefault(missing, None)
        rows.append(row)

    if skipped_not_syncing:
        source = f"{source}; {skipped_not_syncing} non-syncing listings excluded"
    return rows, source


@app.route("/api/executive-report")
def executive_report():
    """Aggregate scorecard data into group-level and portfolio-level executive summary."""
    exec_source = "uploaded scorecard"
    if not SCORECARD_PATH.exists():
        # No upload - build the same rows from Report Builder instead of
        # leaving the page empty.
        rows, exec_source = _exec_rows_from_report_builder()
        if not rows:
            return jsonify({
                "ok": False,
                "error": f"No scorecard uploaded, and Report Builder is unavailable: {exec_source}",
            }), 404
        return _executive_report_from_rows(rows, exec_source)

    try:
        import openpyxl, os
        wb = openpyxl.load_workbook(str(SCORECARD_PATH), read_only=True, data_only=True)
        ws = wb.active
        header = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
        # Normalize away all whitespace differences (PriceLabs exports have been
        # observed with inconsistently spaced headers, e.g. "YoY %" vs "YoY%" —
        # this endpoint was silently reading None for every YoY column because
        # of exactly that mismatch) so a spacing change doesn't silently break
        # column lookups again.
        _norm_header_idx = {re.sub(r"\s+", "", h): i for i, h in enumerate(header)}

        def col(row, name):
            idx = _norm_header_idx.get(re.sub(r"\s+", "", name))
            if idx is None:
                return None
            try:
                return row[idx].value
            except IndexError:
                return None

        def _f(v):
            try: return float(v) if v not in (None, "") else None
            except: return None

        portfolio_idx = {p.name: p for p in _PORTFOLIO}

        rows = []
        for row in ws.iter_rows(min_row=2):
            name = str(col(row, "Listing Name") or "").strip()
            if not name: continue
            prop = portfolio_idx.get(name)
            group = (prop.customization_group if prop else "") or ""
            subgroup = (prop.customization_sub_group if prop else "") or ""
            beds = (prop.bedrooms if prop else 0) or 0
            rows.append({
                "name": name, "group": group, "subgroup": subgroup, "bedrooms": beds,
                "rental_revenue": _f(col(row, "Rental Revenue")),
                "rental_revenue_ly": _f(col(row, "Rental Revenue STLY")),
                "rental_revenue_full_ly": _f(col(row, "Rental Revenue LY")),
                "rental_revenue_yoy_pct": _f(col(row, "Rental Revenue STLY YoY %")),
                "revpar": _f(col(row, "Rental RevPAR")),
                "revpar_ly": _f(col(row, "Rental RevPAR STLY")),
                "revpar_yoy_pct": _f(col(row, "Rental RevPAR STLY YoY %")),
                "market_revpar": _f(col(row, "Market RevPAR")),
                "market_revpar_ly": _f(col(row, "Market RevPAR STLY")),
                "market_revpar_yoy_pct": _f(col(row, "Market RevPAR STLY YoY %")),
                "occ_pct": _f(col(row, "Paid Occupancy %")),
                "occ_pct_ly": _f(col(row, "Paid Occupancy % STLY")),
                "occ_yoy_diff": _f(col(row, "Paid Occupancy STLY YoY Difference")),
                "market_occ_pct": _f(col(row, "Market Occupancy %")),
                "market_occ_pct_ly": _f(col(row, "Market Occupancy % STLY")),
                "market_occ_yoy_diff": _f(col(row, "Market Occupancy STLY YoY Difference")),
                "adr": _f(col(row, "Rental ADR")),
                "adr_ly": _f(col(row, "Rental ADR STLY")),
                "adr_yoy_pct": _f(col(row, "Rental ADR STLY YoY %")),
                "market_adr": _f(col(row, "Market ADR")),
                "market_adr_ly": _f(col(row, "Market ADR STLY")),
                "market_adr_yoy_pct": _f(col(row, "Market ADR STLY YoY %")),
                "mpi": _f(col(row, "Market Penetration Index %")),
                "booking_window": _f(col(row, "Median Booking Window")),
                "market_booking_window": _f(col(row, "Average Market Booking Window")),
                "pickup_30d": _f(col(row, "Booked Nights Pickup (30 Days)")),
            })
        wb.close()

        return _executive_report_from_rows(rows, exec_source)
    except Exception as e:
        app.logger.exception("executive report failed")
        return jsonify({"ok": False, "error": str(e)}), 500


def _executive_report_from_rows(rows, exec_source="uploaded scorecard"):
    try:
        # Same-Store Sales filter: curated SSS roster if uploaded, else keep only
        # listings that were earning at this time last year (STLY revenue > 0)
        sss = request.args.get("sss", "").strip() in {"1", "true", "yes"}
        total_before_sss = len(rows)
        if sss:
            roster = _sss_roster()
            if roster:
                rows = [r for r in rows if r["name"] in roster]
            else:
                rows = [r for r in rows if (r.get("rental_revenue_ly") or 0) > 0]
        sss_excluded = total_before_sss - len(rows)

        # Group aggregates
        from collections import defaultdict
        groups_map = defaultdict(list)
        for r in rows:
            groups_map[r["group"] or "Ungrouped"].append(r)

        def safe_avg(vals):
            v = [x for x in vals if x is not None]
            return round(sum(v) / len(v), 2) if v else None

        def safe_sum(vals):
            v = [x for x in vals if x is not None]
            return round(sum(v), 2) if v else None

        def yoy_pct(cy, ly):
            if cy is not None and ly and ly != 0:
                return round((cy - ly) / abs(ly) * 100, 1)
            return None

        group_summaries = []
        for gname, grows in sorted(groups_map.items()):
            rev_cy = safe_sum(r["rental_revenue"] for r in grows)
            rev_ly = safe_sum(r["rental_revenue_ly"] for r in grows)
            rev_full_ly = safe_sum(r["rental_revenue_full_ly"] for r in grows)
            mkt_revpar = safe_avg(r["market_revpar"] for r in grows)
            mkt_revpar_ly = safe_avg(r["market_revpar_ly"] for r in grows)
            mkt_adr = safe_avg(r["market_adr"] for r in grows)
            mkt_adr_ly = safe_avg(r["market_adr_ly"] for r in grows)
            group_summaries.append({
                "group": gname,
                "listings": len(grows),
                "revenue_cy": rev_cy,
                "revenue_ly": rev_ly,
                "revenue_full_ly": rev_full_ly,
                "revenue_yoy_pct": yoy_pct(rev_cy, rev_ly),
                "avg_revpar": safe_avg(r["revpar"] for r in grows),
                "avg_revpar_ly": safe_avg(r["revpar_ly"] for r in grows),
                "avg_market_revpar": mkt_revpar,
                "avg_market_revpar_ly": mkt_revpar_ly,
                "avg_market_revpar_yoy_pct": yoy_pct(mkt_revpar, mkt_revpar_ly),
                "avg_occ_pct": safe_avg(r["occ_pct"] for r in grows),
                "avg_occ_pct_ly": safe_avg(r["occ_pct_ly"] for r in grows),
                "avg_occ_yoy_diff": safe_avg(r["occ_yoy_diff"] for r in grows),
                "avg_market_occ_pct": safe_avg(r["market_occ_pct"] for r in grows),
                "avg_market_occ_pct_ly": safe_avg(r["market_occ_pct_ly"] for r in grows),
                "avg_market_occ_yoy_diff": safe_avg(r["market_occ_yoy_diff"] for r in grows),
                "avg_adr": safe_avg(r["adr"] for r in grows),
                "avg_adr_ly": safe_avg(r["adr_ly"] for r in grows),
                "avg_market_adr": mkt_adr,
                "avg_market_adr_ly": mkt_adr_ly,
                "avg_market_adr_yoy_pct": yoy_pct(mkt_adr, mkt_adr_ly),
                "avg_mpi": safe_avg(r["mpi"] for r in grows),
                "avg_booking_window": safe_avg(r["booking_window"] for r in grows),
                "avg_market_booking_window": safe_avg(r["market_booking_window"] for r in grows),
                "total_pickup_30d": safe_sum(r["pickup_30d"] for r in grows),
            })

        # Portfolio totals
        rev_cy_total = safe_sum(r["rental_revenue"] for r in rows)
        rev_ly_total = safe_sum(r["rental_revenue_ly"] for r in rows)
        rev_full_ly_total = safe_sum(r["rental_revenue_full_ly"] for r in rows)
        mkt_revpar_p = safe_avg(r["market_revpar"] for r in rows)
        mkt_revpar_ly_p = safe_avg(r["market_revpar_ly"] for r in rows)
        mkt_adr_p = safe_avg(r["market_adr"] for r in rows)
        mkt_adr_ly_p = safe_avg(r["market_adr_ly"] for r in rows)
        portfolio = {
            "listings": len(rows),
            "revenue_cy": rev_cy_total,
            "revenue_ly": rev_ly_total,
            "revenue_full_ly": rev_full_ly_total,
            "revenue_yoy_pct": yoy_pct(rev_cy_total, rev_ly_total),
            "avg_revpar": safe_avg(r["revpar"] for r in rows),
            "avg_revpar_ly": safe_avg(r["revpar_ly"] for r in rows),
            "avg_market_revpar": mkt_revpar_p,
            "avg_market_revpar_ly": mkt_revpar_ly_p,
            "avg_market_revpar_yoy_pct": yoy_pct(mkt_revpar_p, mkt_revpar_ly_p),
            "avg_occ_pct": safe_avg(r["occ_pct"] for r in rows),
            "avg_occ_pct_ly": safe_avg(r["occ_pct_ly"] for r in rows),
            "avg_occ_yoy_diff": safe_avg(r["occ_yoy_diff"] for r in rows),
            "avg_market_occ_pct": safe_avg(r["market_occ_pct"] for r in rows),
            "avg_market_occ_pct_ly": safe_avg(r["market_occ_pct_ly"] for r in rows),
            "avg_market_occ_yoy_diff": safe_avg(r["market_occ_yoy_diff"] for r in rows),
            "avg_adr": safe_avg(r["adr"] for r in rows),
            "avg_adr_ly": safe_avg(r["adr_ly"] for r in rows),
            "avg_market_adr": mkt_adr_p,
            "avg_market_adr_ly": mkt_adr_ly_p,
            "avg_market_adr_yoy_pct": yoy_pct(mkt_adr_p, mkt_adr_ly_p),
            "avg_mpi": safe_avg(r["mpi"] for r in rows),
            "avg_market_booking_window": safe_avg(r["market_booking_window"] for r in rows),
            "total_pickup_30d": safe_sum(r["pickup_30d"] for r in rows),
        }

        # Only meaningful when the rows came from an upload; Report Builder
        # rows are live and have no upload date.
        if SCORECARD_PATH.exists():
            mtime = os.path.getmtime(str(SCORECARD_PATH))
            uploaded_at = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%b %d, %Y")
        else:
            uploaded_at = None

        return jsonify({
            "ok": True,
            "portfolio": portfolio,
            "groups": group_summaries,
            "uploaded_at": uploaded_at,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sss": sss,
            "sss_excluded": sss_excluded,
            "total_listings_all": total_before_sss,
            "source": exec_source,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/weekly-report")
def weekly_report():
    """Assemble everything needed for the downloadable weekly executive report:
    group scorecard + the prioritized listings to actually work this week."""
    from kcity_surge_dso import _is_kcity, SURGE_DATES
    from high_demand_calendar import HIGH_DEMAND_DATES

    sss_param = "?sss=1" if request.args.get("sss") in {"1", "true", "yes"} else ""
    with app.test_request_context("/api/executive-report" + sss_param):
        exec_resp = executive_report()
    exec_data = exec_resp.get_json() if hasattr(exec_resp, "get_json") else exec_resp[0].get_json()
    if not exec_data.get("ok"):
        return jsonify({"ok": False, "error": exec_data.get("error", "Executive report unavailable")}), 500

    active = [p for p in _PORTFOLIO if p.active]

    def _listing_row(p, extra=None):
        row = {
            "name": p.name,
            "group": p.customization_group or "Ungrouped",
            "bedrooms": p.bedrooms,
            "occ_60d": round(p.adj_occ_60d * 100, 1),
            "occ_90d": round(p.adj_occ_90d * 100, 1),
            "base_price": p.base_price,
            "min_price": p.min_price,
            "booked_14d": p.booked_14d,
            "last_booked_days": p.last_booked_days,
            "urgency": p.urgency,
        }
        if extra:
            row.update(extra)
        return row

    # ── Priority 1: rescue list — emptiest 60-day calendars ──────────────────
    rescue = sorted([p for p in active if p.urgency in {"critical", "warning"}],
                    key=lambda p: p.adj_occ_60d)[:20]
    rescue_rows = [_listing_row(p, {"why": f"{p.adj_occ_60d*100:.0f}% occupied next 60 days"}) for p in rescue]

    # ── Priority 2: pending price actions awaiting approval ──────────────────
    pending = [a for a in _load_actions()
               if a.get("status") == "pending" and not _is_pace_year_action(a)
               and not _is_monthly_pacing_action(a)]
    def _act_rank(a):
        return (0 if a.get("priority") == "high" else 1, str(a.get("property", "")))
    pending_rows = [{
        "property": a.get("property", ""),
        "type": a.get("type", ""),
        "suggestion": a.get("suggestion", ""),
        "current_value": a.get("current_value", ""),
        "proposed_value": a.get("proposed_value", ""),
        "priority": a.get("priority", "medium"),
        "source": a.get("source") or a.get("system") or "",
    } for a in sorted(pending, key=_act_rank)[:40]]

    # ── Priority 3: promo candidates — zero pickup but priced above floor ────
    promo = [p for p in active
             if p.booked_14d == 0 and p.adj_occ_60d < 0.40
             and p.min_price and p.base_price > p.min_price]
    promo_rows = [_listing_row(p, {
        "why": "No bookings in 14 days · room above min price for a promo/discount"
    }) for p in sorted(promo, key=lambda p: p.adj_occ_60d)[:15]]

    # ── Priority 4: overperformers — candidates for an increase ──────────────
    # Exclude listings whose "100%" is owner blocks rather than real demand —
    # raising rates on a blocked calendar accomplishes nothing.
    increase_pool = [p for p in active
                     if p.urgency == "overperforming" and not _is_likely_blocked(p)]
    excluded_blocked = [p for p in active
                        if p.urgency == "overperforming" and _is_likely_blocked(p)]
    increase = sorted(increase_pool, key=lambda p: -p.adj_occ_60d)[:15]
    increase_rows = [_listing_row(p, {
        "why": f"{p.adj_occ_60d*100:.0f}% occupied next 60 days — test a rate increase"
    }) for p in increase]
    blocked_rows = [_listing_row(p, {
        "why": f"{p.adj_occ_60d*100:.0f}% occ but no booking activity — verify owner block / calendar"
    }) for p in sorted(excluded_blocked, key=lambda p: p.name)]

    # ── Upcoming demand windows within the actionable planning horizon ───────
    today = TODAY
    group_occ = _group_forward_occ(_PORTFOLIO)
    kcity_pool = [p for p in active if _is_kcity(p)]

    def _avg_attr(props, attr):
        vals = [getattr(p, attr, 0) for p in props if getattr(p, attr, 0)]
        return round(sum(vals) / len(vals), 3) if vals else None

    kcity_occ = {"listings": len(kcity_pool),
                 **{f"occ_{d}d": _avg_attr(kcity_pool, f"adj_occ_{d}d") for d in (30, 60, 90, 120, 180)}}

    events = []
    seen = set()
    for start_str, end_str, event, demand in SURGE_DATES:
        if date.fromisoformat(end_str) < today or (start_str, event) in seen:
            continue
        seen.add((start_str, event))
        days_until = (date.fromisoformat(start_str) - today).days
        if days_until > 120:
            continue
        occ, window = _nearest_window_occ(kcity_occ, days_until)
        events.append({"event": event, "group": "Knoxville / KCity", "start_date": start_str,
                       "end_date": end_str, "days_until": days_until,
                       "occ_pct": round(occ * 100, 1) if occ is not None else None,
                       "occ_window": window, **_demand_action(occ)})
    for start_str, end_str, event, demand in HIGH_DEMAND_DATES:
        if date.fromisoformat(end_str) < today:
            continue
        days_until = (date.fromisoformat(start_str) - today).days
        if days_until > 120:
            continue
        for g, occ_map in group_occ.items():
            occ, window = _nearest_window_occ(occ_map, days_until)
            events.append({"event": event, "group": g, "start_date": start_str,
                           "end_date": end_str, "days_until": days_until,
                           "occ_pct": round(occ * 100, 1) if occ is not None else None,
                           "occ_window": window, **_demand_action(occ)})
    events.sort(key=lambda e: (e["days_until"], e["event"], e["group"]))
    # Only the windows that actually need a decision this week
    action_events = [e for e in events if e["action"] in {"increase", "decrease"}][:25]

    # Always include BOTH bases so the report shows this year's headline growth
    # and the like-for-like same-store number side by side — total growth alone
    # overstates performance when new listings were onboarded mid-year.
    ytd = None
    try:
        ytd = _compute_ytd_summary()
    except Exception as e:
        app.logger.warning("weekly-report: YTD summary unavailable: %s", e)

    return jsonify({
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_date": today.isoformat(),
        "scorecard_uploaded": exec_data.get("uploaded_at"),
        "sss_mode": bool(sss_param),
        "portfolio": exec_data["portfolio"],
        "groups": exec_data["groups"],
        "ytd": ytd,   # {all: {...}, sss: {...}, window_cy, window_ly, sss_excluded}
        "counts": {
            "critical": sum(1 for p in active if p.urgency == "critical"),
            "warning": sum(1 for p in active if p.urgency == "warning"),
            "overperforming": sum(1 for p in active if p.urgency == "overperforming"),
            "ok": sum(1 for p in active if p.urgency == "ok"),
            "total": len(active),
            "zero_pickup_14d": sum(1 for p in active if p.booked_14d == 0),
            "likely_blocked": sum(1 for p in active if _is_likely_blocked(p)),
        },
        "priorities": {
            "rescue": rescue_rows,
            "pending_actions": pending_rows,
            "promo_candidates": promo_rows,
            "increase_candidates": increase_rows,
            "blocked_review": blocked_rows,
            "demand_windows": action_events,
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# Weekly review folder — the user's own workspace, deliberately OUTSIDE the
# Haven app directory and gitignored, so their added numbers can never collide
# with app data files, be overwritten by an update, or block a git pull.
# ─────────────────────────────────────────────────────────────────────────────

WEEKLY_REVIEWS_DIR = Path(__file__).parent.parent / "weekly_reviews"

KEY_NUMBERS_TEMPLATE = """Metric,Value,Notes
# Add your own key numbers below. Anything you enter here appears in the
# report the next time you save it, under "Additional Key Data".
# Delete these comment lines if you like — lines starting with # are ignored.
Owner payouts this month,,
Net revenue after fees,,
Cancellations this week,,
New listings onboarded,,
Listings offboarded,,
Cleaning cost per turn (avg),,
Channel commission total,,
"""

NOTES_TEMPLATE = """# Weekly Review Notes — {date}

## What I'm adding to the analysis

(Your commentary, context the dashboard can't see, decisions made this week.)

## Actions taken

-

## Follow-ups for next week

-
"""


def _read_key_numbers(week_dir: Path) -> list[dict]:
    """Read the user's key-numbers.csv if they've filled it in.
    Blank rows and # comment lines are skipped."""
    path = week_dir / "key-numbers.csv"
    if not path.exists():
        return []
    out = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                metric = (row.get("Metric") or "").strip()
                value = (row.get("Value") or "").strip()
                if not metric or metric.startswith("#") or not value:
                    continue
                out.append({"metric": metric, "value": value,
                            "notes": (row.get("Notes") or "").strip()})
    except (OSError, csv.Error):
        return []
    return out


@app.route("/api/weekly-review/save", methods=["POST"])
def save_weekly_review():
    """Save this week's review into weekly_reviews/<date>/ — report HTML,
    raw CSVs for spreadsheet work, and a key-numbers file to fill in."""
    payload = request.get_json(silent=True) or {}
    report_html = payload.get("report_html") or ""
    week = (payload.get("week") or TODAY.isoformat()).strip()

    try:
        week_dir = WEEKLY_REVIEWS_DIR / week
        week_dir.mkdir(parents=True, exist_ok=True)
        data_dir = week_dir / "data"
        data_dir.mkdir(exist_ok=True)

        with app.test_request_context("/api/weekly-report"):
            wr = weekly_report().get_json()
        if not wr.get("ok"):
            return jsonify({"ok": False, "error": wr.get("error", "report build failed")}), 500

        written = []

        if report_html:
            (week_dir / "weekly-report.html").write_text(report_html, encoding="utf-8")
            written.append("weekly-report.html")

        # Raw CSVs so the numbers can be pivoted/extended in Excel
        def _write_csv(name: str, rows: list[dict], cols: list[str]):
            if not rows:
                return
            with open(data_dir / name, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            written.append(f"data/{name}")

        _write_csv("groups.csv", wr.get("groups", []),
                   ["group", "listings", "revenue_cy", "revenue_ly", "revenue_yoy_pct",
                    "avg_revpar", "avg_market_revpar", "avg_occ_pct", "avg_market_occ_pct",
                    "avg_adr", "avg_market_adr", "avg_mpi", "total_pickup_30d"])

        pr = wr.get("priorities", {})
        listing_cols = ["name", "group", "bedrooms", "occ_60d", "occ_90d",
                        "base_price", "min_price", "booked_14d", "last_booked_days", "why"]
        _write_csv("rescue.csv", pr.get("rescue", []), listing_cols)
        _write_csv("increase-candidates.csv", pr.get("increase_candidates", []), listing_cols)
        _write_csv("promo-candidates.csv", pr.get("promo_candidates", []), listing_cols)
        _write_csv("blocked-review.csv", pr.get("blocked_review", []), listing_cols)
        _write_csv("pending-actions.csv", pr.get("pending_actions", []),
                   ["property", "type", "priority", "suggestion", "current_value",
                    "proposed_value", "source"])
        _write_csv("demand-windows.csv", pr.get("demand_windows", []),
                   ["event", "group", "start_date", "end_date", "days_until",
                    "occ_pct", "occ_window", "label", "detail"])

        # Portfolio summary as a single tidy row
        p = wr.get("portfolio", {})
        y = wr.get("ytd") or {}
        summary = {"week": week, "scorecard": wr.get("scorecard_uploaded", ""), **p}
        if y:
            summary.update({
                "ytd_all_revenue": y["all"]["revenue_cy"], "ytd_all_yoy_pct": y["all"]["revenue_yoy_pct"],
                "ytd_sss_revenue": y["sss"]["revenue_cy"], "ytd_sss_yoy_pct": y["sss"]["revenue_yoy_pct"],
                "ytd_sss_excluded": y.get("sss_excluded"),
            })
        summary.update({f"count_{k}": v for k, v in (wr.get("counts") or {}).items()})
        with open(data_dir / "portfolio-summary.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary.keys()))
            w.writeheader(); w.writerow(summary)
        written.append("data/portfolio-summary.csv")

        # Templates — only created if absent, never overwrite the user's work
        kn = week_dir / "key-numbers.csv"
        if not kn.exists():
            kn.write_text(KEY_NUMBERS_TEMPLATE, encoding="utf-8")
            written.append("key-numbers.csv")
        nt = week_dir / "notes.md"
        if not nt.exists():
            nt.write_text(NOTES_TEMPLATE.format(date=week), encoding="utf-8")
            written.append("notes.md")

        return jsonify({
            "ok": True,
            "folder": str(week_dir),
            "week": week,
            "files_written": written,
            "key_numbers_found": len(_read_key_numbers(week_dir)),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/weekly-review/key-numbers")
def weekly_review_key_numbers():
    """Return the user's key numbers for a given week, if they've filled them in."""
    week = (request.args.get("week") or TODAY.isoformat()).strip()
    week_dir = WEEKLY_REVIEWS_DIR / week
    return jsonify({"ok": True, "week": week, "exists": week_dir.exists(),
                    "key_numbers": _read_key_numbers(week_dir)})


@app.route("/api/weekly-review/list")
def weekly_review_list():
    """List saved weekly reviews, newest first."""
    if not WEEKLY_REVIEWS_DIR.exists():
        return jsonify({"ok": True, "reviews": [], "folder": str(WEEKLY_REVIEWS_DIR)})
    reviews = []
    for d in sorted(WEEKLY_REVIEWS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        reviews.append({
            "week": d.name,
            "has_report": (d / "weekly-report.html").exists(),
            "key_numbers": len(_read_key_numbers(d)),
            "has_notes": (d / "notes.md").exists(),
        })
    return jsonify({"ok": True, "reviews": reviews[:26], "folder": str(WEEKLY_REVIEWS_DIR)})


_YTD_CACHE: dict = {"key": None, "data": None}
YTD_WEEKLY_HISTORY_PATH = Path(__file__).parent / "ytd_weekly_history.json"


def _scorecard_rows_brief() -> tuple[list[dict], str]:
    """Read the uploaded scorecard Excel and return minimal per-listing rows + upload date."""
    import openpyxl
    wb = openpyxl.load_workbook(str(SCORECARD_PATH), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h or "").strip() for h in next(rows_iter)]

    def _f(rec, key):
        v = rec.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    out = []
    for row in rows_iter:
        if not row or not row[0]:
            continue
        rec = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
        name = str(rec.get("Listing Name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "revenue": _f(rec, "Rental Revenue"),
            "revenue_ly": _f(rec, "Rental Revenue STLY"),
            "occ": _f(rec, "Paid Occupancy %"),
            "occ_ly": _f(rec, "Paid Occupancy % STLY"),
            "adr": _f(rec, "Rental ADR"),
            "adr_ly": _f(rec, "Rental ADR STLY"),
        })
    wb.close()
    mtime = os.path.getmtime(str(SCORECARD_PATH))
    uploaded_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    return out, uploaded_at


def _compute_ytd_summary() -> dict:
    """YTD revenue comparison from the PriceLabs scorecard (Report Builder export):
    this year vs same time last year (STLY), for all listings and the SSS subset."""
    if not SCORECARD_PATH.exists():
        raise RuntimeError("No scorecard uploaded yet — upload the weekly PriceLabs Report Builder export.")
    rows, uploaded_at = _scorecard_rows_brief()

    def _totals(subset):
        rev_cy = sum(r["revenue"] or 0 for r in subset)
        rev_ly = sum(r["revenue_ly"] or 0 for r in subset)
        occ_vals = [r["occ"] for r in subset if r["occ"] is not None]
        occ_ly_vals = [r["occ_ly"] for r in subset if r["occ_ly"] is not None]
        adr_vals = [r["adr"] for r in subset if r["adr"] is not None]
        adr_ly_vals = [r["adr_ly"] for r in subset if r["adr_ly"] is not None]
        return {
            "revenue_cy": round(rev_cy, 2), "revenue_ly": round(rev_ly, 2),
            "revenue_yoy_pct": round((rev_cy - rev_ly) / rev_ly * 100, 1) if rev_ly else None,
            "occ_cy": round(sum(occ_vals) / len(occ_vals), 1) if occ_vals else None,
            "occ_ly": round(sum(occ_ly_vals) / len(occ_ly_vals), 1) if occ_ly_vals else None,
            "adr_cy": round(sum(adr_vals) / len(adr_vals), 2) if adr_vals else None,
            "adr_ly": round(sum(adr_ly_vals) / len(adr_ly_vals), 2) if adr_ly_vals else None,
            "listings": len(subset),
        }

    roster = _sss_roster()
    if roster:
        sss_rows = [r for r in rows if r["name"] in roster]
    else:
        sss_rows = [r for r in rows if (r["revenue_ly"] or 0) > 0]
    today = date.today()
    return {
        "ok": True,
        "as_of": today.isoformat(),
        "source": "PriceLabs Report Builder scorecard",
        "uploaded_at": uploaded_at,
        "window_cy": f"YTD {today.year} (scorecard of {uploaded_at})",
        "window_ly": f"same period {today.year - 1} (STLY)",
        "all": _totals(rows),
        "sss": _totals(sss_rows),
        "sss_excluded": len(rows) - len(sss_rows),
    }


def _load_ytd_history() -> list[dict]:
    if not YTD_WEEKLY_HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(YTD_WEEKLY_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []

    # Drop legacy rows with no uploaded_at — written by an older version of
    # this endpoint (before it switched to the PriceLabs scorecard as the
    # source) and not comparable to current rows. Self-heals old history
    # files without requiring anyone to manually delete them.
    entries = [h for h in raw if isinstance(h, dict) and h.get("uploaded_at")]

    # Collapse consecutive weeks that share the same scorecard upload into a
    # single row. If the scorecard hasn't been re-uploaded in a month, the
    # numbers genuinely don't change — showing 4-5 visually identical rows
    # reads as a bug, not as "nothing changed yet."
    collapsed: list[dict] = []
    for h in entries:
        if collapsed and collapsed[-1].get("uploaded_at") == h.get("uploaded_at"):
            collapsed[-1] = h  # keep the latest as_of for this same upload
        else:
            collapsed.append(h)

    # Flag rows whose ALL-LISTINGS totals are identical to the previous
    # DIFFERENT upload. A genuinely fresh YTD export must grow week over week
    # (new nights keep getting booked), so identical totals across different
    # upload dates almost always means the PriceLabs Report Builder export is
    # configured with a FIXED date range instead of a rolling/YTD one — the
    # file is new, the data inside is frozen. Computed on the fly so old rows
    # get flagged too.
    for i, h in enumerate(collapsed):
        if i == 0:
            h["stale_data_suspected"] = False
            continue
        prev, cur = collapsed[i - 1].get("all") or {}, h.get("all") or {}
        h["stale_data_suspected"] = (
            cur.get("revenue_cy") is not None
            and cur.get("revenue_cy") == prev.get("revenue_cy")
            and cur.get("revenue_ly") == prev.get("revenue_ly")
        )
    return collapsed


def _snapshot_ytd_weekly() -> dict | None:
    """Compute YTD summary and append to the weekly history file.
    Skips if the scorecard file hasn't changed since the last snapshot —
    duplicate rows from a stale upload are noise, not trend."""
    data = _compute_ytd_summary()
    history = _load_ytd_history()
    if any(h.get("as_of") == data["as_of"] for h in history):
        return data  # already snapshotted today
    if history and history[-1].get("uploaded_at") == data.get("uploaded_at"):
        return data  # same scorecard as last snapshot — nothing new to record
    history.append(data)
    history = history[-104:]  # keep ~2 years of weekly entries
    YTD_WEEKLY_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return data


def _ytd_weekly_scheduler() -> None:
    """Background loop: snapshot YTD every Monday (or on startup if stale >7 days)."""
    import time as _time
    while True:
        try:
            history = _load_ytd_history()
            last = history[-1]["as_of"] if history else None
            today = date.today()
            stale = (not last) or (today - date.fromisoformat(last)).days >= 7
            if today.weekday() == 0 and (not last or last != today.isoformat()):
                _snapshot_ytd_weekly()
                app.logger.info("Weekly YTD snapshot taken (Monday)")
            elif stale:
                _snapshot_ytd_weekly()
                app.logger.info("Weekly YTD snapshot taken (stale catch-up)")
        except Exception as e:
            app.logger.warning("Weekly YTD snapshot failed: %s", e)
        _time.sleep(3600)  # check hourly


@app.route("/api/ytd-summary")
def ytd_summary():
    today = date.today()
    # Include the scorecard file's mtime in the cache key so a same-day
    # re-upload (correction) invalidates the cache immediately instead of
    # serving stale numbers until midnight.
    scorecard_mtime = os.path.getmtime(str(SCORECARD_PATH)) if SCORECARD_PATH.exists() else 0
    cache_key = (today.isoformat(), scorecard_mtime)
    if _YTD_CACHE["key"] == cache_key and _YTD_CACHE["data"]:
        data = dict(_YTD_CACHE["data"])
        data["history"] = _load_ytd_history()
        return jsonify(data)
    try:
        data = _compute_ytd_summary()
    except Exception as e:
        # "No scorecard uploaded yet" is a normal empty state, not a server
        # fault - returning 500 for it made the UI show a red error where a
        # prompt to upload belongs. Real failures still surface via ok:false.
        return jsonify({"ok": False, "error": str(e), "history": _load_ytd_history()}), 200
    _YTD_CACHE["key"] = cache_key
    _YTD_CACHE["data"] = data
    out = dict(data)
    out["history"] = _load_ytd_history()
    return jsonify(out)


@app.route("/api/scorecard/upload", methods=["POST"])
def upload_scorecard():
    """Accept a weekly Excel upload and save it as scorecard_upload.xlsx.

    Saves to a temp file, sanity-checks it actually opens as a workbook, then
    atomically swaps it into place — so a failed/corrupted upload can't
    clobber the working scorecard, and a concurrent reader never sees a
    partially-written file mid-upload.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file in request"}), 400
    f = request.files["file"]
    if not f.filename.endswith((".xlsx", ".xls")):
        return jsonify({"ok": False, "error": "Only .xlsx files accepted"}), 400

    tmp_path = SCORECARD_PATH.with_suffix(SCORECARD_PATH.suffix + ".tmp")
    try:
        f.save(str(tmp_path))
        import openpyxl as _openpyxl
        _openpyxl.load_workbook(str(tmp_path), read_only=True).close()
        os.replace(str(tmp_path), str(SCORECARD_PATH))
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": f"Upload failed — file may be corrupted or not a valid Excel file: {e}"}), 400
    return jsonify({"ok": True, "message": "Scorecard uploaded successfully"})


# ─────────────────────────────────────────────────────────────────────────────
# Self-update: one-click "pull latest code + restart" from the dashboard UI
# ─────────────────────────────────────────────────────────────────────────────

UPDATE_BRANCH = "claude/compassionate-fermat-CphqC"
_REPO_ROOT = Path(__file__).parent.parent

# Local runtime data the app rewrites while running. Backed up before any
# git reset and restored after, so an update can NEVER lose live data even
# on a machine whose checkout still has these files tracked.
_UPDATE_PROTECTED_FILES = [
    "scorecard_upload.xlsx", "scorecard_sss.xlsx",
    "pricelabs_portfolio.csv", "pricelabs_api_snapshot.json",
    "pricelabs_weekly_action_queue.json", "weekly_action_queue.json",
    "booking_promotion_lab.json", "pricelabs_report_builder_monthly.csv",
    "hostaway_enrichment.json", "hostaway_api_snapshot.json",
    "ytd_weekly_history.json", "weather_cache.json", ".env",
]


def _git(*args: str) -> tuple[int, str]:
    import subprocess
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as e:  # git missing, timeout, etc.
        return 1, str(e)


def _current_commit() -> str:
    code, out = _git("rev-parse", "--short", "HEAD")
    return out if code == 0 else "unknown"


def _restart_self() -> None:
    """Relaunch this app in a fresh process, then exit this one."""
    import subprocess, time as _time
    _time.sleep(1.0)  # let the HTTP response flush to the browser first
    kwargs = {"cwd": str(Path(__file__).parent)}
    if os.name == "nt":
        # New console window, fully detached from this dying process
        kwargs["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(Path(__file__).name)], **kwargs)
    _time.sleep(0.5)
    os._exit(0)


@app.route("/api/version")
def app_version():
    code, log_line = _git("log", "-1", "--format=%h %s (%cs)")
    return jsonify({"ok": True, "commit": _current_commit(),
                    "summary": log_line if code == 0 else "unknown",
                    "branch": UPDATE_BRANCH})


@app.route("/api/update", methods=["POST"])
def self_update():
    """Pull the latest code from the update branch and restart the server.

    Data-safe by construction: every runtime data file is copied to a backup
    dir before `git reset --hard` and restored after, so a hard reset can't
    delete live scorecards/queues even during the tracked->untracked
    transition. Returns before restarting; the frontend polls /api/version
    until the new process is up.
    """
    import shutil as _shutil
    old_commit = _current_commit()

    code, out = _git("fetch", "origin", UPDATE_BRANCH)
    if code != 0:
        return jsonify({"ok": False, "step": "fetch", "error": out}), 500

    code, remote_commit = _git("rev-parse", "--short", f"origin/{UPDATE_BRANCH}")
    if code != 0:
        return jsonify({"ok": False, "step": "rev-parse", "error": remote_commit}), 500

    if remote_commit == old_commit:
        return jsonify({"ok": True, "updated": False, "commit": old_commit,
                        "message": "Already up to date — no restart needed."})

    backup_dir = Path(__file__).parent / "_update_backup"
    backup_dir.mkdir(exist_ok=True)
    backed_up = []
    for name in _UPDATE_PROTECTED_FILES:
        src = Path(__file__).parent / name
        if src.exists():
            _shutil.copy2(str(src), str(backup_dir / name))
            backed_up.append(name)

    code, out = _git("reset", "--hard", f"origin/{UPDATE_BRANCH}")
    if code != 0:
        return jsonify({"ok": False, "step": "reset", "error": out}), 500

    for name in backed_up:
        _shutil.copy2(str(backup_dir / name), str(Path(__file__).parent / name))

    threading.Thread(target=_restart_self, daemon=True).start()
    return jsonify({
        "ok": True, "updated": True,
        "old_commit": old_commit, "new_commit": remote_commit,
        "data_files_protected": backed_up,
        "message": "Updated — restarting now. The page will reload automatically.",
    })


if __name__ == "__main__":
    if not _get_ai_api_key():
        print(
            f"WARNING: none of {', '.join(AI_API_KEY_ENV_VARS)} are set. Dashboard will run, but AI reports use fallback/error handling.",
            file=sys.stderr,
        )
    active_count = _SUMMARY["total_active"]
    critical_count = _SUMMARY["critical_count"]
    print(f"Starting STR Portfolio Dashboard at http://localhost:8080")
    print(f"Portfolio: {active_count} active properties · {critical_count} critical")
    threading.Thread(target=_ytd_weekly_scheduler, daemon=True).start()
    app.run(debug=False, port=8080, threaded=True, use_reloader=False)
