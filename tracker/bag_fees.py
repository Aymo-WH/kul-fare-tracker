#!/usr/bin/env python3
"""Checked-bag AND meal assumptions for the like-for-like price comparison.

Since 4-Aug-2026 the with-bag basis is really a FULL-SERVICE-
COMPARABLE basis: fare + 20kg bag + one pre-book meal per leg. Bags first:

WHY THIS IS AN ASSUMPTION FILE AND NOT A LOOKUP: there is no live source for it.
Google's search filter has no reachable checked-bag field (every plausible
protobuf field 1-45 tested 22-Jul-2026, all inert), and the budget carriers
price bags dynamically per flight. Rough figures are good enough for a
comparison, but only the AirAsia figures are well sourced — so the numbers
below are labelled by how much they can be trusted, and the weakest ones are
NOT silently rounded into the comparison.

Four confidence levels, and the whole point of the file is that they stay apart:

  INCLUDED   fee 0, because 20kg comes with the economy fare. A fact.
  ESTIMATE   a real sourced figure that still varies flight to flight.
  AMBIGUOUS  the carrier sells both bag-inclusive and bag-free fare families and
             Google does not reveal which one it is quoting. Refusing to answer
             is the only correct answer; the message names the fare and says
             what to check.
  UNKNOWN    no source. Never priced, never silently treated as free.

An AMBIGUOUS or UNKNOWN fare is excluded from the with-bag winner but is still
REPORTED in the message, because "the cheapest fare might be even cheaper than
what I showed you, go check its bag policy" is useful, and dropping it silently
would quietly hand the comparison to a worse option.

To correct any figure: edit below and redeploy. Nothing else needs touching.
"""

# --- ESTIMATE -----------------------------------------------------------------
# AirAsia international 20kg prepaid = MYR 222.60 at initial booking (312.20
# after). Third-party mirror of AirAsia's own schedule, reviewed quarterly:
# https://www.klia2.info/check_airasia_fees_charges.php   (read 22-Jul-2026)
AIRASIA_INTL_20KG = 222.60

# Scoot prices bags dynamically by route distance; prepaid 20kg lands around
# SGD 30-50, midpoint ~SGD 40 ~= MYR 130 at ~3.3.
# https://www.flyscoot.com/en/plan/booking-your-flight/fares-fees  (read 22-Jul-2026)
SCOOT_20KG = 130.0

# HK Express prepaid 20kg at INITIAL BOOKING = HKD 310 ~= MYR 186 at ~0.60
# (rises to HKD 380-600 bought later; initial-booking matches the planned-
# traveller assumption the other estimates make).
# https://www.hkexpress.com/en/Plan/Extras/Baggage/Checked-Baggage (read 4-Aug-2026)
# Was UNKNOWN until the 4-Aug v2 review — the carrier was silently excluded
# from every with-bag comparison on the KUL-HKG series.
HKEXPRESS_20KG = 186.0

BAG_FEE_20KG = {
    "AirAsia X":    AIRASIA_INTL_20KG,   # listed before "AirAsia" for clarity;
    "Thai AirAsia": AIRASIA_INTL_20KG,   # matching is substring-based either way
    "AirAsia":      AIRASIA_INTL_20KG,
    "Scoot":        SCOOT_20KG,
    "HK Express":   HKEXPRESS_20KG,
}

# --- INCLUDED -----------------------------------------------------------------
# 20kg included in the economy fare on these routes. Kept separate from the
# estimates above precisely because this part is not guesswork.
FULL_SERVICE = {
    "Malaysia Airlines", "Cathay Pacific", "Cathay Dragon",
    "Singapore Airlines", "EVA Air", "STARLUX Airlines", "China Southern",
    "China Airlines", "Air Macau", "Xiamen Air", "Hong Kong Airlines",
    # "THAI" is how Google renders Thai Airways. Safe as a substring despite
    # "Thai AirAsia" existing, because budget carriers are matched FIRST.
    "Garuda Indonesia", "Thai Airways", "THAI", "Vietnam Airlines",
    # Added 22-Jul-2026 after the first real sweep surfaced them as "unknown"
    # on these routings. All carry >=20kg in economy on Asian sectors.
    "Korean Air", "Asiana", "Japan Airlines", "All Nippon", "ANA",
    "Air China", "Hainan Airlines", "Shenzhen Airlines", "Juneyao",
    "Sichuan Airlines", "Philippine Airlines", "Royal Brunei",
    "Qatar Airways", "Emirates", "Turkish Airlines",
}

