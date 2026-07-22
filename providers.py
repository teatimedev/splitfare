"""Event-calendar providers for splitfare.

Each provider takes (day, http, options) and returns a list of event dicts:
  {"title": str, "venue": str, "time": str, "price_str": str, "djs": [str]}

Select one in config.json:
  "events": {"provider": "ibiza-spotlight"}
  "events": {"provider": "resident-advisor", "area_id": 25}   # 25 = Ibiza

Resident Advisor area ids: open ra.co, pick your city, and the id is in the
GraphQL calls on the network tab (e.g. London 13, Berlin 34, Ibiza 25).
"""

from __future__ import annotations

import json
import re

from selectolax.lexbor import LexborHTMLParser

PROVIDERS: dict[str, object] = {}


def provider(name: str):
    def wrap(fn):
        PROVIDERS[name] = fn
        return fn
    return wrap


def fetch(day: str, http, options: dict) -> list[dict]:
    name = options.get("provider", "none")
    fn = PROVIDERS.get(name)
    if fn is None:
        raise ValueError(
            f"unknown events provider '{name}' — available: {', '.join(PROVIDERS)}")
    return fn(day, http, options)


@provider("none")
def none_provider(day: str, http, options: dict) -> list[dict]:
    """No events — flights only."""
    return []


@provider("ibiza-spotlight")
def ibiza_spotlight(day: str, http, options: dict) -> list[dict]:
    """ibiza-spotlight.com daily party calendar (Ibiza only, includes prices)."""
    y, m, d = day.split("-")
    r = http.get(f"https://www.ibiza-spotlight.com/night/events/{y}/{m}/{d}")
    if r.status_code != 200:
        return []
    p = LexborHTMLParser(r.text)
    events = []
    for card in p.css("div.card-ticket"):
        title_node = card.css_first("h3 a") or card.css_first("h3")
        if not title_node:
            continue
        venue_node = card.css_first(".ticket-header-bottom img")
        time_node = card.css_first("time")
        price_node = card.css_first(".currencyVal")
        when = ""
        if time_node:
            when = re.sub(r"\s+", "", time_node.text(strip=True)).replace("-", "–")
        events.append({
            "title": title_node.text(strip=True),
            "venue": venue_node.attributes.get("alt", "?") if venue_node else "?",
            "time": when,
            "price_str": (f"from €{price_node.text(strip=True)}"
                          if price_node else ""),
            "djs": [n.text(strip=True) for n in card.css(".partyDj a")][:6],
        })
    return events


RA_QUERY = """query GET_EVENT_LISTINGS($filters: FilterInputDtoInput,
  $filterOptions: FilterOptionsInputDtoInput, $page: Int, $pageSize: Int) {
  eventListings(filters: $filters, filterOptions: $filterOptions,
                pageSize: $pageSize, page: $page) {
    data { id listingDate event {
      id title date startTime endTime
      venue { name } artists { name } } }
    totalResults } }"""


