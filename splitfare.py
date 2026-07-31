#!/usr/bin/env python3
"""splitfare — cheap flight-combo hunter with split-ticket (self-transfer) routing.

Born from a manual hunt for Belfast→Ibiza. Automates the method:
  1. Price the DIRECT one-way each way.
  2. Price O→hub and hub→D one-ways for every configured hub airport.
  3. Compose feasible same-day self-transfer combos (min connection time,
     same airport, home the same day) and rank everything by total price.

Data source: Google Flights (via fast-flights protobuf query + consent cookie).
Prices are totals for the whole party, GBP by default.

Usage:
  python splitfare.py window 2026-08-16 2026-08-17 --events
  python splitfare.py scan --month 2026-08 --nights 1 --out-dow thu,sun --deep 4
  python splitfare.py events 2026-08-16 --days 2
  python splitfare.py window 2026-08-16 2026-08-17 --html report.html
"""

from __future__ import annotations

import argparse
import html as html_mod
import os
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.parser import parse as gf_parse

import providers
import sources

ROOT = Path(__file__).resolve().parent
# Config, cache and the generated site live here; override with SPLITFARE_HOME
# (useful when splitfare is pip-installed rather than run from a checkout).
HOME_DIR = Path(os.environ.get("SPLITFARE_HOME", ROOT))
CACHE_DIR = HOME_DIR / ".cache"
CONFIG_PATH = HOME_DIR / "config.json"
HISTORY_FILE = CACHE_DIR / "price_history.jsonl"  # append-only price observations
WATCH_FILE = HOME_DIR / "watches.json"

# Google's cookie-consent bypass: pre-consented SOCS cookie (EU wall otherwise
# returns a page with no data script and parsing fails).
SOCS = "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg"

POLITE_DELAY_S = 1.5

# Per-source concurrency: (max workers, delay per request). Fetches run in a
# thread pool so a month sweep is parallel, but each source keeps its own
# politeness budget so we don't get blocked.
SOURCE_SEMS = {name: threading.Semaphore(lim)
               for name, (lim, _) in sources.SOURCE_LIMITS.items()}
EXECUTOR = ThreadPoolExecutor(max_workers=14, thread_name_prefix="splitfare")
_client_local = threading.local()

CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$", "SEK": "kr",
                    "DKK": "kr", "NOK": "kr", "PLN": "zł", "CHF": "CHF",
                    "CZK": "Kč", "HUF": "Ft"}
SYM = "£"  # display symbol for the configured currency; set by load_config


def set_currency(code: str) -> None:
    global SYM
    SYM = CURRENCY_SYMBOLS.get(code.upper(), code.upper() + " ")

console = Console(highlight=False)

# fetch health accounting — per source: live fetches vs fetches that came
# back empty (rate-limiting / parser breakage). We track and warn per source.
FETCH_STATS: dict = {"cached": 0}


# ---------------------------------------------------------------- data model

@dataclass
class Leg:
    """One bookable nonstop flight for the whole party."""

    origin: str
    dest: str
    date: str  # YYYY-MM-DD as queried
    airline: str
    dep_min: int  # minutes since midnight, local
    arr_min: int
    arr_day_offset: int  # 0 = same day, 1 = lands next day
    price: int  # total for the party, whole currency units

    @property
    def dep_hm(self) -> str:
        return f"{self.dep_min // 60:02d}:{self.dep_min % 60:02d}"

    @property
    def arr_hm(self) -> str:
        plus = "+1" if self.arr_day_offset else ""
        return f"{self.arr_min // 60:02d}:{self.arr_min % 60:02d}{plus}"

    def describe(self) -> str:
        return (
            f"{self.airline} {self.origin}→{self.dest} "
            f"{self.dep_hm}–{self.arr_hm} {SYM}{self.price}"
        )


@dataclass
class Option:
    """A one-direction itinerary: direct leg or a two-leg self-transfer."""

    legs: list[Leg]
    kind: str  # "direct" | "split"

    @property
    def price(self) -> int:
        return sum(l.price for l in self.legs)

    @property
    def arrival_min(self) -> int:
        return self.legs[-1].arr_min

    @property
    def connect_min(self) -> int | None:
        if len(self.legs) < 2:
            return None
        return self.legs[1].dep_min - self.legs[0].arr_min

    @property
    def route(self) -> str:
        return "→".join([self.legs[0].origin] + [l.dest for l in self.legs])

    def describe(self) -> str:
        return "  +  ".join(l.describe() for l in self.legs)


def fmt_dur(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def hhmm_to_min(s: str) -> int:
    try:
        h, m = s.split(":")
        v = int(h) * 60 + int(m)
        if not 0 <= v < 1440:
            raise ValueError
        return v
    except ValueError:
        raise ValueError(f"'{s}' is not a time like 14:00") from None


def dow(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%a")


def nice_date(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%a %-d %b")


def gf_link(origin: str, dest: str, day: str, adults: int,
            currency: str = "GBP") -> str:
    q = f"one way flights for {adults} adults from {origin} to {dest} on {day}"
    return ("https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)
            + "&curr=" + currency)


# ------------------------------------------------------------------- fetcher

def client() -> Client:
    """Thread-local primp client (primp isn't thread-safe; one per worker)."""
    c = getattr(_client_local, "c", None)
    if c is None:
        c = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            cookie_store=True,
        )
        c.set_cookies("https://www.google.com", {"SOCS": SOCS})
        _client_local.c = c
    return c


def leg_from_dict(d: dict) -> Leg:
    return Leg(
        origin=d["origin"], dest=d["dest"], date=d["date"], airline=d["airline"],
        dep_min=d["dep_min"], arr_min=d["arr_min"],
        arr_day_offset=d.get("arr_day_offset", 0), price=d["price"],
    )


def merge_legs(*lists: list[Leg]) -> list[Leg]:
    """Merge legs from several sources, deduped by (airline, dep, arr) keeping
    the cheapest price. Google is authoritative for airline naming; the API
    sources use the same carrier names so the dedupe keys line up."""
    best: dict[tuple[str, int, int], Leg] = {}
    for legs in lists:
        for l in legs:
            key = (l.airline.lower().replace(" ", ""), l.dep_min, l.arr_min)
            cur = best.get(key)
            if cur is None or l.price < cur.price:
                best[key] = l
    return sorted(best.values(), key=lambda l: (l.dep_min, l.price))


def fetch_legs(
    origin: str,
    dest: str,
    day: str,
    adults: int,
    currency: str,
    ttl_hours: float,
    fresh: bool,
    status=None,
    sources_list: tuple = ("google",),
) -> list[Leg]:
    """All nonstop flights origin→dest on a date, cached. Google Flights is
    the primary source (all carriers); any other names in sources_list —
    'ryanair', 'easyjet', 'wizz' — are fetched from their public APIs and
    merged in: they add coverage when Google returns nothing for the
    route-date, and can surface a cheaper fare tier for the same flight."""
    CACHE_DIR.mkdir(exist_ok=True)
    key = f"{origin}-{dest}-{day}-{adults}pax-{currency}.json"
    cache_file = CACHE_DIR / key

    if not fresh and cache_file.exists():
        try:
            blob = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            blob = None
        if blob:
            age_h = (time.time() - blob["fetched_at"]) / 3600
            # empty results get a short TTL so one consent hiccup doesn't
            # poison a route-date for the full cache window; also refetch if
            # the configured sources have grown since this was cached
            stale_sources = not set(sources_list).issubset(
                set(blob.get("sources", ["google"])))
            limit = ttl_hours if blob["legs"] else min(ttl_hours, 0.5)
            if age_h <= limit and not stale_sources:
                FETCH_STATS["cached"] += 1
                return [Leg(**l) for l in blob["legs"]]

    if status is not None:
        status.update(f"[dim]fetching[/dim] {origin}→{dest} {day} …")

    # ---- google first (primary: every carrier on the route) ----
    with SOURCE_SEMS["google"]:
        g_legs = _fetch_google(origin, dest, day, adults, currency)
    FETCH_STATS.setdefault("google", {"live": 0, "empty": 0})
    FETCH_STATS["google"]["live"] += 1
    if not g_legs:
        FETCH_STATS["google"]["empty"] += 1

    # ---- extra public-API sources, merged in ----
    extra: list[list[Leg]] = []
    for name in sources_list:
        if name == "google" or name not in sources.SOURCES:
            continue
        sem = SOURCE_SEMS.get(name)
        if sem is not None:
            with sem:
                raw = sources.fetch_source(name, client(), origin, dest, day,
                                           adults, currency)
        else:
            raw = sources.fetch_source(name, client(), origin, dest, day,
                                       adults, currency)
        time.sleep(sources.delay_for(name))
        legs_n = [leg_from_dict(d) for d in raw]
        extra.append(legs_n)
        FETCH_STATS.setdefault(name, {"live": 0, "empty": 0})
        FETCH_STATS[name]["live"] += 1
        if not legs_n:
            FETCH_STATS[name]["empty"] += 1

    legs = merge_legs(g_legs, *extra)

    # remember the cheapest observation for price history / buy-wait scoring
    if legs:
        record_price(key, min(l.price for l in legs))

    blob = {"fetched_at": time.time(), "sources": sorted(sources_list),
            "legs": [l.__dict__ for l in legs]}
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob))
    tmp.replace(cache_file)  # atomic: readers never see a half-written file
    return legs


def _fetch_google(origin: str, dest: str, day: str, adults: int,
                  currency: str) -> list[Leg]:
    """Google Flights via fast-flights. [] if no route that day or consent
    hiccup (the caller tracks empties for the health warning)."""
    q = create_query(
        flights=[FlightQuery(date=day, from_airport=origin, to_airport=dest,
                             max_stops=0)],
        trip="one-way",
        passengers=Passengers(adults=adults),
        currency=currency,
    )
    res = client().get("https://www.google.com/travel/flights", params=q.params())
    time.sleep(POLITE_DELAY_S)

    try:
        flights = gf_parse(res.text)
    except Exception:
        return []

    def as_min(t: object) -> int:
        parts = list(t) if isinstance(t, (list, tuple)) else [t]
        h = int(parts[0]) if parts and parts[0] is not None else 0
        m = int(parts[1]) if len(parts) > 1 and parts[1] is not None else 0
        return h * 60 + m

    qdate = tuple(int(x) for x in day.split("-"))
    legs: list[Leg] = []
    for fl in flights:
        if fl.type == "multi" or len(fl.flights) != 1 or not fl.price:
            continue
        seg = fl.flights[0]
        dep, arr = seg.departure, seg.arrival
        offset = 0 if tuple(arr.date) == tuple(dep.date) else 1
        if tuple(dep.date) != qdate:
            continue
        legs.append(
            Leg(
                origin=seg.from_airport.code,
                dest=seg.to_airport.code,
                date=day,
                airline=", ".join(fl.airlines),
                dep_min=as_min(dep.time),
                arr_min=as_min(arr.time),
                arr_day_offset=offset,
                price=int(fl.price),
            )
        )
    return legs


