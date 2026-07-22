# splitfare 🎟️

**A flight-hacker's scanner for short trips: split tickets, trip-shape search,
and it knows what's on when you land.**

Mainstream flight engines answer *"how much are flights on these dates?"*
splitfare answers the question you actually have:

> *"Find me any 24-hour window in August — out Thursday to Sunday, landing
> before 2pm, home the same day — and tell me who's playing that night."*

It was born hunting a Belfast → Ibiza one-nighter, where the honest cheapest
answer turned out to be three separate budget-airline tickets glued together
(£234 for two, Swedish House Mafia and Carl Cox included) — a combination no
booking site will ever show you.

**[Live demo →](https://splitfare-rho.vercel.app)** *(a real scan: Belfast ⇄
Ibiza, every 24h window in August, with the club calendar per night)*

<p align="center">
  <img src="docs/days.png" width="30%" alt="Every window in the month, priced, with headliners per night">
  <img src="docs/day-detail.png" width="30%" alt="A day's detail: boarding-pass tickets, alternatives, the night's lineup">
  <img src="docs/nights.png" width="30%" alt="The party calendar, browsable by date">
</p>

## What it does

- **Split-ticket routing** — prices the direct fare *and* every viable
  self-transfer combo through your hub airports (same airport, minimum
  connection time, home the same day), ranked by total price for your whole
  party.
- **Trip-shape search** — sweep a whole month for the cheapest N-night window,
  constrained the way humans think: *out on these weekdays, land by this time,
  home by that time.*
- **Overnight positioning** — when your land-by time is impossible same-day,
  it prices the classic hack: fly to the hub the evening before, sleep, catch
  the morning flight.
- **Events fusion** — every candidate night comes with its party calendar
  (pluggable providers: Ibiza Spotlight, Resident Advisor for any city
  worldwide), so you compare *nights*, not just prices.
- **A phone dashboard** — one command renders a boarding-pass-styled static
  site (browse by day, tap into flights + alternatives + lineups) you can
  deploy anywhere static.
- **AI-agent native** — an MCP server (`splitfare-mcp`) exposes
  `find_flights` / `scan_month` / `whats_on` to any MCP-capable assistant,
  plus a `splitfare ask "…"` command that drives the tool via the Claude CLI.

## Quickstart

```bash
git clone https://github.com/teatimedev/splitfare && cd splitfare
python3 -m venv .venv && .venv/bin/pip install -e ".[mcp]"
.venv/bin/splitfare init          # your airports, party size, events provider

# price one out/back pair — direct + split combos + what's on
.venv/bin/splitfare window 2026-08-16 2026-08-17 --events

# sweep a month for the cheapest 1-night window with constraints
.venv/bin/splitfare scan --month 2026-08 --out-dow thu,fri,sat,sun --arrive-by 14:00

# render + deploy the phone dashboard (any static host; vercel shown)
.venv/bin/splitfare publish --month 2026-08 --arrive-by 14:00
npx vercel deploy site --prod

# plain English (uses your local `claude` CLI if you have Claude Code)
.venv/bin/splitfare ask "cheapest weekend in september for 2, landing before 2pm?"
```

First scan of a month is slow (~1.5s per route-date, politely rate-limited);
results cache for 6h and everything after is instant.

## Agent / MCP usage

```bash
claude mcp add splitfare -- /path/to/.venv/bin/splitfare-mcp
```

Then ask your assistant things like *"find us flights for the weekend of the
16th, landing before 2, and tell me who's on that night"* — it gets structured
data back (routings, per-leg booking links, connection gaps, event lineups)
and can reason about the trade-offs.

## Configuration

`splitfare init` writes `config.json`:

| key | meaning |
|---|---|
| `origins` / `destination` | airport-code lists (multiple codes = searched together) |
| `hubs` | self-transfer candidates between them |
| `adults` | party size — **all prices are totals for the party** |
| `min_connect_minutes` | self-transfer buffer (default 120 — these are separate tickets; bigger is safer) |
| `events.provider` | `ibiza-spotlight`, `resident-advisor` (+ `area_id`), or `none` |
| `events.tier1_venues` / `tier2_venues` | optional venue lists for headline/secondary billing |

`SPLITFARE_HOME` env var relocates config/cache/site if you pip-install
rather than run from a checkout.

## How it works, honestly

Flight data comes from **Google Flights** via
[fast-flights](https://github.com/AWeirdDev/flights) (protobuf query + a
pre-consented cookie to get past the EU consent wall). That means:

- **This can break whenever Google changes things.** It's a hacker tool you
  run yourself, not a service. Treat prices as estimates and book each leg
  directly with the airline — every result links to the matching search.
- **Split tickets carry real risk.** Separate bookings mean no
  missed-connection protection. The tool enforces a minimum gap and shows the
  gap on every result, but the risk is yours. Prefer long gaps; the tool's
  default posture (same airport, same day, 2h minimum) is deliberately
  conservative.
- Be polite: requests are rate-limited and cached. Don't hammer it.

Events come from public calendars (Ibiza Spotlight's day pages, Resident
Advisor's GraphQL). Add a provider for your scene in
[providers.py](providers.py) — it's ~30 lines.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -q
```

PRs welcome — provider plugins (Songkick? Bandsintown? ski-resort snow
reports?), new data sources, and better routing logic especially.

## License

MIT.
