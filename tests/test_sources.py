"""Tests for the multi-source merge, price history/insight and watch logic.

Pure logic only — no network. Live source behaviour is exercised by the
CLI (fetch health stats warn per source if one breaks).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from splitfare import (  # noqa: E402
    Leg, _pct, load_watches, merge_legs, price_insight, price_key,
    render_dashboard, save_watches,
)


def leg(o="BFS", d="IBZ", date="2026-08-16", dep=400, arr=650, price=78,
        airline="Jet2", offset=0):
    return Leg(origin=o, dest=d, date=date, airline=airline,
               dep_min=dep, arr_min=arr, arr_day_offset=offset, price=price)


# ------------------------------------------------------------------ merge_legs

def test_merge_dedupes_same_flight_keeps_cheapest():
    merged = merge_legs(
        [leg(airline="easyJet", dep=400, arr=650, price=80)],
        [leg(airline="easyJet", dep=400, arr=650, price=70)],
    )
    assert len(merged) == 1
    assert merged[0].price == 70


def test_merge_keeps_distinct_flights_and_sorts():
    merged = merge_legs(
        [leg(airline="Jet2", dep=500, arr=730, price=90)],
        [leg(airline="easyJet", dep=400, arr=650, price=70)],
    )
    assert [l.dep_min for l in merged] == [400, 500]
    assert [l.price for l in merged] == [70, 90]


def test_merge_cheaper_api_fare_keeps_google_naming():
    # google (first list) wins the airline display name; a cheaper fare for
    # the same flight from an airline API updates the price
    merged = merge_legs(
        [leg(airline="Wizz Air", dep=400, arr=650, price=60)],
        [leg(airline="Wizz Air", dep=400, arr=650, price=55)],
    )
    assert len(merged) == 1
    assert merged[0].price == 55
    assert merged[0].airline == "Wizz Air"


def test_merge_empty_lists():
    assert merge_legs([], []) == []


# ------------------------------------------------------------ price insight

H = {
    "BFS-IBZ-2026-08-12-2pax-GBP": [
        (1, 120), (2, 110), (3, 130), (4, 95), (5, 100),
    ],
}


def test_price_key_format():
    assert price_key("BFS", "IBZ", "2026-08-12", 2, "GBP") == \
        "BFS-IBZ-2026-08-12-2pax-GBP"


def test_insight_needs_three_observations():
    thin = {"k": [(1, 100), (2, 110)]}
    assert price_insight("k", 100, thin) is None


def test_insight_verdicts():
    k = "BFS-IBZ-2026-08-12-2pax-GBP"
    assert price_insight(k, 90, H)["verdict"] == "good"
    assert price_insight(k, 105, H)["verdict"] == "fair"
    assert price_insight(k, 115, H)["verdict"] == "typical"
    assert price_insight(k, 140, H)["verdict"] == "pricey"


def test_insight_trend_falling_and_rising():
    k = "BFS-IBZ-2026-08-12-2pax-GBP"
    falling = {"k": [(1, 140), (2, 130), (3, 120), (4, 110)]}
    rising = {"k": [(1, 100), (2, 110), (3, 120), (4, 130)]}
    assert price_insight("k", 120, falling)["trend"] == "falling"
    assert price_insight("k", 120, rising)["trend"] == "rising"


def test_pct_bounds():
    assert _pct([100, 200, 300, 400], 0) == 100
    assert _pct([100, 200, 300, 400], 1.0) == 400


# ------------------------------------------------------------- watch storage

def test_watch_roundtrip(tmp_path, monkeypatch):
    import splitfare as sf
    monkeypatch.setattr(sf, "WATCH_FILE", tmp_path / "watches.json")
    w = [{"id": "abc123", "name": "sep", "month": "2026-09", "nights": 1,
          "max_total": 180, "last_alerts": {}}]
    save_watches(w)
    assert load_watches() == w
    assert load_watches()[0]["id"] == "abc123"


def test_watch_check_by_id_keeps_sibling_watches(tmp_path, monkeypatch):
    """Regression: `watch check --id X` used to save the FILTERED list back to
    disk, silently deleting every other watch when X fired a hit."""
    import argparse
    import splitfare as sf
    monkeypatch.setattr(sf, "WATCH_FILE", tmp_path / "watches.json")
    save_watches([
        {"id": "aaa", "name": "sep", "month": "2026-09", "nights": 1,
         "max_total": 180, "last_alerts": {}},
        {"id": "bbb", "name": "aug", "month": "2026-08", "nights": 1,
         "max_total": 200, "last_alerts": {}},
    ])

    o = leg(date="2026-09-02", dep=400, arr=650, price=60)
    b = leg(o="IBZ", d="BFS", date="2026-09-03", dep=800, arr=1000, price=82)
    hit = {"total": 142, "od": "2026-09-02", "bd": "2026-09-03",
           "outs": [__import__("splitfare").Option([o], "direct")],
           "backs": [__import__("splitfare").Option([b], "direct")]}

    def fake_scan(cfg, args, hubs, status):
        return [hit], []

    monkeypatch.setattr(sf, "two_pass_scan", fake_scan)
    monkeypatch.setattr(sf, "tg_send", lambda cfg, text: False)
    monkeypatch.setattr(sf, "console", type("Q", (), {
        "print": lambda *a, **k: None, "status": lambda *a, **k: type(
            "S", (), {"__enter__": lambda s: s, "__exit__": lambda *x: None})()})())

    sf.cmd_watch_check(argparse.Namespace(id="aaa", fresh=False))
    ids = {w["id"] for w in load_watches()}
    assert ids == {"aaa", "bbb"}, f"sibling watch was dropped: {ids}"
    assert load_watches()[0]["last_alerts"]["2026-09-02|2026-09-03"] == 142


# ---------------------------------------------------------------- dashboard

def _min_results():
    o = leg(date="2026-08-12", dep=400, arr=650, price=78)
    b = leg(o="IBZ", d="BFS", date="2026-08-13", dep=800, arr=1000, price=99)
    return [
        {"total": 177, "od": "2026-08-12", "bd": "2026-08-13",
         "outs": [__import__("splitfare").Option([o], "direct")],
         "backs": [__import__("splitfare").Option([b], "direct")]},
    ]


def test_dashboard_renders_with_history_sparkline():
    import splitfare as sf
    hist = {
        "BFS-IBZ-2026-08-12-2pax-GBP": [(1, 100), (2, 90), (3, 85), (4, 78)],
        "IBZ-BFS-2026-08-13-2pax-GBP": [(1, 120), (2, 110), (3, 100)],
    }
    page = render_dashboard(
        _min_results(), {},
        {"adults": 2, "currency": "GBP", "origins": ["BFS"],
         "destination": ["IBZ"]},
        "Thu 12 Aug, 10:00", hist=hist)
    assert "spark" in page                      # sparkline svg present
    assert "vs typical" in page                 # buy/wait badge present
    assert "polyline" in page
