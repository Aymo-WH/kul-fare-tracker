#!/usr/bin/env python3
"""Daily flight-price collection.

For each tracked series it prices every departure date in THAT ROUTE'S window,
takes the cheapest fare found across the whole grid, and files it as the day's
reading. `date_grid()` owns the three window shapes; `routes.py` owns which one
each route uses.

WINDOWS ARE PER ROUTE since 6-Sep-2026, and the reason matters more than the
mechanism. Every route used to share one rolling 60-180 day grid re-anchored to
today. That sounds neutral and is not: it compares DIFFERENT TRAVEL DATES on
different days. Measured on KUL-CAN-RT, the cheapest reading moved from RM827
(depart 4-Oct) to RM605 (depart 28-Feb) -- the window drifting out of the
Christmas peak, read by every rank downstream as a price drop. So a real trip
now gets FIXED calendar dates and a watching brief keeps a rolling window, wide
enough (20-270 days) to span a year of seasonality.

THE DAILY READING IS A MINIMUM OVER THAT GRID, which is what makes the window
load-bearing: change the window or the step and readings either side are not
comparable. Widening is especially dangerous because a wider grid is a superset,
so its minimum can only fall -- every widened route steps down on its first run
and reports a record that never happened. routes.py therefore pairs every window
change with a history cutover (each route's `history_since` in the routes config), and
flight_db.history() excludes anything older. NEVER change one without the other.

Only Google Flights is collected. Rows stay keyed by `source` so a second source
could never be blended into one series -- the Amadeus path was removed 6-Sep-2026
(never configured, 0 rows); see tracker/retired/.

Usage:
    python3 collect_flights.py                 # normal cron run
    python3 collect_flights.py --dry-run       # fetch, print, write nothing
    python3 collect_flights.py --routes KUL-HKG-OW   # one series
    python3 collect_flights.py --sample 2      # 2 dates/series, for smoke tests
"""

import argparse
import datetime as dt
import random
import sys
import time
import traceback

import flight_db
import gflights
import routes as cfg

MYT = dt.timezone(dt.timedelta(hours=8))

# Politeness between Google requests. routes.py owns BOTH the measured per-lookup
# rate AND the lookup count -- see its cost block. Restating either here is how one
# number ends up in four files disagreeing; this module owns only the pause below.
GOOGLE_PAUSE = (2.0, 4.5)


def date_grid(today=None, sample=None, route=None):
    """Departure dates to price for one route.

    THREE SHAPES, in precedence order:
      1. route.window_dates -- a FIXED calendar range. Used for a real trip, so
         the same dates are priced every day and a price change is a price
         change (see routes.py FIXED-DATE WINDOWS for the sliding-window bug this fixes).
      2. route.window       -- a rolling (start_days, end_days) override.
      3. the module default -- cfg.WINDOW_START_DAYS..WINDOW_END_DAYS.

    Passing no route keeps shape 3, so --sample smoke tests are unaffected.
    """
    today = today or dt.date.today()
    step = (route.step if route is not None and route.step
            else cfg.STEP_DAYS)

    if route is not None and route.window_dates:
        lo, hi = route.window_dates
        # Never price a date that has passed, nor one too close to book.
        # MIN_LEAD_DAYS is an ASSUMPTION, not a measured value -- see routes.py.
        floor = today + dt.timedelta(days=cfg.MIN_LEAD_DAYS)
        lo = max(lo, floor)
        if lo > hi:
            # The trip window has closed. Return EMPTY rather than silently
            # falling back to a rolling window: a route whose dates have all
            # passed should go visibly quiet, not quietly start tracking
            # something else. The caller logs the empty grid.
            return []
        n = (hi - lo).days
        grid = [lo + dt.timedelta(days=d) for d in range(0, n + 1, step)]
    else:
        start, end = (route.window if route is not None and route.window
                      else (cfg.WINDOW_START_DAYS, cfg.WINDOW_END_DAYS))
        days = list(range(start, end + 1, step))
        grid = [today + dt.timedelta(days=d) for d in days]

    if sample and sample < len(grid):
        # Evenly spaced across the window, not the first N -- the near end of the
        # window is systematically pricier and would bias a truncated sample.
        s = (len(grid) - 1) / (sample - 1) if sample > 1 else 1
        grid = [grid[round(i * s)] for i in range(sample)]
    return grid


