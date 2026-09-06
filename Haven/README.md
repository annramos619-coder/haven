# STR Portfolio Web Dashboard

This folder contains a Flask dashboard for reviewing short-term rental portfolio performance, pricing settings, listing quality, and Airbnb/VRBO review status.

This client version focuses on PriceLabs for revenue-management pricing decisions and exports.

## Run

```bash
cd /path/to/MPP
export Grok_XAI_API_KEY="your_xai_api_key_here"   # xAI/Grok key. XAI_API_KEY also accepted; GROQ_API_KEY only as last-resort fallback.
# Optional overrides:
# export XAI_MODEL="grok-4-fast-reasoning"
# export XAI_BASE_URL="https://api.x.ai/v1"
export PRICELABS_API_KEY="your_pricelabs_customer_api_key"
export PRICELABS_EMAIL="your_pricelabs_email"
export PRICELABS_PASSWORD="your_pricelabs_password"
export PRICELABS_POST_APPLY_ENDPOINTS="POST /listings/{listing_id}/refresh, POST /listings/{listing_id}/sync"
export BOOKING_CLIENT_ID="your_booking_machine_account_client_id"
export BOOKING_CLIENT_SECRET="your_booking_machine_account_client_secret"
python3 app.py
```

Then open:

```text
http://localhost:8080
```

## Main Files

- `app.py` - Flask web dashboard entry point.
- `dashboard_analysis.py` - xAI (Grok) powered analysis engine used by the dashboard.
- `xai_client.py` - Minimal OpenAI-compatible HTTP client for xAI's chat-completions API (replaces the prior Groq SDK).
- `wheelhouse_portfolio.py` - Legacy-named parser for the active `pricelabs_portfolio.csv` export.
- `marketing_links.py` - Loads Airbnb/VRBO listing links, ratings, review counts, and PMS IDs.
- `pricelabs_api.py` - PriceLabs Customer API authentication/request helper.
- `pricelabs_sync.py` - PriceLabs export sync helper.
- `booking_api.py` - Booking.com Connectivity Promotions API helper.
- `templates/index.html` - Dashboard UI.

## Current Integration Notes

PriceLabs recommendations are generated and reviewed in the dashboard. Approved recommendations should be applied in PriceLabs, then marked applied in the dashboard. Hostaway listing edits are intentionally out of scope for this pass.

Booking.com promotions can be drafted, AI-reviewed, approved, and pushed from the Booking Promo Lab once these are configured:

- Booking.com token-based machine account with Promotions API permissions.
- `BOOKING_CLIENT_ID` and `BOOKING_CLIENT_SECRET`.
- Booking.com hotel/property ID, room type IDs, and parent rate plan IDs per listing. Add these optional columns to `marketing_links.csv`: `Booking Hotel ID`, `Booking Room IDs`, `Booking Parent Rate IDs`. Room/rate IDs can also be typed directly into a draft before pushing.

If Booking.com returns `403`, ask Connectivity Support to enable Promotions API permissions for the affected machine account.

After downloading a fresh PriceLabs portfolio export, regenerate the weekly task cards:

```powershell
py generate_pricelabs_weekly_tasks.py
```

The dashboard reads the generated `pricelabs_weekly_action_queue.json` file from the Weekly Actions tab.

When a weekly action is pushed to PriceLabs, the dashboard now:

- sends the base-price or date-override update to PriceLabs,
- optionally calls configured post-apply Save/Refresh/Sync API hooks from `PRICELABS_POST_APPLY_ENDPOINTS`,
- re-syncs the dashboard from the PriceLabs Customer API, and
- stores the verification result on the action card.

`PRICELABS_POST_APPLY_ENDPOINTS` is comma-separated and accepts endpoint templates such as `POST /listings/{listing_id}/refresh`. Leave it unset until PriceLabs support or the account API docs confirm the exact Save/Refresh/Sync endpoint for your account.

To set the PriceLabs Customer API key on Windows PowerShell:

```powershell
.\setup_pricelabs_api.ps1
.\check_pricelabs_env.ps1
```

Open a new terminal after saving the key permanently.

To save the PriceLabs login used by browser export sync:

```powershell
.\setup_pricelabs_login.ps1
```

## Optional Rating Columns

To include Airbnb/VRBO rating status in the dashboard analysis, add these optional columns to `marketing_links.csv`:

```csv
Airbnb Rating,Airbnb Reviews,Vrbo Rating,Vrbo Reviews
```
