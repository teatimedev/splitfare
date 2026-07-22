"""splitfare MCP server — plug flight-hunting into any MCP-capable AI agent.

Run:      splitfare-mcp            (stdio transport)
Claude:   claude mcp add splitfare -- /path/to/.venv/bin/splitfare-mcp

Exposes three read-only tools:
  splitfare_find_flights — price one out/back date pair in full detail
  splitfare_scan_month   — cheapest N-night windows across a month
  splitfare_whats_on     — destination event calendar

Prices are totals for the whole configured party. All tools return structured
JSON. Tool errors come back as messages with a suggested fix, never crashes.
"""

from __future__ import annotations

import re
from datetime import date as date_cls, timedelta
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP

import splitfare as sf

mcp = FastMCP(
    "splitfare_mcp",
    instructions=(
        "Cheap-flight hunter with split-ticket (self-transfer) routing and a "
        "destination event calendar. Prices are totals for the whole party in "
        "the configured currency. Split results are separate bookings with no "
        "missed-connection protection — always tell the user this. A month "
        "scan is slow on a cold cache (can take minutes); prefer "
        "splitfare_find_flights for specific dates. Route/airports/party size "
        "come from the user's splitfare config."
    ),
)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^\d{1,2}:\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,  # fetches live prices/events from the web
}


class ToolError(ValueError):
    pass


def _check(pattern: re.Pattern, value: str | None, what: str, example: str) -> None:
    if value is not None and not pattern.match(value):
        raise ToolError(f"invalid {what} '{value}' — expected format like {example}")


def _cfg(adults: int | None) -> dict:
    if not sf.CONFIG_PATH.exists():
        raise ToolError(
            f"no splitfare config at {sf.CONFIG_PATH} — the user needs to run "
            "'splitfare init' once (or set SPLITFARE_HOME to where their "
            "config.json lives)."
        )
    import json
    cfg = json.loads(sf.CONFIG_PATH.read_text())
    if adults:
        cfg["adults"] = adults
    return cfg


def _opt(o: sf.Option, adults: int) -> dict:
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


@mcp.tool(name="splitfare_find_flights", annotations=READ_ONLY)
def splitfare_find_flights(
    out_date: str,
    back_date: str,
    adults: int | None = None,
    arrive_by: str | None = None,
    home_by: str | None = None,
    top: int = 4,
) -> dict:
    """Price one out/back date pair (YYYY-MM-DD): direct fares plus split-ticket
    self-transfer combos via the configured hub airports, ranked by total price.

    arrive_by / home_by are optional HH:MM caps on the outbound / return
    arrival time. top limits distinct routings returned per direction.
    Takes ~1.5s per uncached route-date (up to ~1 min cold for many hubs)."""
    _check(_DATE, out_date, "out_date", "2026-08-16")
    _check(_DATE, back_date, "back_date", "2026-08-17")
    _check(_HHMM, arrive_by, "arrive_by", "14:00")
    _check(_HHMM, home_by, "home_by", "22:00")
    cfg = _cfg(adults)
    ab = sf.hhmm_to_min(arrive_by) if arrive_by else None
    hb = sf.hhmm_to_min(home_by) if home_by else None
    outs = sf.best_direction(cfg["origins"], cfg["destination"], out_date,
                             cfg["hubs"], cfg, False, arrive_by_min=ab)
    backs = sf.best_direction(cfg["origins"], cfg["destination"], back_date,
                              cfg["hubs"], cfg, False, reverse=True,
                              arrive_by_min=hb)
    a = cfg["adults"]
    return {
        "adults": a, "currency": cfg.get("currency", "GBP"),
        "outbound": [_opt(o, a) for o in sf.dedupe(outs, max(1, min(top, 10)))],
        "return": [_opt(b, a) for b in sf.dedupe(backs, max(1, min(top, 10)))],
        "cheapest_total": (outs[0].price + backs[0].price) if outs and backs else None,
        "note": ("split results are separate tickets — no protection if a leg "
                 "is delayed; empty lists mean no option fits the constraints"),
    }


@mcp.tool(name="splitfare_scan_month", annotations=READ_ONLY)
def splitfare_scan_month(
    month: str,
    nights: int = 1,
    out_dow: str | None = None,
    arrive_by: str | None = None,
    home_by: str | None = None,
    deep: int = 6,
    adults: int | None = None,
) -> dict:
    """Sweep a month (YYYY-MM) for the cheapest N-night windows.

    out_dow limits outbound weekdays (e.g. 'thu,fri,sat,sun'). deep = how many
    candidate windows get full split-ticket pricing (0 = all — thorough but
    SLOW on a cold cache, potentially 10+ minutes; 4-8 is a good default).
    Windows that miss the time caps are returned under 'excluded' with the
    reason and what relaxing the cap would cost."""
    _check(_MONTH, month, "month", "2026-08")
    _check(_HHMM, arrive_by, "arrive_by", "14:00")
    _check(_HHMM, home_by, "home_by", "22:00")
    if not 1 <= nights <= 14:
        raise ToolError("nights must be 1-14")
    cfg = _cfg(adults)
    args = SimpleNamespace(month=month, nights=nights, out_dow=out_dow,
                           arrive_by=arrive_by, home_by=home_by,
                           deep=max(0, deep), fresh=False)

    class _Quiet:
        def update(self, *a, **k):
            pass

    results, dead = sf.two_pass_scan(cfg, args, cfg["hubs"], _Quiet())
    a = cfg["adults"]
    return {
        "adults": a, "currency": cfg.get("currency", "GBP"),
        "windows": [
            {"out": r["od"], "back": r["bd"], "total": r["total"],
             "outbound": _opt(r["outs"][0], a), "return": _opt(r["backs"][0], a)}
            for r in results
        ],
        "excluded": [
            {"out": od, "back": bd, "reason": info.get("why", "no route")}
            for od, bd, info in dead
        ],
    }


@mcp.tool(name="splitfare_whats_on", annotations=READ_ONLY)
def splitfare_whats_on(date: str, days: int = 1) -> dict:
    """Destination event calendar via the user's configured events provider
    (e.g. Resident Advisor for their city) for a date, optionally up to 7
    consecutive days. Returns events with venue, lineup, times and, where the
    provider has them, door prices."""
    _check(_DATE, date, "date", "2026-08-16")
    if not 1 <= days <= 7:
        raise ToolError("days must be 1-7")
    _cfg(None)  # surface the config-missing message early
    d0 = date_cls.fromisoformat(date)
    return {
        (d0 + timedelta(days=i)).isoformat():
            sf.fetch_events((d0 + timedelta(days=i)).isoformat())
        for i in range(days)
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
