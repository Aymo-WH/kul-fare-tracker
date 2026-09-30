#!/usr/bin/env python3
"""Decide whether today's fare is worth waking the phone for, and say it well.

Trigger: a fare must be **rare AND materially cheap** — in the bottom 20% of its
own history (set 21-Jul-2026) AND at or below MAX_PCT_OF_TYPICAL of that
history's median (added 22-Aug-2026, after 11 of 25 alerts turned out to be
worth RM8–RM38). A rank on its own is not a deal: it fires on a fixed share of
days no matter what prices do. See routes.py for the measured reasoning.
Silence is the normal state — a quiet phone means nothing worth acting on.

Each route is judged on TWO bases (22-Jul-2026: I may or may not bring
checked bags): the fare as advertised, and the cheapest fare once a 20kg bag
AND pre-book meals are priced in (meals joined 4-Aug-2026 --
fairness to budget carriers, so the second basis is a full-service-comparable
price). **Either being cheap fires the alert, and the message always shows
both** -- because the winner frequently differs, and a bagless AirAsia fare
that loses to MAS once a bag is added is exactly the comparison
this is for. The cooldown stays keyed on the ROUTE, so this never doubles the
number of messages.

THE TWO BASES ARE SEPARATE MINIMA OVER THE SAME GRID, not one flight priced
two ways -- adding a bag can change WHICH departure date and carrier is
cheapest, and measured on 5-Sep-2026 it did (KUL<->HKG: 13 Jan without a bag,
17 Feb with one). Each basis therefore prints its own flight line. Do not
reword this as "the same search re-ranked"; it was, until 6-Sep-2026, and the
sentence was wrong.

The honesty rules this module is built around:

  * A percentile drawn from a handful of readings is noise wearing a signal's
    clothes. Below MIN_HISTORY_DAYS we do not claim one -- we fall back to
    Google's own low/typical/high call for that date, which speaks only for
    the advertised fare, and if there is none we stay silent and log why. We
    never round "we don't know yet" up to "cheap". (An Amadeus-quartile
    fallback was named here until 6-Sep-2026; it never ran a single time --
    see assess().)
  * Every message states what the comparison is against and how deep it is.
  * The bag figure is an assumption and is always shown as one, with the fee it
    added. Fares whose bag policy we cannot establish are named rather than
    silently dropped or silently assumed free (see bag_fees.py).

Usage:
    python3 flight_alert.py                # cron: evaluate and push
    python3 flight_alert.py --preview      # render, send nothing  (ALWAYS FIRST)
    python3 flight_alert.py --force KUL-HKG-RT   # render one regardless of trigger
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.environ.get("NOTIFY_DIR", os.path.dirname(os.path.abspath(__file__))))

import bag_fees
import flight_db
import notify
import routes as cfg

MYT = dt.timezone(dt.timedelta(hours=8))

HEADLINE_SOURCE = "google"   # full grid + full carrier coverage incl. AirAsia
BASES = ("nobag", "bag20")
# Google's own price call, stored per daily_best row by the collector. Anything
# outside this set is treated as "no verdict" rather than guessed at.
VERDICT_WORDS = ("low", "typical", "high")
# _bag_line() owns the basis wording (a second copy was deleted 4-Aug-2026). The SHORT forms in
# _standing_block's typical line ("no bag" / "all-in") are deliberate: that line compares two
# numbers, and the rule is less information, not more.


def fmt_day(iso):
    # NOT "%-d": that is a glibc extension. It works on Linux and raises
    # ValueError on Windows, which is where every pre-deploy test runs -- so the
    # one line using this (test_push's provenance line) could not be exercised
    # before it reached the phone. Formatting the day number by hand is
    # portable and produces the identical string.
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def fmt_dur(minutes):
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes else None


def percentile_of(price, hist_prices):
    """Share of past readings STRICTLY cheaper than today. 0 = nothing beat it.

    STRICTLY is the load-bearing word, and its complement is not the same
    function. `100 - percentile_of(...)` is the share NOT strictly cheaper,
    which silently counts every TIE as a day today beat: on a fare that never
    moves every reading ties, so the complement reads 100 about a price today
    merely equals.

    Live on 22-Aug-2026: KUL->CAN bag20 held RM602 for every reading without
    moving once, computed percentile 0, and the message rendered "CHEAPEST
    EVER" -- re-firing every 3 days as the cooldown lapsed, about a constant.
    That superlative is retired, and the same strictly-below counting is what
    now makes `rank` honest: a fare matching a standing low comes out
    "joint-lowest", never "lowest".

    A companion `beats_pct()` (strictly-greater) rendered the retired "cheaper
    than N%" chip and was deleted 6-Sep-2026 with it. The tie trap did not go
    with it -- it lives here now, on the function that still decides the
    trigger.
    """
    if not hist_prices:
        return None
    return 100.0 * sum(1 for p in hist_prices if p < price) / len(hist_prices)


def assess(conn, route, run_day, basis):
    """Verdict for one route on one basis, or None when there is no reading."""
    today = conn.execute(
        """SELECT * FROM daily_best
           WHERE run_day=? AND route_key=? AND source=? AND basis=?""",
        (run_day, route.key, HEADLINE_SOURCE, basis)).fetchone()
    if not today:
        return None

    # `since` drops readings taken before this route's window changed. Without
    # it the fixed Nov-Dec grid would be ranked against sliding-window history
    # -- see flight_db.history() and each route's history_since.
    hist = flight_db.history(conn, route.key, HEADLINE_SOURCE, basis,
                             exclude_day=run_day,
                             since=getattr(route, "history_since", None))
    prices = [h["price"] for h in hist]
    v = {"route": route, "basis": basis, "today": today, "n_hist": len(prices)}

    if len(prices) >= cfg.MIN_HISTORY_DAYS:
        pct = percentile_of(today["price"], prices)
        ordered = sorted(prices)
        typical = ordered[len(ordered) // 2]

        # A series that has never moved carries no information about whether today is cheap:
        # suppressed explicitly and named in the log (since 22-Aug-2026).
        # `==`, not `>=`: this flag also LABELS the log line, so it must mean "nothing has moved, today
        # included" -- a price ABOVE a flat shelf has moved (the rank gate holds it back). A break BELOW
        # the shelf is NOT flat and stays eligible; the ratio gate decides whether it is deep enough.
        flat = ordered[0] == ordered[-1] and today["price"] == ordered[0]

        # Rare AND cheap — see cfg.MAX_PCT_OF_TYPICAL for why a rank alone is
        # not a deal. Ratio is guarded because a zero typical would divide.
        rare = pct <= cfg.PERCENTILE_TRIGGER
        cheap = typical > 0 and today["price"] <= typical * cfg.MAX_PCT_OF_TYPICAL

        # rank counts readings STRICTLY below today, so ties never inflate it: a fare equal to the
        # standing low is rank 1 with tied=True, rendered "joint-lowest", never "lowest".
        rank = sum(1 for p in prices if p < today["price"]) + 1
        tied = any(p == today["price"] for p in prices)
        # LATEST day the low was hit, not the earliest: min() returns the FIRST minimum, and on a series
        # that keeps tying its low that printed "3 weeks ago" when it was also hit yesterday. Recency is
        # why the date is shown (is the floor holding, or did I just miss it?).
        _low = min(h["price"] for h in hist)
        low_day = max(h["run_day"] for h in hist if h["price"] == _low)

        # The tie trap (a complement of a strictly-below count counts ties as wins) lives on
        # percentile_of(); docs/DESIGN.md gotcha 9.
        v.update(source_basis="own", percentile=pct,
                 rank=rank, tied=tied, low_day=low_day,
                 # `high=ordered[-1]` was here and read by nothing -- the same
                 # discarded-computation shape as coverage() and beats_pct(),
                 # removed 6-Sep-2026. `low` IS read (the "previous best" line).
                 typical=typical, low=ordered[0],
                 since=hist[0]["run_day"],
                 flat=flat, rare=rare, cheap=cheap,
                 triggered=rare and cheap and not flat)
        return v

    # WARM-UP BASELINE: Google's OWN low/typical/high call for this departure date, collected in the
    # same fetch as the fare. (The Amadeus branch that fed a year-long median into "normal day" was
    # deleted 6-Sep-2026 -- tracker/retired/README.md.)
    # It judges the ADVERTISED fare, so it may only speak for the nobag basis: our bag20 price is
    # arithmetic Google never saw. Only 'low' triggers; 'typical' and 'high' are recorded, never fire.
    if basis == "nobag" and today["verdict"] in VERDICT_WORDS:
        v.update(source_basis="google", percentile=None, typical=None,
                 verdict=today["verdict"], since=None,
                 triggered=today["verdict"] == "low")
        return v

    v.update(source_basis="none", triggered=False, percentile=None,
             typical=None, since=None)
    return v


def counterpart_oneway(conn, route, run_day):
    """Today's one-way price for the same city pair, if we track one.

    A return that costs less than its own one-way is a real and actionable quirk
    on full-service carriers, worth a line in the message.
    """
    if route.trip_type != "return":
        return None
    ow = next((r for r in cfg.ROUTES
               if r.trip_type == "oneway" and r.origin == route.origin
               and r.dest == route.dest), None)
    if not ow:
        return None
    row = conn.execute(
        """SELECT price FROM daily_best WHERE run_day=? AND route_key=?
           AND source=? AND basis='nobag'""",
        (run_day, ow.key, HEADLINE_SOURCE)).fetchone()
    return row["price"] if row else None


def _short_place(layover):
    """'Singapore Changi Airport in Singapore' -> 'Singapore'.

    Google spells layovers as '<Airport> in <City>'. The airport name is the
    long half; the city is what a person needs at a glance on a phone. Left
    unshortened, one layover eats three lines of the message.
    """
    if not layover:
        return "?"
    if " in " in layover:
        return layover.rsplit(" in ", 1)[1].strip()
    return layover.replace(" International Airport", "").strip()


def _span_phrase(since_iso, today=None):
    """Plain-language length of the history we are comparing against.

    The ask was "% vs the past 1 year". We must not SAY "past year" until
    we actually have one — the phrase is generated from the real span, so the
    message upgrades itself from "the last 3 weeks" to "the past year" as the
    series matures instead of overclaiming from day one.
    """
    days = ((today or dt.date.today()) - dt.date.fromisoformat(since_iso)).days
    if days >= 330:
        return "the past year"
    if days >= 60:
        return f"the past {round(days / 30.4)} months"
    if days >= 14:
        return f"the past {round(days / 7)} weeks"
    return f"the past {max(days, 1)} days"


def fmt_date(iso, today=None):
    """'(Sun) 18 Oct', or "(Sun) 17 Jan '27" when it crosses into another year.

    Weekday first, because which day of the week a flight leaves usually decides
    whether it is bookable at all. The year is added only when it differs from
    today's — the search window runs 6 months out, so a bare "17 Jan" read in
    July is genuinely ambiguous.
    """
    d = dt.date.fromisoformat(iso)
    today = today or dt.date.today()
    year = f" '{d:%y}" if d.year != today.year else ""
    return f"({d:%a}) {d.day} {d:%b}{year}"


def _standing_block(verdicts, today=None):
    """WHAT THIS IS COMPARING — the answer to "cheap compared to WHAT?".

    Promoted to its own labelled section 22-Jul-2026 and RETITLED 6-Sep-2026.
    It used to lead with "cheaper than 97%" and was headed HOW CHEAP IS THIS,
    which answered a question nobody was asking. The real one was blunter --
    "whats the trend? what is this tracking? what do you mean trend?" -- and
    nothing in the message had ever said what the numbers were measured
    against. So this block now states the comparison in plain words: how often
    we price the route, how many checks back it goes, that "normal day" is the
    MIDDLE reading and not an average, and that today's cheapest may be a
    different departure date from yesterday's.
    """

    # The per-option lines now carry the percentages, so this block's job is to
    # say what backs them: how much history, and how much work went into today.
    own = [(b, v) for b, v in verdicts.items() if v["source_basis"] == "own"]
    if own:
        # WHAT IS BEING TRACKED, in words -- the most important part of the message (the 6-Sep-2026
        # question "what is this tracking?"). It says "normal day" is the MIDDLE reading, not an average,
        # and warns that today's cheapest may be a different departure date from yesterday's.
        # The count is the DEEPEST basis (this line answers "how much do we know"; each option's rank
        # carries its own denominator). n_hist EXCLUDES today and `typical` is the median of exactly
        # those readings, so print n_hist, not n_hist + 1; today is named by the rank line.
        deepest = max((v for _, v in own), key=lambda v: v["n_hist"])
        n = deepest["n_hist"]
        span = _span_phrase(deepest["since"], today)
        typ = [f"RM {v['typical']:,.0f} {'no bag' if b == 'nobag' else 'all-in'}"
               for b, v in sorted(own, key=lambda kv: BASES.index(kv[0]))]
        # WIDTH IS A CORRECTNESS PROPERTY: a 67-char line broke mid-number on the phone ("RM 1,3" /
        # "80 all-in"). Every line below is <= 46 characters; RE-MEASURE after any edit.
        out = ["WHAT THIS IS COMPARING",
               "We price this route once a day and keep the",
               f"cheapest. {n} checks over {span}.",
               '"Normal day" is the MIDDLE of those readings —',
               "half were dearer, half cheaper:"] \
            + [f"  {t}" for t in typ] \
            + ["Today's cheapest may be a different departure",
               "date from yesterday's."]
        # No "Today: N fares over M dates" line (cut 6-Sep-2026): provenance, not a booking fact.
        return out + [""]

    # Google's own call, used while our history warms up. NOT turned into a percentage: it is a
    # three-way judgement, and "cheaper than N%" would manufacture precision. The price line stays
    # bare, per the rule that omits percentages when there is nothing to compute them from.
    gv = next((v for v in verdicts.values()
               if v["source_basis"] == "google"), None)
    if gv:
        # Same heading as the own-history block, so the message keeps one shape whatever the history
        # depth. It NAMES THE BASIS: the verdict speaks only for the advertised fare, but two prices
        # are shown.
        return ["WHAT THIS IS COMPARING",
                f"Only {gv['n_hist']} of {cfg.MIN_HISTORY_DAYS} days of our own data",
                "so far, so this is Google's call and not ours:",
                f"it rates this date {gv['verdict'].upper()} against its own",
                "history. That verdict is about the no-bag",
                "fare only."] + [""]

    # SAME HEADING AS THE OTHER TWO BRANCHES, so the message keeps one shape. Reachable whenever
    # Google omits its insight block during warm-up.
    n = next(iter(verdicts.values()))["n_hist"]
    return ["WHAT THIS IS COMPARING",
            f"Nothing yet — {n} of {cfg.MIN_HISTORY_DAYS} daily checks done,",
            "and Google gave no verdict for this date."] + [""]


def _ordinal(n):
    """1st, 2nd, 3rd, 4th... Plain English beats '#3' on a phone."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _ago(day, today=None):
    """How long ago a record was set, in the words a person would use.

    Added 6-Sep-2026: the record's DATE matters because "best ever
    RM580 three weeks ago" and "best ever RM580 yesterday" are different
    signals -- one says the floor is holding and I can wait, the other says I
    just missed it.
    """
    today = today or dt.date.today()
    d = (today - dt.date.fromisoformat(str(day))).days
    if d <= 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 14:
        return f"{d} days ago"
    if d < 60:
        return f"{round(d / 7)} weeks ago"
    return f"{round(d / 30.4)} months ago"


