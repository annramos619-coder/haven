#!/usr/bin/env python3
"""
Market weather monitor — checks the National Weather Service (api.weather.gov,
free, no API key) for active alerts and the near-term forecast in each of the
portfolio's markets, and flags severe conditions (winter storms, extreme heat)
that could affect guest travel, bookings, or on-the-ground operations.

NWS requires a descriptive User-Agent identifying the application; no key or
account is needed. Two calls per market:
  1. GET /alerts/active?point={lat},{lon}  -> any currently active alerts
  2. GET /points/{lat},{lon} -> resolves to a forecast URL -> next few periods

This module makes real outbound HTTPS calls, so it only works wherever the
Flask app is actually running with normal internet access.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib import error, request

NWS_BASE = "https://api.weather.gov"
USER_AGENT = "HavenVacationRentalsDashboard/1.0 (internal ops tool)"
CACHE_PATH = Path(__file__).parent / "weather_cache.json"
CACHE_TTL_SECONDS = 3600  # 1 hour — NWS updates forecasts a few times a day

# Approximate town-center coordinates for each market. Precision only needs
# to land in the right NWS forecast zone/office, not survey-grade accuracy.
MARKET_LOCATIONS: dict[str, tuple[float, float]] = {
    "Sevierville":   (35.8687, -83.5618),
    "Gatlinburg":    (35.7143, -83.5102),
    "Pigeon Forge":  (35.7884, -83.5543),
    "Knoxville":     (35.9606, -83.9207),
    "Townsend":      (35.6743, -83.7101),
    "Dandridge":     (36.0176, -83.4218),
    "Maryville":     (35.7565, -83.9705),
    "Walland":       (35.7212, -83.8391),
    "Cosby":         (35.7626, -83.2116),
    "Seymour":       (35.8626, -83.7157),
    "Pittman Center": (35.7357, -83.3949),
}

# Alert event-type substrings that should raise a hard flag on the dashboard.
SNOW_ALERT_KEYWORDS = ("winter storm", "blizzard", "ice storm", "winter weather", "snow squall", "freeze")
HEAT_ALERT_KEYWORDS = ("excessive heat", "heat advisory")

HEAT_TEMP_THRESHOLD_F = 95   # forecast high at/above this -> extreme heat flag
COLD_TEMP_THRESHOLD_F = 20   # forecast low at/below this -> hard freeze flag
SNOW_FORECAST_KEYWORDS = ("snow", "ice", "sleet", "wintry mix")


class WeatherAPIError(RuntimeError):
    pass


def _request_json(url: str) -> dict[str, Any]:
    req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
    except error.HTTPError as e:
        raise WeatherAPIError(f"NWS API {e.code} for {url}") from e
    except error.URLError as e:
        raise WeatherAPIError(f"NWS API connection failed: {e.reason}") from e
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise WeatherAPIError("NWS API returned a non-JSON response.") from e


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


def _fetch_market(city: str, lat: float, lon: float) -> dict:
    """Fetch active alerts + near-term forecast for one market. Raises
    WeatherAPIError on failure — caller decides how to degrade."""
    alerts_resp = _request_json(f"{NWS_BASE}/alerts/active?point={lat},{lon}")
    alerts = []
    for feature in (alerts_resp.get("features") or [])[:10]:
        props = feature.get("properties") or {}
        alerts.append({
            "event": props.get("event") or "",
            "severity": props.get("severity") or "",
            "headline": props.get("headline") or "",
            "effective": props.get("effective") or "",
            "expires": props.get("expires") or "",
        })

    points_resp = _request_json(f"{NWS_BASE}/points/{lat},{lon}")
    forecast_url = (points_resp.get("properties") or {}).get("forecast")
    periods = []
    if forecast_url:
        forecast_resp = _request_json(forecast_url)
        for p in (forecast_resp.get("properties") or {}).get("periods", [])[:6]:
            periods.append({
                "name": p.get("name") or "",
                "temperature": p.get("temperature"),
                "temperature_unit": p.get("temperatureUnit") or "F",
                "short_forecast": p.get("shortForecast") or "",
                "is_daytime": p.get("isDaytime"),
            })

    return {"city": city, "alerts": alerts, "forecast": periods, "fetched_at": int(time.time())}


def _classify(market: dict) -> dict:
    """Decide the flag level for one market's alerts + forecast."""
    flags = []
    for a in market.get("alerts", []):
        event_low = (a.get("event") or "").lower()
        if any(k in event_low for k in SNOW_ALERT_KEYWORDS):
            flags.append({"type": "snow", "source": "alert", "detail": a.get("headline") or a.get("event")})
        elif any(k in event_low for k in HEAT_ALERT_KEYWORDS):
            flags.append({"type": "heat", "source": "alert", "detail": a.get("headline") or a.get("event")})

    for p in market.get("forecast", []):
        temp = p.get("temperature")
        short = (p.get("short_forecast") or "").lower()
        if temp is not None and p.get("temperature_unit", "F") == "F":
            if temp >= HEAT_TEMP_THRESHOLD_F:
                flags.append({"type": "heat", "source": "forecast", "detail": f"{p['name']}: {temp}°F — {p.get('short_forecast')}"})
            if temp <= COLD_TEMP_THRESHOLD_F:
                flags.append({"type": "cold", "source": "forecast", "detail": f"{p['name']}: {temp}°F — {p.get('short_forecast')}"})
        if any(k in short for k in SNOW_FORECAST_KEYWORDS):
            flags.append({"type": "snow", "source": "forecast", "detail": f"{p['name']}: {p.get('short_forecast')}"})

    level = "normal"
    if any(f["type"] == "snow" for f in flags):
        level = "snow"
    elif any(f["type"] == "heat" for f in flags):
        level = "heat"
    elif any(f["type"] == "cold" for f in flags):
        level = "cold"

    market["flags"] = flags
    market["level"] = level
    return market


def get_market_weather(force_refresh: bool = False) -> dict[str, Any]:
    """Return weather + flags for every market, using a 1-hour cache so the
    dashboard doesn't hammer the NWS API on every page load."""
    cache = _load_cache()
    now = time.time()
    results = {}
    errors = {}

    for city, (lat, lon) in MARKET_LOCATIONS.items():
        cached = cache.get(city)
        if not force_refresh and cached and (now - cached.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            results[city] = _classify(cached)
            continue
        try:
            market = _fetch_market(city, lat, lon)
            results[city] = _classify(market)
            cache[city] = market
        except WeatherAPIError as e:
            errors[city] = str(e)
            if cached:
                results[city] = _classify(cached)  # serve stale data over nothing

    _save_cache(cache)
    return {"markets": results, "errors": errors, "checked_at": int(now)}