def fetch_many(
    pairs: list[tuple[str, str, str]],
    adults: int,
    currency: str,
    ttl_hours: float,
    fresh: bool,
    status=None,
    sources_list: tuple = ("google",),
) -> dict[tuple[str, str, str], list[Leg]]:
    """Fetch many route-dates (origin, dest, day) in parallel, deduped.
    Returns {(o, d, day): [Leg]}. A single failing route-date never kills
    the sweep — it just comes back empty."""
    todo = list(dict.fromkeys(pairs))
    out: dict[tuple[str, str, str], list[Leg]] = {}
    lock = threading.Lock()

    def work(pair: tuple[str, str, str]) -> None:
        o, d, day = pair
        try:
            legs = fetch_legs(o, d, day, adults, currency, ttl_hours, fresh,
                              None, sources_list)
        except Exception:
            legs = []
        with lock:
            out[pair] = legs

    futures = [EXECUTOR.submit(work, p) for p in todo]
    for f in futures:
        f.result()  # work() swallows exceptions; this just joins
    return out


# ------------------------------------------------------- price history / insight

def price_key(origin: str, dest: str, day: str, adults: int,
              currency: str) -> str:
    return f"{origin}-{dest}-{day}-{adults}pax-{currency}"


def record_price(key: str, price: int, ts: float | None = None) -> None:
    """Append one cheapest-price observation for a route-date."""
    try:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps({"key": key, "ts": ts or time.time(),
                                "min": price}) + "\n")
    except OSError:
        pass


def _backfill_history_from_cache() -> None:
    """Seed history from legacy flat cache files so baselines exist on the
    first run after upgrading."""
    if not CACHE_DIR.exists():
        return
    rows = []
    for f in CACHE_DIR.glob("*-*-*-*pax-*.json"):
        try:
            blob = json.loads(f.read_text())
            legs = blob.get("legs") or []
        except (json.JSONDecodeError, OSError):
            continue
        if not legs:
            continue
        rows.append({"key": f.name[:-5],
                     "ts": blob.get("fetched_at", time.time()),
                     "min": min(l["price"] for l in legs)})
    if not rows:
        return
    try:
        HISTORY_FILE.parent.mkdir(exist_ok=True)
        with open(HISTORY_FILE, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except OSError:
        pass


def load_history() -> dict[str, list[tuple[float, int]]]:
    """key -> [(ts, min_price), ...] sorted by ts. Backfills from legacy
    cache files when the history file doesn't exist yet."""
    if not HISTORY_FILE.exists():
        _backfill_history_from_cache()
    hist: dict[str, list[tuple[float, int]]] = {}
    try:
        for line in HISTORY_FILE.read_text().splitlines():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            hist.setdefault(o["key"], []).append((o["ts"], o["min"]))
    except OSError:
        pass
    for v in hist.values():
        v.sort()
    return hist


def _pct(sorted_prices: list[int], q: float) -> int:
    if not sorted_prices:
        return 0
    return sorted_prices[min(len(sorted_prices) - 1, int(q * len(sorted_prices)))]


def price_insight(key: str, price: int, hist: dict | None = None) -> dict | None:
    """Buy/wait signal for a route-date price vs its own history.

    Returns None until >=3 observations exist for that route-date. Verdicts:
    good (<= p25), fair (<= p50), typical (<= p75), pricey (> p75), plus a
    rising/falling/stable trend from the most recent observations.
    """
    h = (hist if hist is not None else load_history()).get(key) or []
    prices = [p for _, p in h]
    if len(prices) < 3:
        return None
    prices.sort()
    p25, p50, p75 = _pct(prices, .25), _pct(prices, .5), _pct(prices, .75)
    if price <= p25:
        verdict, label = "good", "well below typical"
    elif price <= p50:
        verdict, label = "fair", "below typical"
    elif price <= p75:
        verdict, label = "typical", "around typical"
    else:
        verdict, label = "pricey", "above typical"
    recent = h[-6:]
    trend = "stable"
    if len(recent) >= 3:
        n = len(recent)
        xs = [i for i in range(n)]
        ys = [p for _, p in recent]
        mx = sum(xs) / n
        my = sum(ys) / n
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / max(
            sum((x - mx) ** 2 for x in xs), 1e-9)
        if slope <= -2:
            trend = "falling"
        elif slope >= 2:
            trend = "rising"
    return {"verdict": verdict, "label": label, "p25": p25, "p50": p50,
            "p75": p75, "trend": trend, "samples": len(prices)}


def warn_if_unhealthy() -> None:
    for name, st in FETCH_STATS.items():
        if name == "cached":
            continue
        live = st.get("live", 0)
        if live >= 6 and st.get("empty", 0) / live > 0.85:
            console.print(
                f"[bold yellow]⚠ {name}: {st['empty']}/{live} live fetches "
                f"returned no flights — source may be rate-limiting or its "
                f"parser/API changed. Other sources are unaffected.[/bold yellow]"
            )


# ------------------------------------------------------------------ composer

def best_direction(
    origins: list[str],
    dest_airports: list[str],
    day: str,
    hubs: list[str],
    cfg: dict,
    fresh: bool,
    reverse: bool = False,
    require_same_day_arrival: bool = True,
    arrive_by_min: int | None = None,
    status=None,
) -> list[Option]:
    """Ranked options one direction. reverse=True means dest→origin (the way home).

    All route-dates needed are fetched in parallel via fetch_many; each source
    keeps its own concurrency budget so a hub-heavy scan is fast but polite."""
    adults = cfg["adults"]
    currency = cfg.get("currency", "GBP")
    ttl = cfg.get("cache_ttl_hours", 6)
    min_connect = cfg.get("min_connect_minutes", 120)
    src_list = tuple(cfg.get("flight_sources", ["google"]))

    froms, tos = (dest_airports, origins) if reverse else (origins, dest_airports)

    # gather every (origin, dest, day) we need, then fetch them all at once
    pairs: list[tuple[str, str, str]] = []
    for o in froms:
        for d in tos:
            pairs.append((o, d, day))
    for hub in hubs:
        for o in froms:
            pairs.append((o, hub, day))
        for d in tos:
            pairs.append((hub, d, day))

    legs_by = fetch_many(pairs, adults, currency, ttl, fresh, status, src_list)

    def usable(l: Leg) -> bool:
        return l.arr_day_offset == 0 if require_same_day_arrival else True

    options: list[Option] = []

    # direct
    for o in froms:
        for d in tos:
            for leg in legs_by.get((o, d, day), []):
                if usable(leg):
                    options.append(Option([leg], "direct"))

    # splits via hubs
    for hub in hubs:
        first_legs = [
            l for o in froms
            for l in legs_by.get((o, hub, day), [])
            if l.arr_day_offset == 0
        ]
        second_legs = [
            l for d in tos
            for l in legs_by.get((hub, d, day), [])
            if usable(l)
        ]
        for a in first_legs:
            for b in second_legs:
                if b.dep_min - a.arr_min >= min_connect:
                    options.append(Option([a, b], "split"))

    if arrive_by_min is not None:
        options = [o for o in options if o.arrival_min <= arrive_by_min]

    options.sort(key=lambda o: (o.price, o.arrival_min))
    return options


def overnight_option(
    cfg: dict,
    out_day: str,
    hubs: list[str],
    fresh: bool,
    arrive_by_min: int | None,
    status=None,
) -> Option | None:
    """Position to a hub the evening before, fly to the destination next morning.

    Returns the cheapest such pair (marked kind="overnight"); hotel not priced.
    """
    adults = cfg["adults"]
    currency = cfg.get("currency", "GBP")
    ttl = cfg.get("cache_ttl_hours", 6)
    src_list = tuple(cfg.get("flight_sources", ["google"]))
    day_before = (date.fromisoformat(out_day) - timedelta(days=1)).isoformat()

    pairs: list[tuple[str, str, str]] = []
    for hub in hubs:
        for o in cfg["origins"]:
            pairs.append((o, hub, day_before))
        for d in cfg["destination"]:
            pairs.append((hub, d, out_day))
    legs_by = fetch_many(pairs, adults, currency, ttl, fresh, status, src_list)

    best: Option | None = None
    for hub in hubs:
        evenings = [
            l for o in cfg["origins"]
            for l in legs_by.get((o, hub, day_before), [])
            if l.dep_min >= 17 * 60 and l.arr_day_offset == 0
        ]
        mornings = [
            l for d in cfg["destination"]
            for l in legs_by.get((hub, d, out_day), [])
            if l.arr_day_offset == 0
            and (arrive_by_min is None or l.arr_min <= arrive_by_min)
        ]
        if not evenings or not mornings:
            continue
        cand = Option([min(evenings, key=lambda l: l.price),
                       min(mornings, key=lambda l: l.price)], "overnight")
        if best is None or cand.price < best.price:
            best = cand
    return best


def dedupe(options: list[Option], keep: int) -> list[Option]:
    """Keep the cheapest option per distinct routing shape."""
    seen: set[str] = set()
    out = []
    for o in options:
        sig = "-".join(l.origin for l in o.legs) + "-" + o.legs[-1].dest
        if sig in seen:
            continue
        seen.add(sig)
        out.append(o)
        if len(out) >= keep:
            break
    return out


# ---------------------------------------------------------------- presenting

def options_table(title: str, options: list[Option], adults: int) -> Table:
    t = Table(
        title=title,
        title_style="bold cyan",
        title_justify="left",
        header_style="bold",
        border_style="dim",
        pad_edge=False,
    )
    t.add_column("Total", justify="right", style="bold green")
    t.add_column("Route")
    t.add_column("Legs")
    t.add_column("Connect", justify="right")
    for i, o in enumerate(options):
        legs_txt = Text()
        for j, l in enumerate(o.legs):
            if j:
                legs_txt.append("\n")
            legs_txt.append(f"{l.airline:<14}", style="magenta")
            legs_txt.append(f" {l.origin}→{l.dest} ")
            legs_txt.append(f"{l.dep_hm}–{l.arr_hm}", style="yellow")
            legs_txt.append(f"  {SYM}{l.price}", style="dim")
        conn = fmt_dur(o.connect_min) + f" @ {o.legs[0].dest}" if o.connect_min else "—"
        style = "on grey11" if i == 0 else ""
        t.add_row(f"{SYM}{o.price}", o.route, legs_txt, conn, style=style)
    if not options:
        t.add_row("—", "no viable options", "", "")
    return t


def print_booking_links(options: list[Option], adults: int, label: str = "") -> None:
    if not options:
        return
    console.print(f"[bold]Booking searches — {label or 'cheapest option'}:[/bold]")
    for l in options[0].legs:
        console.print(
            f"  [link={gf_link(l.origin, l.dest, l.date, adults)}]"
            f"{l.origin}→{l.dest} {nice_date(l.date)} — Google Flights ↗[/link]"
            f"   [dim](then book direct with {l.airline})[/dim]"
        )


# -------------------------------------------------------------------- events

def events_options() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text()).get("events", {"provider": "none"})
    return {"provider": "none"}