# --- AMBIGUOUS ----------------------------------------------------------------
# Sells both bag-inclusive and bag-free economy families; Google does not expose
# which one it quoted, so any single number would be a coin flip presented as a
# fact. Value shown on the phone is the reason, not a price.
AMBIGUOUS = {
    "Batik Air": ("Economy Value includes 20kg on most international sectors "
                  "but Super Saver includes none — check the fare type"),
}

# --- MEALS (added 4-Aug-2026 — fairness to budget carriers) --------
# Same philosophy and confidence discipline as bags. A full-service fare includes
# a meal (fee 0 — a fact). Budget carriers sell it separately, so the comparable
# basis adds one PRE-BOOK meal per leg — the planned traveller pre-books meals
# for the same reason they pre-book bags. Figures reviewed 4-Aug-2026;
# edit and redeploy to correct, nothing else needs touching.
MEAL_FEE_PER_LEG = {
    # Santan Classic Combo pre-book: RM19 on AK, RM29 on D7 long-haul.
    # https://newsroom.airasia.com/news/santan-introduces-new-value-combo-with-prices-from-rm10
    # + airasia.com pre-book menu (read 4-Aug-2026). Thai AirAsia assumed AK-priced.
    "AirAsia X":    29.0,
    "Thai AirAsia": 19.0,
    "AirAsia":      19.0,
    # Scoot: hot meal SGD 12 onboard / SGD 18.50 pre-book incl. drink -> mid
    # ~SGD 15 ~= MYR 50 at ~3.3. https://www.flyscoot.com/en/plan/booking-your-flight/meals
    # (read 4-Aug-2026)
    "Scoot":        50.0,
    # HK Express cafe: items from HKD 30, hot meal ~HKD 50 ~= MYR 30 at ~0.60.
    # https://www.hkexpress.com/en/Need-help/Customer-Care/food-and-drinks-faq
    # (read 4-Aug-2026)
    "HK Express":   30.0,
}


def meal_fee(carrier, legs):
    """Meal estimate for the WHOLE trip (legs = 1 one-way, 2 return), or 0.0
    when the fare already includes meals.

    A bag-priceable budget carrier with no meal figure returns 0.0 rather than
    excluding the fare — a missing ~RM20 lunch estimate must not knock a fare
    out of a comparison its ~RM200 bag fee already qualified it for. The message
    line says so when it happens (see flight_alert._bag_line).
    """
    if not carrier:
        return 0.0
    name = carrier.strip().lower()
    hits = [f for known, f in MEAL_FEE_PER_LEG.items() if known.lower() in name]
    return max(hits) * legs if hits else 0.0


BAG_KG = 20
ESTIMATE_NOTE = "est. — varies by flight"

INCLUDED, ESTIMATE, AMBIG, UNKNOWN = "included", "estimate", "ambiguous", "unknown"


def classify(carrier):
    """-> (fee_or_None, kind, note_or_None).

    Google returns combined itineraries as "EVA Air and Air Macau". Precedence
    is deliberate: ambiguity and budget fees beat an "included" match, because
    bags are charged by whoever operates the leg you check in for — one
    bag-charging operator in the chain is enough to make the fare bag-excluding.
    """
    if not carrier:
        return None, UNKNOWN, None
    name = carrier.strip().lower()

    for known, reason in AMBIGUOUS.items():
        if known.lower() in name:
            return None, AMBIG, reason

    hits = [fee for known, fee in BAG_FEE_20KG.items() if known.lower() in name]
    if hits:
        return max(hits), ESTIMATE, None

    if any(fs.lower() in name for fs in FULL_SERVICE):
        return 0.0, INCLUDED, None

    return None, UNKNOWN, "no published bag fee on record for this carrier"


