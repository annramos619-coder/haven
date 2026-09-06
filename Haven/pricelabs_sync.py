#!/usr/bin/env python3
"""
PriceLabs auto-sync — logs in and downloads:
  • Portfolio Analytics CSV  → portfolio_analytics.csv
  • Market Dashboard CSV     → market_dashboard.csv
  • Neighborhood Data CSV    → neighborhood_data.csv

Then POSTs them to the running dashboard to hot-reload.

Usage:
  python3 pricelabs_sync.py               # download + reload dashboard
  python3 pricelabs_sync.py --no-reload   # download only

Credentials read from env vars:
  PRICELABS_EMAIL
  PRICELABS_PASSWORD
"""

import argparse
import os
import shutil
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── Config ──────────────────────────────────────────────────────────────────
PROJECT_DIR   = Path(__file__).parent
DOWNLOAD_DIR  = PROJECT_DIR / "_pl_downloads"
DASHBOARD_URL = "http://localhost:8080"

PRICELABS_URL      = "https://app.pricelabs.co"
LOGIN_URL          = f"{PRICELABS_URL}/login"
PORTFOLIO_URL      = f"{PRICELABS_URL}/portfolio-analytics"
MARKET_URL         = f"{PRICELABS_URL}/market-dashboard"
NEIGHBORHOOD_URL   = f"{PRICELABS_URL}/neighborhood-data"

# Output filenames (matched by dashboard reload endpoint)
OUTPUTS = {
    "portfolio":    PROJECT_DIR / "pricelabs_portfolio.csv",
    "market":       PROJECT_DIR / "pricelabs_market.csv",
    "neighborhood": PROJECT_DIR / "pricelabs_neighborhood.csv",
}


def wait_for_manual_login(page, start_url: str | None = None):
    print("  Opening PriceLabs for manual login...")
    page.goto(start_url or LOGIN_URL)
    print("  Please log in manually in the browser window.")
    print("  If this URL is wrong, paste the correct PriceLabs URL into the browser address bar.")
    input("  When you can see PriceLabs logged in, press Enter here to continue...")
    page.wait_for_load_state("networkidle", timeout=30000)
    print(f"  Continuing from: {page.url}")


def login(page, email: str, password: str):
    print("  Logging into PriceLabs…")
    page.goto(LOGIN_URL)
    page.wait_for_load_state("networkidle")

    # Fill login form
    page.fill('input[type="email"], input[name="email"], #email', email)
    page.fill('input[type="password"], input[name="password"], #password', password)
    if not page.input_value('input[type="password"], input[name="password"], #password').strip():
        raise RuntimeError("Password field is empty. Set PRICELABS_PASSWORD before running sync.")

    for selector in [
        'button[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'input[type="submit"]',
    ]:
        buttons = page.query_selector_all(selector)
        clicked = False
        for btn in buttons:
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                clicked = True
                break
        if clicked:
            break
    else:
        page.screenshot(path=str(PROJECT_DIR / "_pl_login_button.png"))
        raise RuntimeError("Visible PriceLabs sign-in button not found. Screenshot saved to _pl_login_button.png")

    # Wait for redirect away from login
    login_wait_seconds = int(os.environ.get("PRICELABS_LOGIN_WAIT_SECONDS", "180"))
    print(f"  Waiting for login/MFA to finish for up to {login_wait_seconds}s...")
    page.wait_for_url(lambda url: "login" not in url, timeout=login_wait_seconds * 1000)
    print("  ✅ Logged in")


def download_portfolio_analytics(page, context, url: str | None = None) -> bool:
    """Download Portfolio Analytics export."""
    print("  Fetching Portfolio Analytics…")
    try:
        page.goto(url or PORTFOLIO_URL)
        page.wait_for_load_state("networkidle", timeout=20000)

        # Look for export/download button
        with page.expect_download(timeout=30000) as dl_info:
            # Try common export button patterns
            for selector in [
                'button:has-text("Export")',
                'button:has-text("Download")',
                'a:has-text("Export")',
                '[data-testid="export-btn"]',
                '.export-button',
            ]:
                btn = page.query_selector(selector)
                if btn:
                    btn.click()
                    break
            else:
                print("  ⚠️  Export button not found — screenshot saved to _pl_portfolio.png")
                page.screenshot(path=str(PROJECT_DIR / "_pl_portfolio.png"))
                return False

        download = dl_info.value
        download.save_as(str(OUTPUTS["portfolio"]))
        print(f"  ✅ Portfolio Analytics → {OUTPUTS['portfolio'].name}")
        return True

    except PWTimeout:
        page.screenshot(path=str(PROJECT_DIR / "_pl_portfolio.png"))
        print("  ⚠️  Timed out — screenshot saved to _pl_portfolio.png")
        return False


def download_market_dashboard(page, context) -> bool:
    """Download Market Dashboard export."""
    print("  Fetching Market Dashboard…")
    try:
        page.goto(MARKET_URL)
        page.wait_for_load_state("networkidle", timeout=20000)

        with page.expect_download(timeout=30000) as dl_info:
            for selector in [
                'button:has-text("Export")',
                'button:has-text("Download")',
                'button:has-text("CSV")',
                'button:has-text("Excel")',
                '[data-testid="download-btn"]',
            ]:
                btn = page.query_selector(selector)
                if btn:
                    btn.click()
                    break
            else:
                page.screenshot(path=str(PROJECT_DIR / "_pl_market.png"))
                print("  ⚠️  Download button not found — screenshot saved to _pl_market.png")
                return False

        download = dl_info.value
        download.save_as(str(OUTPUTS["market"]))
        print(f"  ✅ Market Dashboard → {OUTPUTS['market'].name}")
        return True

    except PWTimeout:
        page.screenshot(path=str(PROJECT_DIR / "_pl_market.png"))
        print("  ⚠️  Timed out — screenshot saved")
        return False


