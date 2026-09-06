#!/usr/bin/env python3
"""Pull PriceLabs Report Builder template data.

Report Builder is the source behind Haven's weekly scorecard. This fetches a
saved template directly so the weekly report does not need a manual Excel
upload.

Two paths, tried in order:

1. The PriceLabs API, if the account's key can reach Report Builder. The
   endpoint path differs by account tier, so both the data and poll paths are
   overridable via PRICELABS_REPORT_BUILDER_PATH / _POLL_PATH.
2. A CSV cache on disk (report_builder_<template_id>.csv). Written on every
   successful API pull, and used as the fallback when the API is unavailable -
   so a cache exported another way (including by Claude via the PriceLabs MCP
   connector) works exactly the same.

Known template IDs on Haven's account:
    1158  Jordan's Revenue Tracker      all 296 listings, trailing 30 days
    3089  Noeline Daily Tracking Scorecard   1-4BR Cabins only, 31 days,
                                             but carries STLY / ADR / MPI
    3199  Knoxville Scorecard
    1324  Revenue On The Books - Knoxville (AS)

1158 is the only one covering the whole portfolio, which is why it is the
default here. It does NOT carry STLY, ADR or MPI, so year-over-year figures
still require 3089 or a manual scorecard.
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Any

from pricelabs_api import PriceLabsAPIError, client_from_env

HERE = Path(__file__).resolve().parent

DEFAULT_TEMPLATE_ID = int(os.environ.get("PRICELABS_REPORT_TEMPLATE_ID", "1158"))

DATA_PATH = os.environ.get("PRICELABS_REPORT_BUILDER_PATH", "/report_builder/data")
POLL_PATH = os.environ.get("PRICELABS_REPORT_BUILDER_POLL_PATH", "/report_builder/poll")

POLL_INTERVAL_SECONDS = 4
POLL_MAX_ATTEMPTS = 30


def cache_path(template_id: int) -> Path:
    return HERE / f"report_builder_{template_id}.csv"


def _extract_rows(body: Any) -> list[dict] | None:
    """Find report_data in a response, whatever depth it is nested at."""
    if isinstance(body, dict):
        if isinstance(body.get("report_data"), list):
            return body["report_data"]
        for key in ("data", "result"):
            nested = body.get(key)
            found = _extract_rows(nested)
            if found is not None:
                return found
    return None


def _request_id(body: Any) -> str | None:
    if isinstance(body, dict):
        if isinstance(body.get("request_id"), str):
            return body["request_id"]
        for key in ("data", "result"):
            found = _request_id(body.get(key))
            if found:
                return found
    return None


def fetch_from_api(template_id: int = DEFAULT_TEMPLATE_ID) -> list[dict]:
    """Run a Report Builder template and return its rows.

    Raises PriceLabsAPIError if the account cannot reach Report Builder, if the
    job never completes, or if the response shape is unrecognised.
    """
    client = client_from_env()
    body = client.request("POST", DATA_PATH, payload={"template_id": template_id})

    rows = _extract_rows(body)
    if rows is not None:
        return rows

    request_id = _request_id(body)
    if not request_id:
        raise PriceLabsAPIError(
            f"Report Builder at {DATA_PATH} returned neither rows nor a request_id. "
            "Set PRICELABS_REPORT_BUILDER_PATH if your account uses a different endpoint."
        )

    for _ in range(POLL_MAX_ATTEMPTS):
        time.sleep(POLL_INTERVAL_SECONDS)
        poll = client.request("POST", POLL_PATH, payload={"request_id": request_id})
        rows = _extract_rows(poll)
        if rows is not None:
            return rows
        status = ""
        if isinstance(poll, dict):
            status = str(poll.get("status") or (poll.get("data") or {}).get("status") or "")
        if status.upper() == "STATUS_NOT_FOUND":
            raise PriceLabsAPIError(
                f"Report Builder request {request_id} expired before returning data."
            )

    raise PriceLabsAPIError(
        f"Report Builder template {template_id} did not finish within "
        f"{POLL_INTERVAL_SECONDS * POLL_MAX_ATTEMPTS}s."
    )


def write_cache(rows: list[dict], template_id: int = DEFAULT_TEMPLATE_ID) -> Path:
    """Save rows as CSV so a later run works without the API."""
    path = cache_path(template_id)
    if not rows:
        return path
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return path


def read_cache(template_id: int = DEFAULT_TEMPLATE_ID) -> list[dict]:
    """Load a previously written CSV cache. Returns [] when absent."""
    path = cache_path(template_id)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # CSV loses types; restore numbers so callers can do arithmetic.
    for row in rows:
        for key, value in list(row.items()):
            if value in ("", None):
                row[key] = None
                continue
            try:
                row[key] = int(value)
            except (TypeError, ValueError):
                try:
                    row[key] = float(value)
                except (TypeError, ValueError):
                    pass
    return rows


def load(template_id: int = DEFAULT_TEMPLATE_ID) -> tuple[list[dict], str]:
    """Rows plus a one-line description of where they came from.

    Tries the API, falls back to the CSV cache. Never raises for an unreachable
    API - the caller decides how to present a stale or missing source.
    """
    try:
        rows = fetch_from_api(template_id)
    except Exception as e:
        cached = read_cache(template_id)
        path = cache_path(template_id)
        if cached:
            age_days = (time.time() - path.stat().st_mtime) / 86400
            return cached, (
                f"cache {path.name}, {age_days:.1f} days old "
                f"(live pull failed: {e})"
            )
        return [], f"unavailable - live pull failed and no cache at {path.name}: {e}"

    write_cache(rows, template_id)
    return rows, f"PriceLabs Report Builder template {template_id} (live)"
