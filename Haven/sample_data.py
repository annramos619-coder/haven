"""
Sample property and market data for the Maui STR dashboard analysis engine.
Represents a 2BR oceanview condo in Kaanapali, West Maui.
Includes intentional revenue issues for the dashboard analysis to discover and fix.
"""

PROPERTY = {
    "id": "KAA-202",
    "name": "Kaanapali Sunset Retreat",
    "location": "Kaanapali, West Maui",
    "bedrooms": 2,
    "bathrooms": 2,
    "max_guests": 6,
    "view": "oceanfront",
    "amenities": ["pool", "hot_tub", "ac", "washer_dryer", "parking", "wifi"],
    "platforms": ["Airbnb", "VRBO", "Booking.com", "Direct"],
    "pms": "Hostaway",
    "revenue_software": "PriceLabs",
    "license": "STR-MAU-7842",
}

PRICING_SETTINGS = {
    "base_rate": 450,
    "weekend_premium_pct": 0,       # ISSUE: missing weekend uplift
    "cleaning_fee": 175,
    "min_stay_default": 3,
    "min_stay_rules": [
        # No holiday/event overrides â€” ISSUE
    ],
    "seasonal_pricing": {
        "peak_winter": {"months": [12, 1, 2, 3], "multiplier": 1.45},
        "summer":      {"months": [6, 7, 8],      "multiplier": 1.20},
        "shoulder":    {"months": [4, 5, 9, 10],  "multiplier": 1.00},
        "low":         {"months": [11],             "multiplier": 0.85},
    },
    "last_minute_discount": {
        "enabled": False,   # ISSUE: disabled â€” last-minute dates going empty
        "within_days": 7,
        "discount_pct": 15,
    },
    "length_of_stay_discounts": [
        {"nights": 7, "pct": 5},
        {"nights": 14, "pct": 10},
    ],
    # PriceLabs configuration
    "PriceLabs": {
        "dynamic_pricing_enabled": True,
        "base_rate_override": True,     # We override PriceLabs base â€” can cause drift
        "market_data_sync": "daily",
        "min_price_floor": 350,
        "max_price_ceiling": 1200,
        "health_score": 72,             # ISSUE: below 80 target â€” settings conflicts
        "last_sync": "2026-05-12",
        "notes": "PriceLabs recommends enabling weekend boost and last-minute discounts â€” both currently ignored",
    },
}

# Bookings for the next ~120 days from 2026-05-13
# Platforms: Airbnb, VRBO, Booking.com, Direct â€” all sync via Hostaway PMS
CALENDAR = [
    # â”€â”€â”€ May â”€â”€â”€
    {"checkin": "2026-05-15", "checkout": "2026-05-18", "nights": 3,  "rate": 450, "platform": "Airbnb",      "status": "confirmed"},
    # GAP: May 18-19 (1 night â€” unfillable with 3-night min, but could use gap pricing)
    {"checkin": "2026-05-19", "checkout": "2026-05-23", "nights": 4,  "rate": 425, "platform": "VRBO",        "status": "confirmed"},
    # May 23-25: OPEN â€” start of Memorial Day weekend, no bookings, base rate only
    # May 25-26: OPEN â€” Memorial Day weekend (base rate $450, market is $680+)
    # May 27-June 4: OPEN â€” soft shoulder, no promo pricing active

    # â”€â”€â”€ June â”€â”€â”€
    {"checkin": "2026-06-05", "checkout": "2026-06-08", "nights": 3,  "rate": 475, "platform": "Booking.com", "status": "confirmed"},
    # GAP: June 8-9 (1 night)
    # June 10-17: Maui Film Festival â€” OPEN, priced at base summer rate ($540)
    # Market comps during Film Festival: $750-900/night
    {"checkin": "2026-06-17", "checkout": "2026-06-22", "nights": 5,  "rate": 490, "platform": "Airbnb",      "status": "confirmed"},
    {"checkin": "2026-06-24", "checkout": "2026-06-28", "nights": 4,  "rate": 510, "platform": "VRBO",        "status": "confirmed"},
    # June 28-July 3: OPEN

    # â”€â”€â”€ July â”€â”€â”€
    # July 4th weekend (Jul 3-6): OPEN â€” priced at $540 (summer multiplier only)
    # Market comps July 4th: $850-1100/night
    {"checkin": "2026-07-08", "checkout": "2026-07-13", "nights": 5,  "rate": 540, "platform": "Airbnb",      "status": "confirmed"},
    {"checkin": "2026-07-14", "checkout": "2026-07-18", "nights": 4,  "rate": 510, "platform": "Booking.com", "status": "confirmed"},
    # GAP: July 18-19 (1 night)
    {"checkin": "2026-07-20", "checkout": "2026-07-27", "nights": 7,  "rate": 490, "platform": "Airbnb",      "status": "confirmed"},
    # July 27-Aug 1: OPEN (last-minute, within 14 days, no discount active)

    # â”€â”€â”€ August â”€â”€â”€
    {"checkin": "2026-08-02", "checkout": "2026-08-09", "nights": 7,  "rate": 490, "platform": "Direct",      "status": "confirmed"},
    # August 9-17: OPEN â€” 8 nights unbooked, no last-minute pricing
    {"checkin": "2026-08-18", "checkout": "2026-08-22", "nights": 4,  "rate": 475, "platform": "Airbnb",      "status": "confirmed"},
    # Aug 22-31: OPEN â€” end-of-summer shoulder approaching
    {"checkin": "2026-09-03", "checkout": "2026-09-07", "nights": 4,  "rate": 420, "platform": "VRBO",        "status": "confirmed"},
]

