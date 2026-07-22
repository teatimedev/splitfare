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
         "price_str": "from €50", "djs": ["Solomun"]}]}
    html = render_dashboard(results, events, {"origins": ["BFS"],
                            "destination": ["IBZ"], "adults": 2},
                            "Mon 20 Jul, 15:00", dead=[])
    assert "£214" in html and "Solomun" in html
    assert "day-2026-08-16" in html          # detail screen exists
    assert "BFS" in html and "MAN" in html
    assert nice_date("2026-08-16") in html


def test_currency_symbol_threading():
    import splitfare
    splitfare.set_currency("EUR")
    try:
        assert splitfare.SYM == "€"
        out = Option([leg()], "direct")
        results = [{"total": 214, "od": "2026-08-16", "bd": "2026-08-17",
                    "outs": [out], "backs": [out]}]
        html = render_dashboard(results, {}, {"origins": ["BER"],
                                "destination": ["LIS"], "adults": 2},
                                "Mon 20 Jul, 15:00", dead=[])
        assert "€214" in html and "£" not in html
    finally:
        splitfare.set_currency("GBP")


def test_gf_link_respects_currency():
    assert "curr=EUR" in gf_link("BER", "LIS", "2026-09-04", 2, "EUR")


def test_hhmm_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        hhmm_to_min("2pm")
    with pytest.raises(ValueError):
        hhmm_to_min("25:99")


def test_ticketmaster_parser():
    import json
    from providers import parse_ticketmaster
    fixture = json.dumps({"_embedded": {"events": [{
        "name": "Techno Night",
        "dates": {"start": {"localTime": "23:00:00"}},
        "priceRanges": [{"min": 15.5, "currency": "EUR"}],
        "_embedded": {"venues": [{"name": "Warehouse X"}],
                       "attractions": [{"name": "DJ A"}, {"name": "DJ B"}]},
    }]}})
    evs = parse_ticketmaster(fixture)
    assert evs == [{"title": "Techno Night", "venue": "Warehouse X",
                    "time": "23:00", "price_str": "from EUR 15.5",
                    "djs": ["DJ A", "DJ B"]}]
    assert parse_ticketmaster("not json") == []


def test_skiddle_parser():
    import json
    from providers import parse_skiddle
    fixture = json.dumps({"results": [{
        "eventname": "Warehouse Project",
        "venue": {"name": "Depot Mayfield"},
        "openingtimes": {"doorsopen": "21:00"},
        "entryprice": "£25",
        "artists": [{"name": "Four Tet"}],
    }]})
    evs = parse_skiddle(fixture)
    assert evs[0]["venue"] == "Depot Mayfield"
    assert evs[0]["price_str"] == "£25"
    assert parse_skiddle("{}") == []