def fetch_events(day: str) -> list[dict]:
    """Party calendar for one date via the configured provider (cached 24h)."""
    opts = events_options()
    name = opts.get("provider", "none")
    if name == "none":
        return []
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"events2-{name}-{day}.json"
    ttl = 24.0
    if CONFIG_PATH.exists():
        ttl = json.loads(CONFIG_PATH.read_text()).get("cache_ttl_hours", 6) * 4
    if cache_file.exists():
        blob = json.loads(cache_file.read_text())
        if (time.time() - blob["fetched_at"]) / 3600 <= ttl:
            return blob["events"]

    events = providers.fetch(day, client(), opts)
    cache_file.write_text(json.dumps({"fetched_at": time.time(), "events": events}))
    return events


BIG_VENUES = (
    "Ushuaïa", "Hï Ibiza", "Pacha", "Amnesia", "[UNVRS]", "DC10", "DC-10", "Eden",
)

# second-tier venues worth showing with lineups (open-airs, beach clubs, halls)
MID_VENUES = (
    "Destino", "528", "Cova Santa", "Akasha", "Chinois", "O Beach",
    "Ibiza Rocks", "Lío", "Es Paradis", "Playa Soleil",
)


def venue_tier(venue: str) -> int:
    opts = events_options()
    tier1 = opts.get("tier1_venues", BIG_VENUES)
    tier2 = opts.get("tier2_venues", MID_VENUES)
    if any(v in venue for v in tier1):
        return 1
    if any(v in venue for v in tier2):
        return 2
    return 3


def events_panel(day: str) -> Panel:
    events = fetch_events(day)
    lines = Text()
    big = [e for e in events if venue_tier(e["venue"]) == 1]
    rest = [e for e in events if e not in big]
    for e in big + rest:
        star = "★ " if e in big else "  "
        style = "bold" if e in big else "dim"
        lines.append(f"{star}{e['venue']}: ", style=style)
        lines.append(e["title"], style="cyan" if e in big else "dim cyan")
        if e["time"]:
            lines.append(f"  {e['time']}", style="dim")
        if e["price_str"]:
            lines.append(f"  {e['price_str']}", style="dim green")
        if e["djs"] and e in big:
            lines.append(f"\n    {', '.join(e['djs'])}", style="dim")
        lines.append("\n")
    if not events:
        lines.append("nothing listed — check ibiza-spotlight.com manually", style="dim")
    return Panel(
        lines,
        title=f"🎶 {nice_date(day)} — party calendar ({len(events)} events)",
        title_align="left",
        border_style="magenta",
    )


# ------------------------------------------------------------------ HTML out

def html_report(
    title: str,
    windows: list[tuple[str, str, Option, Option]],
    adults: int,
    events_by_day: dict[str, list[dict]],
) -> str:
    """Self-contained dark-mode-friendly report page."""

    def esc(s: str) -> str:
        return html_mod.escape(str(s))

    def leg_row(l: Leg) -> str:
        return (
            f"<div class='leg'><span class='al'>{esc(l.airline)}</span> "
            f"{esc(l.origin)}→{esc(l.dest)} <span class='tm'>{l.dep_hm}–{l.arr_hm}</span> "
            f"<span class='pr'>{SYM}{l.price}</span> "
            f"<a href='{gf_link(l.origin, l.dest, l.date, adults)}' target='_blank'>search ↗</a></div>"
        )

    cards = []
    for i, (out_day, back_day, o, b) in enumerate(windows):
        conn_o = f"<span class='conn'>connect {fmt_dur(o.connect_min)} @ {o.legs[0].dest}</span>" if o.connect_min else ""
        conn_b = f"<span class='conn'>connect {fmt_dur(b.connect_min)} @ {b.legs[0].dest}</span>" if b.connect_min else ""
        badge = "<span class='badge'>CHEAPEST</span>" if i == 0 else ""
        cards.append(f"""
        <div class="card {'win' if i == 0 else ''}">
          <div class="card-head">
            <span class="dates">{esc(nice_date(out_day))} → {esc(nice_date(back_day))}</span>
            <span class="total">{SYM}{o.price + b.price}</span>{badge}
          </div>
          <div class="dir"><h4>OUT · {SYM}{o.price}</h4>{''.join(leg_row(l) for l in o.legs)}{conn_o}</div>
          <div class="dir"><h4>BACK · {SYM}{b.price}</h4>{''.join(leg_row(l) for l in b.legs)}{conn_b}</div>
        </div>""")

    ev_blocks = []
    for day, events in events_by_day.items():
        rows = []
        for e in events:
            big = venue_tier(e["venue"]) == 1
            djs = f"<div class='djs'>{esc(', '.join(e['djs']))}</div>" if e["djs"] and big else ""
            price = f"<span class='pr'>{esc(e['price_str'])}</span>" if e["price_str"] else ""
            rows.append(
                f"<div class='ev {'big' if big else ''}'><b>{esc(e['venue'])}</b>: "
                f"{esc(e['title'])} <span class='tm'>{esc(e['time'])}</span> {price}{djs}</div>"
            )
        ev_blocks.append(
            f"<div class='card'><div class='card-head'><span class='dates'>🎶 {esc(nice_date(day))}</span>"
            f"<span class='dim'>{len(events)} events</span></div>{''.join(rows)}</div>"
        )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fafafa; --card:#fff; --dim:#777;
          --accent:#0a7d33; --line:#e5e5e5; --hl:#fffbe6; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#eee; --bg:#111; --card:#1c1c1e; --dim:#999; --accent:#4ade80;
             --line:#333; --hl:#292518; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:1.2rem; font:15px/1.5 -apple-system, system-ui, sans-serif;
         color:var(--fg); background:var(--bg); max-width:760px; margin-inline:auto; }}
  h1 {{ font-size:1.35rem; }} h4 {{ margin:.7rem 0 .25rem; color:var(--dim);
        text-transform:uppercase; font-size:.72rem; letter-spacing:.08em; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:1rem 1.1rem; margin:.8rem 0; }}
  .card.win {{ border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }}
  .card-head {{ display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap; }}
  .dates {{ font-weight:650; font-size:1.05rem; }}
  .total {{ margin-left:auto; font-weight:750; font-size:1.3rem; color:var(--accent); }}
  .badge {{ background:var(--accent); color:#fff; font-size:.65rem; font-weight:700;
            padding:.15rem .45rem; border-radius:99px; letter-spacing:.06em; }}
  .leg {{ padding:.15rem 0; }}
  .al {{ font-weight:600; }} .tm {{ color:var(--dim); font-variant-numeric:tabular-nums; }}
  .pr {{ color:var(--accent); font-weight:600; }}
  .conn {{ display:block; color:var(--dim); font-size:.85rem; padding-left:.1rem; }}
  .ev {{ padding:.2rem 0; color:var(--dim); font-size:.9rem; }}
  .ev.big {{ color:var(--fg); font-size:1rem; background:var(--hl);
             padding:.35rem .5rem; border-radius:8px; margin:.2rem 0; }}
  .djs {{ color:var(--dim); font-size:.85rem; }}
  a {{ color:inherit; }} .dim {{ color:var(--dim); margin-left:auto; }}
  footer {{ color:var(--dim); font-size:.8rem; margin-top:1.5rem; }}
</style></head><body>
<h1>{esc(title)}</h1>
<p style="color:var(--dim)">Prices are totals for {adults} adults, hand luggage only,
via Google Flights on {date.today().isoformat()}. Split tickets = separate bookings:
no protection if a leg is delayed. Book each leg direct with the airline.</p>
{''.join(cards)}
{''.join(ev_blocks)}
<footer>Generated by splitfare.</footer>
</body></html>"""


# ------------------------------------------------------------- dashboard site

DASH_CSS = """
:root{
  --night:#17132E; --panel:#221C42; --panel2:#2A2352; --bone:#F4EFE3;
  --dim:#9A92BE; --coral:#FF6E56; --amber:#FFC46B;
  --line:rgba(244,239,227,.14); --mono:'JetBrains Mono',ui-monospace,monospace;
  --nav-h:calc(3.4rem + env(safe-area-inset-bottom));
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--night);color:var(--bone);
  font:16px/1.55 'Space Grotesk',system-ui,sans-serif}
a{color:inherit}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;text-align:left}
.top{position:sticky;top:0;z-index:20;background:rgba(23,19,46,.92);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);
  padding:calc(.6rem + env(safe-area-inset-top)) 1rem .6rem;
  display:flex;align-items:baseline;gap:.55rem;flex-wrap:wrap}
.top .brand{font:700 .74rem var(--mono);letter-spacing:.22em;color:var(--amber)}
.top .route{font:600 .74rem var(--mono);letter-spacing:.1em}
.top .fresh{margin-left:auto;font:500 .64rem var(--mono);color:var(--dim)}
main{max-width:640px;margin-inline:auto;padding:1rem 1rem calc(var(--nav-h) + 1.5rem)}
.pane{display:none}
.pane.active{display:block;animation:fade .25s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){.pane.active,.day-detail.active{animation:none}}
body.detail-open main{display:none}
body.detail-open .tabbar{display:none}
body.detail-open .top{display:none}
.tabbar{position:fixed;bottom:0;left:0;right:0;z-index:20;
  display:flex;background:rgba(34,28,66,.96);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-top:1px solid var(--line);
  padding:.35rem .6rem calc(.35rem + env(safe-area-inset-bottom))}
.tabbar button{flex:1;color:var(--dim);
  font:600 .62rem var(--mono);letter-spacing:.14em;text-transform:uppercase;
  padding:.45rem 0;display:flex;flex-direction:column;align-items:center;gap:.15rem}
.tabbar button .ic{font-size:1.15rem;line-height:1;
  font-family:'Bricolage Grotesque',sans-serif;font-weight:800}
.tabbar button.active{color:var(--coral)}
.intro{color:var(--dim);font-size:.86rem;margin:.2rem 0 .9rem}
.intro b{color:var(--bone)}
.seclabel{font:700 .68rem var(--mono);letter-spacing:.24em;color:var(--amber);
  text-transform:uppercase;margin:1.5rem 0 .7rem;display:flex;align-items:center;gap:.7rem}
.seclabel::after{content:'';flex:1;height:1px;background:var(--line)}
.row{border-top:1px solid var(--line);width:100%;
  display:grid;grid-template-columns:auto 1fr auto;grid-template-areas:
  "dow dates price" "dow head price" "bar bar bar";
  column-gap:.75rem;padding:.75rem .1rem .6rem;align-items:baseline}
.row .dow{grid-area:dow;font:700 .66rem var(--mono);letter-spacing:.1em;
  color:var(--dim);align-self:center;text-align:center;line-height:1.5;
  border:1px solid var(--line);border-radius:8px;padding:.3rem .4rem;min-width:3.2rem}
.row.top .dow{color:var(--amber);border-color:var(--amber)}
.row .dates{grid-area:dates;font-weight:600}
.row .dates .lands{color:var(--dim);font-weight:400;font-size:.78rem}
.row .dates .badge{background:var(--coral);color:var(--night);font:700 .58rem var(--mono);
  letter-spacing:.08em;border-radius:99px;padding:.1rem .45rem;vertical-align:middle;
  margin-left:.3rem}
.row .head{grid-area:head;color:var(--dim);font-size:.76rem;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;min-width:0}
.row .price{grid-area:price;text-align:right}
.row .price .p{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;
  font-size:1.2rem}
