#!/usr/bin/env python3
"""Generate the weekly revenue report from local dashboard data.

Usage:
    python weekly_report.py                  # writes ../weekly_reviews/<today>/weekly_report.html
    python weekly_report.py --open           # ...and opens it in the browser
    python weekly_report.py --pdf FILE.pdf   # also pull booking pickup from a Key Data PDF export
    python weekly_report.py --template 3089  # use a different Report Builder template
    python weekly_report.py --no-live        # local data only, no PriceLabs call

The sections this builds automatically:
  - Portfolio position (PriceLabs scorecard YTD / same-store)
  - Live PriceLabs pull: occupancy, RevPAR vs market, pickup, last-booked
  - Listing-level health, critical list, rate-increase candidates
  - Group breakdown, Demand Radar, pending action queue

The live pull uses Report Builder template 1158, the only template on the
account covering all listings. It falls back to a CSV cache when the API is
unreachable, and says which it used. That template carries no STLY, ADR or
MPI, so year-over-year still comes from the uploaded scorecard.

Market benchmarking (Haven vs market RevPAR and occupancy) is NOT built here: those
numbers live inside chart images in the Key Data PDF and cannot be read
programmatically. Booking pickup by group IS extracted when --pdf is supplied,
because those pages are real text tables.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import re
import statistics as st
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVIEWS = HERE.parent / "weekly_reviews"

sys.path.insert(0, str(HERE))

import wheelhouse_portfolio as wp  # noqa: E402

try:
    import pricelabs_report_builder as rb
except Exception:  # pragma: no cover - optional
    rb = None

try:
    import high_demand_calendar as hdc
except Exception:  # pragma: no cover - optional
    hdc = None


# --------------------------------------------------------------------------- data

def load_scorecard() -> dict | None:
    path = HERE / "ytd_weekly_history.json"
    if not path.exists():
        return None
    try:
        history = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return history[-1] if history else None


def is_blocked(p) -> bool:
    """Owner-blocked heuristic: full calendar with no booking activity behind it."""
    return p.adj_occ_60d >= 0.99 and p.booked_14d == 0 and p.last_booked_days is None


def peer_medians(properties) -> dict:
    buckets = collections.defaultdict(list)
    for p in properties:
        if p.base_price:
            buckets[(p.customization_group, p.bedrooms)].append(p.base_price)
    return {k: st.median(v) for k, v in buckets.items() if v}


def forward_occ(p, days_out: int) -> float | None:
    """Nearest populated forward window at or beyond days_out."""
    windows = (
        (30, p.adj_occ_30d), (45, p.adj_occ_45d), (60, p.adj_occ_60d),
        (90, p.adj_occ_90d), (120, p.adj_occ_120d), (180, p.adj_occ_180d),
    )
    for limit, value in windows:
        if days_out <= limit and value:
            return value
    return p.adj_occ_180d or p.adj_occ_120d or p.adj_occ_90d


def demand_action(occ_pct: float) -> tuple[str, str]:
    if occ_pct >= 85:
        return "Increase", "good"
    if occ_pct >= 55:
        return "Hold", "good"
    if occ_pct >= 40:
        return "Watch", "warn"
    return "Action", "bad"


def load_queue() -> collections.Counter:
    path = HERE / "pricelabs_weekly_action_queue.json"
    if not path.exists():
        return collections.Counter()
    try:
        items = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return collections.Counter()
    return collections.Counter(
        i.get("type", "unknown") for i in items if i.get("status") == "pending"
    )


def booking_pickup_from_pdf(pdf_path: Path) -> list[tuple[str, int, float]]:
    """Extract the 'Booking L7D BPU' text tables. Returns (group, bookings, revenue)."""
    try:
        import pdfplumber
    except ImportError:
        print("  ! pdfplumber not installed; skipping PDF pickup extraction")
        print("    install with: pip install pdfplumber")
        return []

    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Booking L7D BPU" not in text or "Total" not in text:
                continue
            section = re.search(r"Section: (.+?) Booking L7D BPU", text)
            total = re.search(r"Total\s+(\d+)\s+\$([\d,]+)", text)
            if section and total:
                rows.append((
                    section.group(1).strip(),
                    int(total.group(1)),
                    float(total.group(2).replace(",", "")),
                ))
    return rows


# --------------------------------------------------------------------------- render

def esc(value) -> str:
    return html.escape(str(value))


def kpi(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="kpi"><div class="l">{esc(label)}</div>'
            f'<div class="v {cls}">{esc(value)}</div>'
            f'<div class="s">{esc(sub)}</div></div>')


CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI",Roboto,sans-serif;color:#3b2b21;background:#fbf6f1;margin:0;padding:26px;line-height:1.42;font-size:13px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:20px;margin:0 0 2px}
.sub{color:#8a7566;font-size:12px;margin-bottom:15px}
h2{font-size:14px;margin:22px 0 8px;padding-bottom:5px;border-bottom:2px solid #be6b41}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:9px;margin-bottom:12px}
.kpi{border:1px solid #e6d8cc;border-radius:8px;padding:9px 11px;background:#fff}
.kpi .l{font-size:10.5px;color:#8a7566}.kpi .v{font-size:18px;font-weight:700}.kpi .s{font-size:10.5px;color:#8a7566}
table{width:100%;border-collapse:collapse;font-size:12px;background:#fff;border:1px solid #e6d8cc;border-radius:6px;overflow:hidden}
th{background:#f3e9e0;text-align:left;padding:6px 8px;font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;color:#6b5546}
td{padding:6px 8px;border-top:1px solid #f0e6dc;vertical-align:middle}
.num{text-align:right;white-space:nowrap}
.good{color:#15803d;font-weight:700}.bad{color:#c2410c;font-weight:700}.warn{color:#b45309;font-weight:700}.dim{color:#9c8878}
tr.flag td{background:#fff7ed}
.gap{font-size:11px;color:#8a7566;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:9px 12px;margin-top:10px}
.gap b{color:#92400e}
.basis{font-size:11px;color:#8a7566;background:#f6efe8;border:1px solid #eadfd4;border-radius:6px;padding:9px 12px;margin-top:14px}
.note{font-size:11px;color:#8a7566;margin:5px 0 0}
@media print{body{padding:0;background:#fff;font-size:11.5px}h2{page-break-after:avoid}}
"""


