# Retired code

Kept because it was working code retired for a reason worth re-reading. Nothing here is imported or
deployed.

## `amadeus.py`, retired 6-Sep-2026

Amadeus' Flight Offers Search plus its 1-year price quartiles. It was the intended warm-up baseline:
until a route has `MIN_HISTORY_DAYS` of its own readings, judge today against Amadeus' past year.

**It never ran once.** The developer account registration could not be completed (24-Jul-2026), so
`configured()` returned False on every run. Measured in production on 6-Sep-2026 before removal:

    daily_best   : 644 rows, 100% source='google'
    price_metrics:   0 rows          <- the table this file fed

It was not harmless dead code. `flight_alert.assess()` had a branch that fed `typical` from an Amadeus
year-long, all-dates median into the field the phone message renders as **"normal day"**, which
everywhere else means the median of *our* daily readings. Two different quantities under one word, one
`configured()` away from the phone.

Removed from: `collect_flights.py` (import, `collect_amadeus()`, `--source`, `--force-metrics`,
`AMADEUS_PAUSE`, the two-source loop), `flight_alert.py` (the `assess()` fallback), `routes.py`
(`AMADEUS_SAMPLE_DATES`), `flight_db.py` (`latest_metrics()`).

The `price_metrics` table stays in the schema: dropping it would be a destructive migration on a live
database for no benefit.

**To revive it:** get an Amadeus account and put credentials in `$AMADEUS_CONF` (never committed), restore
the import and `collect_amadeus()`, re-add a `--source` flag, and give the warm-up branch its own wording.
Do not let an Amadeus median print as "normal day".