.row.top .price .p{color:var(--coral)}
.row .price .delta{display:block;font:500 .68rem var(--mono);color:var(--dim)}
.row .price .vt{display:block;font:500 .68rem var(--mono);color:var(--amber)}
.row .bar{grid-area:bar;height:3px;border-radius:2px;background:var(--panel2);
  margin-top:.5rem;position:relative;overflow:hidden}
.row .bar i{position:absolute;inset:0;right:auto;background:var(--dim);opacity:.55;
  border-radius:2px}
.row.top .bar i{background:var(--coral);opacity:1}
.row.dead .dates{opacity:.55}
.row.dead .why{grid-area:head;color:var(--dim);font-size:.76rem;font-style:italic;
  white-space:normal}
.row.dead .price .p{color:var(--dim)}
.day-detail{display:none;max-width:640px;margin-inline:auto;
  padding:0 1rem calc(1.5rem + env(safe-area-inset-bottom))}
.day-detail.active{display:block;animation:fade .25s ease}
.dback{position:sticky;top:0;z-index:21;display:flex;align-items:center;gap:.7rem;
  width:calc(100% + 2rem);margin:0 -1rem;padding:calc(.6rem + env(safe-area-inset-top)) 1rem .6rem;
  background:rgba(23,19,46,.92);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.dback .chev{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;
  font-size:1.3rem;color:var(--coral)}
.dback .t{font-weight:600}
.dback .p{margin-left:auto;font-family:'Bricolage Grotesque',sans-serif;
  font-weight:800;font-size:1.15rem;color:var(--coral)}
.dsub{font:500 .72rem var(--mono);color:var(--dim);margin:.8rem 0 .2rem}
.ticket{background:var(--panel);border-radius:14px;display:flex;position:relative;
  overflow:hidden;margin:.55rem 0}
.t-main{flex:1;padding:.95rem 1rem 1rem;min-width:0}
.t-air{font:600 .68rem var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--amber)}
.t-route{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;
  font-size:1.6rem;letter-spacing:.01em;margin:.15rem 0 .1rem}
.t-route .arr{color:var(--coral);font-weight:600}
.t-times{font:500 .86rem var(--mono);color:var(--dim)}
.t-spark{width:100%;height:22px;margin-top:.4rem;color:var(--dim);opacity:.75}
.t-spark svg{width:100%;height:22px;display:block}
.t-stub{width:96px;flex:none;border-left:2px dashed var(--line);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:.3rem;padding:.8rem .5rem;position:relative;background:var(--panel2)}
.t-stub::before,.t-stub::after{content:'';position:absolute;left:-9px;width:16px;
  height:16px;border-radius:50%;background:var(--night)}
.t-stub::before{top:-9px}.t-stub::after{bottom:-9px}
.t-price{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;font-size:1.25rem}
.t-book{font:600 .66rem var(--mono);letter-spacing:.1em;color:var(--coral);
  text-transform:uppercase;text-decoration:none;border:1px solid var(--coral);
  border-radius:99px;padding:.3rem .6rem}
.t-book:active{background:var(--coral);color:var(--night)}
.barcode{height:14px;width:70px;opacity:.5;
  background:repeating-linear-gradient(90deg,var(--bone) 0 2px,transparent 2px 4px,
  var(--bone) 4px 5px,transparent 5px 8px)}
.hop{display:flex;align-items:center;gap:.65rem;padding:.15rem .4rem .15rem 1rem;
  color:var(--dim);font:500 .8rem var(--mono)}
.hop::before{content:'';width:2px;height:1.5rem;margin-left:.4rem;
  background:repeating-linear-gradient(var(--dim) 0 3px,transparent 3px 7px);opacity:.6}
.altrow{display:flex;gap:.7rem;align-items:baseline;padding:.3rem 0;
  font-size:.88rem;border-top:1px dashed var(--line)}
.altrow:first-of-type{border-top:0}
.altrow .m{font:500 .8rem var(--mono);color:var(--dim);flex:none}
.altrow b{font-weight:600}
.altrow .via{color:var(--dim);font-size:.78rem;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.altrow .ap{font-family:'Bricolage Grotesque',sans-serif;font-weight:800;
  margin-left:auto;flex:none}
.altrow a{color:var(--amber);text-decoration:none;font:600 .68rem var(--mono);flex:none}
.mini{display:flex;gap:.7rem;align-items:baseline;padding:.16rem 0;font-size:.86rem}
.mini .m{font:500 .8rem var(--mono);color:var(--dim)}
.mini b{font-weight:600}
.mini a{color:var(--amber);text-decoration:none;font:600 .7rem var(--mono);
  letter-spacing:.08em;margin-left:auto;flex:none}
.nite{color:var(--dim);font-size:.8rem;padding:.1rem 0}
.nite b{color:var(--bone);font-weight:600}
.chips{position:sticky;top:3.1rem;z-index:10;display:flex;gap:.45rem;
  overflow-x:auto;padding:.6rem 0 .7rem;margin:0 -1rem;padding-inline:1rem;
  background:linear-gradient(var(--night) 75%,transparent);
  -webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;background:var(--panel);border:1px solid var(--line);
  color:var(--dim);border-radius:10px;padding:.4rem .6rem;
  font:600 .68rem var(--mono);letter-spacing:.06em;text-align:center;line-height:1.4}
.chip b{display:block;color:var(--bone);font-size:.9rem}
.chip.active{border-color:var(--coral);color:var(--coral)}
.chip.active b{color:var(--coral)}
.night{display:none}
.night.active{display:block}
.ev-big{background:var(--panel);border-radius:12px;padding:.75rem .9rem;margin:.5rem 0}
.ev-big .v{font:600 .66rem var(--mono);letter-spacing:.16em;text-transform:uppercase;
  color:var(--amber)}
.ev-big .t{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:1.05rem}
.ev-big .d{color:var(--dim);font-size:.84rem;margin-top:.1rem}
.ev-big .meta{font:500 .75rem var(--mono);color:var(--dim);margin-top:.25rem}
.ev-big .meta b{color:var(--coral);font-weight:600}
.ev-mid{background:none;border:1px solid var(--line);border-radius:12px;
  padding:.6rem .8rem;margin:.4rem 0}
.ev-mid .v{font:600 .62rem var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim)}
.ev-mid .t{font-weight:600;font-size:.95rem}
.ev-mid .d{color:var(--dim);font-size:.8rem}
.ev-mid .meta{font:500 .72rem var(--mono);color:var(--dim);margin-top:.15rem}
.ev-mid .meta b{color:var(--coral);font-weight:600}
.ev-small{color:var(--dim);font-size:.84rem;padding:.16rem 0 .16rem .2rem}
.ev-small b{color:var(--bone);font-weight:600}
.tag{background:var(--panel);border:1px dashed var(--line);border-radius:12px;
  padding:.85rem 1rem;margin-top:1.6rem;color:var(--dim);font-size:.84rem}
.tag b{color:var(--amber)}
footer{margin-top:1.6rem;font:500 .68rem var(--mono);color:var(--dim);
  letter-spacing:.08em}