def live_section(today: dt.date, template_id: int | None, use_live: bool) -> list[str]:
    """Portfolio snapshot straight from PriceLabs Report Builder.

    Template 1158 is the only one covering all listings. It carries occupancy,
    RevPAR, market RevPAR, pickup and last-booked date, but no STLY/ADR/MPI -
    so year-over-year still needs the scorecard.
    """
    if rb is None or not use_live:
        return []
    tid = template_id or rb.DEFAULT_TEMPLATE_ID
    rows, source = rb.load(tid)
    out: list[str] = []
    add = out.append

    add(f"<h2>Live from PriceLabs — Report Builder template {tid}</h2>")
    if not rows:
        add(f'<div class="gap"><b>No live data.</b> {esc(source)}</div>')
        return out

    def num(row, key):
        v = row.get(key)
        return v if isinstance(v, (int, float)) else None

    revenue = sum(num(r, "Rental Revenue") or 0 for r in rows)
    revpars = [(num(r, "RevPar"), num(r, "Average Market RevPar")) for r in rows]
    revpars = [(a, b) for a, b in revpars if a is not None and b]
    occs = [num(r, "Occupancy") for r in rows]
    occs = [o for o in occs if o is not None]

    add('<div class="kpis">')
    add(kpi("Listings", str(len(rows)), "all groups"))
    add(kpi("Rental revenue", f"${revenue:,.0f}", "trailing 30 days"))
    if occs:
        add(kpi("Occupancy", f"{st.median(occs):.1f}%", "median"))
        add(kpi("At 0% occupancy", str(sum(1 for o in occs if o == 0)), "last 30 days", "bad"))
    if revpars:
        idx = st.median(a / b for a, b in revpars) * 100
        beat = sum(1 for a, b in revpars if a >= b)
        cls = "good" if idx >= 100 else "bad"
        add(kpi("RevPAR vs market", f"{idx:.0f}", "median index, 100 = parity", cls))
        add(kpi("Beating market", f"{beat}/{len(revpars)}", "listings"))
    add("</div>")

    # last booked - the field the portfolio CSV export leaves empty
    def days_since_booked(row):
        raw = row.get("Last Booked date")
        if not raw:
            return None
        try:
            return (today - dt.date.fromisoformat(str(raw)[:10])).days
        except ValueError:
            return None

    aged = [(days_since_booked(r), r) for r in rows]
    dated = [(d, r) for d, r in aged if d is not None]
    if dated:
        add("<h2>Last Booked</h2>")
        add("<table><thead><tr><th>Last booking taken</th>"
            "<th class='num'>Listings</th></tr></thead><tbody>")
        buckets = [(0, 3, "0–3 days"), (4, 7, "4–7 days"), (8, 14, "8–14 days"),
                   (15, 30, "15–30 days"), (31, 90, "31–90 days"), (91, 10**6, "over 90 days")]
        for lo, hi, label in buckets:
            count = sum(1 for d, _ in dated if lo <= d <= hi)
            cls = " class='flag'" if lo >= 15 and count else ""
            add(f"<tr{cls}><td>{label}</td><td class='num'>{count}</td></tr>")
        add(f"<tr><td class='dim'>no date recorded</td>"
            f"<td class='num dim'>{len(aged) - len(dated)}</td></tr>")
        add("</tbody></table>")

        stale = sorted((x for x in dated if x[0] > 14), key=lambda x: -x[0])
        if stale:
            add(f"<h2>Not Booked in Over 14 Days — {len(stale)} listings</h2>")
            add("<table><thead><tr><th class='num'>Days</th><th>Listing</th>"
                "<th class='num'>Occ 30d</th><th class='num'>Base</th></tr></thead><tbody>")
            for d, r in stale[:25]:
                occ = num(r, "Occupancy")
                base = num(r, "Base Price") or 0
                add(f"<tr><td class='num bad'>{d}</td>"
                    f"<td>{esc(r.get('Listing Name') or '')}</td>"
                    f"<td class='num'>{occ if occ is not None else '—'}%</td>"
                    f"<td class='num'>${base:,.0f}</td></tr>")
            add("</tbody></table>")
            if len(stale) > 25:
                add(f'<p class="note">Showing 25 of {len(stale)}.</p>')

    zero7 = sum(1 for r in rows if not (num(r, "Num Booked Pickup 7") or 0))
    zero14 = sum(1 for r in rows if not (num(r, "Num Booked Pickup 14") or 0))
    add(f'<p class="note">Zero pickup — last 7 days: <b>{zero7}</b>, '
        f'last 14 days: <b>{zero14}</b>.</p>')

    add(f'<p class="note">Source: {esc(source)}. This template carries no STLY, '
        'ADR or MPI, so year-over-year figures still come from the scorecard '
        'section above.</p>')
    return out