MARKET_DEMAND = {
    # Demand score 1-10, competitor avg nightly rate, search volume index
    "2026-05-23": {"demand": 7.2, "comp_avg": 620, "search_idx": 88,  "reason": "Memorial Day weekend lead-up"},
    "2026-05-24": {"demand": 7.2, "comp_avg": 620, "search_idx": 88,  "reason": "Memorial Day weekend lead-up"},
    "2026-05-25": {"demand": 8.9, "comp_avg": 695, "search_idx": 142, "reason": "Memorial Day â€” peak holiday demand"},
    "2026-05-26": {"demand": 8.9, "comp_avg": 695, "search_idx": 142, "reason": "Memorial Day â€” peak holiday demand"},
    "2026-06-10": {"demand": 8.5, "comp_avg": 760, "search_idx": 130, "reason": "Maui Film Festival opens"},
    "2026-06-11": {"demand": 8.5, "comp_avg": 770, "search_idx": 135, "reason": "Maui Film Festival peak"},
    "2026-06-12": {"demand": 8.8, "comp_avg": 790, "search_idx": 141, "reason": "Maui Film Festival â€” weekend"},
    "2026-06-13": {"demand": 8.8, "comp_avg": 790, "search_idx": 141, "reason": "Maui Film Festival â€” weekend"},
    "2026-06-14": {"demand": 8.2, "comp_avg": 750, "search_idx": 128, "reason": "Maui Film Festival midweek"},
    "2026-06-15": {"demand": 8.2, "comp_avg": 750, "search_idx": 128, "reason": "Maui Film Festival midweek"},
    "2026-06-16": {"demand": 8.4, "comp_avg": 760, "search_idx": 133, "reason": "Maui Film Festival closing"},
    "2026-07-03": {"demand": 9.5, "comp_avg": 980, "search_idx": 195, "reason": "July 4th holiday â€” premium demand"},
    "2026-07-04": {"demand": 9.8, "comp_avg": 1050,"search_idx": 210, "reason": "4th of July â€” highest demand night"},
    "2026-07-05": {"demand": 9.2, "comp_avg": 920, "search_idx": 188, "reason": "July 4th holiday weekend"},
    "2026-07-06": {"demand": 8.5, "comp_avg": 820, "search_idx": 165, "reason": "July 4th holiday weekend"},
    "2026-07-27": {"demand": 6.0, "comp_avg": 520, "search_idx": 75,  "reason": "Last-minute window â€” discount opportunity"},
    "2026-07-28": {"demand": 5.8, "comp_avg": 505, "search_idx": 72,  "reason": "Last-minute window â€” discount opportunity"},
    "2026-07-29": {"demand": 5.5, "comp_avg": 495, "search_idx": 68,  "reason": "Last-minute window â€” discount opportunity"},
    "2026-07-30": {"demand": 5.5, "comp_avg": 495, "search_idx": 68,  "reason": "Last-minute window â€” discount opportunity"},
    "2026-07-31": {"demand": 5.3, "comp_avg": 490, "search_idx": 65,  "reason": "Last-minute window â€” discount opportunity"},
    "2026-08-09": {"demand": 5.8, "comp_avg": 510, "search_idx": 70,  "reason": "Late summer, moderate demand"},
    "2026-08-10": {"demand": 5.8, "comp_avg": 510, "search_idx": 70,  "reason": "Late summer, moderate demand"},
    "2026-08-22": {"demand": 5.0, "comp_avg": 475, "search_idx": 60,  "reason": "End-of-summer shoulder"},
}