def _bag_line(t, basis):
    """Bag+meal status as ONE line (22-Jul: the label and the caveat were
    two lines saying the same thing). Always states what is inside the price,
    and where a figure was assumed, what was assumed. Meals joined the basis
    4-Aug-2026 (fairness to budget carriers)."""
    fee, kind, _ = bag_fees.classify(t["carrier"])
    if basis == "nobag":
        if kind in (bag_fees.AMBIG, bag_fees.UNKNOWN):
            return "No checked bag (may already include one — varies by fare)"
        return "No checked bag"
    if kind == bag_fees.INCLUDED:
        return f"With {bag_fees.BAG_KG}kg bag + meals (included in this fare)"
    meal = t["meal_fee"] if "meal_fee" in t.keys() else None
    if meal:
        return (f"With {bag_fees.BAG_KG}kg bag + meals "
                f"(est. +RM{fee:,.0f} bag, +RM{meal:,.0f} meals)")
    return (f"With {bag_fees.BAG_KG}kg bag "
            f"(est. +RM{fee:,.0f}, varies by flight; meals not priced)")


def _option_block(route, v, basis):
    """One fare option: price, what it is being compared against, the flight.

    Rewritten 6-Sep-2026. The docstring here used to show the old render --
    `RM 827 - 57% of typical - cheaper than 92%` -- and explain "the two
    percentages", fifteen lines above the comment recording that both were
    deleted. Kept short now, because the reasoning below is the part worth
    reading and a worked example in a docstring rots the moment the format
    moves.
    """
    t = v["today"]

    # OPTION C (chosen from three drafts, 6-Sep-2026): one price, then the two anchors that make
    # it mean something -- what a normal day costs, and the PREVIOUS best WITH ITS DATE ("3 weeks
    # ago" and "yesterday" say opposite things about whether to wait). No percentages: at 34
    # readings "cheaper than 92%" means "beat 31 of 34" while sounding like hundreds; the rank says
    # it honestly. "normal day" is the MEDIAN (one freak reading would drag an average).
    # SCOPE IN WORDS ("both legs"): a bare "RM 1,078" does not say whether it buys one leg or two.
    scope = "both legs" if route.trip_type == "return" else "single leg"
    head = [f"RM {t['price']:,.0f} · {scope}"]
    anchors = []
    if v.get("typical"):
        anchors.append(f"normal day RM {v['typical']:,.0f}")
    if v.get("low"):
        # "PREVIOUS best", not "best ever": v["low"] EXCLUDES today, so on a record day "best ever
        # RM 520" would sit above "lowest of 34" while today is RM 500 -- self-contradiction on exactly
        # the days the trigger selects.
        best = f"previous best RM {v['low']:,.0f}"
        if v.get("low_day"):
            best += f", {_ago(v['low_day'])}"
        anchors.append(best)
    # ONE ANCHOR PER LINE: joined they reached 58 chars and wrapped; one line per idea.
    head.extend(anchors)
    # RANK AS AN ORDINAL: direct like a percentage, but it carries its own sample size ("2nd lowest
    # of 34" today, "of 200" in a year) so it never overstates itself or needs re-tuning.
    # TIES STAY HONEST: rank counts readings
    # STRICTLY below today, so a fare matching a standing low is "joint-lowest", never "lowest".
    if v.get("rank") is not None:
        n_all = v["n_hist"] + 1                     # n_hist excludes today
        # TIES HONEST AT EVERY RANK, not only rank 1: a fare tied with 21 others at rank 10 is
        # "joint-10th" (KUL-CAN-RT bag20 did exactly this). `tied` is the only thing that distinguishes
        # "10th" from "joint-10th".
        joint = "joint-" if v.get("tied") else ""
        word = "lowest" if v["rank"] == 1 else f"{_ordinal(v['rank'])} lowest"
        out_rank = f"{joint}{word} of {n_all} daily checks"
        head.append(out_rank)
    # THREE LINES, NOT ONE: joined, the price line ran to ~95 chars and wrapped mid-fact. Price
    # first, then the two anchors, then the rank.
    out = [head[0]]
    for extra in head[1:]:
        out.append("  " + extra)

    when = fmt_date(t["depart_date"])
    if route.trip_type == "return":
        # DEGRADE VISIBLY, do not crash. A return row with no return_date is a
        # data problem worth seeing, but fmt_date(None) raised TypeError from
        # inside compose() -- which aborts main() mid-loop, so ONE malformed row
        # silences every remaining route for the day and the health check reports a
        # crashed alerter rather than the actual cause. Say what is missing.
        when += (f" → {fmt_date(t['return_date'])}" if t["return_date"]
                 else " → (return date missing)")
    timing = " ".join(x for x in (t["depart_time"], fmt_dur(t["duration_min"]))
                      if x)
    shape = ("Direct" if t["stops"] == 0
             else f"{t['stops']} stop via {_short_place(t['layover'])}")
    out.append(" · ".join(x for x in
                          (f"{t['carrier'] or '?'} | {when}", timing, shape)
                          if x))

    out.append(_bag_line(t, basis))
    return out