"""


def render_dashboard(
    results: list[dict],
    events_by_day: dict[str, list[dict]],
    cfg: dict,
    checked_at: str,
    dead: list[tuple[str, str, dict]] | None = None,
    hist: dict | None = None,
) -> str:
    def esc(s: object) -> str:
        return html_mod.escape(str(s))

    adults = cfg["adults"]
    cur = cfg.get("currency", "GBP")
    hist = hist or {}
    best = results[0]
    best_price = best["total"]
    max_price = max(r["total"] for r in results)
    n_nights = (date_cls_diff := (datetime.strptime(best["bd"], "%Y-%m-%d")
                - datetime.strptime(best["od"], "%Y-%m-%d")).days)
    trip_label = "24-hour window" if n_nights == 1 else f"{n_nights}-night window"

    def spark_svg(prices: list[int]) -> str:
        if len(prices) < 2:
            return ""
        lo, hi = min(prices), max(prices)
        span = (hi - lo) or 1
        w, h = 96, 22
        pts = []
        for i, p in enumerate(prices):
            x = i / (len(prices) - 1) * (w - 4) + 2
            y = h - 3 - (p - lo) / span * (h - 6)
            pts.append(f"{x:.1f},{y:.1f}")
        return (f'<svg class="spark" viewBox="0 0 {w} {h}" '
                f'preserveAspectRatio="none"><polyline points="'
                f'{" ".join(pts)}" fill="none" stroke="currentColor" '
                f'stroke-width="1.6" stroke-linejoin="round"/></svg>')

    def leg_hist(leg: Leg) -> str:
        key = price_key(leg.origin, leg.dest, leg.date, adults, cur)
        prices = [p for _, p in (hist.get(key) or [])]
        return spark_svg(prices[-24:])  # last 24 observations

    def typical_for(leg: Leg) -> int | None:
        key = price_key(leg.origin, leg.dest, leg.date, adults, cur)
        ins = price_insight(key, leg.price, hist)
        return ins["p50"] if ins else None

    def tiered(day: str) -> tuple[list[dict], list[dict], list[dict]]:
        evs = events_by_day.get(day, [])
        return ([e for e in evs if venue_tier(e["venue"]) == 1],
                [e for e in evs if venue_tier(e["venue"]) == 2],
                [e for e in evs if venue_tier(e["venue"]) == 3])

    def ticket(leg: Leg) -> str:
        return f"""
      <div class="ticket">
        <div class="t-main">
          <div class="t-air">{esc(leg.airline)} · {esc(nice_date(leg.date))}</div>
          <div class="t-route">{esc(leg.origin)} <span class="arr">→</span> {esc(leg.dest)}</div>
          <div class="t-times">{leg.dep_hm} — {leg.arr_hm}</div>
          {f'<div class="t-spark">{leg_hist(leg)}</div>' if leg_hist(leg) else ''}
        </div>
        <div class="t-stub">
          <div class="t-price">{SYM}{leg.price}</div>
          <a class="t-book" href="{gf_link(leg.origin, leg.dest, leg.date, adults)}">book</a>
          <div class="barcode"></div>
        </div>
      </div>"""

    def alt_row(opt: Option) -> str:
        via = ("nonstop" if opt.kind == "direct"
               else f"via {opt.legs[0].dest} · {fmt_dur(opt.connect_min or 0)} gap")
        legs_m = " + ".join(f"{l.dep_hm} {l.origin}→{l.dest}" for l in opt.legs)
        books = " ".join(
            f'<a href="{gf_link(l.origin, l.dest, l.date, adults)}">↗</a>'
            for l in opt.legs)
        return (f'<div class="altrow"><span class="m">{esc(legs_m)}</span>'
                f'<span class="via">{esc(via)} · lands {opt.legs[-1].arr_hm}</span>'
                f'<span class="ap">{SYM}{opt.price}</span>{books}</div>')

    def mini_leg(l: Leg, show_date: bool = False) -> str:
        d = f'{esc(dow(l.date))} ' if show_date else ""
        return (f'<div class="mini"><span class="m">{d}{l.dep_hm}–{l.arr_hm}</span> '
                f'<b>{esc(l.origin)}→{esc(l.dest)}</b> '
                f'<span class="m">{esc(l.airline)} · {SYM}{l.price}</span> '
                f'<a href="{gf_link(l.origin, l.dest, l.date, adults)}">BOOK ↗</a></div>')

    def event_cards(day: str, mids: int | None = None, smalls: bool = True) -> str:
        t1, t2, t3 = tiered(day)
        out = "".join(
            f"""<div class="ev-big"><div class="v">{esc(e["venue"])}</div>
              <div class="t">{esc(e["title"])}</div>
              {'<div class="d">' + esc(", ".join(e["djs"])) + '</div>' if e["djs"] else ''}
              <div class="meta">{esc(e["time"])}{' · <b>' + esc(e["price_str"]) + '</b>' if e["price_str"] else ''}</div>
            </div>""" for e in t1
        ) + "".join(
            f"""<div class="ev-mid"><div class="v">{esc(e["venue"])}</div>
              <div class="t">{esc(e["title"])}</div>
              {'<div class="d">' + esc(", ".join(e["djs"])) + '</div>' if e["djs"] else ''}
              <div class="meta">{esc(e["time"])}{' · <b>' + esc(e["price_str"]) + '</b>' if e["price_str"] else ''}</div>
            </div>""" for e in (t2 if mids is None else t2[:mids])
        )
        if smalls:
            out += "".join(
                f'<div class="ev-small"><b>{esc(e["venue"])}</b> — {esc(e["title"])}'
                f'{" · " + esc(e["price_str"]) if e["price_str"] else ""}</div>'
                for e in t3
            )
        return out

    def headliners(day: str) -> str:
        t1, t2, _ = tiered(day)
        names = [e["title"] for e in t1] or [e["title"] for e in t2]
        return " · ".join(names[:3]) if names else "quiet night on the big calendars"

    # ---------- day rows + detail screens ----------
    rows: list[tuple[str, str]] = []
    details: list[str] = []

    for r in results:
        t, od, bd = r["total"], r["od"], r["bd"]
        o, b = r["outs"][0], r["backs"][0]
        is_best = t == best_price
        top = " top" if t <= best_price * 1.25 else ""
        did = f"day-{od}"
        delta = "cheapest" if is_best else f"+{SYM}{t - best_price}"
        badge = '<span class="badge">CHEAPEST</span>' if is_best else ""
        barw = int(t / max_price * 100)
        to_, tb_ = typical_for(o.legs[0]), typical_for(b.legs[0])
        vt = (f'<span class="vt">vs typical {SYM}{to_ + tb_}</span>'
              if to_ is not None and tb_ is not None else "")

        rows.append((od, f"""
      <button class="row{top}" onclick="openDay('{did}')">
        <span class="dow">{dow(od).upper()}<br>→{dow(bd).upper()}</span>
        <span class="dates">{esc(nice_date(od))} – {esc(nice_date(bd))}{badge}
          <span class="lands">· lands {o.legs[-1].arr_hm}</span></span>
        <span class="head">{esc(headliners(od))}</span>
        <span class="price"><span class="p">{SYM}{t}</span>
          <span class="delta">{esc(delta)}</span>{vt}</span>
        <span class="bar"><i style="width:{barw}%"></i></span>
      </button>"""))

        def direction_block(label: str, opts: list[Option]) -> str:
            prim = opts[0]
            parts = [f'<div class="seclabel">{esc(label)} · {SYM}{prim.price}</div>']
            for i, leg in enumerate(prim.legs):
                if i:
                    parts.append(f'<div class="hop">{fmt_dur(prim.connect_min or 0)} on the '
                                 f'ground in {esc(prim.legs[0].dest)}</div>')
                parts.append(ticket(leg))
            if len(opts) > 1:
                parts.append('<div class="dsub">other ways that day</div>')
                parts += [alt_row(a) for a in opts[1:]]
            return "".join(parts)

        details.append(f"""
    <section class="day-detail" id="{did}">
      <button class="dback" onclick="closeDay()"><span class="chev">←</span>
        <span class="t">{esc(nice_date(od))} – {esc(nice_date(bd))}</span>
        <span class="p">{SYM}{t}</span></button>
      {direction_block("getting there", r["outs"])}
      {direction_block("getting home", r["backs"])}
      <div class="seclabel">your night · {esc(nice_date(od))}</div>
      {event_cards(od)}
      <div class="seclabel">before you fly · {esc(nice_date(bd))}</div>
      {event_cards(bd, mids=3, smalls=False) or '<div class="ev-small">nothing on before your flight.</div>'}
      <div class="tag"><b>Split tickets are separate bookings.</b> A delay on one
      leg doesn't protect the next — the ground-time gaps are your buffer.</div>
    </section>""")

    for od, bd, info in (dead or []):
        did = f"day-{od}"
        why = info.get("why", "no route")
        has_on = "overnight" in info
        price_txt = f"{SYM}{info['overnight'][0]}*" if has_on else "—"
        rows.append((od, f"""
      <button class="row dead" onclick="openDay('{did}')">
        <span class="dow">{dow(od).upper()}<br>→{dow(bd).upper()}</span>
        <span class="dates">{esc(nice_date(od))} – {esc(nice_date(bd))}</span>
        <span class="why">{esc(why)}{' · overnight play inside' if has_on else ''}</span>
        <span class="price"><span class="p">{esc(price_txt)}</span></span>
      </button>"""))

        body = []
        if has_on:
            ot, oo, ob = info["overnight"]
            body.append(f'<div class="seclabel">the overnight play · {SYM}{ot} + hotel</div>')
            body.append(ticket(oo.legs[0]))
            body.append(f'<div class="hop">sleep in {esc(oo.legs[0].dest)} '
                        f'(hotel not priced)</div>')
            body.append(ticket(oo.legs[1]))
            body.append(f'<div class="dsub">home · {SYM}{ob.price}</div>')
            body += [mini_leg(l) for l in ob.legs]
        if "late" in info:
            lt, lo, lb = info["late"]
            body.append(f'<div class="seclabel">the late option · {SYM}{lt}</div>')
            body += [mini_leg(l, show_date=True) for l in (*lo.legs, *lb.legs)]
        details.append(f"""
    <section class="day-detail" id="{did}">
      <button class="dback" onclick="closeDay()"><span class="chev">←</span>
        <span class="t">{esc(nice_date(od))} – {esc(nice_date(bd))}</span>
        <span class="p">{esc(price_txt)}</span></button>
      <div class="intro" style="margin-top:.8rem">{esc(why)}</div>
      {"".join(body)}
      <div class="seclabel">that night · {esc(nice_date(od))}</div>
      {event_cards(od)}
    </section>""")

    rows.sort(key=lambda x: x[0])

    pane_days = f"""
    <section id="pane-days" class="pane active">
      <div class="intro" style="margin-top:.3rem">Every {trip_label} in your range,
      priced for {adults}. Cheapest is <b>{SYM}{best_price}</b>
      ({esc(nice_date(best["od"]))}), dearest {SYM}{max_price}. Tap a day for flights,
      alternatives and what's on.</div>
      {"".join(h for _, h in rows)}
      <div class="tag">Greyed rows miss your land-by time — open them for the late
      option or the fly-out-the-night-before play (<b>*</b> = add a hotel night).
      Prices checked {esc(checked_at)}, totals for {adults}, hand luggage,
      book each leg direct.</div>
    </section>"""

    # ---------- nights pane ----------
    days = sorted(events_by_day.keys())
    chips, nights = [], []
    for i, day in enumerate(days):
        d = datetime.strptime(day, "%Y-%m-%d")
        active = " active" if i == 0 else ""
        chips.append(f'<button class="chip{active}" data-d="{day}">'
                     f'{d.strftime("%a").upper()}<b>{d.day}</b></button>')
        nights.append(f'<div class="night{active}" id="night-{day}">'
                      f'{event_cards(day) or "<div class=ev-small>nothing listed.</div>"}</div>')

    pane_nights = f"""
    <section id="pane-nights" class="pane">
      <div class="chips">{"".join(chips)}</div>
      {"".join(nights)}
    </section>"""

    org = "+".join(cfg["origins"])
    dst = "+".join(cfg["destination"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#17132E">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>{esc(org)} → {esc(dst)} · from {SYM}{best_price}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎟️</text></svg>">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700;800&family=Space+Grotesk:wght@400;600&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>{DASH_CSS}</style></head><body>
<header class="top"><span class="brand">SPLITFARE</span>
  <span class="route">{esc(org)} ⇄ {esc(dst)}</span>
  <span class="fresh">checked {esc(checked_at)}</span></header>
<main>
{pane_days}
{pane_nights}
<footer>GENERATED BY SPLITFARE · PRICES ARE TOTALS FOR {adults} ADULTS</footer>
</main>
{"".join(details)}
<nav class="tabbar">
  <button data-t="days" class="active"><span class="ic">≡</span>days</button>
  <button data-t="nights"><span class="ic">♪</span>nights</button>
</nav>
<script>
const tabs=[...document.querySelectorAll('.tabbar button')];
tabs.forEach(t=>t.addEventListener('click',()=>{{
  closeDay();
  tabs.forEach(x=>x.classList.toggle('active',x===t));
  document.querySelectorAll('.pane').forEach(p=>
    p.classList.toggle('active',p.id==='pane-'+t.dataset.t));
  window.scrollTo(0,0);
}}));
function openDay(id){{
  document.body.classList.add('detail-open');
  document.querySelectorAll('.day-detail').forEach(d=>
    d.classList.toggle('active',d.id===id));
  history.replaceState(null,'','#'+id);
  window.scrollTo(0,0);
}}
function closeDay(){{
  document.body.classList.remove('detail-open');
  document.querySelectorAll('.day-detail').forEach(d=>d.classList.remove('active'));
  history.replaceState(null,'','#');
  window.scrollTo(0,0);
}}
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>{{
  document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x===c));
  document.querySelectorAll('.night').forEach(n=>
    n.classList.toggle('active',n.id==='night-'+c.dataset.d));
}}));
if(location.hash.startsWith('#day-'))openDay(location.hash.slice(1));
</script>
</body></html>"""


# ------------------------------------------------------------------ watch mode

def load_watches() -> list[dict]:
    if not WATCH_FILE.exists():
        return []
    try:
        data = json.loads(WATCH_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_watches(watches: list[dict]) -> None:
    WATCH_FILE.write_text(json.dumps(watches, indent=2) + "\n")


def _tg_conf(cfg: dict) -> tuple[str | None, str | None]:
    """(bot token, chat id) from config alerts.telegram + env, tolerantly."""
    alerts = cfg.get("alerts") or {}
    tg = alerts.get("telegram") or {}
    env = tg.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(env)
    if not token and env != "TELEGRAM_BOT_TOKEN":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
    return (token or None), (tg.get("chat_id") or None)


def tg_send(cfg: dict, text: str) -> bool:
    """Send a Telegram message if configured. Returns False when unconfigured
    or the send fails (watch still prints to stdout either way)."""
    token, chat = _tg_conf(cfg)
    if not token or not chat:
        return False
    try:
        r = client().post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text,
                                "disable_web_page_preview": False})
        return r.status_code == 200
    except Exception:
        return False