EVENTS_CALENDAR = [
    {"name": "Memorial Day Weekend",  "start": "2026-05-23", "end": "2026-05-26", "impact": "high",   "premium_pct": 50, "notes": "Major US holiday â€” one of Maui's top 5 demand periods"},
    {"name": "Maui Film Festival",    "start": "2026-06-10", "end": "2026-06-16", "impact": "high",   "premium_pct": 55, "notes": "Wailea-based; draws affluent guests; West Maui benefits too"},
    {"name": "4th of July Weekend",   "start": "2026-07-03", "end": "2026-07-06", "impact": "peak",   "premium_pct": 90, "notes": "#1 demand weekend of summer; 3-night min recommended"},
    {"name": "Hawaii State Holiday â€” Statehood Day", "start": "2026-08-21", "end": "2026-08-21", "impact": "low", "premium_pct": 10, "notes": "Minor local holiday"},
    {"name": "End-of-Summer Push",    "start": "2026-08-22", "end": "2026-09-01", "impact": "medium", "premium_pct": 0,  "notes": "Last push before school year; families book late"},
    {"name": "Labor Day Weekend",     "start": "2026-09-05", "end": "2026-09-07", "impact": "high",   "premium_pct": 40, "notes": "Final summer holiday â€” strong demand"},
]

LISTING_HEALTH = {
    "photos": {
        "total_count": 18,
        "cover_photo_score": 6.2,         # ISSUE: below 8.0 â€” not eye-catching enough
        "cover_photo_subject": "interior living room",  # ISSUE: should be the ocean view
        "has_bedroom_shots": True,
        "has_bathroom_shots": False,      # ISSUE
        "has_view_shots": True,
        "has_pool_hot_tub_shots": False,  # ISSUE: premium amenities not shown
        "has_kitchen_shots": True,
        "has_outdoor_lanai_shots": False, # ISSUE: lanai/balcony not showcased
        "professional_quality": False,   # ISSUE: guest-taken, not pro photographer
        "last_updated": "2024-08-15",    # ISSUE: pre-season, misses 2025 renovation
        "airbnb_photo_score": 62,        # out of 100 â€” below 80 threshold for search boost
        "notes": "Photos predate the 2025 furniture refresh. Hot tub and pool not shown. Cover photo is a dim interior shot instead of the oceanfront view.",
    },
    "description": {
        "airbnb_title": "Kaanapali 2BR Condo | Ocean Views",         # ISSUE: weak, undersells oceanfront
        "vrbo_title":   "2BR/2BA Kaanapali Condo - Ocean Views",     # ISSUE: same weakness
        "bookingcom_title": "Kaanapali Sunset Retreat",              # OK
        "word_count": 142,               # ISSUE: Airbnb recommends 400+ for ranking
        "last_updated": "2023-11-20",   # ISSUE: 18 months stale
        "mentions_amenities": ["pool", "ac", "wifi"],
        "missing_amenity_mentions": ["hot_tub", "parking", "washer_dryer", "oceanfront"],
        "missing_keywords": ["Kaanapali Beach", "whale watching", "snorkeling", "sunset", "Black Rock", "Whaler's Village"],
        "has_neighborhood_guide": False, # ISSUE: high-converting content absent
        "has_local_tips": False,         # ISSUE
        "has_seasonal_hooks": False,     # ISSUE: no mention of whale season, summer, events
        "has_house_rules_summary": True,
        "description_excerpt": (
            "Beautiful 2-bedroom condo with ocean views in Kaanapali. Fully equipped kitchen, "
            "pool access. Close to beach and restaurants. Perfect for families and couples."
        ),
        "issues": [
            "Title omits 'oceanfront' â€” the #1 search and click-through differentiator for this property tier",
            "Only 142 words â€” Airbnb's algorithm favors listings with 400+ words; this suppresses search rank",
            "Last updated November 2023 â€” doesn't mention 2025 furniture refresh or hot tub",
            "Hot tub, parking, washer/dryer absent from description despite being premium amenities",
            "No neighborhood guide or local tips â€” guests use these to choose between similar properties",
            "No seasonal hooks: whale season (Janâ€“Mar), summer snorkeling, Film Festival proximity",
        ],
    },
    "reviews": {
        "airbnb_rating": 4.72,           # ISSUE: Superhost threshold is 4.8
        "vrbo_rating": 4.6,              # ISSUE
        "bookingcom_rating": 8.8,        # out of 10 â€” OK
        "total_reviews": 31,
        "response_rate": 0.58,           # ISSUE: Superhost requires 90%
        "response_time_hours": 6.2,      # ISSUE: Superhost requires <1 hr
        "superhost_status": False,       # ISSUE: lost Superhost â€” hurts search placement
        "recent_reviews": [
            {
                "date": "2026-04-10", "platform": "Airbnb", "score": 5,
                "category_scores": {"cleanliness": 5, "accuracy": 5, "check_in": 5, "communication": 5, "location": 5, "value": 4},
                "snippet": "Stunning views and great location. Would definitely come back!",
                "responded": True,
            },
            {
                "date": "2026-03-22", "platform": "VRBO", "score": 3,
                "snippet": "Views were partially blocked by a construction crane. Bathroom wasn't very clean. Check-in instructions were hard to find.",
                "responded": False,   # ISSUE: unanswered 3-star
            },
            {
                "date": "2026-03-05", "platform": "Airbnb", "score": 4,
                "category_scores": {"cleanliness": 3, "accuracy": 4, "check_in": 4, "communication": 5, "location": 5, "value": 4},
                "snippet": "Great location and communication. Cleanliness could be improved â€” found dust behind furniture.",
                "responded": True,
            },
            {
                "date": "2026-02-14", "platform": "Booking.com", "score": 9,
                "snippet": "Perfect Valentine's trip. Beautiful sunsets from the lanai.",
                "responded": False,
            },
            {
                "date": "2026-01-28", "platform": "Airbnb", "score": 3,
                "category_scores": {"cleanliness": 2, "accuracy": 3, "check_in": 3, "communication": 4, "location": 5, "value": 3},
                "snippet": "Disappointed with cleanliness â€” previous guests' items left behind. The location and views are 5-star though.",
                "responded": False,   # ISSUE: unanswered 3-star
            },
        ],
        "category_averages": {
            "cleanliness": 3.8,   # ISSUE: biggest rating drag
            "accuracy": 4.3,
            "check_in": 4.1,      # ISSUE: below 4.5 target
            "communication": 4.7,
            "location": 5.0,
            "value": 3.9,         # ISSUE
        },
        "recurring_complaints": ["cleanliness", "check_in_instructions", "value_perception"],
    },
    "links": {
        "airbnb": {
            "url": "https://airbnb.com/rooms/12345678",
            "status": "active",
            "instant_book": True,
            "superhost": False,     # ISSUE: lost badge â€” search rank penalty
            "listing_score": 62,   # ISSUE: below 80 threshold for search boost
        },
        "vrbo": {
            "url": "https://vrbo.com/1234567",
            "status": "active",
            "instant_book": False,  # ISSUE: 60%+ of VRBO searches filter for instant book
            "premier_host": False,
            "listing_score": 71,
        },
        "booking_com": {
            "url": "https://booking.com/hotel/us/kaanapali-sunset.html",
            "status": "restricted",  # ISSUE: flagged â€” listing suppressed
            "genius_program": False,
            "action_required": "Booking.com flagged listing for incomplete property photos. Upload bathroom and amenity photos to restore full visibility.",
        },
        "direct": {
            "url": "https://mauivacationrentals.com/kaanapali-sunset",
            "status": "active",
            "booking_widget": True,
        },
    },
}