def compose(verdicts, oneway_price=None, now=None):
    """Build the phone message. One event, plain names, MYT, status emoji.

    Layout rewritten 22-Jul-2026 after a reader could not tell whether a quoted
    price was for one leg or two. Trip type is now the FIRST thing the message
    says, stated in words, and the price's scope is spelled out under it —
    "→" versus "⇄" in a route label is far too subtle to carry that meaning.
    """
    now = now or dt.datetime.now(MYT)
    route = next(iter(verdicts.values()))["route"]

    # OPENER SPLIT BY INTENT, 6-Sep-2026 (a real trip vs a route I am just
    # watching). A trip I intend to take and a curiosity should never look
    # alike at a glance -- the emoji does that work before a word is read.
    watching = not getattr(route, "real_trip", False)
    kind = "watching" if watching else "cheap right now"
    icon = "👀" if watching else "✈️"
    lines = [f"{icon} {route.place} — {kind}"]
    if route.trip_type == "return":
        lines += [f"{route.label} · return · {route.nights} nights"]
    else:
        lines += [f"{route.label} · one way"]
    lines.append("")

    for basis in BASES:
        if (v := verdicts.get(basis)):
            lines += _option_block(route, v, basis)
            lines.append("")

    lines += _standing_block(verdicts)

    # Fares we could not price with a bag: named with their dates, not silently
    # dropped — one of them may well be the best deal on offer.
    a = verdicts.get("nobag")
    bag_v = verdicts.get("bag20")
    if bag_v and (raw := bag_v["today"]["excluded"]):
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            items = []
        # Drop the one that is already the headline fare above — it is called
        # out in place there instead.
        if a:
            shown = (a["today"]["carrier"], a["today"]["price"])
            items = [e for e in items
                     if (e["carrier"], e["price"]) != shown]
        if items:
            lines.append("Also worth checking (bag varies by fare):")
            for e in items:
                when = (f" · {fmt_date(e['depart_date'])}"
                        if e.get("depart_date") else "")
                lines.append(f"{e['carrier']} RM{e['price']:,.0f}{when}")
            lines.append("")

    if oneway_price and a and a["today"]["price"] < oneway_price:
        lines.append(f"Return costs LESS than the one-way "
                     f"(RM {oneway_price:,.0f}) —")
        lines.append("worth booking even for a single leg.")
        lines.append("")

    # Source moved here from the price line to make room for the two
    # percentages, which are what a reader actually reads first.
    lines.append(f"Google Flights, checked {now:%H:%M} MYT.")
    return "\n".join(lines)


