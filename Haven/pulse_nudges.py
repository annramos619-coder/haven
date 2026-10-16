"""Increase-only nightly suggestions and explicitly approved PriceLabs pushes."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import re
from copy import copy

POLICY = "pulse-increase-only-v1"


def money(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def nudge_band(tags, bedrooms):
    fixed = bool(re.search(r"\b(?:fix(?:ed)?[\s_-]*min(?:imum)?|min(?:imum)?[\s_-]+rate)\b", str(tags), re.I))
    return (1, 3) if fixed else ((1, 5) if int(bedrooms) <= 2 else (5, 10))


def calendar_night_open(row):
    info = (row.get("reason") or {}).get("listing_info") or {}
    return (str(row.get("unbookable")) == "0"
            and row.get("booking_status") in ("", "available")
            and str(info.get("num_bookings", "0")) == "0"
            and str(info.get("num_blocked", "0")) == "0")


def load_pulse_calendars(properties, client, today=None, batch_size=20):
    """Load dated prices for Pulse without overwriting the portfolio CSV.

    POST /listing_prices is a read operation. Match both ID and PMS, and never
    substitute stale CSV rates when an API response is missing or fails.
    """
    today = today or date.today()
    loaded = [copy(p) for p in properties]
    eligible = []
    for prop in loaded:
        prop.calendar_rates = []
        prop.pulse_open_dates = set()
        prop.pulse_calendar = {}
        prop.pulse_data_error = ""
        prop.pulse_last_refreshed = ""
        if not str(prop.listing_id or "").strip() or not str(prop.pms_name or "").strip():
            prop.pulse_data_error = "Missing PriceLabs listing ID or PMS. Sync the portfolio first."
        else:
            eligible.append(prop)
    for offset in range(0, len(eligible), batch_size):
        batch = eligible[offset:offset + batch_size]
        payload = {"listings": [{"id": str(p.listing_id), "pms": p.pms_name,
                    "dateFrom": (today + timedelta(days=1)).isoformat(),
                    "dateTo": (today + timedelta(days=120)).isoformat(), "reason": True} for p in batch]}
        try:
            response = client.request("POST", "/listing_prices", payload)
            if not isinstance(response, list):
                raise ValueError("PriceLabs returned an unexpected nightly-price response.")
        except Exception as exc:
            for prop in batch:
                prop.pulse_data_error = f"Nightly-price lookup failed: {exc}"
            continue
        for prop in batch:
            matches = [row for row in response if isinstance(row, dict)
                       and str(row.get("id")) == str(prop.listing_id) and row.get("pms") == prop.pms_name]
            if len(matches) != 1:
                prop.pulse_data_error = "PriceLabs did not return one matching listing/PMS calendar."
                continue
            listing = matches[0]
            if listing.get("error") or listing.get("error_status"):
                prop.pulse_data_error = "PriceLabs: " + str(listing.get("error") or listing.get("error_status"))
                continue
            if listing.get("currency") != "USD":
                prop.pulse_data_error = "USD nightly prices are required for these dollar nudges."
                continue
            prop.pulse_last_refreshed = str(listing.get("last_refreshed_at") or "not supplied")
            for row in listing.get("data") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    day = date.fromisoformat(str(row.get("date")))
                except ValueError:
                    continue
                rate = money(row.get("price"))
                if rate is None or not today < day <= today + timedelta(days=120):
                    continue
                key = day.isoformat()
                prop.calendar_rates.append((key, float(rate)))
                prop.pulse_calendar[key] = row
                if calendar_night_open(row):
                    prop.pulse_open_dates.add(key)
            if not prop.calendar_rates:
                prop.pulse_data_error = "PriceLabs returned no usable nightly prices in the next 120 days."
    return loaded


def build_nudges(prop, reservations, today=None):
    today = today or date.today()
    end = today + timedelta(days=120)
    low, high = nudge_band(prop.tags, prop.bedrooms or 0)
    fixed = (low, high) == (1, 3)
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
    open_dates = getattr(prop, "pulse_open_dates", None)
    live_calendar = getattr(prop, "pulse_calendar", {})
    availability_known = open_dates is not None or reservations is not None
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
        if day in booked or (open_dates is not None and day.isoformat() not in open_dates):
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
        if day in booked or (open_dates is not None and day.isoformat() not in open_dates):
            continue
        # Never call unverified availability a confirmed gap.
        reasons = []
        if availability_known:
            reasons.append("PriceLabs calendar shows this night is open" if open_dates is not None else
                           "Unreserved night: verify owner blocks and availability")
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
        daily_info = (live_calendar.get(day.isoformat(), {}).get("reason") or {}).get("listing_info") or {}
        daily_min = money(daily_info.get("min_price_limit")) or minimum
        daily_max = money(daily_info.get("max_price_limit")) or maximum
        if daily_max is not None:
            delta = min(delta, int(daily_max - rate))
        if delta < low:
            ceiling_holds += 1
            continue
        proposed = rate + Decimal(delta)
        if daily_min is not None and proposed < daily_min:
            continue  # Do not jump past the requested nudge cap to fix bad data.
        nudges.append({"date": day.isoformat(), "day": day.strftime("%A"),
            "minimum_price": float(daily_min) if daily_min else None, "days_out": horizon,
            "decision": "pending", "current_rate": float(rate), "nudge": delta, "proposed_rate": float(proposed),
            "reason": "; ".join(reasons),
            "demand": f"{'Fri/Sat' if weekend else 'Sun–Thu'}; " +
                (f"listing occupancy proxy {occ:.0%}" if occ > 0 else "daily demand unavailable; conservative nudge"),
            "at_minimum": daily_min is not None and rate <= daily_min})
    return {"daily_nudges": nudges, "fixed_minimum": fixed, "band": f"${low}–${high}",
            "ceiling_holds": ceiling_holds, "has_rates": bool(rates),
            "reservation_data_available": availability_known, "policy": POLICY,
            "generated_on": today.isoformat(),
            "data_error": getattr(prop, "pulse_data_error", ""),
            "rates_source": "PriceLabs nightly calendar API" if open_dates is not None else "Portfolio export",
            "rates_last_refreshed": getattr(prop, "pulse_last_refreshed", ""),
            "open_nights": len(open_dates) if open_dates is not None else None}


class PulsePushError(ValueError):
    pass


def push_nudges(action, client, today=None):
    """Send approved absolute targets; never recompute/add nudges on retries.

    Customer API docs: /listings/{id}, /listing_prices,
    /listings/{id}/overrides, and /refresh_listing.
    """
    today = today or date.today()
    if action.get("generated_on") != today.isoformat():
        raise PulsePushError("Refresh Pulse suggestions today before pushing.")
    lid, pms = str(action.get("listing_id") or ""), str(action.get("pms_name") or "")
    rows = action.get("daily_nudges")
    if not lid or not pms or not isinstance(rows, list) or not 1 <= len(rows) <= 120:
        raise PulsePushError("Missing listing identity or daily targets. Refresh Pulse suggestions.")
    response = client.request("GET", f"/listings/{lid}")
    matches = [x for x in response.get("listings", [])
               if str(x.get("id")) == lid and x.get("pms") == pms]
    if len(matches) != 1:
        raise PulsePushError("Cannot verify the PriceLabs listing and PMS.")
    listing = matches[0]
    if listing.get("currency") != "USD" or "tags" not in listing or listing.get("no_of_bedrooms") is None:
        raise PulsePushError("USD currency, current tags, and bedroom count must be available before pushing.")
    low, high = nudge_band(listing["tags"], listing["no_of_bedrooms"])
    prices = client.request("POST", "/listing_prices", {"listings": [{"id": lid, "pms": pms,
        "dateFrom": (today + timedelta(days=1)).isoformat(),
        "dateTo": (today + timedelta(days=120)).isoformat(), "reason": True}]})
    matches = [x for x in prices if isinstance(x, dict) and str(x.get("id")) == lid and x.get("pms") == pms] if isinstance(prices, list) else []
    if len(matches) != 1 or matches[0].get("error"):
        raise PulsePushError("Current PriceLabs nightly rates are unavailable.")
    calendar = {x["date"]: x for x in matches[0].get("data", []) if isinstance(x, dict) and x.get("date")}
    endpoint = f"/listings/{lid}/overrides"
    existing_response = client.request("GET", endpoint, params={"pms": pms})
    if not isinstance(existing_response, dict) or not isinstance(existing_response.get("overrides"), list):
        raise PulsePushError("Cannot verify existing PriceLabs overrides.")
    existing = {x["date"]: x for x in existing_response["overrides"] if isinstance(x, dict) and x.get("date")}
    items, targets = [], {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        key = day.isoformat()
        old, target = money(row.get("current_rate")), money(row.get("proposed_rate"))
        if key in targets or not today < day <= today + timedelta(days=120):
            raise PulsePushError("Duplicate or expired Pulse dates. Refresh suggestions.")
        if old is None or target is None or not low <= target - old <= high:
            raise PulsePushError(f"{key}: increase must be ${low}-${high} under the current listing tags.")
        current = calendar.get(key, {})
        live = money(current.get("price"))
        info = (current.get("reason") or {}).get("listing_info") or {}
        if (str(current.get("unbookable")) != "0" or current.get("booking_status") not in ("", "available")
                or str(info.get("num_bookings", "0")) != "0" or str(info.get("num_blocked", "0")) != "0"):
            raise PulsePushError(f"{key}: PriceLabs does not confirm this night is open.")
        minimum = money(info.get("min_price_limit")) or money(listing.get("min"))
        maximum = money(info.get("max_price_limit")) or money(listing.get("max"))
        if live is None or target < live or (minimum and target < minimum) or (maximum and target > maximum):
            raise PulsePushError(f"{key}: target conflicts with current prices or limits. Refresh suggestions.")
        targets[key] = target
        prior = existing.get(key, {})
        if prior.get("price_type") == "fixed" and money(prior.get("price")) == target:
            continue  # Exact target already saved: retry cannot compound it.
        if prior.get("price_type") == "fixed" and money(prior.get("price")) not in (None, old):
            raise PulsePushError(f"{key}: an existing override changed. Refresh prices in PriceLabs first.")
        if live != old:
            raise PulsePushError(f"{key}: nightly price changed. Refresh suggestions before pushing.")
        # Preserve other date-level restrictions when changing the price field.
        allowed = ("min_stay", "min_price", "min_price_type", "max_price", "max_price_type",
                   "base_price", "check_in_check_out_enabled", "check_in", "check_out", "lead_time_expiry")
        item = {k: prior[k] for k in allowed if k in prior and prior[k] is not None}
        item.update(date=key, price=float(target), price_type="fixed", currency="USD", reason="Approved Pulse increase")
        items.append(item)
    sent = {"pms": pms, "overrides": items, "update_children": False}
    if items:
        client.request("POST", endpoint, sent)
    confirmation = client.request("GET", endpoint, params={"pms": pms})
    confirmed = {x.get("date"): x for x in confirmation.get("overrides", []) if isinstance(x, dict)}
    if any(confirmed.get(k, {}).get("price_type") != "fixed" or money(confirmed.get(k, {}).get("price")) != v for k, v in targets.items()):
        raise PulsePushError("PriceLabs did not confirm every target. Some changes may be saved; verify before retrying.")
    refresh = {"configured": True, "message": "Overrides saved; price refresh not yet confirmed."}
    try:
        refreshed = client.request("POST", "/refresh_listing", {"listing_id": lid, "pms": pms})
        actual = {x.get("date"): money(x.get("price")) for x in refreshed.get("pricing_array", [])}
        verified = all(actual.get(k) == v for k, v in targets.items())
        refresh["message"] = ("PriceLabs refreshed and confirmed all nightly targets." if verified else
                              "Overrides saved, but refreshed nightly prices differ. Verify in PriceLabs.")
    except Exception as exc:
        verified = False
        refresh["message"] = f"Overrides saved; refresh failed: {exc}. Use Save & Refresh in PriceLabs."
    return {"provider": "PriceLabs", "listing_id": lid, "pms": pms, "endpoint": endpoint,
            "sent": sent if items else None, "confirmed_dates": sorted(targets), "idempotent": not items,
            "pulse_push": True, "post_apply": {"refresh": refresh,
            "verified_sync": {"verified": verified, "message": refresh["message"]}}}
