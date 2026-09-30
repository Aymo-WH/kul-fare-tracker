#!/usr/bin/env python3
"""SQLite store for the flight tracker.

Two tables carry the weight:

  quotes      — every fare seen, one row per (run, series, sample date).
                Raw material. Measured 31-Aug..5-Sep-2026: ~9-10 rows per
                date-lookup (1,150-1,250 rows on 126 lookups). The lookup count
                is owned by routes.py and changes as windows age, so multiply
                rather than restate it. (This line read "~250 rows/day" until
                6-Sep-2026 -- written before the grid was this wide.)
  daily_best  — ONE row per day per series per source: the cheapest fare found
                anywhere in that route's window that day. This is the series
                the percentile is computed against.

Why daily_best exists rather than percentiling `quotes` directly: quotes mixes
36-61 departure dates whose prices differ by 3x, so its distribution describes
"how much do dates vary" and not "is today cheap". Collapsing to a daily
minimum first is what makes the comparison across days honest.

AND WHY THAT MAKES THE WINDOW LOAD-BEARING: daily_best is a MINIMUM over the
day's grid, so its meaning is defined by the grid. Change the window or the
step and readings either side are not comparable -- history() therefore takes
a `since` cutover, and routes.py sets one on every route whose shape changed.
The measured incident that forced this, and the reasoning, live ONCE: in
docs/DESIGN.md, "Why a real trip gets FIXED dates". Do not restate
it here.

Rows are keyed by `source` so that two price sources could never be mixed into
one series. Only 'google' has ever been written (the Amadeus path was deleted
6-Sep-2026 as never-configured); the column stays because the day a second
source appears is the day mixing them would silently look like a price crash.
The `price_metrics` table below is Amadeus' -- it holds 0 rows and is kept
only so an existing DB does not need a destructive migration.
"""

import json
import os
import sqlite3

DB_PATH = os.environ.get("FLIGHT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "flights.sqlite"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id            INTEGER PRIMARY KEY,
    collected_at  TEXT NOT NULL,
    run_day       TEXT NOT NULL,
    route_key     TEXT NOT NULL,
    source        TEXT NOT NULL,
    depart_date   TEXT NOT NULL,
    return_date   TEXT,
    price         REAL NOT NULL,
    currency      TEXT NOT NULL,
    carrier       TEXT,
    stops         INTEGER,
    duration_min  INTEGER,
    layover       TEXT,
    depart_time   TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_series
    ON quotes (route_key, source, run_day);

-- `basis` splits each series in two: 'nobag' is the fare as advertised, 'bag20'
-- adds a 20kg checked bag (see bag_fees.py). They are separate series with
-- separate histories because the WINNER can differ — a bagless AirAsia fare can
-- beat everything on price and still lose to MAS once a bag is
-- added, which is the whole comparison this exists for.
CREATE TABLE IF NOT EXISTS daily_best (
    run_day       TEXT NOT NULL,
    route_key     TEXT NOT NULL,
    source        TEXT NOT NULL,
    basis         TEXT NOT NULL DEFAULT 'nobag',
    price         REAL NOT NULL,      -- the basis price (fare, or fare + bag)
    base_fare     REAL,               -- the advertised fare before any bag fee
    bag_fee       REAL,               -- what was added; 0 when already included
    fee_kind      TEXT,               -- included | estimate
    currency      TEXT NOT NULL,
    depart_date   TEXT,
    return_date   TEXT,
    carrier       TEXT,
    stops         INTEGER,
    duration_min  INTEGER,
    layover       TEXT,
    depart_time   TEXT,
    n_quotes      INTEGER,
    excluded      TEXT,               -- fares we could not price with a bag
    -- Google's own low/typical/high call for THIS row's depart_date. Added
    -- 24-Jul-2026 as the warm-up baseline in place of Amadeus (no account).
    -- Declared last so a migrated DB and a fresh one have identical column
    -- order — ALTER TABLE can only append.
    verdict       TEXT,
    -- Meal estimate folded into the bag20 basis 4-Aug-2026 (
    -- fairness to budget carriers — see bag_fees.MEAL_FEE_PER_LEG). Appended
    -- after verdict for the same ALTER-only-appends reason.
    meal_fee      REAL,
    PRIMARY KEY (run_day, route_key, source, basis)
);

-- Amadeus' own 1-year price quartiles. This is what lets the tracker answer
-- "cheap vs the past year" on day one, before we own enough history ourselves.
CREATE TABLE IF NOT EXISTS price_metrics (
    run_day       TEXT NOT NULL,
    route_key     TEXT NOT NULL,
    depart_date   TEXT NOT NULL,
    minimum       REAL, q1 REAL, median REAL, q3 REAL, maximum REAL,
    currency      TEXT,
    PRIMARY KEY (run_day, route_key, depart_date)
);

CREATE TABLE IF NOT EXISTS alerts (
    sent_at       TEXT NOT NULL,
    route_key     TEXT NOT NULL,
    source        TEXT NOT NULL,
    price         REAL NOT NULL,
    percentile    REAL,
    basis         TEXT,
    kind          TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts_route ON alerts (route_key, sent_at);

CREATE TABLE IF NOT EXISTS runs (
    run_day       TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    source        TEXT NOT NULL,
    ok_series     INTEGER,
    failed_series INTEGER,
    quotes        INTEGER,
    note          TEXT
);
"""


def _add_missing_columns(conn):
    """Idempotent forward migration for tables that already exist.

    `CREATE TABLE IF NOT EXISTS` is a no-op once the table is there, so a new
    column declared in SCHEMA never reaches a live database. Every added column
    must therefore be listed here too. Kept additive on purpose: ALTER ... ADD
    COLUMN cannot drop or retype anything, so this can never destroy history.
    """
    wanted = {"daily_best": {"verdict": "TEXT", "meal_fee": "REAL"}}
    for table, cols in wanted.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def record_quotes(conn, run_day, collected_at, route_key, source, rows):
    conn.executemany(
        """INSERT INTO quotes (collected_at, run_day, route_key, source,
               depart_date, return_date, price, currency, carrier, stops,
               duration_min, layover, depart_time)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(collected_at, run_day, route_key, source, r["depart_date"],
          r.get("return_date"), r["price"], r["currency"], r.get("carrier"),
          r.get("stops"), r.get("duration_min"), r.get("layover"),
          r.get("depart_time")) for r in rows])


