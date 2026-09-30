#!/usr/bin/env python3
"""Google Flights source adapter — no third-party dependencies.

Why not the `fast-flights` pip package: it pulls in a browser-impersonation HTTP
stack and an HTML parser, and it keys off Google's minified CSS class names,
which churn. This module hand-builds the same protobuf request filter (~40 lines
of varint encoding) and reads results out of the **aria-label** text instead.
Those labels are written for screen readers, so they change far less often than
the class names, and they carry everything we need in one string:

    "From 575 Malaysian ringgits. Nonstop flight with AirAsia. Leaves Kuala
     Lumpur International Airport at 1:45 PM on Wednesday, October 14 and
     arrives at Hong Kong International Airport at 5:55 PM ... Total duration
     4 hr 10 min. Select flight"

Verified against live responses 21-Jul-2026: AirAsia, Batik, Scoot, MAS, Cathay,
STARLUX and EVA all appear, and `curr=MYR` is honoured.

Three assertions guard the things that would corrupt the price history *silently*
rather than loudly:

  1. Round-trip results say "round trip total" and one-ways do not. We require
     the wording to match the trip type we asked for. Without this, a request
     that Google quietly reinterprets would file round-trip totals into the
     one-way series and poison the percentile for months.
  2. The price is only accepted from a label reading "Malaysian ringgit". If
     `curr` is ever ignored we get zero rows and a loud failure, not a series
     that silently switches to USD.
  3. The parsed number must fall inside a plausibility band (see MIN_PRICE_MYR_RT
     / MIN_PRICE_MYR_OW / MAX_PRICE_MYR below). Without this, a markup change that glues two
     digit runs together (e.g. a price and a duration) would still match the
     wording guards above and write a garbled-but-plausible-looking number
     straight into the series.
"""

import base64
import gzip
import html as htmllib
import random
import re
import time
import urllib.error
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SEAT_CODES = {"economy": 1, "premium": 2, "business": 3, "first": 4}

# Plausibility bounds on the parsed price (see guard #3 above). The floor is
# trip-type-aware because a one-way total is roughly half a round-trip total:
#   - round-trip: RM150 sits comfortably below the cheapest AirAsia short-hop
#     round-trip.
#   - one-way: cheapest realistic KUL one-way promos sit around RM80-120, so
#     RM150 would silently drop exactly the cheap fares this tracker exists to
#     catch. RM75 leaves margin below those promos while still rejecting
#     garbage like 12 or 25 (a duration/price digit-run glued together).
# The ceiling (RM20,000) stays the same for both trip types — it leaves
# generous headroom above even long-haul economy round-trips in peak season
# (KUL-LHR/KUL-JFK style, one stop, typically RM6k-8k) — wide enough that a
# genuine fare is never rejected, but a garbled/concatenated number from a
# markup break still is.
MIN_PRICE_MYR_RT = 150
MIN_PRICE_MYR_OW = 75
MAX_PRICE_MYR = 20000


class GFlightsError(RuntimeError):
    pass


# --- protobuf (just enough of it) -------------------------------------------

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _pb_str(field, s):
    b = s.encode()
    return _tag(field, 2) + _varint(len(b)) + b


def _pb_msg(field, payload):
    return _tag(field, 2) + _varint(len(payload)) + payload


def _pb_int(field, v):
    return _tag(field, 0) + _varint(v)


def _leg(origin, dest, date, max_stops):
    leg = _pb_str(2, date)
    leg += _pb_msg(13, _pb_str(2, origin))
    leg += _pb_msg(14, _pb_str(2, dest))
    if max_stops is not None:
        leg += _pb_int(5, max_stops)
    return _pb_msg(3, leg)


def build_tfs(origin, dest, depart, return_date=None, max_stops=1,
              adults=1, seat="economy"):
    """The `tfs` query parameter: a base64url-encoded protobuf search filter."""
    info = _leg(origin, dest, depart, max_stops)
    if return_date:
        info += _leg(dest, origin, return_date, max_stops)
    info += _pb_int(9, SEAT_CODES[seat])
    for _ in range(adults):
        info += _pb_int(8, 1)          # 1 = adult, repeated once per passenger
    info += _pb_int(19, 1 if return_date else 2)   # 1 = round trip, 2 = one way
    return base64.urlsafe_b64encode(info).decode().rstrip("=")


