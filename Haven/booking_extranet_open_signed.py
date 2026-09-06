#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys

from playwright.sync_api import sync_playwright

needle = sys.argv[1] if len(sys.argv) > 1 else "Strategic earning"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
    page = browser.contexts[0].pages[0]
    links = page.locator("a").evaluate_all(
        """els => els.map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href
        })).filter(x => x.text || x.href)"""
    )
    target = next((item["href"] for item in links if needle.lower() in item["text"].lower()), None)
    print("target", target)
    if target:
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
    body = page.locator("body").inner_text()
    new_links = page.locator("a").evaluate_all(
        """els => els.map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href
        })).filter(x => x.text || x.href)"""
    )
    matches = [
        item for item in new_links
        if re.search(r"promo|deal|opportun|rate|earning|visibility|campaign|discount", item["text"] + " " + item["href"], re.I)
    ]
    print(json.dumps({
        "url": page.url,
        "title": page.title(),
        "body": body[:2500],
        "matches": matches[:80],
    }, indent=2))
    browser.close()