def _cheapest_sensible(candidates):
    """Cheapest fare, but a connection must clearly beat the best direct.

    See routes.DIRECT_PREFERENCE. Saving RM21 for a 21-hour layover is not a
    deal, and offering it as one wastes the message on something nobody will
    never book.
    """
    import routes as cfg

    if not candidates:
        return None
    overall = min(candidates, key=lambda r: r["price"])
    if overall.get("stops") == 0:
        return overall
    directs = [r for r in candidates if r.get("stops") == 0]
    if not directs:
        return overall
    best_direct = min(directs, key=lambda r: r["price"])
    if overall["price"] < best_direct["price"] * (1 - cfg.DIRECT_PREFERENCE):
        return overall
    return best_direct


def pick_best(rows, basis):
    """Cheapest sensible fare on one basis, plus whatever could not be priced.

    For 'bag20' a fare is only a candidate if bag_fees can price it. Carriers
    whose bag policy is ambiguous or unknown are set aside rather than assumed
    free — assuming free would let them win the comparison on an assumption we
    know we cannot support. They are returned so the message can name them.
    """
    import bag_fees

    if basis == "nobag":
        best = _cheapest_sensible(rows)
        if not best:
            return None, []
        return {**best, "basis": "nobag", "base_fare": best["price"],
                "bag_fee": None, "meal_fee": None, "fee_kind": None}, []

    priced, excluded = [], []
    for r in rows:
        fee, kind, note = bag_fees.classify(r.get("carrier"))
        if fee is None:
            excluded.append({"carrier": r.get("carrier"), "price": r["price"],
                             "depart_date": r.get("depart_date")})
            continue
        # Meals folded in 4-Aug-2026: one pre-book meal per leg,
        # so a return trip carries two. Full-service carriers return 0.0 —
        # their fare already includes both bag and meal.
        legs = 2 if r.get("return_date") else 1
        meal = bag_fees.meal_fee(r.get("carrier"), legs)
        priced.append({**r, "basis": "bag20", "base_fare": r["price"],
                       "bag_fee": fee, "meal_fee": meal, "fee_kind": kind,
                       "price": r["price"] + fee + meal})
    return _cheapest_sensible(priced), excluded


