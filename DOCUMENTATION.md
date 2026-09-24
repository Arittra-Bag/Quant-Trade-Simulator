# Quant Trade Simulator Documentation

## Model Selection and Parameters

### Slippage Estimation Model
Slippage is measured by walking the live book, the way an execution desk estimates it:

```
fill the order notional level by level on the ask side (buy) or bid side (sell)
VWAP      = filled USD / filled base units
slippage  = |VWAP - mid| / mid * notional        (USD, always >= 0)
```

It includes the half-spread, which is what a market order actually pays. If the order is larger than the visible book, the remainder is priced by continuing the book at the average USD density of the levels it can see, and the result reports how much of the order that covered. When no book is available there is no estimate: the function returns zero rather than guessing.

### Market Impact Model
Only the permanent part is reported. The temporary part is already paid in the fill price, so
adding a separate impact term on top of the walk would count it twice:

```
end_bps    = displacement the walk pushes the price to, from the same book
impact     = PERMANENT_SHARE * end_bps / 1e4 * Q
```

`PERMANENT_SHARE` defaults to 0.4, a literature value, not a fitted one. The square-root law
is kept as an independent cross-check via `estimate_market_impact(..., model="sqrt")`, never as
a second charge. `validation/` fits the share against real price moves; see COST_MODEL.md.

### Maker/Taker Proportion Model
A market order always removes liquidity, so it is 0% maker. For a passive limit order at the touch the maker probability is `1 / (1 + Q / queue_usd)`, which falls as the order grows relative to the queue ahead of it.

### Fee Model
Each venue has its own maker and taker schedule, read from that venue's published perpetual
fee page and dated in `fee_model.py`. The rate charged is the two blended by the maker
probability, so a passive order is no longer billed as if it crossed the spread. The venue
comes from the book the feed delivered, and the UI shows which venue and tier produced the
number.

### Gemini AI Integration
The application integrates Google's Gemini AI to provide market analysis and trading strategy recommendations:
- Market sentiment analysis (Bullish, Bearish, or Neutral)
- Order imbalance interpretation
- Trading strategy recommendations based on orderbook data and transaction costs
- Execution approach suggestions

The Gemini integration:
- Uses the `google-genai` SDK with `gemini-3.8-flash` by default (override with `GEMINI_MODEL`)
- Falls back to `gemini-3.5-flash-lite` (override with `GEMINI_FALLBACK_MODELS`) if Google retires or restricts the default, so a model shutdown no longer breaks the panel
- Makes a single JSON-mode call per request
- Securely stores API credentials in environment variables
- Formats orderbook data into structured prompts
- Processes JSON responses for clean UI presentation

## Environment Configuration
The application uses environment variables for sensitive configuration:

1. **API Keys**
   - `GEMINI_API_KEY`: Required for Google Gemini AI integration
   - Stored in `.env` file (excluded from version control)
   - Loaded using python-dotenv

2. **Security Practices**
   - No hardcoded API keys in source code
   - .env files excluded from Git via .gitignore
   - Graceful handling when credentials are missing

To configure:
1. Create a `.env` file in the project root
2. Add `GEMINI_API_KEY=your_key_here`
3. The application will automatically load this configuration

## Fallback Regression

When no order book is available at all, `estimate_slippage` falls back to a `LinearRegression`
on `[order size, volatility]`, floored at zero.

**This is a placeholder, not a model.** It is fitted at import time on four hand-picked
points and was never trained on market data. It exists so the UI degrades to a finite number
instead of an exception while the feed is down. Every slippage figure you see with a live
feed comes from walking the book, not from here.

There is no logistic regression in the codebase. Maker/taker is the closed-form queue
expression documented above.

## Architecture and Latency

### Process layout

The feed client runs as a separate process from the Dash server. It writes the newest book to
`latest_orderbook.json` and its connection state to `feed_status.json`, each via a temporary
file and an atomic rename so the UI never reads a half-written book. While a feed runs, a
thread in the server takes in each new book (and feeds the volatility estimate) every 500 ms,
whether or not a browser is open. The browser polls the server at most twice a second, and
only once the previous poll has answered, so a slow link updates less often rather than
freezing; a hidden tab polls every 5 s. A poll whose book and order are unchanged returns only
the feed state and the ages. The decoupling means a slow or reconnecting venue cannot block
the UI, and the feed keeps running across page reloads.

One feed runs per server process and is shared by everyone who opens the page, so a public
deployment behaves as single-user.

### What is measured

The app times its own model pass (slippage, fees, impact and maker/taker for the current
order) on every tick, and the **Latency** panel reports p50 and p99 over a bounded rolling
buffer. That is the only latency figure the project actually measures, and it covers model
computation and render preparation, not the network path.

End-to-end latency is dominated by the poll cadence (at most two a second) and the network
round trip, by construction. The model pass is orders of magnitude smaller than either, which
is why the cadence, not the maths, is what you would shorten first.

### What is not measured

There is no offline benchmark harness in this repository: no recorded books, no replay, and
no committed latency or memory numbers. Wire-to-screen latency, exchange-to-client latency
and memory behaviour under long runs are all unmeasured. Any figure quoted for them would be
an invention, so none is quoted here.

## Known Gaps

Ranked roughly by how much each would change the numbers:

1. **No calibration against real fills.** `γ`, `η` and `T` are dimensionless defaults. Until
   estimates are compared against executed trades, the impact term is a shape, not a
   quantity.
2. **Volatility is a slider**, not an estimate from the tape, and it is unitless: it does
   not carry an annualisation or a horizon.
3. **Visible depth only.** `books5` is five levels. Orders past the visible book have the
   remainder priced at the last level and flagged, which understates the true cost.
4. **One fee schedule for all venues**, taker only, no maker rebates.
5. **The Gemini panel has no schema validation, no tool use and no evals**, and is not
   exercised by the test suite or CI.
6. **Polling, not push.** The UI pulls from disk on a fixed interval rather than being driven
   by book updates.
