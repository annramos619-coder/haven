# Vacation Rental Listing Quality Rules

Source: vacation-rental-marketing skill provided by the user. Use this as the dashboard's listing-quality playbook.

## Core Behavior

- Always produce a scored audit with ranked action items, not generic tips.
- Score six pillars from 1 to 10: Title, Description, Photos, Amenities, Reviews, and Completeness.
- Assign an overall grade: A = 85-100, B = 70-84, C = 50-69, D = below 50.
- Each recommendation must say what to change, why it matters, where to update it, and expected impact.
- If data is missing, say manual review is needed. Do not invent amenities, photos, review text, host status, or platform settings.
- Missing synced data should usually be scored `NA`, not `D`. Use `D` only when bad data is explicitly present.
- Never use placeholder output such as `[Inferred]`, `[NA]`, `[Specific USP]`, or bracketed template text. Write a real sentence, or say `Not enough synced data`.
- Do not upgrade claims. `Mountain view` is not `panoramic mountain view`; `pool access` is not `private indoor pool`; `near Gatlinburg` is not `walk to downtown` unless the source says so.
- If a current title is available, always score the title. Do not mark Title Score as `NA` unless both Airbnb and VRBO titles are missing.
- It is acceptable to infer target guest segments from bedroom count, location, property type, and title, but label them as inferred from available data.

## Title Rules

- Airbnb title must be 50 characters or fewer.
- Airbnb ideal mobile title is 45-50 characters when possible.
- VRBO title target is 60-75 characters.
- Front-load the strongest Smokies differentiator: hot tub, mountain view, indoor pool, game room, theater, pet friendly, fire pit, A-frame, sauna, EV charger, bunk room, or walkable Gatlinburg/Pigeon Forge location.
- Only use exact amenity/location claims when source data supports them. `Mountain View` is not `Panoramic Mountain View`; `pool access` is not `private indoor pool`; `near Gatlinburg` is not `walk to downtown` unless the source says so.
- Include a searchable keyword naturally.
- Avoid filler words such as amazing, perfect, cozy, beautiful, best.
- Avoid emojis, all caps, and keyword stuffing.
- Airbnb title formula: `[Differentiator] + [Property Type] + [Location Signal] + [Perk]`.
- Always include the character count for suggested Airbnb titles.
- If the Airbnb title is over 50 characters, rewrite it until it is 50 or fewer.
- Accurately count the current title length before calling it too long. If it is already 50 characters or fewer, judge it on clarity, keywords, promo language, and differentiation instead.
- Do not claim a title has promotional language unless it actually contains words like sale, discount, special, deal, limited time, or excessive punctuation/symbols.

## Description Rules

- The hook should lead with the guest experience, not "Welcome to..."
- Use guest-experience language: "wake up to...", "walk to...", "unwind on..."
- Include Smokies keywords naturally: hot tub, mountain view, indoor pool, game room, theater room, fire pit, pet friendly, family cabin, Gatlinburg, Pigeon Forge, Sevierville, Townsend, full kitchen, parking, workspace.
- Structure: Hook, The Space, Guest Experience, Practical Info, Location.
- Avoid excessive capitalization, emoji overuse, and keyword stuffing.
- Suggested opening paragraph should be specific to the property and 80-100 words unless the user requests otherwise.
- If the full description is not synced, Listing Description Score should be `NA`, not `D`. Recommend syncing or pasting the description before judging quality.

## Photo Rules

- 20+ photos is the minimum healthy baseline.
- 25+ photos is a stronger platform target.
- Photo coverage grading: under 15 = D, 15-24 = C, 25-34 = B, 35+ = A.
- Cover photo should be the strongest visual selling point: hot tub with mountain view, indoor pool, sunset deck, dramatic A-frame/cabin exterior, theater/game room lighting, luxury outdoor lounge, or standout design.
- Recommended sequence: hero experience amenity, outdoor view/deck, living/gathering area, primary bedroom, kitchen/dining, bathroom, secondary sleeping/bunks, game/theater/pool room, fire pit/grill/outdoor lounge, exterior/location/aerial.
- Every photo should have a descriptive caption using: `[What it shows] + [guest-experience note]`.
- If only photo count is known, grade coverage only and require manual visual review for brightness, blur, cropping, and cover quality.
- If photo count and visual data are both unknown, Image Score should be `NA`, not `D`.

## Amenity Rules

- Treat amenities as search filters. Missing accurate amenity boxes can make listings invisible.
- Tier 1 must-haves: hot tub when available, fast Wi-Fi, full kitchen, washer/dryer, A/C/heat, free parking, TV/streaming, keyless entry, smoke/CO safety.
- Tier 2 occupancy drivers: pet-friendly setup, fire pit, covered deck with grill, game room, theater room, workspace, competitive cleaning fee, flexible cancellation, bunk/kid amenities.
- Tier 3 pricing-premium differentiators: indoor pool, exceptional mountain views, luxury outdoor experience, theater + arcade combo, sauna/cold plunge, EV charger, unique design cabin, coffee bar/Instagrammable interiors, event/wedding-friendly setup.
- Do not claim an amenity exists unless provided in source data.