def upsert_daily_best(conn, run_day, route_key, source, best, n_quotes,
                      excluded=None):
    """Store one basis' winning fare for the day.

    Nothing is written when `best` is None — an empty result is a real outcome
    (route not served, source down, no bag-priceable carrier) and must never be
    recorded as a price of zero.
    """
    if not best:
        return None
    note = None
    if excluded:
        # Cheapest per CARRIER, not the cheapest three fares — the same airline
        # appears many times across the date grid and would otherwise fill the
        # whole note with one name. Stored as JSON so the departure date travels
        # with the price (22-Jul: dates are needed too) and the message can
        # format it however it likes.
        cheapest = {}
        for e in excluded:
            c = e["carrier"]
            if c not in cheapest or e["price"] < cheapest[c]["price"]:
                cheapest[c] = e
        note = json.dumps(sorted(cheapest.values(),
                                 key=lambda e: e["price"])[:3])
    conn.execute(
        """INSERT INTO daily_best (run_day, route_key, source, basis, price,
               base_fare, bag_fee, meal_fee, fee_kind, currency, depart_date,
               return_date, carrier, stops, duration_min, layover, depart_time,
               n_quotes, excluded, verdict)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_day, route_key, source, basis) DO UPDATE SET
               price=excluded.price, base_fare=excluded.base_fare,
               bag_fee=excluded.bag_fee, meal_fee=excluded.meal_fee,
               fee_kind=excluded.fee_kind,
               currency=excluded.currency, depart_date=excluded.depart_date,
               return_date=excluded.return_date, carrier=excluded.carrier,
               stops=excluded.stops, duration_min=excluded.duration_min,
               layover=excluded.layover, depart_time=excluded.depart_time,
               n_quotes=excluded.n_quotes, excluded=excluded.excluded,
               verdict=excluded.verdict
           WHERE excluded.price < daily_best.price""",
        (run_day, route_key, source, best["basis"], best["price"],
         best.get("base_fare"), best.get("bag_fee"), best.get("meal_fee"),
         best.get("fee_kind"),
         best["currency"], best["depart_date"], best.get("return_date"),
         best.get("carrier"), best.get("stops"), best.get("duration_min"),
         best.get("layover"), best.get("depart_time"), n_quotes, note,
         best.get("verdict")))
    return best


def history(conn, route_key, source, basis="nobag", exclude_day=None,
            since=None):
    """Past daily minima for one series+basis, oldest first, excluding today.

    SINCE is the HISTORY CUTOVER (added 6-Sep-2026 at pre-deploy review, and it
    BLOCKED that push). Readings taken before a route's scan window changed are
    NOT comparable with readings taken after it, and averaging the two is the
    same defect the window change exists to fix.

    Why it matters concretely: a daily reading is the MINIMUM over that day's
    grid. Change the grid and you change what the minimum means. The fixed-date routes moved
    from a sliding 60-180 day window (which on 6-Sep reached 5-Mar-2027) to a
    fixed 1-Nov..31-Dec grid. Ranking a fixed-window reading against 46
    sliding-window ones would print "lowest of 47 daily checks" for a record
    that never happened -- on the most alert-worthy day, because the trigger
    selects for exactly those days.

    Passing no `since` keeps every row, so untouched series are unaffected.
    """
    sql = ("SELECT run_day, price FROM daily_best "
           "WHERE route_key=? AND source=? AND basis=?")
    args = [route_key, source, basis]
    if exclude_day:
        sql += " AND run_day < ?"
        args.append(exclude_day)
    if since:
        sql += " AND run_day >= ?"
        args.append(str(since))
    sql += " ORDER BY run_day"
    return conn.execute(sql, args).fetchall()


