"""Server-only Supabase persistence helper for the Revven.V2 dashboard.

Reads/writes are confined to the isolated ``revven`` Postgres schema and the
``revven-uploads`` storage bucket in the existing HavenOS Supabase project.
This module never touches HavenOS public tables.

Design goals:
- Zero new third-party Python dependencies (uses ``requests``).
- Server-only: requires ``SUPABASE_SERVICE_ROLE_KEY``; otherwise every helper
  reports ``is_enabled() == False`` and the caller must fall back to the
  bundled JSON/CSV files.
- Best-effort: any HTTP or JSON error is swallowed and surfaced via the
  ``last_error`` accessor; callers keep working off the local snapshot.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Iterable

import requests


SCHEMA = "revven"
UPLOADS_BUCKET = "revven-uploads"

# Logical table names within the ``revven`` schema.
TABLE_PRICING_ACTIONS = "pricing_actions"
TABLE_BOOKING_PROMOTIONS = "booking_promotions"
TABLE_PRICELABS_SNAPSHOTS = "pricelabs_snapshots"
TABLE_HEALTHZ = "healthz"


_last_error_lock = threading.Lock()
_last_error: str | None = None


def _set_last_error(message: str | None) -> None:
    global _last_error
    with _last_error_lock:
        _last_error = message


def last_error() -> str | None:
    """Most recent error message from a Supabase call, or ``None``."""
    with _last_error_lock:
        return _last_error


def _url() -> str | None:
    value = (os.environ.get("SUPABASE_URL") or "").strip()
    return value.rstrip("/") if value else None


def _service_key() -> str | None:
    value = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return value or None


def is_enabled() -> bool:
    """True only when both URL and service-role key are configured."""
    return bool(_url() and _service_key())


def _rest_headers(extra: dict | None = None) -> dict:
    key = _service_key() or ""
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept-Profile": SCHEMA,
        "Content-Profile": SCHEMA,
    }
    if extra:
        headers.update(extra)
    return headers


def _rest_endpoint(table: str) -> str | None:
    base = _url()
    if not base:
        return None
    return f"{base}/rest/v1/{table}"


def _storage_object_url(path: str) -> str | None:
    base = _url()
    if not base:
        return None
    safe_path = path.lstrip("/")
    return f"{base}/storage/v1/object/{UPLOADS_BUCKET}/{safe_path}"


def healthz() -> dict:
    """Run a lightweight read against ``revven.healthz``.

    Returns ``{"ok": bool, "configured": bool, "rows": int, "error": str|None}``.
    Never exposes service-role key contents.
    """
    if not is_enabled():
        return {
            "ok": False,
            "configured": False,
            "rows": 0,
            "error": "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not configured",
        }
    endpoint = _rest_endpoint(TABLE_HEALTHZ)
    if not endpoint:
        return {"ok": False, "configured": True, "rows": 0, "error": "missing endpoint"}
    try:
        response = requests.get(
            endpoint,
            headers=_rest_headers({"Range": "0-0"}),
            params={"select": "*"},
            timeout=8,
        )
    except requests.RequestException as exc:
        _set_last_error(str(exc))
        return {"ok": False, "configured": True, "rows": 0, "error": str(exc)}
    if response.status_code >= 400:
        msg = f"HTTP {response.status_code}: {response.text[:200]}"
        _set_last_error(msg)
        return {"ok": False, "configured": True, "rows": 0, "error": msg}
    try:
        payload = response.json()
        rows = len(payload) if isinstance(payload, list) else 0
    except ValueError:
        rows = 0
    _set_last_error(None)
    return {"ok": True, "configured": True, "rows": rows, "error": None}


def select_rows(table: str, *, order: str | None = None, limit: int | None = None) -> list[dict] | None:
    """Fetch all rows from a ``revven`` table; returns ``None`` on any failure."""
    if not is_enabled():
        return None
    endpoint = _rest_endpoint(table)
    if not endpoint:
        return None
    params: dict[str, str] = {"select": "*"}
    if order:
        params["order"] = order
    if limit:
        params["limit"] = str(limit)
    try:
        response = requests.get(endpoint, headers=_rest_headers(), params=params, timeout=15)
    except requests.RequestException as exc:
        _set_last_error(str(exc))
        return None
    if response.status_code >= 400:
        _set_last_error(f"HTTP {response.status_code}: {response.text[:200]}")
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        _set_last_error(f"invalid JSON: {exc}")
        return None
    if not isinstance(payload, list):
        _set_last_error("unexpected payload shape")
        return None
    _set_last_error(None)
    return payload


def _row_field(row: dict, *names: str) -> Any:
    """Return the first non-None value among ``names`` in ``row``."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _hydrate_action(row: dict) -> dict:
    """Unwrap a ``pricing_actions`` row into the legacy JSON-action shape."""
    payload = _row_field(row, "payload", "data") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Database-level columns win over payload duplicates where present.
    merged = dict(payload)
    for key in ("id", "status", "reviewed_at", "applied_at"):
        value = row.get(key)
        if value is not None:
            merged.setdefault(key, value)
    return merged