def _watch_args(w: dict, fresh: bool) -> argparse.Namespace:
    return argparse.Namespace(
        month=w["month"], nights=w.get("nights", 1), out_dow=w.get("out_dow"),
        deep=w.get("deep", 4), fresh=fresh, arrive_by=w.get("arrive_by"),
        home_by=w.get("home_by"), events_filter=w.get("events_filter"),
    )


def cmd_watch_add(args: argparse.Namespace) -> None:
    cfg = load_config(args)
    watches = load_watches()
    wid = secrets.token_hex(4)
    w = {
        "id": wid,
        "name": args.name or f"{args.month} {args.nights}-night",
        "month": args.month, "nights": args.nights,
        "out_dow": args.out_dow, "deep": args.deep,
        "max_total": args.max_total,
        "arrive_by": args.arrive_by, "home_by": args.home_by,
        "events_filter": args.events_filter,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_alerts": {},  # "od|bd" -> total last alerted at
    }
    watches.append(w)
    save_watches(watches)
    console.print(f"[green]✓[/green] watch [bold]{wid}[/bold] added — "
                  f"{w['name']}, alert when a window totals ≤ {SYM}{args.max_total}")


def cmd_watch_list(args: argparse.Namespace) -> None:
    watches = load_watches()
    if not watches:
        console.print("[dim]no watches — add one: splitfare watch add "
                      "--month 2026-09 --nights 1 --max-total 180[/dim]")
        return
    t = Table(title="price watches", title_style="bold cyan",
              title_justify="left", header_style="bold", border_style="dim")
    t.add_column("id", style="yellow")
    t.add_column("name")
    t.add_column("shape")
    t.add_column("target", justify="right")
    t.add_column("alerts sent", justify="right")
    for w in watches:
        shape = f"{w['month']} · {w['nights']}n"
        if w.get("out_dow"):
            shape += f" · out {w['out_dow']}"
        if w.get("events_filter"):
            shape += f" · events={w['events_filter']}"
        t.add_row(w["id"], w.get("name", "?"), shape,
                  f"{SYM}{w.get('max_total', '?')}", str(len(w.get("last_alerts", {}))))
    console.print(t)


def cmd_watch_remove(args: argparse.Namespace) -> None:
    watches = load_watches()
    before = len(watches)
    watches = [w for w in watches if w["id"] != args.id]
    save_watches(watches)
    if len(watches) == before:
        console.print(f"[yellow]no watch with id {args.id}[/yellow]")
    else:
        console.print(f"[green]✓[/green] removed watch {args.id}")


def cmd_watch_check(args: argparse.Namespace) -> None:
    cfg = load_config(args)
    watches = load_watches()
    if args.id:
        watches = [w for w in watches if w["id"] == args.id]
    if not watches:
        console.print("[dim]no watches to check — add one: splitfare watch add "
                      "--month 2026-09 --nights 1 --max-total 180[/dim]")
        return

    under = 0
    for w in watches:
        label = w.get("name", w["id"])
        with console.status(f"checking {label}…") as status:
            results, _dead = two_pass_scan(cfg, _watch_args(w, args.fresh),
                                           cfg["hubs"], status)
        hits = [r for r in results if r["total"] <= w.get("max_total", 0)]
        under += len(hits)
        new_hits = []
        for r in hits:
            k = f"{r['od']}|{r['bd']}"
            prev = w["last_alerts"].get(k)
            if prev is None or r["total"] < prev * 0.95:  # new, or ≥5% cheaper
                new_hits.append(r)
                w["last_alerts"][k] = r["total"]
        if new_hits:
            save_watches(watches)  # remember alert state before sending
            for r in new_hits:
                o, b = r["outs"][0], r["backs"][0]
                lines = [
                    f"🎟️ splitfare · {label}",
                    f"{nice_date(r['od'])} → {nice_date(r['bd'])} for "
                    f"{SYM}{r['total']} ({cfg['adults']} adults) — "
                    f"target ≤ {SYM}{w['max_total']}",
                    f"OUT  {SYM}{o.price}  {o.describe()}",
                    f"BACK {SYM}{b.price}  {b.describe()}",
                ]
                for l in (o.legs + b.legs):
                    lines.append(f"🔗 {gf_link(l.origin, l.dest, l.date, cfg['adults'])}")
                text = "\n".join(lines)
                sent = tg_send(cfg, text)
                console.print(
                    f"🔔 {label}: {nice_date(r['od'])} → {nice_date(r['bd'])} "
                    f"{SYM}{r['total']}"
                    + (" · telegram sent" if sent
                       else " · [dim]telegram not configured "
                            "(set TELEGRAM_BOT_TOKEN + alerts.telegram.chat_id)[/dim]")
                )
        elif hits:
            console.print(f"[dim]{label}: {len(hits)} under target, already alerted[/dim]")
        else:
            console.print(f"[dim]{label}: nothing under {SYM}{w['max_total']}[/dim]")
    console.print(f"[bold]{under} window(s) under target[/bold] across "
                  f"{len(watches)} watch(es)")


# ---------------------------------------------------------------------- CLI

def load_config(args: argparse.Namespace) -> dict:
    if not CONFIG_PATH.exists():
        console.print("[bold red]no config.json found[/bold red] — run "
                      "[bold]splitfare init[/bold] to set up your route.")
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text())
    if getattr(args, "adults", None):
        cfg["adults"] = args.adults
    set_currency(cfg.get("currency", "GBP"))
    return cfg


def cmd_init(args: argparse.Namespace) -> None:
    """Interactive config wizard."""
    from rich.prompt import Confirm, IntPrompt, Prompt

    console.print(Panel(
        "[bold]splitfare setup[/bold] — a few questions and you're hunting.\n"
        "[dim]Airport codes are IATA codes (the 3 letters on your boarding "
        "pass): LGW = London Gatwick, BER = Berlin, LIS = Lisbon…[/dim]",
        border_style="cyan"))
    origins = Prompt.ask("Home airport code(s), comma-separated (e.g. LGW,STN)")
    dest = Prompt.ask("Destination airport code(s) (e.g. LIS)")
    adults = IntPrompt.ask("Party size (adults)", default=2)
    console.print(
        "\n[bold]Hubs[/bold] are the airports split tickets connect through — "
        "pick 5-10 airports\nwith [bold]cheap budget-airline service from BOTH "
        "your home and your destination[/bold]\n(think Ryanair/easyJet/Wizz "
        "bases: e.g. STN, LTN, BCN, MXP-BGY, VIE, WAW…).\nMore hubs = more "
        "combos found, but slower scans.\n")
    hubs = Prompt.ask("Hub airport codes, comma-separated (empty = direct-only)",
                      default="")
    currency = Prompt.ask("Currency", default="EUR")
    connect = IntPrompt.ask(
        "Minimum self-transfer connection in minutes (separate tickets — "
        "bigger is safer)", default=120)
    ryanair = Confirm.ask(
        "Add Ryanair's public fare API as a fallback source?", default=True)
    easyjet = Confirm.ask(
        "Add easyJet's fare API too? (more coverage, no key)", default=True)
    wizz = Confirm.ask(
        "Add Wizz Air's API as well? (no key)", default=False)
    srcs = ["google"]
    if ryanair:
        srcs.append("ryanair")
    if easyjet:
        srcs.append("easyjet")
    if wizz:
        srcs.append("wizz")
    console.print(
        "\n[dim]Events providers: [bold]resident-advisor[/bold] = clubbing, "
        "worldwide · [bold]ticketmaster[/bold]/[bold]skiddle[/bold] = gigs "
        "(free API key needed) · [bold]ibiza-spotlight[/bold] = Ibiza only · "
        "[bold]none[/bold] = flights only[/dim]")
    ev = Prompt.ask("Events provider", default="none",
                    choices=sorted(providers.PROVIDERS))
    events: dict = {"provider": ev}
    if ev == "resident-advisor":
        known = ", ".join(f"{k} {v}" for k, v in providers.RA_AREAS.items())
        events["area_id"] = IntPrompt.ask(
            f"RA area id ({known}; others: ra.co → your city → network tab)",
            default=25)
    if ev == "ticketmaster":
        events["city"] = Prompt.ask("City name for Ticketmaster search")
        console.print("[dim]put your free key in $TICKETMASTER_API_KEY[/dim]")
    if ev == "skiddle":
        console.print("[dim]put your free key in $SKIDDLE_API_KEY; add "
                      "latitude/longitude/radius_miles to config.json to "
                      "narrow the area[/dim]")

    alerts: dict = {}
    if Confirm.ask("Set up Telegram alerts for watch mode? (needs a bot "
                   "token from @BotFather)", default=False):
        alerts = {
            "telegram": {
                "bot_token_env": Prompt.ask(
                    "Env var holding the bot token", default="TELEGRAM_BOT_TOKEN"),
                "chat_id": Prompt.ask(
                    "Telegram chat id (your id: message @userinfobot)"),
            }
        }

    cfg = {
        "origins": [s.strip().upper() for s in origins.split(",") if s.strip()],
        "destination": [s.strip().upper() for s in dest.split(",") if s.strip()],
        "hubs": [s.strip().upper() for s in hubs.split(",") if s.strip()],
        "adults": adults,
        "currency": currency.upper(),
        "min_connect_minutes": connect,
        "cache_ttl_hours": 6,
        "flight_sources": srcs,
        "events": events,
        "alerts": alerts,
    }
    if not cfg["origins"] or not cfg["destination"]:
        console.print("[red]origins and destination are required — run init "
                      "again.[/red]")
        return
    if CONFIG_PATH.exists() and not Confirm.ask("config.json exists — overwrite?"):
        console.print("[dim]left as it was.[/dim]")
        return
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    fri = date.today() + timedelta(days=(4 - date.today().weekday()) % 7 + 28)
    console.print(f"[green]✓[/green] wrote {CONFIG_PATH.name} — try: "
                  f"[bold]splitfare window {fri} {fri + timedelta(days=1)}[/bold]")


def header(cfg: dict, hubs: list[str]) -> None:
    console.print(Panel(
        f"[bold]{'+'.join(cfg['origins'])} ⇄ {'+'.join(cfg['destination'])}[/bold]"
        f"   [dim]|[/dim]   {cfg['adults']} adults, {cfg.get('currency', 'GBP')}, totals for the party"
        f"\n[dim]hubs: {', '.join(hubs) if hubs else '(direct only)'}"
        f"  ·  min connect {cfg.get('min_connect_minutes', 120)}m  ·  cache TTL {cfg.get('cache_ttl_hours', 6)}h[/dim]",
        border_style="cyan",
    ))