# No coverage() here: its only output line was cut (6-Sep-2026) and nothing else read it.
def grid_widths(conn, route_key, since, source="google"):
    """{run_day: distinct departure dates priced} for that route, on/after `since`.

    THE CUTOVER IS OTHERWISE UNVERIFIED, which is what this exists for. A
    `history_since` in routes.py is a date somebody typed; nothing made the new
    window actually be live on that date. If a deploy slips, the old narrow grid
    keeps running and its readings land at `run_day >= since` -- admitted into
    the "clean" history, and for a widened route in the direction that FIRES
    alerts, since the newer wide-grid minima sit below an inflated median.

    The window shape is recoverable from the data: `quotes` holds one row per
    (run, series, departure date), so the count of distinct departure dates on a
    day IS the grid size that day. A day that disagrees with today's grid was
    collected by different code. Cheap: one grouped query, bounded by `since`.
    """
    # `source` is constrained so ix_quotes_series (route_key, source, run_day) keeps the run_day range
    # usable. since=None is refused: it would str() to "None", every ISO date sorts below it, and the
    # query would silently return {} -- an empty answer that reads as "clean".
    if since is None:
        raise ValueError(
            "grid_widths() needs a cutover date; None silently returns {} "
            "because every ISO run_day sorts below the string 'None'")
    rows = conn.execute(
        """SELECT run_day, COUNT(DISTINCT depart_date) AS n FROM quotes
           WHERE route_key=? AND source=? AND run_day >= ?
           GROUP BY run_day""",
        (route_key, source, str(since))).fetchall()
    return {r["run_day"]: r["n"] for r in rows}


# latest_metrics() lived here and was deleted 6-Sep-2026 with the Amadeus
# path -- its only caller was flight_alert's warm-up fallback, and the table
# it read has never held a row. See the module docstring.
def last_alert(conn, route_key):
    return conn.execute(
        "SELECT * FROM alerts WHERE route_key=? ORDER BY sent_at DESC LIMIT 1",
        (route_key,)).fetchone()


def start_run(conn, run_day, started_at, source):
    conn.execute(
        "INSERT INTO runs (run_day, started_at, source) VALUES (?,?,?)",
        (run_day, started_at, source))


def latest_note(conn, source):
    """The note on the most recent FINISHED run for this source, or "".

    Exists so a --routes subset can MERGE its findings into what the last full
    sweep recorded, rather than replacing it (chosen 6-Sep-2026).
    Deliberately NOT scoped to run_day: the note describes the state of the
    tracked trips, which does not reset at midnight.

    Ordering matches the external health check's read, plus a rowid
    tiebreak -- two runs can finish in the same second, and without the
    tiebreak the two would disagree about which row is current.
    """
    r = conn.execute(
        """SELECT note FROM runs
           WHERE source=? AND finished_at IS NOT NULL
           ORDER BY finished_at DESC, rowid DESC LIMIT 1""",
        (source,)).fetchone()
    return (r[0] if r and r[0] is not None else "")


def finish_run(conn, run_day, source, finished_at, ok, failed, quotes, note=""):
    """note=None CARRIES THE SERIES' EXISTING NOTE FORWARD; "" clears it.

    The distinction exists because runs.note is a WHOLE-RUN fact (which travel
    windows have closed) while a --routes subset only knows about the routes it
    ran. Overwriting from a partial run would let a debug command erase a
    genuine closed-window state and hand the health check a false green.
    """
    if note is None:
        # CARRY THE DAY'S NOTE FORWARD: start_run() INSERTs a new row each invocation and the health check reads
        # the newest finished row, so a NULL note here re-opens the false green. The note follows the
        # SERIES, not the calendar day (scoping to run_day let a morning --routes run clear it for ~16h),
        # and a deliberate clear must STICK (skipping '' resurrected an old closure as a false RED). So:
        # the most recent FINISHED row for this source, whatever its value, excluding the row being
        # updated; "" carries forward as "".
        target = conn.execute(
            "SELECT MAX(rowid) FROM runs WHERE run_day=? AND source=?",
            (run_day, source)).fetchone()
        prior = conn.execute(
            """SELECT note FROM runs
               WHERE source=? AND finished_at IS NOT NULL AND rowid != ?
               ORDER BY finished_at DESC, rowid DESC LIMIT 1""",
            (source, target[0] if target else -1)).fetchone()
        conn.execute(
            """UPDATE runs SET finished_at=?, ok_series=?, failed_series=?,
                   quotes=?, note=?
               WHERE rowid = (SELECT MAX(rowid) FROM runs
                              WHERE run_day=? AND source=?)""",
            (finished_at, ok, failed, quotes,
             (prior[0] if prior and prior[0] is not None else ""),
             run_day, source))
        return
    conn.execute(
        """UPDATE runs SET finished_at=?, ok_series=?, failed_series=?,
               quotes=?, note=?
           WHERE rowid = (SELECT MAX(rowid) FROM runs
                          WHERE run_day=? AND source=?)""",
        (finished_at, ok, failed, quotes, note, run_day, source))
