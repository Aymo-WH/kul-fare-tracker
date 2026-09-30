#!/usr/bin/env python3
"""What the flight tracker watches. One place, so the collector, the alerter and
the health check can never disagree about the route list.

The routes themselves live in a JSON config, not in code:

  * $FARE_ROUTES_CONFIG, if set;
  * else routes.json next to this file (git-ignored: my real routes);
  * else routes.example.json (shipped: sample KUL routes).

The config may also override PERCENTILE_TRIGGER and MAX_PCT_OF_TYPICAL. Every
other constant below stays in code, because each one carries a measured reason
that belongs next to it.

One-way and return fares on the same city pair are tracked as SEPARATE series.
On full-service carriers a return is sometimes cheaper than the one-way (they
price one-ways punitively), so the two have separate histories and are never
compared as if they were the same product. A reverse leg (someone flying TO
KUL) is its own one-way series and is never folded into a return.

Cabin/pax: 1 adult, economy, up to 1 stop.

BAGGAGE WARNING, and the reason `bags_note` exists: AirAsia's headline fare
excludes checked baggage while Cathay/MAS include ~20kg. A raw price comparison
therefore flatters the budget carriers by roughly RM 80-150 each way. We store
the carrier so the alert can say so; we do NOT try to normalise it away, because
guessing a bag fee would be inventing a number.
"""
import json
import os
from datetime import date
from pathlib import Path

# MODULE DEFAULTS. STEP_DAYS is read on every run by routes that pass no `step`.
# WINDOW_START_DAYS/WINDOW_END_DAYS are the fallback for a route configured without a
# window, and the rolling shape all early history was collected on. Do not delete them
# without also deleting the fallback in collect_flights.date_grid().
WINDOW_START_DAYS = 60
WINDOW_END_DAYS = 180
STEP_DAYS = 7

# PER-ROUTE WINDOWS. The globals above are the DEFAULT; a route may override them.
# The bug they fix: a window that SLIDES with today compares different travel dates on
# different days, and every rank built on it inherits the error (docs/DESIGN.md, "Why a real
# trip gets FIXED dates").
#
# For "watching" routes I use a rolling (20, 270) day window. A 270-day end covers a full year
# of seasonality, so the window cannot drift out of one season into another; a 20-day start keeps
# a near-term trip visible (under a 60-day floor, November departures vanished ~15-Oct). It does
# NOT fully fix them: one "cheapest today" over ~36 dates still lets a February fare beat a
# December one -- hence the watching icon.
#
# NEVER widen a window without setting the route's `history_since` cutover. The daily reading is
# the MINIMUM over the grid and a wider grid is a SUPERSET, so the minimum can only fall: every
# route steps down on its first run and pushes "lowest of N" for a record that never happened (a
# first attempt did exactly this and was blocked at review). Measured before deciding: the 60-89
# day bucket was the CHEAPEST of the four held (median RM1,067 vs 1,193 / 1,222 / 1,095), and
# widening adds nearer dates -- so a step-down was likely, not merely possible.
#
# FIXED-DATE WINDOWS (`window_dates`) are for a REAL trip: the same departure dates are priced
# every day, so a price change is a PRICE change and not the window drifting into a cheaper season.
#
# A FIXED GRID IS FIXED ONLY UNTIL today + MIN_LEAD_DAYS PASSES ITS START. From then on it loses a
# date a day from the front. Measured with the shipped code on a 1-Nov..31-Dec trip: 61 dates on
# 18-Oct, 60 on 19-Oct, 47 on 1-Nov, 33 on 15-Nov, 8 on 10-Dec, 1 on 17-Dec, 0 on 18-Dec. That IS a
# grid change: the daily minimum over a shrinking set can only RISE, so the series drifts upward
# from arithmetic, and "normal day" (from earlier, wider readings) sits below what is purchasable.
# NOT given a rolling cutover, deliberately: that would leave the route permanently in warm-up.
# The DECISION window should sit before the contraction starts.
#
# Step 1 prices every candidate date instead of every 7th, so the daily minimum covers the whole
# trip window. It would also allow per-date history, which NOTHING reads yet: assess() still ranks
# the DAILY MINIMUM across the grid. Do not read this as a capability that exists.
#
# COST -- recompute from the code after ANY window change, never by hand:
#     python3 -c "import collect_flights as c, routes as r, datetime as d; \
#       print(sum(len(c.date_grid(d.date.today(),None,x)) for x in r.ROUTES))"
# Measured baseline, production, 31-Aug..5-Sep-2026: 126 date-lookups in 7m38s..7m57s =
# 3.63..3.79 s/lookup, 7 of 7 series ok on every run. At 3 fixed-date routes x 61 dates + 4
# watching routes x 36 dates = 327 lookups -> ~20 min, finishing ~08:35 UTC against an 08:15 start.
#
# THE RISK IS GOOGLE RATE-LIMITING A LONGER RUN. Mitigations: the alerter runs at 09:45 UTC
# (a 70-minute overrun margin), and flight_alert ABORTS if the day's collector run has no
# finished_at. If runs stop coming back clean, raise a fixed-date route's step to 2 before anything
# else. THAT IS A GRID CHANGE AND NEEDS ITS OWN CUTOVER: a minimum over FEWER dates can only rise,
# and the older wider readings would sit under it as a fake "normal day" -- move that route's
# `history_since` forward the same day. `--verify-cutover` catches a forgotten one (verified:
# step 2 flags 30 stale readings, because the test is membership of date_grid(), which applies
# the step).
#
# HISTORY CUTOVER (`history_since`). Readings before this date were taken on a different window
# and are NOT comparable, so flight_db.history() excludes them. The cost: the series restarts from
# zero and prints the warm-up branch until MIN_HISTORY_DAYS (21), ~3 weeks. Keeping 46
# incomparable readings would have printed a confident "lowest of 47 daily checks" for a record
# that never happened.
#
# SET IT whenever a route's window, step or trip shape changes. A window change without a
# cutover silently corrupts every rank and median built on it.

