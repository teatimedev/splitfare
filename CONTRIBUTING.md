# Contributing

Small tool, small rules:

- **Events providers** are the easiest contribution — one function in
  `providers.py` returning `{title, venue, time, price_str, djs}` dicts, plus
  a parse-level test with a canned fixture (see `tests/test_core.py`).
- **Flight sources**: `fetch_ryanair` in `splitfare.py` is the pattern —
  return `Leg` objects; wire into `fetch_legs` behind a `flight_sources` name.
- Run `pytest tests/ -q` before a PR; tests must not touch the network.
- Keep scraping polite (rate limits, caching) and honest (label estimates).