def cmd_window(args: argparse.Namespace) -> None:
    cfg = load_config(args)
    hubs = args.hubs.split(",") if args.hubs else cfg["hubs"]
    out_day, back_day = args.out_date, args.back_date
    header(cfg, hubs)

    arrive_by = hhmm_to_min(args.arrive_by) if args.arrive_by else None
    home_by = hhmm_to_min(args.home_by) if args.home_by else None
    with console.status("warming up…") as status:
        outs = best_direction(cfg["origins"], cfg["destination"], out_day, hubs, cfg,
                              args.fresh, arrive_by_min=arrive_by, status=status)
        backs = best_direction(cfg["origins"], cfg["destination"], back_day, hubs, cfg,
                               args.fresh, reverse=True, arrive_by_min=home_by, status=status)

    console.print(options_table(f"OUTBOUND · {nice_date(out_day)}", dedupe(outs, args.top), cfg["adults"]))
    console.print()
    console.print(options_table(f"RETURN · {nice_date(back_day)} (home same day)", dedupe(backs, args.top), cfg["adults"]))

    if outs and backs:
        o, b = outs[0], backs[0]
        hist = load_history()
        cur = cfg.get("currency", "GBP")
        ins_lines = []
        for leg in (o.legs[0], b.legs[0]):
            ins = price_insight(price_key(leg.origin, leg.dest, leg.date,
                                          cfg["adults"], cur), leg.price, hist)
            if ins:
                ins_lines.append(
                    f"[dim]{leg.origin}→{leg.dest}: {SYM}{leg.price} vs typical "
                    f"{SYM}{ins['p50']} · {ins['label']} · trend {ins['trend']}[/dim]"
                )
        body = (f"[bold green]{SYM}{o.price + b.price} total[/bold green] for "
                f"{cfg['adults']} adults\n"
                f"[bold]OUT[/bold]  {SYM}{o.price:<5} {o.describe()}\n"
                f"[bold]BACK[/bold] {SYM}{b.price:<5} {b.describe()}")
        if ins_lines:
            body += "\n\n" + "\n".join(ins_lines)
        console.print(Panel(
            body,
            title="🏆 cheapest combination", title_align="left", border_style="green",
        ))
        print_booking_links(outs, cfg["adults"], "outbound")
        print_booking_links(backs, cfg["adults"], "return")

    events_by_day: dict[str, list[dict]] = {}
    if args.events or args.html:
        for d in (out_day, back_day):
            if args.events:
                console.print(events_panel(d))
            if args.html:
                events_by_day[d] = fetch_events(d)

    if args.html and outs and backs:
        page = html_report(
            f"{'+'.join(cfg['origins'])} → {'+'.join(cfg['destination'])} · "
            f"{nice_date(out_day)} → {nice_date(back_day)}",
            [(out_day, back_day, outs[0], backs[0])],
            cfg["adults"],
            events_by_day,
        )
        Path(args.html).write_text(page)
        console.print(f"[dim]HTML report written to {args.html}[/dim]")

    warn_if_unhealthy()


def daterange(month: str) -> list[date]:
    y, m = (int(x) for x in month.split("-"))
    d = date(y, m, 1)
    days = []
    while d.month == m:
        days.append(d)
        d += timedelta(days=1)
    return days


def cmd_scan(args: argparse.Namespace) -> None:
    cfg = load_config(args)
    hubs = args.hubs.split(",") if args.hubs else cfg["hubs"]
    nights = args.nights
    dows = {s.strip().lower() for s in args.out_dow.split(",")} if args.out_dow else None
    header(cfg, hubs)

    windows: list[tuple[str, str]] = []
    for d in daterange(args.month):
        if dows and d.strftime("%a").lower()[:3] not in dows:
            continue
        back = d + timedelta(days=nights)
        windows.append((d.isoformat(), back.isoformat()))

    console.print(f"[bold]Pass 1/2[/bold] — direct-only sweep of {len(windows)} windows…")

    # Rank estimate: sum of known direct prices, plus a typical split-ticket
    # cost for each direction that has no direct — so a £78-direct-out window
    # needing a split home still outranks a £450 all-direct weekend.
    sweep = Table(header_style="bold", border_style="dim", pad_edge=False)
    sweep.add_column("Window")
    sweep.add_column("Direct out", justify="right")
    sweep.add_column("Direct back", justify="right")
    sweep.add_column("Note")

    arrive_by = hhmm_to_min(args.arrive_by) if args.arrive_by else None
    home_by = hhmm_to_min(args.home_by) if args.home_by else None
    split_est = cfg.get("split_estimate", 150)  # typical split-ticket direction cost
    ranked: list[tuple[int, str, str]] = []
    with console.status("sweeping…") as status:
        for out_day, back_day in windows:
            outs = best_direction(cfg["origins"], cfg["destination"], out_day, [], cfg,
                                  args.fresh, arrive_by_min=arrive_by, status=status)
            backs = best_direction(cfg["origins"], cfg["destination"], back_day, [], cfg,
                                   args.fresh, reverse=True, arrive_by_min=home_by, status=status)
            o = outs[0].price if outs else None
            b = backs[0].price if backs else None
            missing = (o is None) + (b is None)
            estimate = (o or 0) + (b or 0) + missing * split_est
            ranked.append((estimate, out_day, back_day))
            note = "" if missing == 0 else f"needs split × {missing}"
            sweep.add_row(
                f"{nice_date(out_day)} → {nice_date(back_day)}",
                f"{SYM}{o}" if o is not None else "[dim]—[/dim]",
                f"{SYM}{b}" if b is not None else "[dim]—[/dim]",
                f"[dim]~{SYM}{estimate} · {note}[/dim]" if note else f"[dim]{SYM}{estimate}[/dim]",
            )
    console.print(sweep)

    ranked.sort(key=lambda r: r[0])
    deep = ranked if args.deep == 0 else ranked[: args.deep]

    console.print(
        f"\n[bold]Pass 2/2[/bold] — deep split-ticket scan of "
        f"{'ALL' if args.deep == 0 else 'top'} {len(deep)} windows…"
    )
    results = []
    with console.status("deep-scanning…") as status:
        for _, out_day, back_day in deep:
            status.update(f"[dim]window[/dim] {out_day} → {back_day}")
            outs = best_direction(cfg["origins"], cfg["destination"], out_day, hubs, cfg,
                                  args.fresh, arrive_by_min=arrive_by, status=status)
            backs = best_direction(cfg["origins"], cfg["destination"], back_day, hubs, cfg,
                                   args.fresh, reverse=True, arrive_by_min=home_by, status=status)
            if not outs or not backs:
                continue
            results.append((outs[0].price + backs[0].price, out_day, back_day, outs[0], backs[0]))

    results.sort(key=lambda r: r[0])
    if not results:
        console.print("[yellow]No complete window found — try more hubs or --deep.[/yellow]")
        warn_if_unhealthy()
        return

    console.print()
    hist = load_history()
    cur = cfg.get("currency", "GBP")
    parts = []
    for i, (total, out_day, back_day, o, b) in enumerate(results):
        marker = "🏆 " if i == 0 else f"{i + 1}. "
        style = "bold green" if i == 0 else "bold"
        conn_o = f"  [dim](connect {fmt_dur(o.connect_min)})[/dim]" if o.connect_min else ""
        conn_b = f"  [dim](connect {fmt_dur(b.connect_min)})[/dim]" if b.connect_min else ""
        parts.append(
            f"[{style}]{marker}{SYM}{total}  {nice_date(out_day)} → {nice_date(back_day)}[/{style}]\n"
            f"   OUT  {SYM}{o.price:<5} {o.describe()}{conn_o}\n"
            f"   BACK {SYM}{b.price:<5} {b.describe()}{conn_b}"
        )
        # buy/wait insight: cheapest legs vs their own route-date history
        for leg in (o.legs[0], b.legs[0]):
            ins = price_insight(price_key(leg.origin, leg.dest, leg.date,
                                          cfg["adults"], cur), leg.price, hist)
            if ins:
                parts.append(
                    f"   [dim]{leg.origin}→{leg.dest}: {SYM}{leg.price} vs "
                    f"typical {SYM}{ins['p50']} · {ins['label']} · "
                    f"trend {ins['trend']}[/dim]"
                )
    console.print(Panel("\n\n".join(parts), title="Final ranking (cheapest first)",
                        title_align="left", border_style="green"))

    winner = results[0]
    print_booking_links([winner[3]], cfg["adults"], "winning outbound")
    print_booking_links([winner[4]], cfg["adults"], "winning return")

    events_by_day: dict[str, list[dict]] = {}
    if args.events or args.html:
        for d in (winner[1], winner[2]):
            if args.events:
                console.print(events_panel(d))
            if args.html:
                events_by_day[d] = fetch_events(d)

    if args.html:
        page = html_report(
            f"{'+'.join(cfg['origins'])} → {'+'.join(cfg['destination'])} · "
            f"best {nights}-night windows, {args.month}",
            [(od, bd, o, b) for _, od, bd, o, b in results],
            cfg["adults"],
            events_by_day,
        )
        Path(args.html).write_text(page)
        console.print(f"[dim]HTML report written to {args.html}[/dim]")

    warn_if_unhealthy()


def cmd_events(args: argparse.Namespace) -> None:
    d0 = datetime.strptime(args.date, "%Y-%m-%d").date()
    for i in range(args.days):
        console.print(events_panel((d0 + timedelta(days=i)).isoformat()))


def two_pass_scan(cfg: dict, args: argparse.Namespace, hubs: list[str],
                  status) -> tuple[list, list]:
    """Quiet version of the scan pipeline; returns the deep-scan results."""
    dows = {s.strip().lower() for s in args.out_dow.split(",")} if args.out_dow else None
    arrive_by = hhmm_to_min(args.arrive_by) if args.arrive_by else None
    home_by = hhmm_to_min(args.home_by) if args.home_by else None
    split_est = cfg.get("split_estimate", 150)

    windows = []
    for d in daterange(args.month):
        if dows and d.strftime("%a").lower()[:3] not in dows:
            continue
        windows.append((d.isoformat(), (d + timedelta(days=args.nights)).isoformat()))

    ranked = []
    for out_day, back_day in windows:
        outs = best_direction(cfg["origins"], cfg["destination"], out_day, [], cfg,
                              args.fresh, arrive_by_min=arrive_by, status=status)
        backs = best_direction(cfg["origins"], cfg["destination"], back_day, [], cfg,
                               args.fresh, reverse=True, arrive_by_min=home_by, status=status)
        o = outs[0].price if outs else None
        b = backs[0].price if backs else None
        missing = (o is None) + (b is None)
        ranked.append(((o or 0) + (b or 0) + missing * split_est, out_day, back_day))

    ranked.sort(key=lambda r: r[0])
    deep = ranked if args.deep == 0 else ranked[: args.deep]
    results, dead = [], []
    ev_filter = getattr(args, "events_filter", None)  # None | "any" | "big"
    for i, (_, out_day, back_day) in enumerate(deep, 1):
        status.update(f"[dim]deep-scanning[/dim] {out_day} → {back_day} "
                      f"[dim]({i}/{len(deep)})[/dim]")
        if ev_filter:
            evs = fetch_events(out_day)
            if ev_filter == "any" and not evs:
                continue
            if ev_filter == "big" and not any(
                    venue_tier(e["venue"]) == 1 for e in evs):
                continue
        outs = best_direction(cfg["origins"], cfg["destination"], out_day, hubs, cfg,
                              args.fresh, arrive_by_min=arrive_by, status=status)
        backs = best_direction(cfg["origins"], cfg["destination"], back_day, hubs, cfg,
                               args.fresh, reverse=True, arrive_by_min=home_by, status=status)
        if outs and backs:
            results.append({
                "total": outs[0].price + backs[0].price,
                "od": out_day, "bd": back_day,
                "outs": dedupe(outs, 4), "backs": dedupe(backs, 4),
            })
        else:
            # Show what the time limits are costing: reprice without them,
            # and check the position-the-night-before play.
            u_outs = outs or best_direction(cfg["origins"], cfg["destination"], out_day,
                                            hubs, cfg, args.fresh, status=status)
            u_backs = backs or best_direction(cfg["origins"], cfg["destination"], back_day,
                                              hubs, cfg, args.fresh, reverse=True, status=status)
            info: dict = {"why": "no same-day route at all"}
            if u_outs and u_backs:
                t = u_outs[0].price + u_backs[0].price
                if not outs:
                    info["why"] = (f"{SYM}{t} exists but lands "
                                   f"{u_outs[0].legs[-1].arr_hm} — after your cutoff")
                    info["late"] = (t, u_outs[0], u_backs[0])
                else:
                    info["why"] = (f"{SYM}{t} exists but gets home "
                                   f"{u_backs[0].legs[-1].arr_hm} — after your cutoff")
                    info["late"] = (t, u_outs[0], u_backs[0])
            if not outs and backs:
                on = overnight_option(cfg, out_day, hubs, args.fresh, arrive_by, status)
                if on:
                    info["overnight"] = (on.price + backs[0].price, on, backs[0])
            dead.append((out_day, back_day, info))
    results.sort(key=lambda r: r["total"])
    return results, dead