# A trip cannot be booked for tomorrow, and scanning dates that have effectively passed wastes
# the budget. An ASSUMPTION, not a measured value.
#
# APPLIES TO FIXED-DATE WINDOWS ONLY -- collect_flights.date_grid() reads it in the
# `window_dates` branch and nowhere else; a rolling window carries its own floor in start_days.
#
# LOWERING IT IS A WINDOW CHANGE AND NEEDS A CUTOVER. On a fixed-date route it decides where the
# grid starts once the trip is inside 14 days. Lowering it WIDENS the grid at the front, the
# minimum can only fall, and the series reports a record that never happened (same mechanism as
# widening a window above; docs/DESIGN.md gotcha 12). So lower it AND move each fixed-date
# route's `history_since` the same day.
MIN_LEAD_DAYS = 14

MAX_STOPS = 1
ADULTS = 1
SEAT = "economy"
CURRENCY = "MYR"

# A connecting flight is only worth offering when it saves REAL money: if a direct
# flight is cheaper, no transfer flight is needed. The first live sweep found Scoot at
# RM554 beating a direct AirAsia at RM575 — RM21 saved for a 21-hour layover,
# which is not a deal, it is noise dressed as one.
#
# This is applied at SELECTION time, not when formatting the message. Filtering
# only the display would leave the alert quoting one price while the tracked
# history was built from a different, cheaper one — the two would silently
# disagree forever.
DIRECT_PREFERENCE = 0.15   # a 1-stop must beat the best direct by >15%


class Route:
    def __init__(self, key, origin, dest, trip_type, label, nights=None,
                 window=None, place=None, real_trip=False,
                 window_dates=None, step=None, history_since=None):
        self.key = key
        self.origin = origin
        self.dest = dest
        self.trip_type = trip_type      # 'oneway' | 'return'
        # (start_days, end_days) or None for the module default. See
        # PER-ROUTE WINDOWS above for why this is per-route.
        self.window = window
        # FIXED calendar (start_date, end_date) -- takes precedence over
        # `window`. See FIXED-DATE WINDOWS above for why a real trip uses fixed dates.
        self.window_dates = window_dates
        # Days between sampled departure dates; None = cfg.STEP_DAYS.
        self.step = step
        # Ignore readings before this date -- see HISTORY CUTOVER above.
        self.history_since = history_since
        # PLACE + REAL_TRIP: the opener names the place in plain words, with an icon saying whether
        # this is a real trip or a route I am only watching.
        self.place = place or dest
        self.real_trip = real_trip
        self.label = label              # what reaches the phone — plain names
        self.nights = nights

    @property
    def pair(self):
        """The city pair, ignoring trip type — used to spot a return that has
        somehow priced below its own one-way."""
        return f"{self.origin}-{self.dest}"

    def __repr__(self):
        return f"<Route {self.key}>"


# --- Route config ------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
EXAMPLE_CONFIG = _HERE / "routes.example.json"
LOCAL_CONFIG = _HERE / "routes.json"


def config_path():
    """$FARE_ROUTES_CONFIG, else routes.json if present, else the shipped example."""
    env = os.environ.get("FARE_ROUTES_CONFIG")
    if env:
        return Path(env)
    return LOCAL_CONFIG if LOCAL_CONFIG.exists() else EXAMPLE_CONFIG


def _day(s):
    return date.fromisoformat(s) if s else None


