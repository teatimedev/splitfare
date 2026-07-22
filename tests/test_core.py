"""Pure-logic tests — no network, no config required."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from splitfare import (  # noqa: E402
    Leg, Option, dedupe, fmt_dur, gf_link, hhmm_to_min, nice_date,
    render_dashboard, venue_tier,
)


def leg(o="BFS", d="IBZ", date="2026-08-16", dep=400, arr=650, price=78,
        airline="Jet2", offset=0):
    return Leg(origin=o, dest=d, date=date, airline=airline,
               dep_min=dep, arr_min=arr, arr_day_offset=offset, price=price)


def test_time_helpers():
    assert hhmm_to_min("14:00") == 840
    assert fmt_dur(520) == "8h40"
    assert leg(dep=400).dep_hm == "06:40"
    assert leg(arr=650, offset=1).arr_hm == "10:50+1"


def test_option_properties():
    direct = Option([leg()], "direct")
    assert direct.price == 78
    assert direct.connect_min is None
    assert direct.route == "BFS→IBZ"

    split = Option([
        leg(d="MAN", arr=730, price=86),
        leg(o="MAN", dep=1250, arr=1310, price=50),
    ], "split")
    assert split.price == 136
    assert split.connect_min == 520
    assert split.route == "BFS→MAN→IBZ"


def test_dedupe_route_signatures():
    cheap = Option([leg(price=50)], "direct")
    dear = Option([leg(price=90)], "direct")
    via = Option([leg(d="MAN", price=40),
                  leg(o="MAN", dep=1000, arr=1100, price=45)], "split")
    kept = dedupe([cheap, via, dear], keep=5)
    assert len(kept) == 2  # one per distinct routing
    assert kept[0] is cheap


def test_gf_link_encodes_query():
    url = gf_link("BFS", "IBZ", "2026-08-16", 2)
    assert "google.com/travel/flights" in url
    assert "2%20adults" in url and "curr=GBP" in url


def test_venue_tier_defaults():
    assert venue_tier("Ushuaïa Ibiza") == 1
    assert venue_tier("Cova Santa") == 2
    assert venue_tier("Some Random Bar") == 3


def test_render_dashboard_smoke():
    out = Option([leg()], "direct")
    back = Option([
        leg(o="IBZ", d="MAN", date="2026-08-17", dep=630, arr=730, price=86,
            airline="Ryanair"),
        leg(o="MAN", d="BFS", date="2026-08-17", dep=1250, arr=1310, price=50,
            airline="easyJet"),
    ], "split")
    results = [{"total": 214, "od": "2026-08-16", "bd": "2026-08-17",
                "outs": [out], "backs": [back]}]
    events = {"2026-08-16": [
        {"title": "Solomun +1", "venue": "Pacha Ibiza", "time": "23:00–06:00",
         "from_eur": "50", "djs": ["Solomun"]}]}
    html = render_dashboard(results, events, {"origins": ["BFS"],
                            "destination": ["IBZ"], "adults": 2},
                            "Mon 20 Jul, 15:00", dead=[])
    assert "£214" in html and "Solomun" in html
    assert "day-2026-08-16" in html          # detail screen exists
    assert "BFS" in html and "MAN" in html
    assert nice_date("2026-08-16") in html
