#!/usr/bin/env python3
from __future__ import annotations

import json
import re

from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
    page = browser.contexts[0].pages[0]
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    links = page.locator("a").evaluate_all(
        """els => els.slice(0, 250).map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href
        })).filter(x => x.text || x.href)"""
    )
    matches = [
        item for item in links
        if re.search(r"promo|deal|opportun|rate|visibility|calendar", item["text"] + " " + item["href"], re.I)
    ]
    print(json.dumps({"url": page.url, "title": page.title(), "matches": matches[:50]}, indent=2))
    browser.close()
