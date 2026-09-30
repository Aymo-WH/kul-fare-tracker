#!/usr/bin/env python3
"""Amadeus source adapter — the official GDS API.

Two jobs, and the second is the reason this module exists at all:

  1. `search_offers()`  — a structured second opinion on today's price.
  2. `price_metrics()`  — Amadeus' own **1-year price quartiles** for a route.
     This is what lets the tracker answer "how does today compare to the past
     year" from day one. Our own history starts empty and needs ~3 weeks before
     a percentile means anything; the quartiles cover that gap.

KNOWN LIMIT, do not paper over it: Amadeus is a GDS and AirAsia largely does not
distribute through it. On KUL short-haul that is the dominant carrier, so an
Amadeus-only price will often read high. Google Flights is the primary source
for the headline number; Amadeus is the cross-check and the historical baseline.
Where they disagree, that is information, not a bug.

`price_metrics` also has partial route coverage — it is built from real booking
volume, so thin routes may return nothing. Absence is
handled as "no baseline available", never as a zero.

Credentials: $AMADEUS_CONF (default amadeus.conf here), mode 600, git-ignored:
    {"client_id": "...", "client_secret": "...", "env": "test"}
Get them free at developers.amadeus.com. Missing config is not an error — the
collector logs it and runs Google-only.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CONF = os.environ.get("AMADEUS_CONF", "amadeus.conf")
HOSTS = {"test": "https://test.api.amadeus.com",
         "prod": "https://api.amadeus.com"}
TIMEOUT = 30

# Enough to keep the phone message readable; anything unmapped shows its code.
CARRIER_NAMES = {
    "AK": "AirAsia", "D7": "AirAsia X", "FD": "Thai AirAsia",
    "MH": "Malaysia Airlines", "CX": "Cathay Pacific", "KA": "Cathay Dragon",
    "OD": "Batik Air", "TR": "Scoot", "SQ": "Singapore Airlines",
    "NX": "Air Macau", "CZ": "China Southern", "BR": "EVA Air",
    "JX": "STARLUX Airlines", "CI": "China Airlines", "HX": "Hong Kong Airlines",
    "UO": "HK Express", "MF": "Xiamen Air", "3K": "Jetstar Asia",
}

_token_cache = {"value": None, "expires": 0.0}


class NotConfigured(RuntimeError):
    pass


class AmadeusError(RuntimeError):
    pass


def load_conf(path=CONF):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def configured(path=CONF):
    c = load_conf(path)
    return bool(c.get("client_id") and c.get("client_secret"))


def _host(conf):
    return HOSTS.get(conf.get("env", "test"), HOSTS["test"])


def _token(conf):
    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires"]:
        return _token_cache["value"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": conf["client_id"],
        "client_secret": conf["client_secret"],
    }).encode()
    req = urllib.request.Request(
        f"{_host(conf)}/v1/security/oauth2/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AmadeusError(
            f"auth failed ({e.code}) — check client_id/secret and that the key "
            f"is enabled for env={conf.get('env', 'test')}") from e
    _token_cache["value"] = payload["access_token"]
    # Refresh a minute early rather than racing the expiry.
    _token_cache["expires"] = now + max(60, payload.get("expires_in", 1799) - 60)
    return _token_cache["value"]


def _get(conf, path, params):
    url = f"{_host(conf)}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_token(conf)}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 404:
            return None                      # no data for this route/date
        if e.code == 429:
            raise AmadeusError("rate limited (429) — quota or TPS") from e
        raise AmadeusError(f"HTTP {e.code}: {body}") from e


_ISO_DUR = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?")


def _duration_min(s):
    m = _ISO_DUR.fullmatch(s or "")
    if not m:
        return None
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 1440 + h * 60 + mi


def search_offers(origin, dest, depart, return_date=None, adults=1,
                  currency="MYR", max_stops=1, limit=20, conf=None):
    """Cheapest offers for one route+date. Returns (rows, note)."""
    conf = conf or load_conf()
    if not conf.get("client_id"):
        raise NotConfigured("amadeus.conf missing or incomplete")

    params = {
        "originLocationCode": origin,
        "destinationLocationCode": dest,
        "departureDate": depart,
        "adults": adults,
        "currencyCode": currency,
        "max": limit,
    }
    if return_date:
        params["returnDate"] = return_date

    payload = _get(conf, "/v2/shopping/flight-offers", params)
    if not payload or not payload.get("data"):
        return [], "no offers returned"

    rows, dropped = [], 0
    for offer in payload["data"]:
        itins = offer.get("itineraries", [])
        # Amadeus has no max-stops parameter, only nonStop=true. Filter here so
        # the row set matches the same "up to 1 stop" rule Google is given.
        worst = max((len(i.get("segments", [])) - 1) for i in itins) if itins else 0
        if worst > max_stops:
            dropped += 1
            continue
        seg0 = itins[0]["segments"][0] if itins and itins[0].get("segments") else {}
        code = (offer.get("validatingAirlineCodes") or
                [seg0.get("carrierCode", "")])[0]
        total = sum(_duration_min(i.get("duration")) or 0 for i in itins)
        dep_at = seg0.get("departure", {}).get("at", "")
        rows.append({
            "price": float(offer["price"]["grandTotal"]),
            "currency": offer["price"].get("currency", currency),
            "carrier": CARRIER_NAMES.get(code, code),
            "stops": worst,
            "duration_min": total or None,
            "depart_time": dep_at[11:16] if len(dep_at) >= 16 else None,
            "layover": None,
            "depart_date": depart,
            "return_date": return_date,
        })

    note = f"{dropped} offers dropped over {max_stops}-stop limit" if dropped else ""
    if rows and rows[0]["currency"] != currency:
        note += f" WARNING currency={rows[0]['currency']} not {currency}"
    return rows, note


def price_metrics(origin, dest, depart, one_way=True, currency="MYR", conf=None):
    """Amadeus' historical price quartiles for a route+date.

    Returns a dict of MINIMUM/FIRST_QUARTILE/MEDIUM/THIRD_QUARTILE/MAXIMUM, or
    None when Amadeus has no history for the route — which is common on thin
    routes and must be treated as "no baseline", not as cheap.
    """
    conf = conf or load_conf()
    if not conf.get("client_id"):
        raise NotConfigured("amadeus.conf missing or incomplete")
    payload = _get(conf, "/v1/analytics/itinerary-price-metrics", {
        "originIataCode": origin,
        "destinationIataCode": dest,
        "departureDate": depart,
        "currencyCode": currency,
        "oneWay": "true" if one_way else "false",
    })
    if not payload or not payload.get("data"):
        return None
    metrics = payload["data"][0].get("priceMetrics", [])
    out = {m["quartileRanking"]: float(m["amount"]) for m in metrics}
    if not out:
        return None
    out["currency"] = currency
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Amadeus adapter smoke test")
    ap.add_argument("origin")
    ap.add_argument("dest")
    ap.add_argument("depart")
    ap.add_argument("--return-date")
    a = ap.parse_args()
    if not configured():
        raise SystemExit(
            f"not configured — write {CONF} with client_id/client_secret\n"
            "Get free credentials at https://developers.amadeus.com")
    rows, note = search_offers(a.origin, a.dest, a.depart, a.return_date)
    print(f"{len(rows)} offers  {note}")
    for r in sorted(rows, key=lambda x: x["price"])[:10]:
        stop = "direct" if r["stops"] == 0 else f"{r['stops']} stop"
        print(f"  {r['currency']} {r['price']:>8,.0f}  {r['carrier']:<22} {stop}")
    m = price_metrics(a.origin, a.dest, a.depart, one_way=not a.return_date)
    print("\n1-year price metrics:", m or "none for this route")