## Smokies Experience Amenity Guidance

- Guests are increasingly choosing cabins that feel like private resorts or social-media-friendly experiences, not basic cabins.
- Highest-converting amenities to audit and feature when present: hot tubs, mountain views, indoor pools, game rooms, pet-friendly setup, fire pits/outdoor lounge spaces, theater rooms, EV chargers, sauna/cold plunge, fast Wi-Fi/workstations, kid-focused amenities, smart cabin tech, covered decks with grills, coffee bars, and wedding/event-friendly spaces.
- Largest pricing premiums usually come from indoor pools, exceptional mountain views, luxury outdoor experiences, theater + arcade combos, sauna/cold plunge, and unique design cabins.
- Biggest occupancy drivers usually come from pet friendly, hot tub, flexible cancellation, competitive cleaning fee, fast Wi-Fi, and game room.
- Highest-clicking cover photos usually show hot tub with mountain view, indoor pool, sunset deck, dramatic A-frame exterior, or theater/game room lighting.
- If amenity data is not synced, do not score the amenity as bad. Put these items into Manual Review Needed and ask the user to verify whether each high-impact Smokies amenity is present, photographed, and enabled as an OTA filter.

## Reviews Rules

- Only analyze review/rating data explicitly provided.
- Airbnb uses a 5-point scale; VRBO uses a 10-point scale.
- Airbnb thresholds: below 4.5 = bad, 4.5-4.69 = needs attention, 4.7-4.79 = good but below top-tier, 4.8+ = strong.
- VRBO thresholds: below 8 = bad, 8-8.9 = needs attention, 9-9.4 = good, 9.5+ = strong.
- For weak ratings, recommend operational diagnosis and review response strategy.
- Do not show or discuss "unknown" reviews as if they are real data.

## Platform Completeness Rules

- Flag missing bed configuration, house rules, cancellation clarity, response-time/host-status data, and platform completeness only when the data is available or explicitly absent.
- Bed configuration discrepancies across platforms are high-risk because they can create guest complaints.
- Missing detector/safety or amenity fields should be escalated to Streamline/platform settings if known.

## Competitive Reasoning

- Compare against typical top-performing properties in the same area/property type, but label this as an inference unless actual competitor data is supplied.
- Use competitor-style reasoning for title keywords, amenities, photo count, and hero image, not for invented facts.

## Output Standard

- Include a score breakdown.
- Include the highest-impact listing issue first.
- Include exact title rewrite(s).
- Include a photo action plan.
- Include missing-data notes.
- End with a ranked top-five action list by expected booking impact.

## PriceLabs-Style Report Format

When producing a full listing optimizer report, mirror this structure:

1. `Listing Basics`
   - Listing Quality Score `X.X/10`
   - `Insights`: exactly three numbered insights. Each insight should name the category, score/grade, and exact fix.

2. `Listing Title Score X`
   - `Current`
   - `Recommended`
   - `Why?`
   - `What's Working Well?`
   - `What Needs Change?`

3. `Image Score X`
   - `Cover Image Analysis`
   - `Grid Analysis`
   - `Why?`
   - `What's Working Well?`
   - `What Needs Change?`
   - If image visuals are unavailable, say the analysis is based only on photo count and available metadata, then list what needs manual visual review.

4. `Listing Description Score X`
   - `Current`
   - `Why?`
   - `What's Working Well?`
   - `What Needs Change?`
   - Include a rewritten opening if the current copy is weak or not available.

5. `Amenities Score X`
   - `Premium Amenities`
   - `Why?`
   - `What Needs Change?`
   - Never claim missing or present amenities unless supplied by source data. If amenities are unknown, return `NA` and list what to audit.

6. `Star Ratings Score`, `Review Summary Score`, and `Guest Favorite Score`
   - Use `NA` when rating/review data is unavailable.
   - Do not describe unknown review data as a problem; say not enough data.

7. `Consistency Score X`
   - Look for mismatches between title, description, photos, amenities, parking, workspace, bed configuration, and platform details.
   - If photo/amenity data is missing, use `NA` or `Needs manual check`; do not invent inconsistencies.

8. `Positioning Insights`
   - Three unique selling points.
   - Target segments with reasoning.

9. `Top Performer Insights`
   - `What's Working Well?`
   - `What Top Performers Are Doing Well?`
   - Use portfolio/market inference only when competitor data is unavailable, and label it as inferred.

10. `Overview`
   - A compact table with category grades.
   - Include legend: `A=Excellent B=Good C=Needs Review D=Low NA=Not Available`.

The tone should be specific and diagnostic, like a PriceLabs listing optimizer export. Avoid generic marketing advice.
