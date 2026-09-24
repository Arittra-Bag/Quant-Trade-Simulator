# Quant Trade Simulator

[![CI](https://github.com/Arittra-Bag/Quant-Trade-Simulator/actions/workflows/ci.yml/badge.svg)](https://github.com/Arittra-Bag/Quant-Trade-Simulator/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776ab)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Live demo](https://img.shields.io/badge/live-demo-2ea44f)](https://quant-trade-simulator.onrender.com)

**[Try it live](https://quant-trade-simulator.onrender.com)** (hosted on Render, so the first
request may take a few seconds to wake the instance).

**A pre-trade transaction cost simulator for crypto perpetuals.** Point it at a live L2
order book, size an order, and it tells you what that order would cost to execute right
now: slippage from walking the actual book, market impact, exchange fees, the maker/taker
split, and the VWAP your fill would land at, in USD and in basis points of notional.

![The desk: order ticket, cost tiles, depth chart and price ladder updating on a live feed](docs/demo.gif)

<sub>Recorded against the built-in simulated feed, which is what the app falls back to when
exchange WebSockets are unreachable. Live venues drive exactly the same path.</sub>

## What it measures

| Output | How it is computed |
| --- | --- |
| **Slippage** | Walks the resting book level by level until the notional is filled, then `abs(VWAP - mid) / mid * notional`. Includes the half-spread, which is what a market order actually pays. Anything past the last visible level is priced by continuing the book at its own average depth density, and the result says how much of the order that was. |
| **Market impact** | The permanent residue of that same walk: `PERMANENT_SHARE * end displacement * notional`. The temporary part is already in the fill price, so it is not charged twice. The square-root law is available as a cross-check, not as a second term. |
| **Fees** | Per-venue maker and taker schedules, blended by the maker probability. |
| **Net cost** | Spread + depth + fees + permanent impact, shown in USD and bps. Timing risk is reported beside it as a one-sigma band, not added in. |
| **Maker/taker** | A market order is 0% maker. A passive limit at the touch is `1 / (1 + Q / queue_usd)`, falling as the order grows against the queue ahead of it. |
| **Calculation latency** | Wall-clock per tick for the model pass and render prep, reported as p50 and p99. |

Books come from OKX (`books5`), Hyperliquid, Binance USD-M futures or Kraken, with automatic
fallback to the next venue when one is unreachable or accepts the connection but never sends
data. OKX swap sizes are converted from contracts to base units using the instrument's
contract value. The header shows which venue is actually live, and whether the feed is
connecting, stale or offline.

## Scope and limits

This is a cost *estimator*, not a validated execution model. Being specific about that:

- **The cost model has not been calibrated against real fills.** `PERMANENT_SHARE = 0.4` is a
  literature value, not a fitted one. `validation/` exists to fit it against the public tape,
  and the one recording taken so far could not: about 400k rests at BTC's touch, so no sample
  walked the book. Treat the impact number as a shape that responds correctly to size, not as
  a number you would size a trade on.
- **Liquidity past the visible book is an assumption.** Depth is the top of book, 25 levels on
  the venues used here. Beyond it the walk continues the book at the average density of the
  levels it can see, which is stated in the result rather than hidden; a real book that thins
  out faster would cost more.
- **Fee schedules are a dated snapshot**, read off each venue's own page rather than fetched
  live, so they drift as venues change their ladders.
- **The Gemini panel is not covered by the tests or CI.** There is no recorded fixture and no
  eval for it; it is optional, off by default, and needs a key you supply.
- **Polling UI, one shared feed per server.** This is pre-trade analysis, not an execution
  system, and a public deployment should be treated as single-user.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                      # dev server on $PORT (default 8050)
./start.sh                         # production: gunicorn, configured by gunicorn.conf.py
```

Open <http://localhost:8050>, pick a venue and press **Start stream**. Choose **Simulated**
if exchange feeds are blocked from where you are running it.

Optional, for the AI panel, create a `.env` (never committed):

```
GEMINI_API_KEY=your_key_here
# GEMINI_MODEL=gemini-3.8-flash                 # default; on the free tier use gemini-3.5-flash-lite
# GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite  # tried if the default is retired, busy or out of quota
# GEMINI_DAILY_REQUESTS=150                     # advisor requests per UTC day before the rules answer
```

The feed client also runs standalone, writing `latest_orderbook.json` and `feed_status.json`:

```bash
python websocket_client.py --symbol BTC-USDT-SWAP --exchange OKX
```

`ORDERBOOK_WS_URL_<VENUE>` (for example `ORDERBOOK_WS_URL_OKX`) overrides a venue's endpoint.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

26 tests, fully offline: every venue parser, the cost models, the CSV and Excel exports, and
the feed client driven against local WebSocket servers, including the case where a venue
accepts the connection but never sends a book, which must trigger fallback. CI runs the same
suite on Python 3.11 and 3.12 on every pull request and on every push to `main`, plus an
import check that the app loads with no feed and no API key.

## How it fits together

| File | Role |
| --- | --- |
| `app.py` | Dash app: layout, callbacks, feed supervision |
| `websocket_client.py` | Multi-venue L2 client, normalisation and venue fallback |
| `models.py` | Walk-the-book slippage, permanent impact, measured volatility, maker/taker, book statistics |
| `fee_model.py` | Per-venue maker and taker fee schedules |
| `visualizations.py` | Depth, cost-stack and latency charts |
| `gemini_integration.py` | Optional Gemini read on the book |
| `export.py` | CSV and Excel export of the current book |
| `validation/` | Records books and the public trade tape, and scores predicted cost against it |
| `assets/theme.css` | Desk theme, served automatically by Dash |

`DOCUMENTATION.md` has the model derivations and the environment configuration in full, and
`COST_MODEL.md` sets out which parts of a quote are measured and which are still assumptions.

## Running on a free instance

The live demo runs on a free web instance (a fraction of a CPU) and the free Gemini tier, so
the desk is built to stay responsive on both:

- **Self-paced polling.** The browser asks for the next update only once the last one has
  answered, at most twice a second, and every 5 s while the tab is hidden. A slow link
  updates less often instead of freezing.
- **Send only what changed.** A poll whose book and order are unchanged returns the feed
  state and the ages, not the ladder, tiles and charts again.
- **Cheap charts.** Figures are built as plain dicts rather than `go.Figure` objects, which
  took a poll from about 55 ms of server time to about 2 ms. The tests still validate every
  figure through plotly.
- **Compressed responses.** A live poll is about 3.5 KB on the wire instead of 22 KB, and the
  compressed JS bundles are cached, not rebuilt for every visitor.
- **Threaded worker.** Start, Stop and the advisor never wait behind the polls.
- **Advisor quota.** Generate is locked while a request is running and requests are spaced
  at least 5 s apart. The demo key is on the paid tier, so 3.8 Flash leads and Flash Lite is
  the fallback, and a daily cap (150 requests by default) keeps a public page from running up a bill.
  A model that fails is rested instead of being tried first on every call: until midnight
  Pacific for a spent daily quota, for Google's retry delay (or 60 s) for a per-minute quota,
  and 30 s when overloaded or timed out. A click has a 40 s budget. When Gemini cannot answer,
  the panel gives the rules-based read and says why, rather than an error.

## Notes

- Binance and OKX restrict some regions. The client falls through to the next venue rather
  than failing, and the header names whichever one is live.
- Dependencies are pinned so a redeploy cannot silently pull a breaking release.

## License

Apache 2.0. See [LICENSE](LICENSE).
