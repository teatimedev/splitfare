"""Event-calendar providers for splitfare.

Each provider takes (day, http, options) and returns a list of event dicts:
  {"title": str, "venue": str, "time": str, "from_eur": str, "djs": [str]}

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
    name = options.get("provider", "ibiza-spotlight")
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
            "from_eur": price_node.text(strip=True) if price_node else "",
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
            "from_eur": "",
            "djs": [a["name"] for a in (e.get("artists") or [])][:6],
        })
    return events