# --- fetch ------------------------------------------------------------------

def _fetch(url, timeout=45, attempts=3):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, enc = r.read(), r.headers.get("Content-Encoding", "")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw.decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep((2 ** i) * 3 + random.uniform(0, 2))
    raise GFlightsError(f"fetch failed after {attempts} attempts: {last}")


# --- parse ------------------------------------------------------------------

_LABEL = re.compile(r'aria-label="([^"]{60,600})"')
_PRICE = re.compile(r"From ([\d,]+) Malaysian ringgit")
_NONSTOP = re.compile(r"Nonstop flight with (.+?)\.")
_STOPS = re.compile(r"(\d+) stops? flight with (.+?)\.")
_DURATION = re.compile(r"Total duration (?:(\d+) hr)?\s*(?:(\d+) min)?\.")
_DEPART_AT = re.compile(r"Leaves .+? at (\d{1,2}:\d{2}\s?[AP]M) on")
_LAYOVER = re.compile(r"layover at ([^.]+?)\.")

# Google's own price assessment for the queried route+dates ("Prices are currently high"): the
# warm-up baseline since 24-Jul-2026. It is computed over the same carrier universe we track (a
# GDS baseline misses AirAsia and biases toward FALSE "cheap!" alerts), and rides the response we
# already fetch. It is DATE-RELATIVE, not price-absolute (measured 24-Jul-2026: RM554 read HIGH
# while RM565 read TYPICAL), so the verdict is attached per departure-date, never once per route.
_VERDICT = re.compile(
    r'Prices are currently\s*<span[^>]*>([a-z ]+)</span>', re.I)
_INSIGHT_BLOCK = re.compile(r"Price insights", re.I)
VERDICTS = ("low", "typical", "high")


def price_verdict(doc):
    """-> (verdict, note). verdict is 'low'/'typical'/'high', or None.

    None is legitimate — Google omits the insight block on some queries — so it
    must degrade to "no baseline", never to "cheap". The guard distinguishes
    that from a parser break the same way `search()` separates an unpriced date
    from a broken parser: if the Price-insights block is THERE but no verdict
    can be read out of it, the markup has moved and the note says so loudly.
    Without this the regex would quietly return None forever and the fallback
    would go silently dead.
    """
    m = _VERDICT.search(doc)
    if m:
        v = m.group(1).strip().lower()
        if v not in VERDICTS:
            # A new word (e.g. "very high") is not a break, but we refuse to
            # guess where it sits on the scale.
            return None, f"unrecognised verdict {v!r} — CHECK PARSER"
        return v, ""
    if _INSIGHT_BLOCK.search(doc):
        return None, "price-insights block present but no verdict — CHECK PARSER"
    return None, ""


