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
3. **Browser-driven adapter (built, in `sources.py` as `easyjet-browser`):**
   drives a real browser through easyJet's own search flow and reads the
   results page. The browser solves Akamai the way a user does (it IS a
   user), so this is the method that works where everything else is blocked.
   It uses the camofox bridge REST API (`EASYJET_BROWSER_URL`, default
   `http://localhost:9377`):
   - `POST /tabs/open {userId, url: <deeplink>}` — starts the booking search
   - poll `GET /tabs/{tabId}/snapshot` until fares or Access Denied appear
   - `POST /tabs/{tabId}/evaluate` → grab `document.documentElement.outerHTML`
   - `DELETE /tabs/{tabId}` — always clean up
   The deeplink format (produced by the live search pod):
   `https://www.easyjet.com/deeplink?dep=<origin>&dest=<dest>&dd=<date>&isOneWay=on&apax=<adults>&cpax=0&ipax=0&fare=Y&lang=en`
   with `*`-market codes for grouped origins (`*BE` = Belfast).
   Fares are extracted by `ej_extract_flights()` — known selectors first,
   decoded-text pattern scan as fallback (unit-tested against canned HTML).
   **IP caveat:** the booking app denies datacenter IPs (verified: the
   deeplink returns Access Denied from a VPS even in a real browser). From a
   residential IP the adapter returns fares; from a datacenter IP it returns
   `[]` and the per-source health stats flag it. Add `"easyjet-browser"` to
   `flight_sources` (e.g. on your home machine) alongside `google`/`ryanair`.

## What the booking app's fare API looks like (for future reference)

The booking app's real fare search (behind the same wall, but documented
from the JS bundles, 2026-07-31):

- Host comes from `FpsHostV2` in a secret called
  `Stream_<B2B_STREAM>_<B2B_CREDENTIALS_VERSION>` fetched from AWS Secrets
  Manager (the app bundles the AWS SDK and calls GetSecretValue at runtime).
- `POST https://<FpsHostV2>/comm/v2/flight-fare/fare-search/get`
  and `.../comm/v2/flight-fare/flight-availability/search`
- Headers: `Authorization: Bearer <OAuth token>` (client_credentials against
  the secret's `tokenURL` with embedded `ClientID`/`ClientSecret`/`scope`),
  `X-Client-Id: "easyjet Web"`, `X-POS-ID: "DigitalWeb"`,
  `X-Client-Transaction-Id: <uuid>`.
- No embedded AWS credentials in the bundles (no AKIA keys); the secrets
  are fetched per-session.

Last verified: 2026-07-31.
