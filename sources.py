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
import re
from collections.abc import Callable
from pathlib import Path

# max concurrent requests + per-request delay per source (politeness budget)
SOURCE_LIMITS: dict[str, tuple[int, float]] = {
    "google": (3, 1.0),
    "ryanair": (4, 0.4),
    "easyjet": (4, 0.4),
    "easyjet-browser": (1, 0.5),  # one browser tab at a time
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

# easyJet has no public API. The current fare surface (verified live
# 2026-07-31, see docs/easyjet-api.md) is the homepage fare-calendar endpoint
# behind Akamai bot protection: it only answers requests that ride a real
# browser session that solved the challenge. From a residential IP with
# session cookies injected this works; from a datacenter IP it returns empty
# (per-source health stats will show it).
#
# Market-group codes: the site uses `*`-prefixed codes for grouped cities
# (observed: *BE = Belfast). Plain IATA codes work for the destination and
# for most origins; keep this map for the groups we've confirmed.
EASYJET_MARKET_GROUPS = {"BFS": "*BE", "BHD": "*BE"}


def ej_origin_code(origin: str) -> str:
    return EASYJET_MARKET_GROUPS.get(origin.upper(), origin.upper())


def ej_deeplink(origin: str, dest: str, day: str, adults: int) -> str:
    """Booking-app entry URL the homepage search pod produces. Loading this
    in a real browser starts the flight search for the route/date."""
    return ("https://www.easyjet.com/deeplink?dep={dep}&dest={dest}"
            "&dd={day}&isOneWay=on&apax={adults}&cpax=0&ipax=0&fare=Y&lang=en"
            .format(dep=ej_origin_code(origin), dest=dest.upper(), day=day,
                    adults=adults))


# Selectors for the booking results page, in order of preference. easyJet's
# app uses data-testid attributes heavily; the exact flight-card testid is
# not confirmable from a datacenter IP (the booking app denies those) — the
# extractor falls back to scanning for time/price patterns.
EJ_FLIGHT_CARD_SELECTORS = (
    '[data-testid="flight-card"]',
    '[data-testid*="flightCard"]',
    '[data-testid*="flight-card"]',
    '[data-testid*="fare-card"]',
    '[data-testid*="flightRow"]',
    '[class*="flightCard"]',
    '[class*="flight-card"]',
)


def _ej_cookies() -> str | None:
    """Session cookies from a real browser that solved easyJet's Akamai
    challenge. Points at a Netscape-cookie or JSON file via EASYJET_COOKIES."""
    import os
    path = os.environ.get("EASYJET_COOKIES")
    if not path:
        return None
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    # JSON shape: {"name": "value", ...} or [{"name","value"}, ...]
    try:
        import json as _json
        data = _json.loads(text)
        if isinstance(data, list):
            return "; ".join(f"{c['name']}={c['value']}"
                             for c in data if "name" in c)
        if isinstance(data, dict):
            return "; ".join(f"{k}={v}" for k, v in data.items())
    except Exception:
        pass
    # Netscape cookies.txt shape: domain \t flag \t path \t secure \t exp \t name \t value
    out = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            out.append(f"{parts[5]}={parts[6]}")
    return "; ".join(out) if out else None


@source("easyjet")
def fetch_easyjet(http, origin: str, dest: str, day: str, adults: int,
                  currency: str) -> list[dict]:
    """easyJet's homepage fare-calendar endpoint (current as of 2026).

    Requires a browser-grade session (Akamai). With EASYJET_COOKIES pointing
    at session cookies from a real browser, this returns per-day fares for
    Returns [] from an unprotected/datacenter context — the per-source health
    stats will show the source as empty, which is the expected signal."""
    try:
        headers = {
            "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0 Safari/537.36"),
            "accept": "application/json, text/plain, */*",
            "referer": "https://www.easyjet.com/en/",
        }
        cookies = _ej_cookies()
        if cookies:
            headers["cookie"] = cookies
        origin_code = EASYJET_MARKET_GROUPS.get(origin.upper(), origin.upper())
        r = http.get(
            "https://www.easyjet.com/homepage/api/availability",
            params={
                "origin": origin_code,
                "destination": dest.upper(),
                "currency": currency,
                "isReturn": "false",
                "originMarketGroup": origin.upper(),
                "startDate": day,
                "endDate": day,
                "isWorldwide": "false",
            },
            headers=headers)
        if r.status_code != 200:
            return []
        data = json.loads(r.text)
        # the calendar returns per-day fare objects; tolerate both a bare
        # list and a nested envelope without knowing the exact 200 shape
        # (we could only observe the call, not its response, from the VPS)
        rows = data if isinstance(data, list) else []
        if not rows and isinstance(data, dict):
            for key in ("availability", "fares", "days", "results", "data"):
                v = data.get(key)
                if isinstance(v, list):
                    rows = v
                    break
    except Exception:
        return []
    legs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dep_iso = (row.get("departureDate") or row.get("date") or row.get("day")
                   or "")
        if not dep_iso.startswith(day):
            continue
        price = (row.get("price") or row.get("amount") or row.get("fare"))
        if isinstance(price, dict):
            price = price.get("amount") or price.get("value")
        if price is None:
            continue
        try:
            amount = float(price)
        except (TypeError, ValueError):
            continue
        dep_min, _ = _hhmm(row.get("departureTime") or "")
        arr_min, _ = _hhmm(row.get("arrivalTime") or "")
        legs.append({
            "origin": row.get("departureAirportCode") or origin.upper(),
            "dest": row.get("arrivalAirportCode") or dest.upper(),
            "date": day, "airline": "easyJet",
            "dep_min": dep_min, "arr_min": arr_min,
            "arr_day_offset": 0,
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


def ej_extract_flights(html_text: str, origin: str, dest: str, day: str,
                       adults: int) -> list[dict]:
    """Parse easyJet booking results HTML into normalized legs.

    Best-effort: tries the known selectors first, then falls back to a
    text-pattern scan over the decoded body text (times like 06:40 and
    prices like £32.49 near each other). Returns [] when the page shape is
    unrecognized (e.g. Access Denied or a layout change) — callers treat
    that as 'no data'."""
    from selectolax.lexbor import LexborHTMLParser
    try:
        parser = LexborHTMLParser(html_text)
    except Exception:
        return []
    legs: list[dict] = []
    seen: set[tuple] = set()

    def push(dep_min: int | None, arr_min: int | None, price: float | None,
             airline: str) -> None:
        if dep_min is None or arr_min is None or price is None:
            return
        sig = (dep_min, arr_min, round(price * adults))
        if sig in seen:
            return
        seen.add(sig)
        legs.append({
            "origin": origin.upper(), "dest": dest.upper(), "date": day,
            "airline": airline or "easyJet",
            "dep_min": dep_min, "arr_min": arr_min, "arr_day_offset": 0,
            "price": round(price * adults),
        })

    TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
    PRICE_RE = re.compile(r"£\s?([0-9]+(?:\.[0-9]{2})?)")

    cards = []
    for sel in EJ_FLIGHT_CARD_SELECTORS:
        cards = parser.css(sel)
        if cards:
            break

    if cards:
        for card in cards:
            text = card.text(separator=" ", strip=True)
            time_matches = TIME_RE.findall(text)
            price_m = PRICE_RE.search(text)
            if len(time_matches) < 2 or not price_m:
                continue
            dep_min = int(time_matches[0][0]) * 60 + int(time_matches[0][1])
            arr_min = int(time_matches[1][0]) * 60 + int(time_matches[1][1])
            push(dep_min, arr_min, float(price_m.group(1)), "easyJet")
        if legs:
            return legs

    # fallback: scan decoded body text (entities resolved, whitespace kept)
    body = parser.body
    if body is None:
        return []
    body_text = body.text(separator=" ", strip=True)
    for m in TIME_RE.finditer(body_text):
        dep_min = int(m.group(1)) * 60 + int(m.group(2))
        tail = body_text[m.end():m.end() + 400]
        pm = PRICE_RE.search(tail)
        if not pm:
            continue
        am = TIME_RE.search(tail[:pm.start()])
        arr_min = (int(am.group(1)) * 60 + int(am.group(2))) if am \
            else dep_min + 150
        push(dep_min, arr_min, float(pm.group(1)), "easyJet")
        if len(legs) >= 30:
            break
    return legs


@source("easyjet-browser")
def fetch_easyjet_browser(http, origin: str, dest: str, day: str, adults: int,
                          currency: str) -> list[dict]:
    """Drive a real browser (camofox bridge) through easyJet's booking flow
    and scrape the results. This is the method that works where the API is
    Akamai-gated: the browser IS the user, so easyJet lets it through.

    Requires the camofox bridge (EASYJET_BROWSER_URL, default
    http://localhost:9377) and a network easyJet accepts (residential IP —
    datacenter IPs are denied on the booking app). Returns [] on any failure;
    per-source health stats will show it."""
    import os
    import time as _time
    import urllib.request

    base = os.environ.get("EASYJET_BROWSER_URL", "http://localhost:9377")
    user = os.environ.get("EASYJET_BROWSER_USER", "splitfare")
    url = ej_deeplink(origin, dest, day, adults)

    def bridge(method: str, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(base + path, method=method,
                                     headers={"content-type": "application/json"})
        data = json.dumps(payload).encode() if payload is not None else None
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return json.loads(r.read().decode())

    try:
        tab = bridge("POST", "/tabs/open", {"userId": user, "url": url})
        tab_id = tab.get("tabId") or tab.get("id")
        if not tab_id:
            return []
        try:
            # poll until results render or a clear dead-end (Access Denied)
            for _ in range(30):
                _time.sleep(2)
                snap = bridge("GET", f"/tabs/{tab_id}/snapshot",
                              {"userId": user})
                text = (snap.get("text") or snap.get("content")
                        or json.dumps(snap))
                if "Access Denied" in text or "denied" in text.lower()[:200]:
                    return []
                if re.search(r"£\s?[0-9]", text) or re.search(
                        r"\b[0-2]\d:[0-5]\d\b", text):
                    break
            eval_res = bridge("POST", f"/tabs/{tab_id}/evaluate", {
                "userId": user,
                "expression": "document.documentElement.outerHTML",
            })
            html_text = (eval_res.get("result") or eval_res.get("value")
                         or json.dumps(eval_res))
            return ej_extract_flights(html_text, origin, dest, day, adults)
        finally:
            try:
                bridge("DELETE", f"/tabs/{tab_id}", {"userId": user})
            except Exception:
                pass
    except Exception:
        return []


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