def cmd_publish(args: argparse.Namespace) -> None:
    import subprocess

    cfg = load_config(args)
    hubs = args.hubs.split(",") if args.hubs else cfg["hubs"]
    header(cfg, hubs)

    with console.status("scanning…") as status:
        results, dead = two_pass_scan(cfg, args, hubs, status)
        if not results:
            console.print("[yellow]No complete window found — nothing to publish.[/yellow]")
            return
        status.update("[dim]fetching destination events…[/dim]")
        days_needed = sorted({d for r in results for d in (r["od"], r["bd"])})
        events_by_day = {d: fetch_events(d) for d in days_needed}

    checked_at = datetime.now().strftime("%a %-d %b, %H:%M")
    page = render_dashboard(results, events_by_day, cfg, checked_at, dead,
                            hist=load_history())
    site = HOME_DIR / "site"
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text(page)
    console.print(f"[green]✓[/green] dashboard written to site/index.html "
                  f"(cheapest {SYM}{results[0]['total']}, "
                  f"{nice_date(results[0]['od'])} → {nice_date(results[0]['bd'])})")

    if args.deploy:
        console.print("[dim]deploying to Vercel…[/dim]")
        r = subprocess.run(
            ["vercel", "deploy", "--prod", "--yes"],
            cwd=site, capture_output=True, text=True,
        )
        url = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode == 0 and url.startswith("https://"):
            console.print(f"[bold green]live:[/bold green] [link={url}]{url}[/link]")
        else:
            console.print(f"[red]deploy failed:[/red]\n{r.stderr[-800:]}")
    warn_if_unhealthy()


ASK_BRIEF = """You are the flight-hunting assistant for the splitfare CLI.
Answer the user's question by RUNNING the tool with Bash, then give a short,
concrete answer (flights, times, total prices, and any caveats). Prices are
totals for the whole party.

Run commands with exactly this executable:
  {SPLITFARE_CMD} window OUT_DATE BACK_DATE [--events] [--top N] [--adults N] [--arrive-by HH:MM] [--home-by HH:MM]
  {SPLITFARE_CMD} scan --month YYYY-MM [--nights N] [--out-dow thu,fri] [--deep N] [--adults N] [--arrive-by HH:MM] [--home-by HH:MM] [--html FILE]
  {SPLITFARE_CMD} events YYYY-MM-DD [--days N]

Notes: results are cached 6h so repeat runs are fast; a full uncached scan can
take minutes — prefer `window` for specific dates. --arrive-by caps the
outbound arrival time; --home-by caps the return arrival time. Split-ticket
results are separate bookings with no missed-connection protection — mention
this when a split wins. Current config:
"""


def cmd_ask(args: argparse.Namespace) -> None:
    import subprocess

    question = " ".join(args.question)
    runner = f"{sys.executable} {Path(__file__).resolve()}"
    prompt = (ASK_BRIEF.replace("{SPLITFARE_CMD}", runner)
              + CONFIG_PATH.read_text() + f"\nQuestion: {question}")
    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", f"Bash({runner}:*)",
    ]
    if args.model:
        cmd += ["--model", args.model]
    console.print(f"[dim]asking claude ({args.model or 'default model'})…[/dim]")
    try:
        subprocess.run(cmd, cwd=HOME_DIR, check=False)
    except FileNotFoundError:
        console.print(
            "[bold red]claude CLI not found[/bold red] — install Claude Code "
            "(https://claude.com/claude-code) to use `ask`."
        )
        sys.exit(1)


def valid_date(s: str) -> str:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{s}' is not YYYY-MM-DD")


def print_welcome() -> None:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        setup = (f"[dim]({'+'.join(cfg['origins'])} ⇄ {'+'.join(cfg['destination'])}, "
                 f"{cfg['adults']} adults)[/dim]")
    else:
        setup = "[dim](not set up yet — run [/dim][bold]splitfare init[/bold][dim] first)[/dim]"
    console.print(Panel(
        f"[bold]splitfare[/bold] — cheap flight-combo hunter {setup}\n\n"
        "[bold cyan]Ask in plain English (easiest):[/bold cyan]\n"
        '  splitfare ask [green]"cheapest weekend next month for 2, landing before 2pm"[/green]\n\n'
        "[bold cyan]Or run it directly:[/bold cyan]\n"
        "  splitfare [yellow]window[/yellow] OUT-DATE BACK-DATE [dim]--events[/dim]        "
        "[dim]# price specific dates + party calendar[/dim]\n"
        "  splitfare [yellow]scan[/yellow] --month YYYY-MM [dim]--arrive-by 14:00[/dim]      "
        "[dim]# find the cheapest dates in a month[/dim]\n"
        "  splitfare [yellow]events[/yellow] YYYY-MM-DD                       "
        "[dim]# what's on at the destination[/dim]\n\n"
        "[dim]Handy flags: --adults 2 · --arrive-by HH:MM · --home-by HH:MM · "
        "--html report.html · --fresh (ignore cache)\n"
        "Full help: splitfare --help  ·  per-command: splitfare scan --help[/dim]",
        border_style="cyan",
    ))


def main() -> None:
    if len(sys.argv) == 1:
        print_welcome()
        return

    ap = argparse.ArgumentParser(prog="splitfare", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="interactive setup — writes config.json")
    ini.set_defaults(func=cmd_init)

    w = sub.add_parser("window", help="deep-scan one out/back date pair")
    w.add_argument("out_date", type=valid_date)
    w.add_argument("back_date", type=valid_date)
    w.add_argument("--hubs", help="comma-separated override of config hubs")
    w.add_argument("--top", type=int, default=6, help="options to show per direction")
    w.add_argument("--adults", type=int, help="override party size from config")
    w.add_argument("--arrive-by", metavar="HH:MM", help="outbound must land by this time")
    w.add_argument("--home-by", metavar="HH:MM", help="return must land by this time")
    w.add_argument("--fresh", action="store_true", help="ignore cache")
    w.add_argument("--events", action="store_true", help="show destination events for both dates")
    w.add_argument("--html", metavar="FILE", help="write a shareable HTML report")
    w.set_defaults(func=cmd_window)

    s = sub.add_parser("scan", help="sweep a month for the cheapest N-night window")
    s.add_argument("--month", required=True, help="YYYY-MM")
    s.add_argument("--nights", type=int, default=1)
    s.add_argument("--out-dow", help="limit outbound days, e.g. thu,fri,sat,sun")
    s.add_argument("--deep", type=int, default=4, help="windows to deep-scan with hubs (0 = all)")
    s.add_argument("--hubs", help="comma-separated override of config hubs")
    s.add_argument("--adults", type=int, help="override party size from config")
    s.add_argument("--arrive-by", metavar="HH:MM", help="outbound must land by this time")
    s.add_argument("--home-by", metavar="HH:MM", help="return must land by this time")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--events", action="store_true", help="show events for the winning window")
    s.add_argument("--events-filter", choices=["any", "big"],
                   help="only keep windows where something's on: any event, or a tier-1 venue")
    s.add_argument("--html", metavar="FILE", help="write a shareable HTML report")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("events", help="destination party calendar")
    e.add_argument("date", type=valid_date, help="YYYY-MM-DD")
    e.add_argument("--days", type=int, default=1)
    e.set_defaults(func=cmd_events)

    p = sub.add_parser("publish", help="render the mobile dashboard (and deploy it)")
    p.add_argument("--month", required=True, help="YYYY-MM")
    p.add_argument("--nights", type=int, default=1)
    p.add_argument("--out-dow", help="limit outbound days, e.g. thu,fri,sat,sun")
    p.add_argument("--deep", type=int, default=0, help="windows to deep-scan (0 = all, default)")
    p.add_argument("--hubs")
    p.add_argument("--adults", type=int)
    p.add_argument("--arrive-by", metavar="HH:MM")
    p.add_argument("--home-by", metavar="HH:MM")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--events-filter", choices=["any", "big"],
                   help="only keep windows where something's on: any event, or a tier-1 venue")
    p.add_argument("--deploy", action="store_true", help="deploy site/ to Vercel")
    p.set_defaults(func=cmd_publish)

    watch = sub.add_parser("watch", help="trip-shape price watches with alerts")
    wsub = watch.add_subparsers(dest="watch_cmd", required=True)
    wa = wsub.add_parser("add", help="add a watch (alerts when a window hits your target)")
    wa.add_argument("--month", required=True, help="YYYY-MM")
    wa.add_argument("--nights", type=int, default=1)
    wa.add_argument("--out-dow", help="limit outbound days, e.g. thu,fri,sat,sun")
    wa.add_argument("--deep", type=int, default=4)
    wa.add_argument("--max-total", type=int, required=True,
                    help="alert when a window totals at or below this (whole party)")
    wa.add_argument("--arrive-by", metavar="HH:MM")
    wa.add_argument("--home-by", metavar="HH:MM")
    wa.add_argument("--events-filter", choices=["any", "big"])
    wa.add_argument("--name", help="label for the watch")
    wa.set_defaults(func=cmd_watch_add)
    wl = wsub.add_parser("list", help="list watches")
    wl.set_defaults(func=cmd_watch_list)
    wr = wsub.add_parser("remove", help="remove a watch")
    wr.add_argument("id")
    wr.set_defaults(func=cmd_watch_remove)
    wc = wsub.add_parser("check", help="run all watches once (cron-friendly)")
    wc.add_argument("--id", help="only check this watch id")
    wc.add_argument("--fresh", action="store_true")
    wc.set_defaults(func=cmd_watch_check)

    a = sub.add_parser("ask", help="ask in plain English (uses the claude CLI)")
    a.add_argument("question", nargs="+", help="e.g. cheapest weekend in august landing before 2pm")
    a.add_argument("--model", help="claude model alias (default: your Claude Code default)")
    a.set_defaults(func=cmd_ask)

    args = ap.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted — cached results are kept.[/dim]")
        sys.exit(130)


if __name__ == "__main__":
    main()