COMP_SET = [
    {"name": "Kaanapali Alii Unit 312",     "bedrooms": 2, "view": "oceanfront", "platform": "Airbnb",  "recent_rates": {"weekday": 595, "weekend": 720, "holiday": 950}},
    {"name": "Whaler on Kaanapali #845",    "bedrooms": 2, "view": "oceanview",  "platform": "VRBO",    "recent_rates": {"weekday": 540, "weekend": 680, "holiday": 890}},
    {"name": "Kaanapali Beach Club 204",    "bedrooms": 2, "view": "oceanfront", "platform": "Direct",  "recent_rates": {"weekday": 620, "weekend": 760, "holiday": 1020}},
    {"name": "Mahana at Kaanapali #510",    "bedrooms": 2, "view": "partial",    "platform": "Airbnb",  "recent_rates": {"weekday": 475, "weekend": 580, "holiday": 750}},
    {"name": "Royal Lahaina Cottage",       "bedrooms": 2, "view": "oceanview",  "platform": "VRBO",    "recent_rates": {"weekday": 510, "weekend": 625, "holiday": 820}},
]

# Rolling 90-day performance (closed bookings)
PERFORMANCE_90D = {
    "occupancy_rate": 0.68,      # 68% â€” below 75% target for peak-adjacent period
    "adr": 471,                  # Average daily rate
    "revpar": 320,               # Revenue per available room-night
    "total_revenue": 28_800,
    "nights_available": 90,
    "nights_booked": 61,
    "nights_blocked": 2,
    "avg_lead_time_days": 38,    # bookings coming in avg 38 days out
    "platform_mix": {"Airbnb": 0.48, "VRBO": 0.28, "Booking.com": 0.13, "Direct": 0.11},
    "cancellation_rate": 0.07,
    "repeat_guest_rate": 0.14,
}

TARGET_KPIs = {
    "occupancy_rate": 0.78,
    "adr": 560,
    "revpar": 437,
    "avg_review_score": 4.85,
}