def load_pricing_actions() -> list[dict] | None:
    """Return pricing actions from ``revven.pricing_actions`` or ``None``."""
    rows = select_rows(TABLE_PRICING_ACTIONS, order="created_at.asc")
    if rows is None:
        return None
    return [_hydrate_action(r) for r in rows]


def _delete_all(table: str) -> bool:
    if not is_enabled():
        return False
    endpoint = _rest_endpoint(table)
    if not endpoint:
        return False
    try:
        response = requests.delete(
            endpoint,
            headers=_rest_headers({"Prefer": "return=minimal"}),
            # PostgREST requires an explicit filter on DELETE; ``not.is.null``
            # on the always-present ``id`` column matches every row.
            params={"id": "not.is.null"},
            timeout=15,
        )
    except requests.RequestException as exc:
        _set_last_error(str(exc))
        return False
    if response.status_code >= 400:
        _set_last_error(f"HTTP {response.status_code}: {response.text[:200]}")
        return False
    return True


def _upsert_rows(table: str, rows: Iterable[dict]) -> bool:
    rows_list = list(rows)
    if not is_enabled() or not rows_list:
        return False
    endpoint = _rest_endpoint(table)
    if not endpoint:
        return False
    try:
        response = requests.post(
            endpoint,
            headers=_rest_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            data=json.dumps(rows_list),
            timeout=30,
        )
    except requests.RequestException as exc:
        _set_last_error(str(exc))
        return False
    if response.status_code >= 400:
        _set_last_error(f"HTTP {response.status_code}: {response.text[:200]}")
        return False
    _set_last_error(None)
    return True


def _action_row(action: dict) -> dict:
    return {
        "id": action.get("id"),
        "status": action.get("status"),
        "reviewed_at": action.get("reviewed_at"),
        "applied_at": action.get("applied_at"),
        "payload": action,
    }


def save_pricing_actions(actions: list[dict]) -> bool:
    """Replace all rows in ``revven.pricing_actions`` with the given actions."""
    if not is_enabled():
        return False
    if not _delete_all(TABLE_PRICING_ACTIONS):
        return False
    if not actions:
        return True
    return _upsert_rows(TABLE_PRICING_ACTIONS, (_action_row(a) for a in actions))


def _promotion_row(promo: dict) -> dict:
    return {
        "id": promo.get("id"),
        "property": promo.get("property"),
        "status": promo.get("status"),
        "reviewed_at": promo.get("reviewed_at"),
        "pushed_at": promo.get("pushed_at"),
        "payload": promo,
    }


def _hydrate_promotion(row: dict) -> dict:
    payload = _row_field(row, "payload", "data") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    merged = dict(payload)
    for key in ("id", "property", "status", "reviewed_at", "pushed_at"):
        value = row.get(key)
        if value is not None:
            merged.setdefault(key, value)
    return merged


def load_booking_promotions() -> list[dict] | None:
    rows = select_rows(TABLE_BOOKING_PROMOTIONS, order="created_at.asc")
    if rows is None:
        return None
    return [_hydrate_promotion(r) for r in rows]


def save_booking_promotions(promotions: list[dict]) -> bool:
    if not is_enabled():
        return False
    if not _delete_all(TABLE_BOOKING_PROMOTIONS):
        return False
    if not promotions:
        return True
    return _upsert_rows(TABLE_BOOKING_PROMOTIONS, (_promotion_row(p) for p in promotions))


def save_pricelabs_snapshot(response: dict) -> bool:
    """Append a PriceLabs API snapshot to ``revven.pricelabs_snapshots``."""
    if not is_enabled() or not isinstance(response, dict):
        return False
    listings = response.get("listings") if isinstance(response, dict) else None
    row = {
        "source": "pricelabs_customer_api",
        "listings_count": len(listings) if isinstance(listings, list) else None,
        "payload": response,
    }
    return _upsert_rows(TABLE_PRICELABS_SNAPSHOTS, [row])


def load_latest_pricelabs_snapshot() -> dict | None:
    """Return the most recent snapshot ``payload`` dict, or ``None``."""
    rows = select_rows(TABLE_PRICELABS_SNAPSHOTS, order="created_at.desc", limit=1)
    if not rows:
        return None
    payload = _row_field(rows[0], "payload", "data")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def upload_csv(path: str, content: bytes, *, content_type: str = "text/csv") -> bool:
    """Upload bytes to ``revven-uploads/<path>``; ``True`` only on 2xx."""
    if not is_enabled():
        return False
    endpoint = _storage_object_url(path)
    if not endpoint:
        return False
    headers = {
        "apikey": _service_key() or "",
        "Authorization": f"Bearer {_service_key() or ''}",
        "Content-Type": content_type,
        # Allow overwriting an existing object at the same path.
        "x-upsert": "true",
    }
    try:
        response = requests.post(endpoint, headers=headers, data=content, timeout=60)
    except requests.RequestException as exc:
        _set_last_error(str(exc))
        return False
    if response.status_code >= 400:
        _set_last_error(f"HTTP {response.status_code}: {response.text[:200]}")
        return False
    _set_last_error(None)
    return True
