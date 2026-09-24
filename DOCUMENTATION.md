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
- Uses the `google-genai` SDK with `gemini-3.5-flash-lite` by default (override with `GEMINI_MODEL`). On the live eval it scored 98.4% in 6.1 s a scenario, against 3.8 Flash's 97.5% in 16.1 s with over twice the tokens, and it also has the larger free-tier quota (15 a minute, 500 a day)
- Falls back to `gemini-3.8-flash` (override with `GEMINI_FALLBACK_MODELS`) if the default is retired, busy or out of quota
- Caps advisor requests at `GEMINI_DAILY_REQUESTS` (default 150) per UTC day, because the demo is public and the key is paid; past the cap the panel answers from the rules and says so. Only requests that reach Gemini count, and the count is kept in `advisor_usage.json` so a restarted worker does not start the day again
- That cap lives in the app, so a host that wipes its disk on redeploy resets it. The hard ceiling belongs on the key: in Google Cloud, lower the Generative Language API's requests-per-day quota for the key's project (APIs & Services, then the API's Quotas page), and add a billing budget alert
- A model that fails is rested rather than tried first on every call: until midnight Pacific for a spent daily quota, for Google's `retryDelay` (or 60 s) for a per-minute one, and 30 s when busy or timed out
- Each call times out after `GEMINI_CALL_TIMEOUT` (20 s) and a click after `GEMINI_DEADLINE` (40 s)
- When Gemini cannot answer, the panel shows the rules-based read with a one-line note saying why, not the raw API error
- Runs a short tool loop against the live book, then one schema-constrained call for the final JSON
- Securely stores API credentials in environment variables
- Formats orderbook data into structured prompts
- Processes JSON responses for clean UI presentation

### Execution Agent
`agent/graph.py` is a LangGraph `StateGraph`: plan, price (fanned out with `Send`), critic, approve (`interrupt()`), execute, with an `InMemorySaver` checkpointer holding a run between the Plan click and the Approve click.
- **Planner**: the Gemini advisor through `GeminiAnalyzer.analyze`, so it shares the daily cap, fallback and rules baseline. The first plan is paced like a Generate click; a revision is not, since it belongs to the same click, but it still counts against the daily cap. One Plan click is at most 3 advisor requests.
- **Critic** (`agent/critic.py`): deterministic, using the rules in `advisor/policy.py` that the graders also use. Blocking: wrong side, a cited cost matching neither the advised plan's price nor the one-clip price (within 1 bps or 10%), a single clip into a book that cannot fill it, advice that never admits the order runs past the visible book, and any strategy but `wait` for an order over 10x the visible depth on its side. Notes: an alternative cheaper by more than 1 bps and 25%, and that a sliced cost assumes the book refills.
- **Budget**: at most 2 revisions, and none once the run is 45 s old; a plan still blocked then reaches the approver marked unresolved.
- **Approval**: refused if more than 120 s after planning. The checkpointer keeps the last 32 runs; an older or restart-lost run reports that it expired.
- **Paper execution** (`agent/execution.py`): each child order walks the latest book from the feed. Slices go 0.25 s apart, not over the advised horizon. A passive limit is filled at its expected value: the queue model's maker share fills at the touch and the rest crosses. The reported cost is implementation shortfall against the arrival mid, fees included, in bps of the filled notional; permanent impact cannot be observed on a paper fill.
- **Known gap**: the paper fill for a resting limit ignores adverse selection, so it comes in 1.7 to 4.2 bps under what a recorded tape says the order would have cost; `quote_order`'s Limit price, which charges the spread, is the closer estimate (validation/POSTTRADE.md).

### Post-trade scoring
`python -m validation.posttrade <recording>` replays the agent's plans over a recording from `validation/record.py`, in non-overlapping windows (default: $250k, 60 s, 4 slices, both sides):
- **TWAP**: each slice walks the recorded book of its moment through the agent's paper executor. The prediction is the same slices walked against the arrival book, which is `compare_schedule`'s full-refill assumption; the difference, measured against each slice's own mid, is the refill error. The score against the arrival mid adds price drift.
- **Passive limit**: the order rests at the touch behind the queue already there, fills from sellers (for a buy) printing at that price or through it, beyond the queue ahead, and crosses the rest at the end. A print through the price counts for its own size: the recording is of a market that never held our order. It is compared with the queue model's maker share, `quote_order`'s Limit price and the agent's expected-value paper fill.
- The tape is polled, so a burst of more than 100 trades between polls is partly missed, and the queue ahead is assumed never to cancel; both make the realised passive fill a floor.

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
3. **Visible depth only.** The venues send 25 levels. Past them the walk continues the book at
   its own average density and says how much of the order that was, which understates the
   cost when a real book thins out faster.
4. **Fee schedules are a dated snapshot** of each venue's published ladder, not fetched live.
5. **The AI advisor is scored on one sample per scenario.** CI runs the evals offline against
   replay fixtures; the live models were run once each (evals/RESULTS.md).
6. **Polling, not push.** The browser asks for the next update when the last one has landed,
   rather than being driven by book updates.
7. **The agent's paper fill for a resting limit is optimistic.** It fills the queue model's
   maker share at the touch and ignores adverse selection; against a recorded tape it was 1.7
   to 4.2 bps cheaper than realised (validation/POSTTRADE.md). `quote_order`'s Limit price, which
   charges the spread, landed within 0 to 1.7 bps of the realised mean, so it stays as it is.
