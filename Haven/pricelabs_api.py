#!/usr/bin/env python3
"""
PriceLabs Customer API helper.

This file only centralizes authentication and request handling. Endpoint-specific
methods should be added after confirming the exact PriceLabs API documentation
available for the client's account.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests


class PriceLabsAPIError(RuntimeError):
    pass


class PriceLabsClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("PRICELABS_API_BASE_URL") or "https://api.pricelabs.co/v1").rstrip("/")

    def request(
        self,
        method: str,
        path_or_url: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        elif self.base_url:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"
        else:
            raise PriceLabsAPIError(
                "Set PRICELABS_API_BASE_URL or pass a full endpoint URL from the PriceLabs API docs."
            )

        body = None
        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "HVR-Smokies-Dashboard/1.0 (+https://api.pricelabs.co)",
        }
        try:
            resp = requests.request(
                method.upper(), url, json=payload, params=params, headers=headers, timeout=45
            )
        except requests.RequestException as e:
            raise PriceLabsAPIError(f"PriceLabs API connection failed: {e}") from e

        text = resp.text or ""
        if resp.status_code >= 400:
            detail = text[:500]
            if "error-code: 1010" in detail.lower() or '"error_code":1010' in detail.lower():
                raise PriceLabsAPIError(
                    "PriceLabs/Cloudflare blocked the API request before it reached PriceLabs "
                    "(Cloudflare 1010 browser signature). This usually means the account/API route "
                    "needs PriceLabs support to allow server-side Customer API access from this machine/network."
                )
            raise PriceLabsAPIError(f"PriceLabs API {resp.status_code}: {detail}")

        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise PriceLabsAPIError("PriceLabs returned a non-JSON response.") from e


def client_from_env() -> PriceLabsClient:
    api_key = os.environ.get("PRICELABS_API_KEY")
    if not api_key:
        raise PriceLabsAPIError("Set PRICELABS_API_KEY before using the PriceLabs Customer API.")
    return PriceLabsClient(api_key=api_key)