# No with_bag() here, deliberately: the comparable total (bag fee + meal_fee() for the trip's leg
# count) has exactly ONE owner, flight_db.pick_best(). Add a caller there, not a second path here.


def describe(carrier):
    """The phrase that appears on the phone under the with-bag price.

    Written as a full statement rather than a fragment: "20kg included" left a
    reader unsure whether that was a fact about the fare or a note about what we
    added to it.
    """
    fee, kind, note = classify(carrier)
    if kind == INCLUDED:
        return f"Bag already included in this fare"
    if kind == ESTIMATE:
        return f"Price includes RM{fee:,.0f} bag fee ({ESTIMATE_NOTE})"
    return note


def _self_test():
    """Committed regression cases for the fee/meal arithmetic (added
    4-Aug-2026 — the meal change first shipped with only ad-hoc verification)."""
    import flight_db
    fails, ran = [], []

    def chk(cond, msg):
        # `ran` counts itself: the first version printed "(10 checks)" as a typed
        # literal, which is the doc-rot class DOC-GUIDE names outright.
        ran.append(msg)
        if not cond:
            fails.append(msg)

    chk(meal_fee("AirAsia", 2) == 38.0, "AirAsia return meals != 38")
    chk(meal_fee("AirAsia X", 2) == 58.0, "AirAsia X return meals != 58")
    chk(meal_fee("HK Express", 1) == 30.0, "HK Express one-way meal != 30")
    chk(meal_fee("Malaysia Airlines", 2) == 0.0, "full-service meals != 0")
    chk(meal_fee(None, 2) == 0.0, "None carrier meals != 0")
    chk(classify("HK Express")[:2] == (186.0, ESTIMATE),
        "HK Express bag not 186/estimate (was UNKNOWN before 4-Aug)")

    common = dict(currency="MYR", stops=0, duration_min=240, layover=None,
                  depart_time="08:00", depart_date="2026-10-04",
                  return_date="2026-10-11")
    rows = [dict(common, price=500.0, carrier="AirAsia"),
            dict(common, price=780.0, carrier="Malaysia Airlines"),
            dict(common, price=520.0, carrier="Batik Air")]
    best, excl = flight_db.pick_best(rows, "bag20")
    chk(abs(best["price"] - 760.60) < 0.01 and best["meal_fee"] == 38.0,
        f"return comparable wrong: {best['price'] if best else None}")
    chk(bool(excl) and excl[0]["carrier"] == "Batik Air",
        "ambiguous carrier not excluded")
    b2, _ = flight_db.pick_best(
        [dict(common, price=300.0, carrier="HK Express", return_date=None)],
        "bag20")
    chk(abs(b2["price"] - 516.0) < 0.01, f"one-way comparable wrong: {b2['price']}")
    b3, _ = flight_db.pick_best(
        [dict(common, price=780.0, carrier="Malaysia Airlines")], "bag20")
    chk(b3["price"] == 780.0 and b3["meal_fee"] == 0.0,
        "full-service fare altered by the comparable basis")

    for f in fails:
        print(f"  FAIL {f}")
    print(f"SELF-TEST PASSED ({len(ran)} checks)" if not fails
          else f"SELF-TEST FAILED ({len(fails)} of {len(ran)} problem(s))")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    for c in ["AirAsia", "Malaysia Airlines", "Batik Air", "Scoot", "HK Express",
              "EVA Air and Air Macau", "EVA Air and STARLUX Airlines",
              "Spring", "Malaysia Airlines and China Southern", None]:
        fee, kind, note = classify(c)
        print(f"{str(c):<38} {kind:<10} fee={str(fee):<8} {describe(c)}")
