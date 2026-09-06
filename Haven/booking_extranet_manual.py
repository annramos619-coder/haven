#!/usr/bin/env python3
"""
Launch a persistent Booking.com Extranet browser session for manual login.

This is a bridge workflow for promotion work when Connectivity API access is
not available yet. The user logs in manually; the browser profile is reused so
Booking.com can keep the session/cookies between launches.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).parent / "_booking_extranet_profile"
START_URL = "https://admin.booking.com/"
REMOTE_DEBUGGING_PORT = 9223


def main() -> None:
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            args=["--start-maximized", f"--remote-debugging-port={REMOTE_DEBUGGING_PORT}"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        print("Booking.com Extranet browser is open.")
        print("Log in manually, then leave this process running while you work.")
        print(f"Automation bridge: http://127.0.0.1:{REMOTE_DEBUGGING_PORT}")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            context.close()


if __name__ == "__main__":
    main()
