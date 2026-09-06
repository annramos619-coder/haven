#!/usr/bin/env python3
from __future__ import annotations

from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
    page = browser.contexts[0].pages[0]
    if "sign-in" in page.url:
        page.goto("https://admin.booking.com/hotel/hoteladmin/groups/home/index.html", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    links = page.locator("a").evaluate_all(
        """els => els.map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href
        }))"""
    )
    target = next((item["href"] for item in links if "Group Opportunity Center" in item["text"]), None)
    print("target", target)
    if target:
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    print(page.url)
    print(page.title())
    print(page.locator("body").inner_text()[:2000])
    browser.close()
