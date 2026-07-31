"""Flight-price sources for splitfare, beyond Google Flights.

Each source is a function (http, origin, dest, day, adults, currency) ->
list[dict], one dict per nonstop flight:

    {"origin", "dest", "date", "airline", "dep_min", "arr_min",
     "arr_day_offset", "price"}

(dep_min/arr_min are minutes since midnight local; price is TOTAL for the
party in whole currency units — sources multiply per-person fares.)

Sources are public/no-key endpoints used by the airlines' own sites. They're
best-effort: any error returns [] so splitfare never crashes on a source
that changed. The 'google' source lives in splitfare.py itself (fast-flights
scraper) because it returns all carriers.

Config: "flight_sources": ["google", "ryanair", "easyjet", "wizz"]
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

# max concurrent requests + per-request delay per source (politeness budget)
SOURCE_LIMITS: dict[str, tuple[int, float]] = {
    "google": (3, 1.0),
    "ryanair": (4, 0.4),
    "easyjet": (4, 0.4),
    "wizz": (4, 0.4),
}

SOURCES: dict[str, Callable] = {}


def source(name: str):
    def wrap(fn):
        SOURCES[name] = fn
        return fn
    return wrap


def _hhmm(iso: str) -> tuple[int, int]:
    """'2026-08-12T06:40:00' -> (400, 0) minutes-since-midnight + day offset."""
    if not iso:
        return 0, 0
    try:
        day, rest = iso.split("T")
        h, m = rest[:5].split(":")
        return int(h) * 60 + int(m), 0
    except (ValueError, IndexError):
        return 0, 0


def _day_offset(day: str, dep_iso: str, arr_iso: str) -> int:
    if not dep_iso or not arr_iso:
        return 0
    return 0 if arr_iso[:10] == dep_iso[:10] else 1


# ------------------------------------------------------------------- ryanair

@source("ryanair")
def fetch_ryanair(http, origin: str, dest: str, day: str, adults: int,
                  currency: str) -> list[dict]:
    """Ryanair's public fare-finder (no key). Cheapest fare per flight;
    per-person so we multiply by party size — estimate only (fare tiers may
    bump the real total)."""
    try:
        r = http.get(
            "https://services-api.ryanair.com/farfnd/v4/oneWayFares",
            params={"departureAirportIataCode": origin,
                    "arrivalAirportIataCode": dest,
                    "outboundDepartureDateFrom": day,
                    "outboundDepartureDateTo": day,
                    "currency": currency})
        fares = json.loads(r.text).get("fares", []) if r.status_code == 200 else []
    except Exception:
        return []
    legs = []
    for f in fares:
        o = f.get("outbound") or {}
        price = o.get("price") or {}
        if price.get("currencyCode") != currency or price.get("value") is None:
            continue
        dep, arr = o.get("departureDate", ""), o.get("arrivalDate", "")
        if not dep.startswith(day):
            continue
        dep_min, _ = _hhmm(dep)
        arr_min, _ = _hhmm(arr)
        legs.append({
            "origin": o["departureAirport"]["iataCode"],
            "dest": o["arrivalAirport"]["iataCode"],
            "date": day, "airline": "Ryanair",
            "dep_min": dep_min, "arr_min": arr_min,
            "arr_day_offset": _day_offset(day, dep, arr),
            "price": round(price["value"] * adults),
        })
    return legs


# ------------------------------------------------------------------- easyjet

@source("easyjet")
def fetch_easyjet(http, origin: str, dest: str, day: str, adults: int,
                  currency: str) -> list[dict]:
    """easyJet's public flightShopping endpoint (the one their site uses).
    Per-person fares; multiply by party size. Best-effort."""
    try:
        session = str(uuid.uuid4()).upper()
        r = http.post(
            "https://www.easyjet.com/ejapi/fareQuote/flightShopping",
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "ej-session-id": session,
                "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/125.0 Safari/537.36"),
            },
            json={
                "market": "uk",
                "locale": "en-GB",
                "currency": currency,
                "passengers": {"adults": adults, "children": 0, "infants": 0},
                "outbound": {
                    "departureAirportCode": origin,
                    "destinationAirportCode": dest,
                    "departureDate": day,
                },
                "inbound": None,
                "isReturn": False,
                "isChangeFlight": False,
            })
        if r.status_code != 200:
            return []
        data = json.loads(r.text)
        flights = ((data.get("outbound") or {}).get("flights")) or []
    except Exception:
        return []
    legs = []
    for fl in flights:
        dep_iso = fl.get("departureDate") or ""
        arr_iso = fl.get("arrivalDate") or ""
        if not dep_iso.startswith(day):
            continue
        fares = fl.get("fares") or []
        if not fares:
            continue
        # cheapest fare bucket
        amount = None
        for f in fares:
            p = ((f.get("price") or {}).get("amount"))
            if p is not None:
                amount = min(amount, p) if amount is not None else p
        if amount is None:
            continue
        dep_min, _ = _hhmm(dep_iso)
        arr_min, _ = _hhmm(arr_iso)
        legs.append({
            "origin": fl.get("departureAirportCode") or origin,
            "dest": fl.get("arrivalAirportCode") or dest,
            "date": day, "airline": "easyJet",
            "dep_min": dep_min, "arr_min": arr_min,
            "arr_day_offset": _day_offset(day, dep_iso, arr_iso),
            "price": round(amount * adults),
        })
    return legs


# ---------------------------------------------------------------------- wizz

@source("wizz")
def fetch_wizz(http, origin: str, dest: str, day: str, adults: int,
               currency: str) -> list[dict]:
    """Wizz Air's public search API (be.wizzair.com, no key). Fares are
    per-person; multiply by party size."""
    try:
        r = http.post(
            "https://be.wizzair.com/28.0.0/Api/search/search",
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/125.0 Safari/537.36"),
            },
            json={
                "isFlightChange": False,
                "isSeniorOrStudent": False,
                "flightList": [
                    {"departureStation": origin, "arrivalStation": dest,
                     "date": day}],
                "adultCount": adults, "childCount": 0, "infantCount": 0,
                "wdc": True,
            })
        if r.status_code != 200:
            return []
        data = json.loads(r.text)
        flights = data.get("outboundFlights") or []
    except Exception:
        return []
    legs = []
    for fl in flights:
        dep_iso = fl.get("departureDate") or ""
        arr_iso = fl.get("arrivalDate") or ""
        if not dep_iso.startswith(day):
            continue
        price = fl.get("price") or {}
        amount = price.get("discountedAmount") or price.get("amount")
        if amount is None:
            continue
        dep_min, _ = _hhmm(dep_iso)
        arr_min, _ = _hhmm(arr_iso)
        legs.append({
            "origin": fl.get("departureStation") or origin,
            "dest": fl.get("arrivalStation") or dest,
            "date": day, "airline": "Wizz Air",
            "dep_min": dep_min, "arr_min": arr_min,
            "arr_day_offset": _day_offset(day, dep_iso, arr_iso),
            "price": round(float(amount) * adults),
        })
    return legs


def fetch_source(name: str, http, origin: str, dest: str, day: str,
                 adults: int, currency: str) -> list[dict]:
    """Run one named source, tolerantly. Returns [] on any failure."""
    fn = SOURCES.get(name)
    if fn is None:
        return []
    try:
        return fn(http, origin, dest, day, adults, currency)
    except Exception:
        return []


def delay_for(name: str) -> float:
    return SOURCE_LIMITS.get(name, (3, 0.5))[1]
