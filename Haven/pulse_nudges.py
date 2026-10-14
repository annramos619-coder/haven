"""Review-only, increase-only nightly nudges. Never changes provider rates."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import re

POLICY = "pulse-increase-only-v1"


def money(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def build_nudges(prop, reservations, today=None):
    today = today or date.today()
    end = today + timedelta(days=120)
    fixed = bool(re.search(r"\bfix(?:ed)?[\s_-]*min(?:imum)?\b", str(prop.tags), re.I))
    low, high = (1, 3) if fixed else ((1, 5) if int(prop.bedrooms or 0) <= 2 else (5, 10))
    rates = {}
    for text, value in prop.calendar_rates:
        try:
            day = date.fromisoformat(str(text)[:10])
        except ValueError:
            continue
        rate = money(value)
        if today < day <= end and rate is not None:
            rates[day] = rate
    booked = set()
    availability_known = reservations is not None
    for row in reservations or []:
        if str(row.get("status", "")).lower() in {"cancelled", "canceled", "declined", "rejected"}:
            continue
        try:
            arrival = date.fromisoformat(str(row.get("arrivalDate", ""))[:10])
            departure = date.fromisoformat(str(row.get("departureDate", ""))[:10])
            if departure <= arrival:
                raise ValueError("invalid stay")
        except (ValueError, TypeError):
            availability_known = False
            continue
        day = max(arrival, today)
        while day < min(departure, end + timedelta(days=1)):
            booked.add(day)
            day += timedelta(days=1)
    # Equal rates on consecutive dates are a flat-rate signal, not evidence
    # that a price has been unchanged over time (no history is stored here).
    flat = set()
    run = []
    for day in sorted(rates):
        if day in booked:
            if len(run) >= 4:
                flat.update(run)
            run = []
            continue
        if run and (day != run[-1] + timedelta(days=1) or rates[day] != rates[run[-1]]):
            if len(run) >= 4:
                flat.update(run)
            run = []
        run.append(day)
    if len(run) >= 4:
        flat.update(run)

    minimum, maximum = money(prop.min_price), money(prop.max_price)
    nudges = []
    ceiling_holds = 0
    for day, rate in sorted(rates.items()):
        if day in booked:
            continue
        # Never call unverified availability a confirmed gap.
        reasons = []
        if availability_known:
            reasons.append("Unreserved night: verify owner blocks and availability")
        if day in flat:
            reasons.append("Flat-rate stretch (4+ nights); price history not available")
        if not reasons:
            continue
        if not availability_known:
            reasons.append("Reservation data unavailable: confirm this night is open")
        if fixed:
            reasons.append("Fixed-minimum tag: increase only, $1–$3")
        # Occupancy is a listing-level proxy, not market demand for this date.
        horizon = (day - today).days
        occ = (getattr(prop, "adj_occ_120d", 0) if horizon > 90 else
               getattr(prop, "adj_occ_90d", 0) if horizon > 60 else prop.adj_occ_60d)
        strong = occ >= 0.70
        weekend = day.weekday() in (4, 5)
        delta = high if strong and weekend else ((low + high) // 2 if strong or weekend else low)
        if maximum is not None:
            delta = min(delta, int(maximum - rate))
        if delta < low:
            ceiling_holds += 1
            continue
        proposed = rate + Decimal(delta)
        if minimum is not None and proposed < minimum:
            continue  # Do not jump past the requested nudge cap to fix bad data.
        nudges.append({"date": day.isoformat(), "day": day.strftime("%A"),
            "current_rate": float(rate), "nudge": delta, "proposed_rate": float(proposed),
            "reason": "; ".join(reasons),
            "demand": f"{'Fri/Sat' if weekend else 'Sun–Thu'}; " +
                (f"listing occupancy proxy {occ:.0%}" if occ > 0 else "daily demand unavailable; conservative nudge"),
            "at_minimum": minimum is not None and rate <= minimum})
    return {"daily_nudges": nudges, "fixed_minimum": fixed, "band": f"${low}–${high}",
            "ceiling_holds": ceiling_holds, "has_rates": bool(rates),
            "reservation_data_available": availability_known, "policy": POLICY,
            "generated_on": today.isoformat()}
