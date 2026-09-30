#!/usr/bin/env python3
"""Rebuild daily_best from raw quotes (22-Jul-2026; re-run 4-Aug-2026).

Re-run 4-Aug-2026 after MEALS joined the bag20 basis (bag_fees.MEAL_FEE_PER_LEG)
and HK Express gained a bag fee: rebuilding recomputes every historical row
under the new semantics, so the percentile history stays comparable instead of
mixing bag-only rows with bag+meal rows.

`daily_best` originally had PRIMARY KEY (run_day, route_key, source) and held
only the advertised fare. Adding the with-bag comparison needs `basis` in the
key, which SQLite cannot do with ALTER TABLE.

Almost nothing is lost, because `quotes` holds every individual fare ever seen —
both bases are simply recomputed from it. THE ONE EXCEPTION (code review, round 2,
4-Aug-2026): Google's `verdict` column lives ONLY on daily_best (the collector
stores it there since 24-Jul; `quotes` never carried it), so a bare rebuild would
NULL every verdict — destroying the warm-up baseline and firing a false
`flight-verdict` health-check alarm. It is therefore stashed by stash_verdicts()
BEFORE ANY RENAME OR DELETE, and restored after the rebuild.

Both the ORDER and the DURABILITY of that stash were wrong once each, on
consecutive review rounds — read stash_verdicts' docstring before touching the
top of main().

Safe to re-run: it rebuilds from quotes each time, refuses to drop the old
table unless every old series-day KEY reappears (key sets, not counts — a
count passes when unrelated new series-days offset a lost row), warns loudly
when a re-run shrinks the table, and keeps the `verdict_stash` table until
every stashed verdict is back on a live row.
"""

import sys

try:                                       # em-dashes mojibake on a Windows console;
    sys.stdout.reconfigure(encoding="utf-8")  # so guard the reconfigure
except Exception:                          # noqa: BLE001
    pass

import flight_db  # noqa: E402


STASH_TABLE = "verdict_stash"


