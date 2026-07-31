# easyJet fare API — reverse-engineering notes (July 2026)

How to get fares out of easyJet programmatically, from live observation of
www.easyjet.com on 31 July 2026. Everything here was found by driving the
real site in a browser and watching its network calls.

## tl;dr

- The old community endpoint `POST /ejapi/fareQuote/flightShopping` is **dead**
  (404). Anything you find referencing it (most pre-2025 repos) is outdated.
- The **current** fare surface has two entry points, both live:
  1. `GET /homepage/api/availability` — a fare **calendar** for a whole date
     range (fires on the homepage's "cheapest month" widget).
  2. `GET /deeplink?dep=..&dest=..&dd=YYYY-MM-DD&isOneWay=on&apax=N...` — the
     booking-app entry point that leads to the per-flight fare search.
- **Both are behind Akamai bot protection.** curl gets 403, plain TLS
  impersonation (primp) gets a 429 `cpr_chlge` challenge, and even a
  cleared browser session only lets the site's *own page-load* requests
  through (they carry a per-page signed token). There is no unauthenticated
  public endpoint.

## Endpoint 1 — homepage fare calendar

Observed request (fired by the homepage widget on load):

```
GET https://www.easyjet.com/homepage/api/availability?origin=*BE&destination=LTN&currency=GBP&isReturn=true&originMarketGroup=Belfast&startDate=2026-07-31&endDate=2027-09-26&isWorldwide=false
```

Params:

| param | meaning |
|---|---|
| `origin` | IATA code, or a market-group code like `*BE` (Belfast). The `*` prefix = airport group |
| `destination` | IATA code (plain, e.g. `LTN`) |
| `currency` | ISO code (`GBP`) |
| `isReturn` | `true`/`false` |
| `originMarketGroup` | human market name (`Belfast`) |
| `startDate` / `endDate` | range for the fare calendar (the widget asks for ~14 months in one call!) |
| `isWorldwide` | `false` |

One call covers a whole range — a month of daily prices in a single request.
That makes it far better suited to splitfare's month sweeps than per-date
queries.

Known market-group codes seen live: `*BE` = Belfast. For other origins try
the plain IATA code first; the widget used `*`-prefixed codes for grouped
multi-airport cities.

## Endpoint 2 — booking deeplink

Observed URL produced by the homepage search pod:

```
https://www.easyjet.com/deeplink?dep=*BE&dest=LTN&dd=2026-09-02&isOneWay=on&apax=1&cpax=0&ipax=0&fare=Y&lang=en
```

`dep`/`dest` airport codes, `dd` = departure date, `apax/cpax/ipax` =
adults/children/infants, `fare=Y`. Loading this in a cleared browser enters
the booking app, which then runs its own fare search (the per-flight API
call). That call is behind the same Akamai wall, so it is only observable
inside a real browser session that solved the challenge.

## The Akamai wall (the actual problem)

Observed behavior from the VPS and from a browser:

| caller | result |
|---|---|
| `curl` (any headers) | 403 Access Denied (Akamai edge) |
| primp `impersonate=chrome_145` | 429 `{"cpr_chlge":"true","t":"..."}` challenge |
| browser page-load widget call | **200** (only this context works) |
| browser `fetch()` from console after load | 403 Access Denied |
| browser `fetch()` with `X-Requested-With`/`Accept` headers | 403 |

The widget call succeeds because it is made *during* page load with the
challenge-clearance cookies + a per-page signed token that the app's own JS
adds. Replaying the URL without that exact context fails. This is Akamai
Bot Manager's "Continue" flow, not a simple referer/UA gate.

## Working methods

1. **Residential browser session (works today, no code):** open the deeplink
   in a normal browser, read fares. Manual.
2. **Cookie injection (best effort):** export the session cookies
   (`_abck`, `ak_bmsc`, `bm_sz`, `bm_mi`, `datadome`-style tokens) from a
   real browser that solved the challenge, and send them with the request
   from a residential IP. Fragile — tokens rotate and are IP/UA-bound, but
   worth trying. `fetch_easyjet` in `sources.py` reads a cookies file when
   `EASYJET_COOKIES` points at one.
3. **Browser-driven adapter (robust):** drive a headless/stealth browser
   (e.g. the local camofox browser server on :9377) to load the deeplink,
   let it solve Akamai, and read fares from the page. This is the only
   method that works from a datacenter IP. splitfare doesn't ship this yet —
   the `sources` module interface (`(http, origin, dest, day, adults,
   currency) -> list[dict]`) is where it would plug in.

## Status in splitfare

`sources.py` `fetch_easyjet` targets the homepage availability endpoint
(endpoint 1 above) with a market-group map for Belfast (`*BE`) and IATA
fallback, plus optional `EASYJET_COOKIES` injection. From a datacenter IP it
will return empty (Akamai) — the per-source health stats will show it, and
the source is expected to light up from a residential IP or via the
browser-driven adapter.

Last verified: 2026-07-31.