def _pair_dates(route, depart):
    if route.trip_type == "return":
        return depart.isoformat(), (depart + dt.timedelta(
            days=route.nights)).isoformat()
    return depart.isoformat(), None


def collect_google(conn, route, grid, run_day, dry_run, log):
    rows_all, failures, priced = [], 0, 0
    for depart in grid:
        d, r = _pair_dates(route, depart)
        try:
            rows, note = gflights.search(
                route.origin, route.dest, d, r,
                max_stops=cfg.MAX_STOPS, adults=cfg.ADULTS,
                seat=cfg.SEAT, currency=cfg.CURRENCY)
            if note:
                log(f"    {d}: {note}")
            if rows:
                priced += 1
            rows_all.extend(rows)
        except Exception as e:                            # noqa: BLE001
            failures += 1
            log(f"    {d}: FAILED {type(e).__name__}: {e}")
        time.sleep(random.uniform(*GOOGLE_PAUSE))

    # Coverage is the early-warning signal for a broken parser. A day where most
    # dates come back unpriced looks identical to a quiet market in the price
    # series, but it is not — so it gets said out loud in the log.
    if priced < len(grid) * 0.5:
        log(f"    WARNING low coverage: only {priced}/{len(grid)} dates priced")
    return rows_all, failures, priced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    ap.add_argument("--routes", nargs="*",
                    help="route keys to limit to (default: all)")
    ap.add_argument("--sample", type=int,
                    help="use N dates instead of the full grid")
    a = ap.parse_args()

    now = dt.datetime.now(MYT)
    run_day = now.date().isoformat()
    stamp = now.isoformat(timespec="seconds")

    def log(msg):
        print(f"[{dt.datetime.now(MYT):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

    selected = [r for r in cfg.ROUTES
                if not a.routes or r.key in a.routes]
    if not selected:
        raise SystemExit(f"no routes matched {a.routes}")

    conn = flight_db.connect()

    # Google is the only source (Amadeus removed 6-Sep-2026, never configured). flight_db still keys
    # rows by source so a second source could be added -- the two minima must never be mixed.
    SOURCE = "google"

    # Report the REAL total, not a default grid no route uses. Every route now
    # overrides its window, so one "N dates (x .. y)" line described nothing
    # that was actually scanned.
    _plan = [(r, date_grid(now.date(), a.sample, r)) for r in selected]
    _total = sum(len(g) for _, g in _plan)
    log(f"run {run_day} — {len(selected)} series, {_total} date-lookups, "
        f"source={SOURCE}{' DRY-RUN' if a.dry_run else ''}")
    for _r, _g in _plan:
        _w = ("fixed " + "..".join(str(d) for d in _r.window_dates)
              if _r.window_dates else f"rolling {_r.window}")
        log(f"    {_r.key}: {len(_g)} dates, {_w}"
            + (f" ({_g[0]} .. {_g[-1]})" if _g else " — WINDOW CLOSED"))


    ok = failed = nquotes = 0
    closed = []
    if not a.dry_run:
        flight_db.start_run(conn, run_day, stamp, SOURCE)
    for route in selected:
        log(f"  [{SOURCE}] {route.key} — {route.label}")
        try:
            # An EMPTY grid is a real, expected state: a fixed-date route
            # whose travel window has passed (e.g. a trip ending 31-Dec).
            # Skip it EXPLICITLY and say so. Falling through would price
            # nothing, record priced=0 and trip the low-coverage warning --
            # a closed trip window would look identical to a broken scraper.
            r_grid = date_grid(now.date(), a.sample, route)
            if not r_grid:
                # RECORDED, not just logged: a closed fixed-date window stops producing readings while ok_series
                # and failed_series stay clean, so without runs.note the health check sees a healthy run (for a trip ending
                # 31-Dec this starts 18-Dec: MIN_LEAD_DAYS closes the grid 14 days earlier).
                # PLAIN PLACE NAME, never the route key: this reaches the phone via the external health check,
                # and translating a key there would mean importing routes.py into it.
                closed.append(route.place)
                log(f"    no dates in window — travel window closed, "
                    f"skipping (route window {route.window_dates})")
                continue
            rows, fails, priced = collect_google(
                conn, route, r_grid, run_day, a.dry_run, log)
            n_dates = len(r_grid)
        except Exception as e:                        # noqa: BLE001
            failed += 1
            log(f"    UNHANDLED {type(e).__name__}: {e}")
            log(traceback.format_exc(limit=3))
            continue

        nquotes += len(rows)
        if not rows:
            failed += 1
            log("    no fares found for any date in the window")
            continue
        ok += 1
        if not a.dry_run:
            flight_db.record_quotes(
                conn, run_day, stamp, route.key, SOURCE, rows)

        # Two bases per series: cheapest as advertised, and cheapest with a 20kg bag priced in. NOT "the
        # same search re-ranked" -- a bag can change WHICH date and carrier wins (flight_alert's module
        # docstring owns that wording).
        for basis in ("nobag", "bag20"):
            best, excluded = flight_db.pick_best(rows, basis)
            if not a.dry_run:
                flight_db.upsert_daily_best(
                    conn, run_day, route.key, SOURCE, best, len(rows),
                    excluded)
            if not best:
                log(f"    [{basis}] nothing priceable"
                    + (f" — {len(excluded)} fare(s) have no known bag fee"
                       if excluded else ""))
                continue
            dur = (f"{best['duration_min'] // 60}h"
                   f"{best['duration_min'] % 60:02d}"
                   if best.get("duration_min") else "?")
            stop = ("direct" if best.get("stops") == 0
                    else f"{best.get('stops')} stop")
            extra = ""
            if basis == "bag20":
                extra = (f" (fare {best['base_fare']:,.0f} + bag "
                         f"{best['bag_fee']:,.0f} {best['fee_kind']})")
                if excluded:
                    extra += f", {len(excluded)} unpriceable"
            log(f"    [{basis}] best MYR {best['price']:,.0f} — "
                f"{best.get('carrier')} {stop} {dur} "
                f"dep {best['depart_date']}{extra}")
        log(f"    ({len(rows)} fares, {priced}/{n_dates} dates priced)")
        conn.commit()

    # THE NOTE IS A WHOLE-RUN FACT WRITTEN BY A RUN THAT MAY BE PARTIAL. `closed` covers only
    # `selected`, while finish_run writes one note for the day that the health check pushes. Three cases
    # (the middle one chosen 6-Sep-2026 over replacing the note outright):
    #   * FULL run           -> replace (definitive, incl. shrinking or clearing the list).
    #   * SUBSET, found some -> MERGE into the last sweep's: never drop closures for routes it did
    #                           not run (routes sharing a trip close the same day under different place names).
    #   * SUBSET, found none -> None, which carries the note forward: a debug command must never clear
    #                           a closure it did not look for.
    _PREFIX = "windows closed: "
    if closed:
        _places = set(closed)
        if a.routes:
            _prior = flight_db.latest_note(conn, SOURCE)
            if _prior.startswith(_PREFIX):
                _places |= {p.strip() for p in _prior[len(_PREFIX):].split(",")
                            if p.strip()}
        _note = _PREFIX + ", ".join(sorted(_places))
    elif a.routes:
        _note = None
    else:
        _note = ""

    if not a.dry_run:
        flight_db.finish_run(
            conn, run_day, SOURCE,
            dt.datetime.now(MYT).isoformat(timespec="seconds"),
            ok, failed, nquotes,
            note=_note)
        conn.commit()
    log(f"  [{SOURCE}] done — {ok} ok, {failed} failed, {nquotes} quotes"
        + (f", {len(closed)} window(s) closed: "
           f"{', '.join(sorted(set(closed)))}" if closed else ""))

    conn.close()

    # Fail loudly for the health check: a run where every series came back empty is
    # a broken collector, not a quiet day in the airline industry.
    if ok == 0:
        log("FATAL: every series came back empty — parser or network broken")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