def stash_verdicts(conn):
    """Copy Google's verdicts into a DURABLE table before anything is destroyed.

    Two review rounds shaped this, and both lessons are load-bearing:

    ORDER (round 2). The first fix read the verdicts AFTER the rename, so on the
    migration path it queried the freshly-created EMPTY daily_best, reported
    "stashed 0" with no warning, and then dropped the old table.

    DURABILITY (round 3). The second fix held the stash in memory and tried to
    undo a bad run with conn.rollback(). That cannot work: sqlite3 runs DDL in
    autocommit, so the RENAME and CREATE are already durable and a rollback
    leaves a committed-but-EMPTY daily_best while printing "nothing was written"
    — and the obvious next move, re-running, takes the re-run branch and drops
    the old table for real. So the stash is now a TABLE. It survives a crash
    and a refusal, and it is the thing a recovery reads. One honest limit
    (round 4): INSERT OR REPLACE means each run RE-STASHES from the current
    daily_best, so it protects the state as of THIS run — it is not an archive
    of every verdict that ever existed, and a verdict deliberately cleared
    between runs stays cleared only if the stash was already retired.

    `verdict` is the only column here that `quotes` cannot rebuild, which is the
    entire reason any of this exists. Legacy rows predate `basis` and were all
    advertised fares, so they map onto 'nobag'. Rows come back as sqlite3.Row
    (flight_db sets that row_factory); the caller unpacks positionally.
    """
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {STASH_TABLE} (
                         run_day TEXT, route_key TEXT, source TEXT,
                         basis TEXT, verdict TEXT,
                         PRIMARY KEY (run_day, route_key, source, basis))""")
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "daily_best" in have:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_best)")}
        if "verdict" in cols:                  # absent = older than 24-Jul-2026
            basis = "basis" if "basis" in cols else "'nobag'"
            conn.execute(
                f"""INSERT OR REPLACE INTO {STASH_TABLE}
                    SELECT run_day, route_key, source, {basis}, verdict
                    FROM daily_best WHERE verdict IS NOT NULL""")
    conn.commit()                              # durable BEFORE the first DELETE
    return conn.execute(
        f"SELECT run_day, route_key, source, basis, verdict FROM {STASH_TABLE}"
    ).fetchall()


def restore_verdicts(conn, stashed):
    """Put the stashed verdicts back, CLASSIFYING every gap. -> (n, orphans, bad)

    A bare count was not enough (round 3): "stashed 3, restored 2" printed a
    warning, dropped the only other copy and exited 0. The two kinds of gap are
    not the same thing, and only one of them is safe:

      ORPHAN       the series-day is not in daily_best at all, because its
                   quotes are gone. Explainable, and harmless as long as the
                   stash row itself is kept.
      UNEXPLAINED  the row EXISTS and did not take its verdict. The stash and
                   the rebuild disagree about the key — a bug, and the one that
                   quietly erases history.

    A separate function ONLY so both branches can be tested against real rows.
    They are unreachable on an empty database, which is exactly the shape that
    let a broken health check deploy green on 3-Aug-2026.
    """
    restored, orphans, unexplained = 0, [], []
    for run_day, route_key, source, basis, verdict in stashed:
        key = (run_day, route_key, source, basis)
        n = conn.execute(
            """UPDATE daily_best SET verdict=? WHERE run_day=? AND route_key=?
               AND source=? AND basis=? AND verdict IS NULL""",
            (verdict, *key)).rowcount
        if n:
            restored += 1
        elif not conn.execute(
                """SELECT 1 FROM daily_best WHERE run_day=? AND route_key=?
                   AND source=? AND basis=?""", key).fetchone():
            orphans.append(key)
        else:
            unexplained.append(key)
    return restored, orphans, unexplained


def _self_test():
    """The three restore classifications, against real rows in a scratch DB."""
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as td:
        conn = flight_db.connect(f"{td}/t.sqlite")
        common = ("MYR", "2026-10-04", "2026-10-11", "AirAsia", 1)
        conn.executemany(
            """INSERT INTO daily_best (run_day, route_key, source, basis, price,
                   currency, depart_date, return_date, carrier, n_quotes, verdict)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [("2026-08-01", "R1", "google", "nobag", 500.0, *common, None),
             ("2026-08-01", "R1", "google", "bag20", 760.0, *common, "high")])
        conn.commit()
        stash = [("2026-08-01", "R1", "google", "nobag", "low"),      # restores
                 ("2026-08-01", "R1", "google", "bag20", "typical"),  # occupied
                 ("2026-07-30", "GONE", "google", "nobag", "low")]    # orphan
        n, orphans, bad = restore_verdicts(conn, stash)
        if n != 1:
            fails.append(f"restored {n}, want 1")
        if [k[1] for k in orphans] != ["GONE"]:
            fails.append(f"orphans wrong: {orphans}")
        if [k[3] for k in bad] != ["bag20"]:
            fails.append(f"unexplained wrong: {bad}")
        got = conn.execute("SELECT verdict FROM daily_best WHERE basis='nobag'"
                           ).fetchone()[0]
        if got != "low":
            fails.append(f"the NULL row did not take its verdict: {got}")
        kept = conn.execute("SELECT verdict FROM daily_best WHERE basis='bag20'"
                            ).fetchone()[0]
        if kept != "high":
            fails.append(f"an occupied verdict was overwritten: {kept}")
        conn.close()
    for f in fails:
        print(f"  FAIL {f}")
    print("SELF-TEST PASSED (5 checks: restore / orphan / unexplained / "
          "value written / occupied row untouched)" if not fails
          else f"SELF-TEST FAILED ({len(fails)})")
    return 1 if fails else 0