def in_cooldown(conn, route, price, now):
    """One dip must not become a week of pushes — but a materially better price
    re-opens the window.

    THE OVERRIDE IS CUTOVER-BLIND, and that is a deliberate, bounded choice
    rather than an oversight (raised at review 6-Sep-2026). `alerts.price` rows
    written before a window change describe a narrower grid, so on the day a
    route widens, today's lower minimum can clear the 8% "materially better"
    bar purely because the grid grew — re-opening a cooldown on a grid change
    rather than a price change.

    Left as is because the blast radius is one extra message: the cooldown only
    ever SUPPRESSES, so the worst case is a push that would have been sent
    anyway three days later. Filtering `alerts` by cutover would need the alert
    row to record which window it was drawn from, which is a schema change to
    buy a duplicate-message guard. Revisit if it actually fires.
    """
    last = flight_db.last_alert(conn, route.key)
    if not last:
        return False, ""
    age_days = (now - dt.datetime.fromisoformat(last["sent_at"])).total_seconds() / 86400
    if age_days >= cfg.COOLDOWN_DAYS:
        return False, ""
    if price <= last["price"] * (1 - cfg.COOLDOWN_OVERRIDE_DROP):
        return False, (f"cooldown overridden: {price:,.0f} well under "
                       f"{last['price']:,.0f}")
    return True, (f"cooled down — alerted {age_days:.1f}d ago at "
                  f"RM {last['price']:,.0f}")