@provider("resident-advisor")
def resident_advisor(day: str, http, options: dict) -> list[dict]:
    """ra.co listings for any area (worldwide; no door prices in this feed)."""
    area = int(options.get("area_id", 25))
    body = {
        "operationName": "GET_EVENT_LISTINGS",
        "variables": {
            "filters": {"areas": {"eq": area},
                        "listingDate": {"gte": day, "lte": day}},
            "filterOptions": {"genre": True},
            "pageSize": 50, "page": 1,
        },
        "query": RA_QUERY,
    }
    r = http.post("https://ra.co/graphql", json=body,
                  headers={"content-type": "application/json",
                           "referer": "https://ra.co/events"})
    if r.status_code != 200:
        return []
    try:
        listings = json.loads(r.text)["data"]["eventListings"]["data"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    events = []
    for row in listings:
        e = row.get("event") or {}
        title = re.sub(r"^\[[^\]]*\]\s*➔?\s*", "", e.get("title") or "").strip()
        start = (e.get("startTime") or "")[11:16]
        end = (e.get("endTime") or "")[11:16]
        events.append({
            "title": title,
            "venue": (e.get("venue") or {}).get("name", "?"),
            "time": f"{start}–{end}" if start else "",
            "price_str": "",
            "djs": [a["name"] for a in (e.get("artists") or [])][:6],
        })
    return events


# Verified live area ids (ra.co GraphQL), for the init wizard and docs:
RA_AREAS = {"ibiza": 25, "london": 13, "berlin": 34, "barcelona": 20}


def _env_key(options: dict, default_env: str):
    import os
    env = options.get("api_key_env", default_env)
    key = os.environ.get(env)
    if not key:
        raise ValueError(
            f"events provider needs an API key in ${env} — free signup; "
            "see README events-providers table")
    return key


@provider("ticketmaster")
def ticketmaster(day: str, http, options: dict) -> list[dict]:
    """Ticketmaster Discovery v2 (free key, 5k calls/day; concerts/festivals
    worldwide — thin on underground club nights). options: api_key_env
    (default TICKETMASTER_API_KEY), city, country_code, classification."""
    key = _env_key(options, "TICKETMASTER_API_KEY")
    params = {
        "apikey": key,
        "startDateTime": f"{day}T00:00:00Z",
        "endDateTime": f"{day}T23:59:59Z",
        "size": "50",
        "sort": "date,asc",
        "classificationName": options.get("classification", "music"),
    }
    if options.get("city"):
        params["city"] = options["city"]
    if options.get("country_code"):
        params["countryCode"] = options["country_code"]
    r = http.get("https://app.ticketmaster.com/discovery/v2/events.json",
                 params=params)
    if r.status_code != 200:
        return []
    return parse_ticketmaster(r.text)


def parse_ticketmaster(text: str) -> list[dict]:
    try:
        data = json.loads(text)
        rows = data.get("_embedded", {}).get("events", [])
    except json.JSONDecodeError:
        return []
    events = []
    for e in rows:
        venues = e.get("_embedded", {}).get("venues", [])
        acts = e.get("_embedded", {}).get("attractions", [])
        pr = (e.get("priceRanges") or [{}])[0]
        price = ""
        if pr.get("min") is not None:
            price = f"from {pr.get('currency', '')} {pr['min']:g}".replace("  ", " ")
        start = e.get("dates", {}).get("start", {}).get("localTime", "")[:5]
        events.append({
            "title": e.get("name", "?"),
            "venue": venues[0].get("name", "?") if venues else "?",
            "time": start,
            "price_str": price,
            "djs": [a.get("name", "") for a in acts][:6],
        })
    return events


@provider("skiddle")
def skiddle(day: str, http, options: dict) -> list[dict]:
    """Skiddle (free key; UK clubbing/gigs). options: api_key_env (default
    SKIDDLE_API_KEY), latitude, longitude, radius_miles, eventcode (CLUB)."""
    key = _env_key(options, "SKIDDLE_API_KEY")
    params = {
        "api_key": key, "minDate": day, "maxDate": day,
        "limit": "50", "order": "trending",
        "eventcode": options.get("eventcode", "CLUB"),
    }
    for src_k, dst_k in (("latitude", "latitude"), ("longitude", "longitude"),
                         ("radius_miles", "radius")):
        if options.get(src_k) is not None:
            params[dst_k] = str(options[src_k])
    r = http.get("https://www.skiddle.com/api/v1/events/search/", params=params)
    if r.status_code != 200:
        return []
    return parse_skiddle(r.text)


def parse_skiddle(text: str) -> list[dict]:
    try:
        rows = json.loads(text).get("results", [])
    except json.JSONDecodeError:
        return []
    events = []
    for e in rows:
        price = e.get("entryprice") or ""
        events.append({
            "title": e.get("eventname", "?"),
            "venue": (e.get("venue") or {}).get("name", "?"),
            "time": (e.get("openingtimes") or {}).get("doorsopen", ""),
            "price_str": price if isinstance(price, str) else "",
            "djs": [a.get("name", "") for a in (e.get("artists") or [])][:6],
        })
    return events
