# Quant Trade Simulator

A pre-trade transaction cost simulator on live L2 orderbook data. Pick a venue and instrument, size an order, and see what it would cost right now: slippage from walking the actual book, market impact, fees, maker/taker split, and the fill's VWAP on the ladder and depth chart. Built with Python, Dash and Plotly.

## Features
- Live L2 books from OKX (`books5`), Hyperliquid, Binance USD-M futures or Kraken, with automatic fallback when a venue is unreachable or silent, plus a simulated feed for offline demos
- Sizes normalised to base units (OKX swap contracts are converted with the contract value)
- Walk-the-book slippage, volatility-scaled Almgren-Chriss impact, tiered fees, all shown in USD and bps
- Price ladder with depth bars and the levels your order would consume highlighted
- Honest feed status: live, connecting, stale or offline, with the last error
- Calculation latency with p50 / p99
- CSV and Excel export of the current book
- Optional Gemini read on the book and your order

## Setup
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optional, for the AI panel: create a `.env` file (never committed) with
```
GEMINI_API_KEY=your_key_here
# GEMINI_MODEL=gemini-3.8-flash              # default
# GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite  # tried if the default is retired
```

## Run
```bash
python app.py            # or ./start.sh; serves on $PORT (default 8050)
```
Open http://localhost:8050, choose a venue and press **Start stream**. Choose **Simulated** if exchange feeds are blocked where you run it.

The feed client also runs on its own:
```bash
python websocket_client.py --symbol BTC-USDT-SWAP --exchange OKX
```
It writes `latest_orderbook.json` and `feed_status.json`. `ORDERBOOK_WS_URL_<VENUE>` (for example `ORDERBOOK_WS_URL_OKX`) overrides a venue's endpoint.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
The tests are offline: they cover every venue parser, the cost models, the exports, and the client against local WebSocket servers (including falling back from a venue that accepts but never sends data).

## Main components
- `app.py`: Dash app, layout and callbacks
- `assets/theme.css`: the trading-desk theme (served automatically by Dash)
- `websocket_client.py`: multi-venue orderbook client
- `models.py`: slippage, market impact, maker/taker and book statistics
- `fee_model.py`: tiered fee model
- `visualizations.py`: depth, cost and latency charts
- `gemini_integration.py`: Gemini analysis
- `export.py`: CSV and Excel export

See `DOCUMENTATION.md` for the models and `PERFORMANCE_ANALYSIS.md` for performance notes.

## Notes
- One feed runs per server process and is shared by everyone who opens the page, so treat a public deployment as single-user.
- Binance and OKX restrict some regions; the client falls through to the next venue and the header shows which one is live.