def download_neighborhood_data(page, context) -> bool:
    """Download Neighborhood Data export."""
    print("  Fetching Neighborhood Data…")
    try:
        page.goto(NEIGHBORHOOD_URL)
        page.wait_for_load_state("networkidle", timeout=20000)

        with page.expect_download(timeout=30000) as dl_info:
            for selector in [
                'button:has-text("Export")',
                'button:has-text("Download")',
                'button:has-text("CSV")',
                '[aria-label*="export"]',
                '[aria-label*="download"]',
            ]:
                btn = page.query_selector(selector)
                if btn:
                    btn.click()
                    break
            else:
                page.screenshot(path=str(PROJECT_DIR / "_pl_neighborhood.png"))
                print("  ⚠️  Download button not found — screenshot saved to _pl_neighborhood.png")
                return False

        download = dl_info.value
        download.save_as(str(OUTPUTS["neighborhood"]))
        print(f"  ✅ Neighborhood Data → {OUTPUTS['neighborhood'].name}")
        return True

    except PWTimeout:
        page.screenshot(path=str(PROJECT_DIR / "_pl_neighborhood.png"))
        print("  ⚠️  Timed out — screenshot saved")
        return False


def reload_dashboard(downloaded: list[str]):
    """POST downloaded files to the running dashboard."""
    print("\n  Reloading dashboard…")
    files = {}
    file_handles = []

    mapping = {
        "portfolio":    ("pricelabs_csv", OUTPUTS["portfolio"]),
        "market":       ("pricelabs_market_csv", OUTPUTS["market"]),
        "neighborhood": ("pricelabs_neighborhood_csv", OUTPUTS["neighborhood"]),
    }

    for key in downloaded:
        field, path = mapping[key]
        if path.exists():
            fh = open(path, "rb")
            file_handles.append(fh)
            files[field] = (path.name, fh, "text/csv")

    if not files:
        print("  ⚠️  No files to reload")
        return

    try:
        r = requests.post(f"{DASHBOARD_URL}/api/reload", files=files, timeout=30)
        data = r.json()
        if data.get("ok"):
            s = data.get("summary", {})
            print(f"  ✅ Dashboard reloaded — {s.get('total_active', '?')} active · {s.get('critical_count', '?')} critical")
        else:
            print(f"  ⚠️  Reload response: {data}")
    except Exception as e:
        print(f"  ⚠️  Dashboard not running or reload failed: {e}")
    finally:
        for fh in file_handles:
            fh.close()


def run(no_reload=False, headless=True, manual_login=False, start_url: str | None = None, portfolio_url: str | None = None):
    email    = os.environ.get("PRICELABS_EMAIL")
    password = os.environ.get("PRICELABS_PASSWORD")

    if not manual_login and (not email or not password):
        print("ERROR: Set PRICELABS_EMAIL and PRICELABS_PASSWORD environment variables")
        print("  export PRICELABS_EMAIL='your@email.com'")
        print("  export PRICELABS_PASSWORD='yourpassword'")
        return

    print(f"\n{'='*55}")
    print("  PriceLabs Sync")
    print(f"{'='*55}")

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    downloaded = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        try:
            if manual_login:
                wait_for_manual_login(page, start_url)
            else:
                login(page, email, password)

            if download_portfolio_analytics(page, context, portfolio_url):
                downloaded.append("portfolio")

            if not manual_login and download_market_dashboard(page, context):
                downloaded.append("market")

            if not manual_login and download_neighborhood_data(page, context):
                downloaded.append("neighborhood")

        except Exception as e:
            print(f"\n  ❌ Error: {e}")
            page.screenshot(path=str(PROJECT_DIR / "_pl_error.png"))
            print("  Screenshot saved to _pl_error.png")
        finally:
            browser.close()

    print(f"\n  Downloaded: {downloaded or 'none'}")

    if downloaded and not no_reload:
        reload_dashboard(downloaded)

    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync PriceLabs exports to dashboard")
    parser.add_argument("--no-reload", action="store_true", help="Download only, don't reload dashboard")
    parser.add_argument("--visible",   action="store_true", help="Show browser window (for debugging)")
    parser.add_argument("--manual-login", action="store_true", help="Open PriceLabs and wait for you to log in manually")
    parser.add_argument("--start-url", default=os.environ.get("PRICELABS_START_URL", LOGIN_URL), help="URL to open for manual login")
    parser.add_argument("--portfolio-url", default=os.environ.get("PRICELABS_PORTFOLIO_URL", PORTFOLIO_URL), help="URL to use for Portfolio Analytics export")
    args = parser.parse_args()
    run(
        no_reload=args.no_reload,
        headless=not (args.visible or args.manual_login),
        manual_login=args.manual_login,
        start_url=args.start_url,
        portfolio_url=args.portfolio_url,
    )