def build_html(today: dt.date, pdf_path: Path | None,
               template_id: int | None = None, use_live: bool = True) -> str:
    properties = [p for p in wp.load_portfolio() if p.active]
    blocked = [p for p in properties if is_blocked(p)]
    assessable = [p for p in properties if not is_blocked(p)]

    critical = sorted((p for p in assessable if p.adj_occ_60d < 0.20),
                      key=lambda p: p.adj_occ_60d)
    warning = [p for p in assessable if 0.20 <= p.adj_occ_60d < 0.40]
    zero_pickup = [p for p in assessable if p.booked_14d == 0]
    increases = sorted((p for p in assessable
                        if p.adj_occ_60d >= 0.85 and p.booked_14d > 0),
                       key=lambda p: -p.adj_occ_60d)

    peers = peer_medians(assessable)
    scorecard = load_scorecard()
    queue = load_queue()

    out: list[str] = []
    add = out.append

    add(f'<!doctype html><html><head><meta charset="utf-8">')
    add(f'<title>Weekly Revenue Report — {today:%d %b %Y}</title>')
    add(f"<style>{CSS}</style></head><body><div class='wrap'>")
    add(f"<h1>Weekly Revenue Report</h1>")
    add(f'<div class="sub">Week ending {today:%d %b %Y} · {len(properties)} active listings '
        f'· generated {dt.datetime.now():%Y-%m-%d %H:%M} by weekly_report.py</div>')

    # ---- portfolio position
    add("<h2>Portfolio Position — PriceLabs scorecard</h2>")
    if scorecard and scorecard.get("all"):
        a, s = scorecard["all"], scorecard.get("sss", {})
        add('<div class="kpis">')
        add(kpi("Revenue YTD", f"${a['revenue_cy']:,.0f}", f"STLY ${a['revenue_ly']:,.0f}"))
        add(kpi("YoY — all listings", f"+{a['revenue_yoy_pct']}%",
                f"{a['listings']} listings", "good"))
        if s:
            add(kpi("YoY — same-store", f"+{s['revenue_yoy_pct']}%",
                    f"{s['listings']} listings, like-for-like", "good"))
            add(kpi("SSS revenue", f"${s['revenue_cy']:,.0f}",
                    f"STLY ${s['revenue_ly']:,.0f}"))
        add(kpi("Occupancy YTD", f"{a['occ_cy']}%", f"LY {a['occ_ly']}%"))
        if s:
            add(kpi("SSS occupancy", f"{s['occ_cy']}%", f"LY {s['occ_ly']}%"))
        add(kpi("ADR YTD", f"${a['adr_cy']:,.2f}", f"LY ${a['adr_ly']:,.2f}"))
        if s:
            add(kpi("SSS ADR", f"${s['adr_cy']:,.2f}", f"LY ${s['adr_ly']:,.2f}"))
        add("</div>")
        add('<p class="note"><b>Use same-store, not the headline.</b> The all-listings figure '
            f'includes {scorecard.get("sss_excluded", 0)} newly onboarded listings.</p>')

        uploaded = scorecard.get("uploaded_at")
        if uploaded:
            try:
                age = (today - dt.date.fromisoformat(uploaded)).days
            except ValueError:
                age = 0
            if age > 10:
                add(f'<div class="gap"><b>This scorecard is {age} days old</b> '
                    f'(uploaded {uploaded}). Revenue, ADR and MPI figures above are stale; '
                    'everything else in this report is current. Re-upload, or repoint '
                    'Report Builder template 3089 at a year-to-date range.</div>')
    else:
        add('<div class="gap"><b>No scorecard loaded.</b> Upload the PriceLabs Report Builder '
            'export in the dashboard to populate revenue, ADR and same-store figures.</div>')

    # ---- live PriceLabs pull
    for chunk in live_section(today, template_id, use_live):
        add(chunk)

    # ---- listing health
    add("<h2>Listing-Level Health — Haven dashboard</h2>")
    add('<div class="kpis">')
    add(kpi("Active listings", str(len(properties)), f"{len(blocked)} owner-blocked"))
    add(kpi("Assessable", str(len(assessable)), "blocked excluded"))
    add(kpi("Critical", str(len(critical)), "under 20% occ 60d", "bad"))
    add(kpi("Warning", str(len(warning)), "20–39% occ 60d", "warn"))
    add(kpi("Zero pickup 14d", str(len(zero_pickup)), "no bookings ahead"))
    add(kpi("Rate-increase candidates", str(len(increases)), "85%+ occ, real pickup", "good"))
    add("</div>")

    groups = collections.defaultdict(list)
    for p in assessable:
        groups[p.customization_group or "(ungrouped)"].append(p)
    add("<table><thead><tr><th>Group</th><th class='num'>Listings</th>"
        "<th class='num'>Median occ 60d</th><th class='num'>Critical</th>"
        "<th class='num'>Warning</th><th class='num'>% flagged</th></tr></thead><tbody>")
    for name, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        occs = [m.adj_occ_60d for m in members]
        crit = sum(1 for m in members if m.adj_occ_60d < 0.20)
        warn = sum(1 for m in members if 0.20 <= m.adj_occ_60d < 0.40)
        pct = (crit + warn) / len(members) * 100
        med = st.median(occs) * 100
        row_cls = ' class="flag"' if pct >= 40 else ""
        med_cls = "bad" if med < 40 else "good" if med >= 55 else "warn"
        add(f"<tr{row_cls}><td>{esc(name)}</td><td class='num'>{len(members)}</td>"
            f"<td class='num {med_cls}'>{med:.1f}%</td><td class='num'>{crit}</td>"
            f"<td class='num'>{warn}</td><td class='num'>{pct:.0f}%</td></tr>")
    add("</tbody></table>")

    # ---- critical list
    add("<h2>Critical Listings — Under 20% Occupancy, Next 60 Days</h2>")
    add("<table><thead><tr><th class='num'>Occ</th><th>Listing</th><th class='num'>BR</th>"
        "<th class='num'>Base</th><th class='num'>vs peer</th>"
        "<th>Likely driver</th></tr></thead><tbody>")
    for p in critical:
        peer = peers.get((p.customization_group, p.bedrooms))
        if peer and p.base_price:
            delta = (p.base_price - peer) / peer * 100
            if delta >= 10:
                driver, cls, flag = "Priced above peers", "bad", ' class="flag"'
            elif delta <= -10:
                driver, cls, flag = "Below peers already — not a price problem", "", ""
            else:
                driver, cls, flag = "At peer price — content or restrictions", "", ""
            delta_txt = f"{delta:+.0f}%"
        else:
            driver, cls, flag, delta_txt = "No peer comparison available", "", "", "—"
        add(f"<tr{flag}><td class='num bad'>{p.adj_occ_60d * 100:.0f}%</td>"
            f"<td>{esc(p.name)}</td><td class='num'>{p.bedrooms}</td>"
            f"<td class='num'>${p.base_price:,.0f}</td>"
            f"<td class='num {cls}'>{delta_txt}</td><td class='{cls}'>{driver}</td></tr>")
    add("</tbody></table>")
    add('<p class="note">Split this list before working it: listings above peer price are '
        'genuine rate cases; listings already below peer price will not be fixed by cutting '
        'further and need content, photos or channel checks.</p>')

    # ---- rate increases
    add("<h2>Rate-Increase Candidates — 85%+ Occupancy With Real Pickup</h2>")
    add("<table><thead><tr><th class='num'>Occ 60d</th><th>Listing</th><th class='num'>BR</th>"
        "<th class='num'>Base</th><th class='num'>Booked 14d</th></tr></thead><tbody>")
    for p in increases:
        add(f"<tr><td class='num good'>{p.adj_occ_60d * 100:.0f}%</td>"
            f"<td>{esc(p.name)}</td><td class='num'>{p.bedrooms}</td>"
            f"<td class='num'>${p.base_price:,.0f}</td>"
            f"<td class='num'>{p.booked_14d}</td></tr>")
    add("</tbody></table>")

    # ---- demand radar
    if hdc and getattr(hdc, "HIGH_DEMAND_DATES", None):
        add("<h2>Demand Radar — Events and Holidays</h2>")
        add("<table><thead><tr><th>Window</th><th class='num'>Dates</th>"
            "<th class='num'>Days out</th><th class='num'>Median occ</th>"
            "<th>Action</th></tr></thead><tbody>")
        any_row = False
        for start, end, label, _tier in hdc.HIGH_DEMAND_DATES:
            start_date = dt.date.fromisoformat(start)
            days_out = (start_date - today).days
            if days_out < 0 or days_out > 240:
                continue
            values = [v for v in (forward_occ(p, days_out) for p in assessable) if v]
            if not values:
                continue
            any_row = True
            med = st.median(values) * 100
            action, cls = demand_action(med)
            row_cls = ' class="flag"' if cls == "bad" and days_out <= 120 else ""
            add(f"<tr{row_cls}><td>{esc(label)}</td>"
                f"<td class='num'>{start_date:%b %d} – {dt.date.fromisoformat(end):%b %d}</td>"
                f"<td class='num'>{days_out}</td>"
                f"<td class='num {cls}'>{med:.0f}%</td>"
                f"<td class='{cls}'>{action}</td></tr>")
        add("</tbody></table>")
        if any_row:
            add('<p class="note"><b>Caveat:</b> medians come from the portfolio\'s standard '
                'forward windows nearest each event, not from the event dates themselves. '
                'They indicate pacing, not literal event-night occupancy — a low number at '
                '150+ days out is normal for the lead time.</p>')

    # ---- booking pickup from PDF
    if pdf_path:
        rows = booking_pickup_from_pdf(pdf_path)
        if rows:
            add("<h2>Booking Pickup — Last 7 Days (Key Data export)</h2>")
            add("<table><thead><tr><th>Group</th><th class='num'>Bookings</th>"
                "<th class='num'>Booked revenue</th>"
                "<th class='num'>Avg per booking</th></tr></thead><tbody>")
            total_n = total_rev = 0
            for name, count, revenue in rows:
                total_n += count
                total_rev += revenue
                avg = revenue / count if count else 0
                add(f"<tr><td>{esc(name)}</td><td class='num'>{count}</td>"
                    f"<td class='num'>${revenue:,.0f}</td>"
                    f"<td class='num'>${avg:,.0f}</td></tr>")
            avg_all = total_rev / total_n if total_n else 0
            add(f"<tr><td><b>Total</b></td><td class='num'><b>{total_n}</b></td>"
                f"<td class='num'><b>${total_rev:,.0f}</b></td>"
                f"<td class='num'><b>${avg_all:,.0f}</b></td></tr>")
            add("</tbody></table>")

    # ---- queue
    if queue:
        add("<h2>Pending Price Actions</h2>")
        add("<table><thead><tr><th>Type</th>"
            "<th class='num'>Pending</th></tr></thead><tbody>")
        for name, count in queue.most_common():
            add(f"<tr><td>{esc(name)}</td><td class='num'>{count}</td></tr>")
        add(f"<tr><td><b>Total pending</b></td>"
            f"<td class='num'><b>{sum(queue.values())}</b></td></tr>")
        add("</tbody></table>")

    # ---- data gaps, detected rather than assumed
    gaps = []
    if not any(p.min_stay for p in assessable):
        gaps.append("<b>Minimum stay is empty for every assessable listing</b> in the current "
                    "export, so minimum-stay diagnosis cannot be done from data.")
    if not any(p.last_booked_days is not None for p in assessable):
        gaps.append("<b>Last-booked date is empty for every assessable listing</b>, so the "
                    "'last booked over 14 days ago' red flag cannot be evaluated, and the "
                    "owner-blocked test is running on occupancy and pickup alone.")
    if not pdf_path:
        gaps.append("<b>No Key Data PDF supplied</b> — market benchmarking (Haven vs market "
                    "RevPAR and occupancy) and booking pickup are not in this report. "
                    "Re-run with <code>--pdf</code> to add pickup.")
    else:
        gaps.append("<b>Market benchmark charts could not be read.</b> Haven-vs-market RevPAR "
                    "and occupancy live inside chart images in the Key Data PDF and are not "
                    "machine-readable; only the booking pickup tables were extracted.")
    if gaps:
        add('<div class="gap"><b>Data gaps affecting this report.</b><ul style="margin:6px 0 0;'
            'padding-left:17px">')
        for item in gaps:
            add(f"<li>{item}</li>")
        add("</ul></div>")

    add('<div class="basis"><b>Sources.</b> PriceLabs Report Builder scorecard as loaded in '
        'the dashboard; Haven dashboard cached portfolio export (forward occupancy windows, '
        'weekly action queue); Key Data booking pickup tables when a PDF is supplied. '
        '<b>Definitions.</b> Critical = under 20% occupancy next 60 days; warning = 20–39%. '
        'Owner-blocked = 99%+ occupancy with zero 14-day pickup, excluded from benchmarks '
        'and from rate-increase candidates.</div>')
    add("</div></body></html>")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path,
                        help="Key Data PDF export, to add booking pickup by group")
    parser.add_argument("--out", type=Path, help="output path (default: weekly_reviews/<date>/)")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="open the report in your browser when done")
    parser.add_argument("--template", type=int, default=None,
                        help="PriceLabs Report Builder template id "
                             "(default 1158, the only one covering all listings)")
    parser.add_argument("--no-live", action="store_true",
                        help="skip the PriceLabs pull and use local data only")
    args = parser.parse_args()

    if args.pdf and not args.pdf.exists():
        parser.error(f"PDF not found: {args.pdf}")

    today = dt.date.today()
    out_path = args.out or (REVIEWS / today.isoformat() / "weekly_report.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Building weekly report for {today:%d %b %Y}...")
    out_path.write_text(
        build_html(today, args.pdf, template_id=args.template,
                   use_live=not args.no_live),
        encoding="utf-8")
    print(f"  written: {out_path}")

    if args.open_browser:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