def _summarise(basis, v):
    """One compact log fragment per basis — for the log, not the phone.

    Names WHICH gate held a fare back. Before 22-Aug-2026 a quiet route logged
    only its percentile, so a fare suppressed for being barely-below-typical
    looked identical to one that was simply not rare — and the reason the
    tracker stayed silent could not be reconstructed after the fact.
    """
    pct = "" if v["percentile"] is None else f" pct={v['percentile']:.0f}"
    ratio = ("" if not v.get("typical")
             else f" {100.0 * v['today']['price'] / v['typical']:.0f}%oftyp")
    if v.get("flat"):
        why = " [flat: price never moved]"
    elif v.get("rare") and not v.get("cheap"):
        why = " [rare but not cheap enough]"
    elif v.get("cheap") and not v.get("rare"):
        why = " [cheap but not rare]"
    else:
        why = ""
    return (f"{basis}:{v['source_basis']}{pct}{ratio} "
            f"RM{v['today']['price']:,.0f} n={v['n_hist']}{why}")


def test_push(conn, route_key, now, preview):
    """Compose a route's REAL message from its latest reading and push it,
    banner-marked as a test.

    Exists so the whole chain — assess, compose, notify, Telegram — can be
    verified on demand without waiting for a genuine signal, and without a
    throwaway script that would test different code than the cron runs.

    The banner is not decoration. Right now there is no price history, so this
    message is NOT a cheap-fare signal; sending it looking like one would be
    training the reader to trust an alert that means nothing yet.
    """
    route = cfg.BY_KEY.get(route_key)
    if not route:
        print(f"unknown route {route_key!r}; known: {', '.join(cfg.BY_KEY)}")
        return 1

    row = conn.execute(
        """SELECT MAX(run_day) AS d FROM daily_best
           WHERE route_key=? AND source=?""",
        (route.key, HEADLINE_SOURCE)).fetchone()
    if not row or not row["d"]:
        print(f"no reading stored for {route_key} — run collect_flights.py first")
        return 1
    run_day = row["d"]

    verdicts = {b: v for b in BASES if (v := assess(conn, route, run_day, b))}
    if not verdicts:
        print(f"no reading for {route_key} on {run_day}")
        return 1

    lines = compose(verdicts, counterpart_oneway(conn, route, run_day),
                    now).splitlines()
    # Replace only the headline. The lines under it (route label, one-way vs
    # return) are exactly what a real alert shows and must survive intact —
    # re-stating the route here duplicated it.
    lines[0] = "✈️ TEST — wiring check, not a price signal"
    # Never let a removed line show up as a gap on the phone.
    lines = [ln for i, ln in enumerate(lines)
             if ln.strip() or (i and lines[i - 1].strip())]
    lines += ["", f"Prices are real, from {fmt_day(run_day)}. Real alerts start "
                  "once there is enough history to judge against."]
    msg = "\n".join(lines)

    print(msg)
    if preview:
        print("\n[--preview: nothing sent]")
        return 0
    where = notify.push(msg)
    print(f"\n[sent via {where}]" if where else "\n[FAILED on every channel]")
    return 0 if where else 1