def parse(doc, expect_return):
    """Pull fare rows out of a Google Flights results page.

    `expect_return` decides which price wording is legal; a mismatch is dropped
    and counted rather than trusted. A parsed price outside the plausibility
    band (MIN_PRICE_MYR_RT/MIN_PRICE_MYR_OW.. MAX_PRICE_MYR, floor picked by
    `expect_return`) is dropped and counted SEPARATELY — it is a different
    failure mode (a garbled number, not a wording break) and must not hide
    inside the same counter.
    """
    rows, mismatched, bounded = [], 0, 0
    min_price = MIN_PRICE_MYR_RT if expect_return else MIN_PRICE_MYR_OW
    for raw in _LABEL.findall(doc):
        label = htmllib.unescape(raw)
        m = _PRICE.search(label)
        if not m:
            continue
        is_rt = "round trip total" in label
        if is_rt != expect_return:
            mismatched += 1
            continue

        price = float(m.group(1).replace(",", ""))
        if not (min_price <= price <= MAX_PRICE_MYR):
            bounded += 1
            continue

        if (ns := _NONSTOP.search(label)):
            stops, carrier = 0, ns.group(1)
        elif (st := _STOPS.search(label)):
            stops, carrier = int(st.group(1)), st.group(2)
        else:
            continue

        dur = _DURATION.search(label)
        minutes = None
        if dur and (dur.group(1) or dur.group(2)):
            minutes = int(dur.group(1) or 0) * 60 + int(dur.group(2) or 0)

        dep = _DEPART_AT.search(label)
        lay = _LAYOVER.search(label)
        rows.append({
            "price": price,
            "currency": "MYR",
            "carrier": carrier.strip(),
            "stops": stops,
            "duration_min": minutes,
            "depart_time": dep.group(1) if dep else None,
            "layover": lay.group(1).strip() if lay else None,
        })

    # The same itinerary appears in both "Best" and "Other" lists.
    seen, uniq = set(), []
    for r in rows:
        k = (r["price"], r["carrier"], r["depart_time"], r["stops"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq, mismatched, bounded


def search(origin, dest, depart, return_date=None, max_stops=1, adults=1,
           seat="economy", currency="MYR", retry_empty=2):
    """One route+date lookup. Returns (rows, note).

    `retry_empty` exists because of a failure mode observed on the first live
    run (22-Jul-2026): Google intermittently returns HTTP 200 with a fully
    formed page carrying **no price labels at all**, and the identical request
    succeeds seconds later. Retrying only on exceptions misses this entirely,
    and the cost is invisible — the date silently drops out of the day's sample
    and the series minimum is drawn from a smaller grid than the log claims.

    A date that is still empty after the retries is treated as genuinely
    unpriced (common at the far end of the window, where airlines have not
    loaded fares yet) and reported as such.
    """
    tfs = build_tfs(origin, dest, depart, return_date, max_stops, adults, seat)
    url = ("https://www.google.com/travel/flights/search"
           f"?tfs={tfs}&hl=en&gl=US&curr={currency}")

    attempts = 0
    while True:
        doc = _fetch(url)
        rows, mismatched, bounded = parse(doc, expect_return=bool(return_date))
        attempts += 1
        if rows or attempts > retry_empty:
            break
        time.sleep(5 + random.uniform(0, 4))

    # Attached per row, matching how depart_date is: the verdict is a property
    # of this route+date query, so whichever fare `pick_best` selects carries
    # the verdict for its OWN departure date rather than a route-wide average.
    verdict, v_note = price_verdict(doc)
    for r in rows:
        r["depart_date"] = depart
        r["return_date"] = return_date
        r["verdict"] = verdict

    note = ""
    if rows and attempts > 1:
        note = f"recovered after {attempts} attempts"
    elif not rows:
        # Distinguish "route genuinely has no fares" from "our parser broke"
        # from "prices were there but fell outside the plausibility band" —
        # the last one is counted separately (see `bounded` in parse()) so it
        # never hides inside the wording-mismatch count.
        reasons = []
        if mismatched:
            reasons.append("page had price-labels we rejected — CHECK PARSER")
        note = (f"no rows after {attempts} attempts; "
                + ("; ".join(reasons) if reasons
                   else "no price labels (likely unpriced date)"))
        if "ringgit" not in doc:
            note += " (currency not MYR — check curr= handling)"
    # Bounded prices are reported WHETHER OR NOT the page yielded rows: a markup change garbling SOME
    # labels (including the cheapest) silently raises the day's minimum while the run looks healthy.
    if bounded:
        note = ((note + "; ") if note else "") + (
            f"{bounded} price(s) outside plausibility bounds — CHECK PARSER")
    # A broken verdict parser must surface even on an otherwise healthy run,
    # or the fallback dies silently while prices keep collecting normally.
    if v_note:
        note = f"{note}; {v_note}" if note else v_note
    return rows, note


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("origin")
    ap.add_argument("dest")
    ap.add_argument("depart")
    ap.add_argument("--return-date")
    ap.add_argument("--max-stops", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows, note = search(a.origin, a.dest, a.depart, a.return_date, a.max_stops)
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        v = rows[0].get("verdict") if rows else None
        print(f"{len(rows)} fares  {note}")
        print(f"Google rates this date: {v.upper() if v else 'no verdict'}")
        for r in sorted(rows, key=lambda x: x["price"])[:10]:
            stop = "direct" if r["stops"] == 0 else f"{r['stops']} stop"
            dur = f"{r['duration_min']//60}h{r['duration_min']%60:02d}" \
                if r["duration_min"] else "?"
            via = f" via {r['layover']}" if r["layover"] else ""
            print(f"  MYR {r['price']:>8,.0f}  {r['carrier']:<22} "
                  f"{stop:<7} {dur:<7} {r['depart_time'] or '':<9}{via}")