def main():
    conn = flight_db.connect()

    # FIRST, before any rename or delete.
    verdicts = stash_verdicts(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_best)")}
    if "basis" in cols:
        print("daily_best already has `basis` — checking coverage instead")
    else:
        print("migrating daily_best -> basis-aware schema")
        conn.execute("ALTER TABLE daily_best RENAME TO daily_best_pre_bags")
        conn.executescript(flight_db.SCHEMA)

    old_rows = conn.execute(
        "SELECT COUNT(*) FROM daily_best_pre_bags").fetchone()[0] \
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='daily_best_pre_bags'").fetchone() else None
    # Row parity on the RE-RUN path too (code review, 4-Aug-2026): `old_rows` is None
    # when there is nothing to migrate, and without this a re-run could quietly
    # shrink daily_best (a series whose quotes no longer exist simply vanishes).
    pre_rows = conn.execute("SELECT COUNT(*) FROM daily_best").fetchone()[0]

    series = conn.execute(
        "SELECT DISTINCT run_day, route_key, source FROM quotes").fetchall()
    nq = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    print(f"recomputing {len(series)} series-days from {nq} quotes")

    # Clear first. upsert_daily_best only overwrites on a STRICTLY lower price —
    # correct during a day's collection, but it makes this rebuild a no-op on a
    # re-run, silently leaving stale rows (caught 22-Jul: a bag-fee correction
    # appeared to have no effect). Safe to delete: every PRICE row here is
    # derived from `quotes`, which is the durable record (verdicts stashed above).
    conn.execute("DELETE FROM daily_best")

    written = 0
    for run_day, route_key, source in series:
        rows = [dict(r) for r in conn.execute(
            """SELECT price, currency, carrier, stops, duration_min, layover,
                      depart_time, depart_date, return_date
               FROM quotes WHERE run_day=? AND route_key=? AND source=?""",
            (run_day, route_key, source))]
        for basis in ("nobag", "bag20"):
            best, excluded = flight_db.pick_best(rows, basis)
            if flight_db.upsert_daily_best(conn, run_day, route_key, source,
                                           best, len(rows), excluded):
                written += 1

    restored, orphans, unexplained = restore_verdicts(conn, verdicts)

    print(f"verdicts: stashed {len(verdicts)}, restored {restored}"
          + (f", orphaned {len(orphans)}" if orphans else "")
          + (f", UNEXPLAINED {len(unexplained)}" if unexplained else ""))
    if unexplained:
        conn.commit()   # the rebuild itself is sound; only the verdicts are not
        print(f"\nREFUSING to finish: {len(unexplained)} verdict(s) belong to "
              f"rows that EXIST but did not take them, e.g. {unexplained[:3]}.\n"
              f"  Nothing has been dropped. This run's verdicts are still in "
              f"the `{STASH_TABLE}` table, and `daily_best_pre_bags` (if this "
              f"was a migration) is still there too.\n"
              f"  Do NOT re-run until the key mismatch is understood — a re-run "
              f"restores from the same stash and will hit the same wall.")
        return 1
    if orphans:
        print(f"  {len(orphans)} stashed verdict(s) have no row to return to "
              f"(their quotes are gone), e.g. {orphans[:3]}. Keeping the "
              f"`{STASH_TABLE}` table so they stay recoverable.")
    conn.commit()

    new_rows = conn.execute("SELECT COUNT(*) FROM daily_best").fetchone()[0]
    nobag = conn.execute(
        "SELECT COUNT(*) FROM daily_best WHERE basis='nobag'").fetchone()[0]
    bag = conn.execute(
        "SELECT COUNT(*) FROM daily_best WHERE basis='bag20'").fetchone()[0]

    print(f"\nwrote {written} rows -> daily_best now {new_rows} "
          f"({nobag} nobag, {bag} bag20)")
    if old_rows is None and new_rows < pre_rows:
        print(f"WARNING: daily_best SHRANK on this re-run ({pre_rows} -> "
              f"{new_rows}). A series-day that had a row no longer has quotes "
              f"backing it. Investigate before trusting today's percentiles.")
    if old_rows is not None:
        print(f"previous table held {old_rows} rows")
        # Every old row must reappear as a nobag row before the old table can
        # go. Compared as KEY SETS, not counts (round 4): a count passes when
        # unrelated new series-days offset a legacy row that failed to rebuild
        # — which is precisely a row whose price/n_quotes exist NOWHERE else.
        # The verdict stash cannot save it; it holds verdicts, not rows.
        old_keys = {tuple(r) for r in conn.execute(
            "SELECT run_day, route_key, source FROM daily_best_pre_bags")}
        new_keys = {tuple(r) for r in conn.execute(
            "SELECT run_day, route_key, source FROM daily_best "
            "WHERE basis='nobag'")}
        lost = sorted(old_keys - new_keys)
        if lost:
            print(f"REFUSING to drop the old table: {len(lost)} of its "
                  f"series-day(s) did not reappear, e.g. {lost[:3]}. Their "
                  f"quotes are gone, so daily_best_pre_bags is their ONLY "
                  f"copy. Recover or accept them explicitly before rerunning.")
            return 1
        conn.execute("DROP TABLE daily_best_pre_bags")
        conn.commit()
        print(f"verified all {len(old_keys)} old series-days reappeared; "
              f"dropped daily_best_pre_bags")

    # The stash is only redundant once every verdict is back on a live row. Any
    # gap at all keeps it: it is then the ONLY copy of data `quotes` cannot
    # rebuild, and it costs a handful of rows to keep.
    if verdicts and restored == len(verdicts):
        conn.execute(f"DROP TABLE {STASH_TABLE}")
        conn.commit()
        print(f"all {restored} verdicts back on live rows; dropped {STASH_TABLE}")

    print("\nper-series result:")
    for r in conn.execute(
            """SELECT route_key, basis, price, base_fare, bag_fee, meal_fee,
                      carrier, excluded
               FROM daily_best ORDER BY route_key, basis"""):
        extra = ""
        if r["basis"] == "bag20" and r["bag_fee"] is not None:
            meal = f" + meals {r['meal_fee']:,.0f}" if r["meal_fee"] else ""
            extra = f"  (fare {r['base_fare']:,.0f} + bag {r['bag_fee']:,.0f}{meal})"
        if r["excluded"]:
            extra += f"  excluded: {r['excluded']}"
        print(f"  {r['route_key']:<12} {r['basis']:<6} "
              f"RM{r['price']:>8,.0f}  {r['carrier'] or '?':<22}{extra}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