def route_from_dict(d):
    """One config entry -> Route. Dates are ISO strings; windows are 2-element lists."""
    wd = d.get("window_dates")
    w = d.get("window")
    return Route(d["key"], d["origin"], d["dest"], d["trip_type"], d["label"],
                 nights=d.get("nights"),
                 window=tuple(w) if w else None,
                 place=d.get("place"), real_trip=bool(d.get("real_trip", False)),
                 window_dates=(_day(wd[0]), _day(wd[1])) if wd else None,
                 step=d.get("step"),
                 history_since=_day(d.get("history_since")))


def load_config(path=None):
    """-> (routes, thresholds dict). Raises on a missing file or duplicate keys."""
    path = Path(path) if path else config_path()
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    routes = [route_from_dict(d) for d in raw["routes"]]
    keys = [r.key for r in routes]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate route keys in {path}")
    return routes, raw.get("thresholds", {})


ROUTES, _THRESHOLDS = load_config()
BY_KEY = {r.key: r for r in ROUTES}

# --- Alerting thresholds -----------------------------------------------------

# "Bottom 20%" of the route's own history. Overridable per config.
PERCENTILE_TRIGGER = float(_THRESHOLDS.get("percentile_trigger", 20.0))

# A RANK ON ITS OWN IS NOT A DEAL. The bottom 20% is a rank test, so by
# construction ~20% of days qualify FOREVER — the alert rate was set by this
# threshold, not by the market. On a tight series that is actively misleading:
# KUL⇄HKG nobag's whole 32-day range was RM1,010–1,078 (6.7%), so its bottom 20%
# sat at 99% of typical and pushed "cheaper than 86%" for RM8 off.
# 11 of the 25 alerts sent between 1-Aug and 22-Aug saved RM8–RM38.
#
# So a fare must ALSO be materially below typical. Both gates, not either:
# rare AND cheap.
#
# RE-MEASURE ON CURRENT DATA before re-tuning. Every figure below was measured on
# the 46 readings a later window cutover discarded, drawn from a sliding window
# that compared unlike departure dates. They are not false as history -- they are
# what the data said, and they are why the second gate exists -- but the next
# tuning decision must not be made on them.
#
# MEASURED PER ALERT, on the advertised (nobag) fare — which is the only price
# the `alerts` table stores — the two populations separate cleanly:
#
#   real deals  77.9% ....... 89.6% of typical   (saved RM70–RM131)
#                    << gap >>
#   noise       96.5% ....... 99.3% of typical   (saved RM8–RM38)
#
# Read the bounds carefully: 96.5% is the noise group's CHEAPEST member, 89.6%
# its nearest real deal. The ceiling is decorative; 96.5 is the load-bearing
# number, because it is the one a threshold has to stay under.
#
# THAT VIEW DOES NOT BOUND THIS CONSTANT, because `assess` applies the gate PER
# BASIS, and per basis the gap has an occupant: KUL-HKG-OW bag20 fired at 91.7%
# of its own bag20 median (RM604 vs RM659 — RM55 saved, a real deal, and also
# that series-basis's all-time low). The honest ladder is therefore
#
#     89.6%  ...  91.7%  ...............  96.5%
#     deals       a REAL deal              noise floor
#
# and thresholds in 90–96% are NOT interchangeable: dropping to 0.91 silences
# an eighth series-basis ("7 of the 14" becomes wrong). 0.95 clears 91.7 with room.
#
# The headroom ABOVE is thinner than the per-alert diagram suggests, and for
# the same per-basis reason: measured per basis, the nearest suppressed
# instance is 95.9% (18-Aug KUL-HKG-RT nobag, RM1,010 vs a RM1,053 median —
# that series' all-time low). So 0.95 has ~0.9pp of margin on the noise side,
# not the ~1.5pp the 96.5 figure implies.
#
# Raising it costs more than it looks: 0.97 readmits 7 of the 16 suppressed
# ALERTS (that population is alert rows, not basis-instances — there are 28 of
# those). Readmitting all of them takes 1.00, i.e. no magnitude gate at all —
# and not even then for KUL-CAN-OW bag20, which the `flat` guard holds shut at
# any threshold because its price has never moved.
MAX_PCT_OF_TYPICAL = float(_THRESHOLDS.get("max_pct_of_typical", 0.95))

# Below this many daily readings a percentile is noise dressed as a signal. Until then we fall
# back to Google's own low/typical/high call for that departure date, which judges the ADVERTISED
# fare only; if that is missing we stay silent and say so in the log.
MIN_HISTORY_DAYS = 21

# One dip must not produce a week of pushes.
COOLDOWN_DAYS = 3
# ...unless it keeps falling: a materially better price re-opens the cooldown.
COOLDOWN_OVERRIDE_DROP = 0.08
