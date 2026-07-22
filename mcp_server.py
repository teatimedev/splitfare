"""splitfare MCP server — plug flight-hunting into any MCP-capable AI agent.

Run:      splitfare-mcp            (stdio transport)
Claude:   claude mcp add splitfare -- splitfare-mcp

Exposes three tools: find_flights (one window, full detail), scan_month
(cheapest N-night windows across a month), whats_on (party calendar).
Prices are totals for the whole party in the configured currency.
"""

from __future__ import annotations

from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP

import splitfare as sf

mcp = FastMCP(
    "splitfare",
    instructions=(
        "Cheap-flight hunter with split-ticket (self-transfer) routing and an "
        "event calendar for the destination. Prices are totals for the whole "
        "party. Split results are separate bookings with no missed-connection "
        "protection — always mention this. A full month scan is slow on a cold "
        "cache (minutes); prefer find_flights for specific dates."
    ),
)


class _Quiet:
    def update(self, *a, **k):
        pass


def _cfg(adults: int | None) -> dict:
    args = SimpleNamespace(adults=adults)
    return sf.load_config(args)


def _opt(o: sf.Option, adults: int = 2) -> dict:
    return {
        "kind": o.kind,
        "price": o.price,
        "route": o.route,
        "connect_minutes": o.connect_min,
        "legs": [
            {"airline": l.airline, "from": l.origin, "to": l.dest, "date": l.date,
             "departs": l.dep_hm, "arrives": l.arr_hm, "price": l.price,
             "booking_search": sf.gf_link(l.origin, l.dest, l.date, adults)}
            for l in o.legs
        ],
    }


@mcp.tool()
def find_flights(
    out_date: str,
    back_date: str,
    adults: int | None = None,
    arrive_by: str | None = None,
    home_by: str | None = None,
    top: int = 4,
) -> dict:
    """Price one out/back date pair (YYYY-MM-DD): direct fares plus split-ticket
    self-transfer combos via hub airports. arrive_by/home_by are HH:MM caps on
    the outbound/return arrival."""
    cfg = _cfg(adults)
    ab = sf.hhmm_to_min(arrive_by) if arrive_by else None
    hb = sf.hhmm_to_min(home_by) if home_by else None
    outs = sf.best_direction(cfg["origins"], cfg["destination"], out_date,
                             cfg["hubs"], cfg, False, arrive_by_min=ab)
    backs = sf.best_direction(cfg["origins"], cfg["destination"], back_date,
                              cfg["hubs"], cfg, False, reverse=True, arrive_by_min=hb)
    return {
        "adults": cfg["adults"], "currency": cfg.get("currency", "GBP"),
        "outbound": [_opt(o, cfg["adults"]) for o in sf.dedupe(outs, top)],
        "return": [_opt(b, cfg["adults"]) for b in sf.dedupe(backs, top)],
        "cheapest_total": (outs[0].price + backs[0].price) if outs and backs else None,
        "note": "split results are separate tickets — no protection if a leg is delayed",
    }


@mcp.tool()
def scan_month(
    month: str,
    nights: int = 1,
    out_dow: str | None = None,
    arrive_by: str | None = None,
    home_by: str | None = None,
    deep: int = 6,
    adults: int | None = None,
) -> dict:
    """Sweep a month (YYYY-MM) for the cheapest N-night windows. out_dow limits
    outbound weekdays, e.g. 'thu,fri,sat,sun'. deep = windows fully priced with
    split-ticket routing (0 = all; slow on a cold cache)."""
    cfg = _cfg(adults)
    args = SimpleNamespace(month=month, nights=nights, out_dow=out_dow,
                           arrive_by=arrive_by, home_by=home_by, deep=deep,
                           fresh=False)
    results, dead = sf.two_pass_scan(cfg, args, cfg["hubs"], _Quiet())
    return {
        "adults": cfg["adults"], "currency": cfg.get("currency", "GBP"),
        "windows": [
            {"out": r["od"], "back": r["bd"], "total": r["total"],
             "outbound": _opt(r["outs"][0], cfg["adults"]),
             "return": _opt(r["backs"][0], cfg["adults"])}
            for r in results
        ],
        "excluded": [
            {"out": od, "back": bd, "reason": info.get("why", "no route")}
            for od, bd, info in dead
        ],
    }


@mcp.tool()
def whats_on(date: str, days: int = 1) -> dict:
    """Destination event calendar (configured provider) for a date, optionally
    several consecutive days."""
    from datetime import date as d_, timedelta
    d0 = d_.fromisoformat(date)
    return {
        (d0 + timedelta(days=i)).isoformat():
            sf.fetch_events((d0 + timedelta(days=i)).isoformat())
        for i in range(days)
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
