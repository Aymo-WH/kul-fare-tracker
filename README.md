# kul-fare-tracker

A daily airfare tracker for routes out of Kuala Lumpur (KUL). It prices a grid of departure dates once a
day from Google Flights, keeps the cheapest sensible fare per route in SQLite, and sends a phone push only
when today's fare is genuinely cheap against that route's own history. It runs from cron on a small VPS.

## The problem

Fare alerts are easy to build and hard to make trustworthy. The failure I cared about is not a missed deal
but a confident alert about nothing: "cheapest ever" on a price that has not moved, or "cheaper than 92%"
backed by 34 readings. A quiet phone should mean nothing is worth acting on, and a push should mean book now.

## Approach

- **Collect** (`collect_flights.py`): for each route, price every date in its window (one-way or
  N-night return, 1 adult, economy, up to 1 stop) and store every quote plus the day's minimum per basis.
- **Two bases**: the advertised fare, and a full-service-comparable fare (fare + 20kg bag + one pre-book
  meal per leg), because a budget carrier's headline fare is not like-for-like with Cathay or MAS.
- **Decide** (`flight_alert.py`): alert only if the fare is **rare AND materially cheap**, in the bottom 20%
  of its own history and at or below 95% of its median. Warm-up (under 21 readings) uses Google's own
  low/typical/high verdict and never turns it into a percentage.
- **Say it plainly**: one message per event, every number with its scope in words, "normal day" defined as
  the median, an ordinal rank ("2nd lowest of 46 daily checks") instead of a percentage.

## Key design choices

- **Fixed calendar dates for a real trip, a wide rolling window for watching.** A window that slides with
  today compares different travel dates on different days; that bug produced fake records.
- **Any window change needs a history cutover** (`history_since`). The daily reading is a minimum over the
  grid, so a wider grid can only lower it and would fire "lowest ever" for a record that never happened.
- **Direct flights win unless a connection saves >15%**, applied at selection time so the stored history
  and the message never disagree.
- **Bag fees are labelled assumptions** (`included` / `estimate` / `ambiguous` / `unknown`), never silently
  priced. Google's filter has no reachable bag field; fields 1–45 were tested and all were inert.
- **No third-party dependencies in the scraper.** `gflights.py` hand-encodes the protobuf search filter and
  parses aria-labels rather than churning CSS class names.
- **Delivery window**: messages go out only 16:00–23:59 local time; anything outside is queued, never dropped.

Full reasoning and the gotchas are in [docs/DESIGN.md](docs/DESIGN.md).

## Results (production, 22-Jul to 23-Sep 2026, seven series)

- **Rank alone was noise.** 11 of 25 alerts sent 1-Aug→22-Aug saved only RM8–RM38. Real deals sat at
  77.9–89.6% of typical (saved RM70–RM131); noise at 96.5–99.3%. Adding the ≤95% gate fixed it, at a known
  cost: 7 of 14 series-bases could no longer fire at their observed lows, because their best day was within
  ~3.5% of typical.
- **The sliding window was wrong.** On one return route the cheapest reading moved RM827 → RM605, all of it
  the window drifting from the Christmas peak into February; top-3/top-2/top-1 fired on 32/32/29 of 46 days.
  Fixed dates plus a cutover (46 readings discarded) replaced it; `--verify-cutover` returned clean on
  23-Sep-2026 over 119 post-cutover day-series.
- **Amadeus was rejected.** It never ran (registration could not be completed): 644 `daily_best` rows, 100%
  Google, 0 Amadeus rows. Google's per-date verdict is a better warm-up baseline because it covers AirAsia.
- **Bags change the answer.** On the first sweep the with-bag winner was a different airline on 4 of 7
  series.
- **Cost**: 3.63–3.79 s per date-lookup; 327 lookups take ~20 minutes.
- **Open**: the thresholds were set on pre-cutover data and still need a re-measure on post-cutover history.

## Layout

```
tracker/
  routes.py             route config loader, windows, thresholds (with the measured reasoning)
  routes.example.json   sample KUL routes; copy to routes.json (git-ignored) for real use
  gflights.py           Google Flights adapter
  collect_flights.py    daily sweep
  flight_db.py          SQLite schema, best-fare selection, history with cutover
  flight_alert.py       gates, cooldown, message, push; --verify-cutover
  bag_fees.py           bag and meal assumptions with confidence levels
  migrate_bags.py       rebuild daily_best from quotes after a bag-fee change
  notify.py             Telegram / ntfy push with the delivery window and queue
  retired/              Amadeus adapter, kept with its retirement notes
tests/                  pytest for config loading
docs/DESIGN.md          design notes and gotchas
```

## Running it

Python 3, standard library only (pytest for the tests).

```bash
cd tracker
cp routes.example.json routes.json         # edit routes and thresholds, or set FARE_ROUTES_CONFIG=/path.json
export FLIGHT_DB=/path/to/flights.sqlite    # default: tracker/flights.sqlite
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...   # or NTFY_URL=https://ntfy.sh/<topic>

python3 gflights.py KUL SIN 2026-11-15                 # one lookup
python3 collect_flights.py --dry-run --sample 2        # fetch, write nothing
python3 collect_flights.py                             # full sweep
python3 flight_alert.py --preview                      # render, send nothing (always first)
python3 flight_alert.py --force KUL-SIN-RT --day YYYY-MM-DD
python3 flight_alert.py                                # evaluate and push
python3 notify.py --flush                              # deliver queued messages (cron at 16:05)
```

Cron, as run in production (UTC): `15 8 * * *` collect, `45 9 * * *` alert.

## Tests

```bash
python -m pytest -q tests                      # config loading: 5 tests
python tracker/bag_fees.py --self-test         # fee/meal arithmetic
python tracker/migrate_bags.py --self-test     # rebuild safety checks
```

The collector and alerter have no automated tests; they were verified against the production database with
`--dry-run`, `--preview`, `--force` and `--verify-cutover`.
