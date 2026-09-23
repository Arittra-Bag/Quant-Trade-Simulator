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
| **Slippage** | Walks the resting book level by level until the notional is filled, then `abs(VWAP - mid) / mid * notional`. Includes the half-spread, which is what a market order actually pays. |
| **Market impact** | Almgren-Chriss style: `permanent = γσx`, `temporary = ησ√(x/T)`, where `x = notional / visible depth`. The square-root term is the empirical impact law. |
| **Fees** | Tiered taker rate on notional, from OKX's VIP 0 to 2 schedule. |
| **Net cost** | Slippage + impact + fees, shown in USD and bps. |
| **Maker/taker** | A market order is 0% maker. A passive limit at the touch is `1 / (1 + Q / queue_usd)`, falling as the order grows against the queue ahead of it. |
| **Calculation latency** | Wall-clock per tick for the model pass and render prep, reported as p50 and p99. |

Books come from OKX (`books5`), Hyperliquid, Binance USD-M futures or Kraken, with automatic
fallback to the next venue when one is unreachable or accepts the connection but never sends
data. OKX swap sizes are converted from contracts to base units using the instrument's
contract value. The header shows which venue is actually live, and whether the feed is
connecting, stale or offline.

## Scope and limits

This is a cost *estimator*, not a validated execution model. Being specific about that:

- **The cost model has not been calibrated against real fills.** `γ = 0.1`, `η = 0.5`, `T = 1`
  are dimensionless defaults, not fitted parameters. Treat the impact number as a shape that
  responds correctly to size and volatility, not as a number you would size a trade on.
- **Volatility is an input, not an estimate.** It comes from a slider; nothing infers σ from
  the tape.
- **Depth is the visible top of book**, five levels on OKX `books5`. An order larger than the
  visible book has its remainder priced at the last visible level and is flagged *exceeds
  visible depth*. Real slippage on such an order would be worse than what is shown.
- **One fee table for every venue.** The OKX taker schedule is applied across the board, and
  maker rebates are not modelled.
- **The fallback slippage regression is a placeholder.** It is fitted on four hand-picked
  points and is only reachable when there is no book at all to walk.
- **The Gemini panel is not covered by the tests or CI.** There is no recorded fixture and no
  eval for it; it is optional, off by default, and needs a key you supply.
- **500 ms polling UI, one shared feed per server process.** This is pre-trade analysis, not
  an execution system, and a public deployment should be treated as single-user.

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                      # or ./start.sh; serves on $PORT (default 8050)
```

Open <http://localhost:8050>, pick a venue and press **Start stream**. Choose **Simulated**
if exchange feeds are blocked from where you are running it.

Optional, for the AI panel, create a `.env` (never committed):

```
GEMINI_API_KEY=your_key_here
# GEMINI_MODEL=gemini-3.8-flash                 # default
# GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite  # tried if the default is retired
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
| `models.py` | Walk-the-book slippage, market impact, maker/taker, book statistics |
| `fee_model.py` | Tiered fee model |
| `visualizations.py` | Depth, cost-stack and latency charts |
| `gemini_integration.py` | Optional Gemini read on the book |
| `export.py` | CSV and Excel export of the current book |
| `assets/theme.css` | Desk theme, served automatically by Dash |

`DOCUMENTATION.md` has the model derivations and the environment configuration in full.

## Notes

- Binance and OKX restrict some regions. The client falls through to the next venue rather
  than failing, and the header names whichever one is live.
- Dependencies are pinned so a redeploy cannot silently pull a breaking release.

## License

Apache 2.0. See [LICENSE](LICENSE).
