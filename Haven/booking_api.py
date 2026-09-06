#!/usr/bin/env python3
"""
Booking.com Connectivity Promotions API helper.

Uses token-based machine-account authentication and sends B.XML requests to
the /hotels/xml/promotions endpoint. Pushes are intentionally narrow: callers
must provide hotel_id, parent rate IDs, and either room IDs or a promotion type
that does not require explicit rooms.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import requests

AUTH_URL = "https://connectivity-authentication.booking.com/token-based-authentication/exchange"
PROMOTIONS_URL = "https://supply-xml.booking.com/hotels/xml/promotions"


class BookingAPIError(RuntimeError):
    pass


@dataclass
class BookingToken:
    jwt: str
    expires_at: float


class BookingClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        auth_url: str | None = None,
        promotions_url: str | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_url = auth_url or os.environ.get("BOOKING_AUTH_URL") or AUTH_URL
        self.promotions_url = promotions_url or os.environ.get("BOOKING_PROMOTIONS_URL") or PROMOTIONS_URL
        self._token: BookingToken | None = None

    def token(self) -> str:
        if self._token and time.time() < self._token.expires_at - 120:
            return self._token.jwt
        try:
            resp = requests.post(
                self.auth_url,
                json={"client_id": self.client_id, "client_secret": self.client_secret},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=45,
            )
        except requests.RequestException as e:
            raise BookingAPIError(f"Booking.com token request failed: {e}") from e
        if resp.status_code >= 400:
            raise BookingAPIError(f"Booking.com auth {resp.status_code}: {(resp.text or '')[:500]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise BookingAPIError("Booking.com auth returned a non-JSON response.") from e
        jwt = data.get("jwt")
        if not jwt:
            raise BookingAPIError("Booking.com auth response did not include a JWT.")
        self._token = BookingToken(jwt=jwt, expires_at=time.time() + 3600)
        return jwt

    def request(self, method: str, url: str, xml_body: str | None = None) -> ET.Element:
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/xml",
            "Accept": "application/xml",
            "User-Agent": "Haven-Booking-Promo-Lab/1.0",
        }
        try:
            resp = requests.request(method.upper(), url, data=xml_body, headers=headers, timeout=60)
        except requests.RequestException as e:
            raise BookingAPIError(f"Booking.com API request failed: {e}") from e
        text = resp.text or ""
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 403:
                hint = " Machine account may lack Promotions API permissions."
            elif resp.status_code == 401:
                hint = " Check client credentials, token status, and machine-account access."
            raise BookingAPIError(f"Booking.com API {resp.status_code}: {text[:500]}{hint}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise BookingAPIError(f"Booking.com returned non-XML response: {text[:500]}") from e
        fault = root.find(".//fault")
        if fault is not None:
            code = fault.attrib.get("code", "")
            detail = fault.attrib.get("string") or "".join(fault.itertext()).strip()
            raise BookingAPIError(f"Booking.com fault {code}: {detail}")
        return root

    def create_promotion(self, xml_body: str) -> dict[str, Any]:
        root = self.request("POST", self.promotions_url, xml_body)
        ids = [node.text for node in root.findall(".//id") if node.text]
        return {"promotion_ids": ids, "raw_xml": ET.tostring(root, encoding="unicode")}


def client_from_env() -> BookingClient:
    client_id = os.environ.get("BOOKING_CLIENT_ID")
    client_secret = os.environ.get("BOOKING_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise BookingAPIError("Set BOOKING_CLIENT_ID and BOOKING_CLIENT_SECRET for Booking.com Connectivity API.")
    return BookingClient(client_id=client_id, client_secret=client_secret)


def _xml_attr(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_text(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_promotion_xml(promo: dict[str, Any]) -> str:
    hotel_id = str(promo.get("booking_hotel_id") or promo.get("booking_id") or "").strip()
    discount = int(float(promo.get("discount_pct") or 0))
    if not hotel_id:
        raise BookingAPIError("Booking.com hotel_id is missing.")
    if discount <= 0:
        raise BookingAPIError("Discount must be greater than 0 before pushing to Booking.com.")

    raw_type = str(promo.get("promotion_type") or "").strip()
    type_map = {
        "basic_deal": "basic",
        "limited_time_deal": "basic",
        "last_minute_deal": "last_minute",
        "early_booker_deal": "early_booker",
        "mobile_rate": "mobile_rate",
        "country_rate": "geo_rate",
    }
    booking_type = type_map.get(raw_type)
    if not booking_type:
        raise BookingAPIError(f"Unsupported Booking.com promotion type for API push: {raw_type}")

    if booking_type == "mobile_rate" and discount < 10:
        raise BookingAPIError("Booking.com mobile rates require at least a 10% discount.")
    if booking_type == "geo_rate" and not 5 <= discount <= 30:
        raise BookingAPIError("Booking.com country rates require a 5%-30% discount.")

    room_ids = [str(v).strip() for v in promo.get("booking_room_ids", []) if str(v).strip()]
    parent_rate_ids = [str(v).strip() for v in promo.get("booking_parent_rate_ids", []) if str(v).strip()]
    if booking_type in {"basic", "last_minute", "early_booker"} and not room_ids:
        raise BookingAPIError("Booking.com room IDs are required for this promotion type.")
    if not parent_rate_ids:
        raise BookingAPIError("Booking.com parent rate IDs are required.")

    target_channel = "public"
    if booking_type == "mobile_rate":
        target_channel = "all" if promo.get("audience") in {"mobile", "all_mobile"} else "app"
    elif booking_type == "geo_rate":
        target_channel = str(promo.get("booking_target_channel") or "us_pos")
    elif promo.get("audience") == "subscribers":
        target_channel = "subscribers"

    attrs = [
        f'type="{_xml_attr(booking_type)}"',
        f'target_channel="{_xml_attr(target_channel)}"',
        'min_stay_through="0"',
        'non_refundable="0"',
    ]
    if booking_type in {"basic", "last_minute", "early_booker"}:
        name = f"Haven {promo.get('display_name') or promo.get('property') or 'Promo'}"
        attrs.insert(0, f'name="{_xml_attr(name[:255])}"')

    lines = ["<request>", f"  <hotel_id>{_xml_text(hotel_id)}</hotel_id>", f"  <promotion {' '.join(attrs)}>"]
    if booking_type == "basic":
        lines.append(f'    <book_date start="{_xml_attr(promo["book_start_date"])}" end="{_xml_attr(promo["book_end_date"])}" />')
    if booking_type == "last_minute":
        lines.append('    <last_minute unit="day" value="3"/>')
    if booking_type == "early_booker":
        lines.append('    <early_booker value="30"/>')
    lines.append(f'    <stay_date start="{_xml_attr(promo["stay_start_date"])}" end="{_xml_attr(promo["stay_end_date"])}" />')
    if room_ids:
        lines.append("    <rooms>")
        lines.extend(f'      <room id="{_xml_attr(room_id)}"/>' for room_id in room_ids)
        lines.append("    </rooms>")
    lines.append("    <parent_rates>")
    lines.extend(f'      <parent_rate id="{_xml_attr(rate_id)}"/>' for rate_id in parent_rate_ids)
    lines.append("    </parent_rates>")
    lines.append(f'    <discount value="{discount}" />')
    lines.append("  </promotion>")
    lines.append("</request>")
    return "\n".join(lines)