def stale_window_days(conn):
    """Post-cutover days whose readings were collected under a DIFFERENT window.

    Returns [(route_key, run_day, why), ...]; empty is healthy. This is a
    DEPLOY-TIME check (`--verify-cutover`), not something the daily run does --
    see main() for why the runtime version was removed.

    THE TEST IS EXACT, NOT A RATIO. An earlier version compared the count of
    distinct departure dates against 75% of today's grid, and that conflated two
    different things: a stale WINDOW and a LOSSY FETCH. A day where Google
    rate-limits half the requests genuinely has fewer priced dates -- the
    collector treats down to 50% coverage as a logged warning
    (collect_flights.py) -- so the ratio flagged normal operation, and because
    the flagged day stays in `quotes` forever it would have flagged it every day
    after, permanently.

    What separates the two is WHICH dates were priced, not how many.
    `date_grid()` is deterministic given a run_day and today's config, so it
    reconstructs exactly the grid that day SHOULD have used. A lossy fetch
    removes dates from that set; it can never add one outside it.

    COVERS ALL SEVEN ROUTES. Round 3 shipped this as fixed-date routes only,
    on the stated reasoning that "the old 60-180 day grid is a strict SUBSET of
    today's 20-270 one, so a stale day there is indistinguishable". That was
    measured at review round 4 and is FALSE -- the two grids are DISJOINT:

        old range(60, 181, 7) -> every offset  60 mod 7 == 4
        new range(20, 271, 7) -> every offset  20 mod 7 == 6
        intersection: 0 dates

    Both step by 7 from different anchors, so a stale rolling reading is
    off-phase by 3 days on every single date and is as detectable as an
    out-of-window one. Leaving the watching routes unchecked would have left the
    exact defect this cutover exists to prevent -- a widened window ranked
    against narrow history, which fires false "lowest of N" records -- passing
    silently on 4 of 7 series.
    """
    import collect_flights

    out = []
    for route in cfg.ROUTES:
        since = getattr(route, "history_since", None)
        if not since:
            continue
        expected = {}          # run_day -> the grid that day should have used
        for run_day, dep in conn.execute(
                """SELECT DISTINCT run_day, depart_date FROM quotes
                   WHERE route_key=? AND source=? AND run_day >= ?
                   ORDER BY run_day, depart_date""",
                (route.key, HEADLINE_SOURCE, str(since))):
            if run_day not in expected:
                expected[run_day] = {
                    str(d) for d in collect_flights.date_grid(
                        dt.date.fromisoformat(run_day), None, route)}
            if not expected[run_day]:
                continue       # travel window closed that day; nothing to check
            if dep not in expected[run_day]:
                # route.window may be None -- routes.py deliberately keeps the
                # module-default fallback alive for a route added without one.
                # Crashing HERE would crash only on the path where a stale date
                # was already found, i.e. exactly when the report matters.
                if route.window_dates:
                    # "the grid", not "the window". The test is membership of
                    # date_grid(), which applies the STEP and the lead-time
                    # floor -- so a date can sit inside 1-Nov..31-Dec and still
                    # be stale. Saying "trip window" made a caught step change
                    # read as a nonsense complaint about an in-range date.
                    shape = ("the " + "..".join(str(x) for x in route.window_dates)
                             + f" grid at step {route.step or cfg.STEP_DAYS}")
                elif route.window:
                    shape = f"the {route.window[0]}-{route.window[1]} day grid"
                else:
                    shape = (f"the default {cfg.WINDOW_START_DAYS}-"
                             f"{cfg.WINDOW_END_DAYS} day grid")
                out.append((route.key, run_day,
                            f"priced {dep}, which is not in {shape}"))
    return out


def verify_cutover(conn, today):
    """Print whether each route's post-cutover history is safe to rank against.

    Returns 0 PROVEN CLEAN / 1 STALE / 2 NOT PROVEN.

    WHAT "PROVEN CLEAN" ACTUALLY PROVES, and it is narrower than it sounds:
    no post-cutover reading used a departure date outside today's grid. A grid
    that only SHRANK is a subset, and a subset is indistinguishable from a lossy
    fetch by any test on dates -- so the widths printed per route are the
    evidence for that half, and this prints today's expected width beside them
    so a mismatch is visible rather than implied. The 6-Sep-2026 cutover itself
    IS fully covered, because the old and new grids are disjoint (see
    stale_window_days); a future change might not be.

    THE THIRD OUTCOME IS THE POINT. Round 3 shipped this with two, and run at
    the moment the deploy runbook calls for it -- before the first post-cutover
    collection -- it examined ZERO rows and printed "no stale-window readings
    found". A check that reports success over an empty set is this workspace's
    own recorded failure (3-Aug-2026, the notify-queue check that passed every
    pre-deploy test on an empty queue and then crashed on the first real one),
    reproduced inside the guard written to replace a removed guard.

    So: run it before the push to confirm nothing is already contaminated, and
    AGAIN after the first collection that follows. Only the second run can
    return 0, and until it does the cutover is unproven.
    """
    import collect_flights

    bad = stale_window_days(conn)
    print(f"cutover check, {today}")
    examined = 0
    odd = []
    for route in cfg.ROUTES:
        since = getattr(route, "history_since", None)
        widths = flight_db.grid_widths(conn, route.key, since) if since else {}
        examined += len(widths)
        shape = ("fixed " + "..".join(str(d) for d in route.window_dates)
                 if route.window_dates else f"rolling {route.window}")
        seen = sorted(set(widths.values()))
        print(f"  {route.key:<12} since {since}  {shape}")
        for run_day in sorted(widths):
            want = len(collect_flights.date_grid(
                dt.date.fromisoformat(run_day), None, route))
            # ONLY BEYOND THE COLLECTOR'S OWN COVERAGE FLOOR. A far date that
            # returns no fare is routine (docs/DESIGN.md gotcha 2), so flagging every
            # shortfall would put this line on nearly every run and train the
            # reader to skim past it. collect_flights warns below 50%; match that,
            # so this speaks only where the collector is already unhappy.
            if want and widths[run_day] < want * 0.5:
                odd.append((route.key, run_day, widths[run_day], want))
        print(f"      {len(widths)} day(s) collected, widths seen "
              f"{seen or 'none yet'}; today expects "
              f"{len(collect_flights.date_grid(today, None, route))}")
    if bad:
        print("\nSTALE WINDOW DETECTED — these readings were NOT collected on "
              "the current configuration:")
        for key, day, why in bad:
            print(f"  {key} {day}: {why}")
        print("\nMove that route's history_since past those days in routes.py, "
              "then re-run this.")
        return 1
    if not examined:
        print("\nNOT PROVEN — no readings exist on or after the cutover yet, so "
              "this run checked nothing.\nRe-run after the first collection "
              "that follows the deploy. Until then the cutover date in "
              "routes.py is an assumption.")
        return 2
    print(f"\nNO STALE DATES — {examined} post-cutover day-series examined; "
          f"none used a departure date outside its grid for that day.")
    if odd:
        print("\nThese days priced FEWER dates than their grid held. "
              "Usually a lossy fetch -- but NOT harmless, and the reason is "
              "worth reading before dismissing it.")
        print("  On the day itself a minimum over fewer dates can only RISE, "
              "so it suppresses. But that inflated reading STAYS in daily_best "
              "and joins the median, so the next CLEAN day is measured "
              "against a baseline lifted by the bad one -- and reads cheaper "
              "than it is. Intermittent shortfalls therefore push the trigger "
              "in the FIRING direction, one day late.")
        print("  It also looks identical to a grid narrowed without moving "
              "the cutover. Check that nothing did:")
        for key, day, saw, want in odd[:20]:
            print(f"  {key} {day}: priced {saw} of {want}")
        if len(odd) > 20:
            print(f"  ... and {len(odd) - 20} more")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true",
                    help="render messages, send nothing")
    ap.add_argument("--test-push", metavar="ROUTE_KEY",
                    help="push this route's real message, marked as a test")
    # nargs="+", not "*" (fixed at review round 4). A bare `--force` gave an
    # EMPTY list, which is falsy, so `forced` was empty, `dry` stayed False and
    # `routes_to_check` fell back to all seven -- a bare --force was a full live
    # run. Measured: 1 real push. argparse now rejects it outright.
    ap.add_argument("--force", nargs="+", metavar="ROUTE_KEY",
                    help="render these regardless of trigger/cooldown")
    ap.add_argument("--day", help="evaluate a past run day (YYYY-MM-DD)")
    ap.add_argument("--verify-cutover", action="store_true",
                    help="check post-cutover history was collected on the "
                         "current windows (RUN AT EVERY DEPLOY)")
    a = ap.parse_args()

    now = dt.datetime.now(MYT)
    run_day = a.day or now.date().isoformat()
    conn = flight_db.connect()

    if a.test_push:
        rc = test_push(conn, a.test_push, now, a.preview)
        conn.close()
        return rc

    # REFUSE TO RUN AGAINST AN UNFINISHED COLLECTION. If the collector overruns, every route reads
    # "no reading" and the run would still print the "done -- N alert(s) sent" sentinel the health check
    # keys on -- a lost day reported GREEN (the 3-Aug notify-queue shape).
    # THE EXEMPTIONS ARE PER ROUTE, NOT PER RUN: exempting a whole run because one route was named
    # let six others push live off partial data (a live-fire bug caught at review). So no SCHEDULED
    # or --force run pushes off a partial day; --force is print-only for the named routes; --day and
    # --preview print the warning and carry on, since neither can send.
    # --test-push IS THE DELIBERATE EXCEPTION and returns above this: it proves the chain reaches
    # Telegram, banner-marks what it sends, and reads the LATEST collected run_day, not today.
    partial = None
    row = conn.execute(
        "SELECT started_at, finished_at FROM runs "
        "WHERE run_day=? AND source='google' "
        "ORDER BY started_at DESC LIMIT 1", (run_day,)).fetchone()
    if row is None:
        partial = f"no collector run recorded for {run_day}"
    elif not row["finished_at"]:
        partial = (f"collector for {run_day} started {row['started_at']} "
                   f"and has not finished")

    if a.verify_cutover:
        rc = verify_cutover(conn, dt.date.fromisoformat(run_day))
        conn.close()
        return rc          # 0 clean / 1 stale / 2 not proven yet

    # NO CUTOVER ABORT HERE, deliberately (one was tried and removed at review): run before the
    # partial-day branch, it reported a collector overrun as "the cutover date is wrong" (inviting
    # discarding good history), and one lossy fetch left a short day in `quotes` that
    # aborted every later run. The cutover is a DEPLOY-TIME question, answered by
    # `--verify-cutover`, which the deploy runbook requires.
    forced = set(a.force or [])
    # VALIDATE THE KEYS. An unknown key filtered routes_to_check to empty and the
    # run exited 0 having rendered nothing -- a typo looked like a clean run.
    # --test-push already validates its key; --force did not, and --force is now
    # the flag that actually renders anything (a warm-up --preview triggers none).
    unknown = sorted(forced - set(cfg.BY_KEY))
    if unknown:
        print(f"unknown route key(s): {', '.join(unknown)}")
        print(f"known: {', '.join(cfg.BY_KEY)}")
        conn.close()
        return 2

    if partial:
        # Non-sentinel wording on purpose: the external health check matches
        # `^done .*alert\(s\) sent`, so none of these lines can be read as a
        # healthy run.
        print(f"PARTIAL DAY: {partial}")
        if not (a.day or a.preview or forced):
            print("ABORT: refusing to alert on a partial day")
            conn.close()
            return 1
        print("  continuing WITHOUT sending: nothing below will be pushed.")

    # --force IS PRINT-ONLY, AND ONLY THE NAMED ROUTES: render exactly what was named, send nothing
    # (an earlier version pushed live for the other routes on a normal day -- 2 real pushes).
    # --day is print-only too: on a FINISHED past day it once ran the whole send path, wrote an
    # `alerts` row stamped NOW (poisoning the cooldown) and printed a fresh "checked HH:MM" over an
    # old fare. --test-push is the flag that deliberately reaches the phone.
    dry = a.preview or bool(partial) or bool(forced) or bool(a.day)
    routes_to_check = [r for r in cfg.ROUTES if not forced or r.key in forced]
    sent = 0
    shown = 0          # messages actually COMPOSED, not routes evaluated


    for route in routes_to_check:
        verdicts = {b: v for b in BASES
                    if (v := assess(conn, route, run_day, b))}
        if not verdicts:
            print(f"{route.key}: no reading for {run_day}")
            continue

        triggered = [b for b, v in verdicts.items() if v["triggered"]]
        summary = " | ".join(_summarise(b, v) for b, v in verdicts.items())

        force = route.key in forced
        if not triggered and not force:
            print(f"{route.key}: quiet — {summary}")
            continue

        # Cooldown is per ROUTE and keyed on the advertised fare, so the two
        # bases can never produce two messages for the same event.
        canon = (verdicts.get("nobag") or verdicts["bag20"])["today"]["price"]
        cooled, note = in_cooldown(conn, route, canon, now)
        if cooled and not force:
            print(f"{route.key}: {note}")
            continue
        if note:
            print(f"{route.key}: {note}")

        # No coverage() query here: the line it fed was cut (6-Sep-2026) and nothing else
        # consumes it.
        msg = compose(verdicts,
                      counterpart_oneway(conn, route, run_day), now)
        if dry or force:
            shown += 1
            # NAME THE DAY. Every relative phrase below ("2 days ago", "the
            # past 3 weeks", the 'yy suffix) is computed against REAL today,
            # not against run_day, so under --day they describe the wrong
            # distance. Harmless for sending (--day cannot send) but silently
            # wrong to read, so the header says which day this is.
            stamp = f" (run day {run_day})" if run_day != str(now.date()) else ""
            print(f"\n----- {route.key}{stamp} " + "-" * 40)
            print(msg)
            print("-" * 52 + "\n")
            continue

        where = notify.push(msg)
        print(f"{route.key}: PUSHED via {where} — {summary}")
        conn.execute(
            """INSERT INTO alerts (sent_at, route_key, source, price,
                   percentile, basis, kind) VALUES (?,?,?,?,?,?,?)""",
            (now.isoformat(timespec="seconds"), route.key, HEADLINE_SOURCE,
             canon, verdicts[triggered[0]].get("percentile"),
             ",".join(triggered), "cheap"))
        conn.commit()
        sent += 1

    # RENDERING NOTHING IS A FAILURE WHEN RENDERING WAS THE POINT: --force/--preview exist to show
    # the message before the cron sends it, so "no reading for <day>" on every named route must not
    # exit 0 like a successful render (same reason the unknown-key branch returns 2).
    if dry and not shown:
        print(f"RENDERED NOTHING — no reading exists for {run_day} on the "
              f"route(s) asked for.")
        print("  The collector runs 16:15 MYT; before it, today has no rows. "
              "Re-run after it, or pass --day <a day that was collected>.")
        conn.close()
        return 2

    conn.close()
    # The sentinel is what the external health check reads as a healthy run,
    # so it is printed ONLY by a run that could actually have sent something.
    # A partial day reached here via --day/--force/--preview: those print, they
    # never push, and none of them may leave a healthy-looking tail behind.
    if not dry:
        print(f"done — {sent} alert(s) sent")
    else:
        print(f"finished WITHOUT sending — {shown} message(s) rendered, "
              f"{len(routes_to_check)} route(s) evaluated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
