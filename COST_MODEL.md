# Cost model

What the simulator quotes for an order, where each number comes from, and which parts
are still assumptions. Anything in the assumed column is a candidate for the next
recording run, not a result.

## The breakdown

For a market order of `Q` USD on side `s`:

| component | how it is produced | measured or assumed |
| --- | --- | --- |
| spread | half-spread at the touch, `(touch - mid) / mid * Q` | measured |
| depth | walking the resting book past the touch, `(VWAP - touch) / mid * Q` | measured inside the visible book |
| residual depth | the part of `Q` past the last visible level, priced by extending the book at its own average density | **assumed** |
| fees | per-venue published schedule, maker and taker blended by the maker probability | published schedule, snapshot dated in `fee_model.py` |
| permanent impact | `PERMANENT_SHARE` of the displacement the same walk implies | **assumed** until validated |
| timing risk | one-sigma move over the execution horizon, from volatility measured on the feed | measured |

```
net = spread + depth + fees + permanent impact
```

Timing risk is reported beside the net, not inside it: it is the size of the move that
could go either way while the order works, not an expected cost.

## Two things this model used to get wrong

**The residual was free.** The old walk filled everything past the last visible level at
that level's price, so a 1M order into a 200k book reported a fraction of a bp of
slippage. The book we receive is 25 levels deep; a large order runs off the end of it,
and pretending the last price absorbs the rest is the single biggest way to flatter a
cost estimate. The walk now continues the book at the average USD density of the
levels it can see, reports how much of the order that covered (`residual_usd`) and says
so in `assumptions`. Extending at constant density is the conservative choice: the
square-root law implies depth grows faster than linearly with distance, so this errs
towards quoting a worse price rather than a better one.

**Slippage and impact double-counted.** Net cost added the book walk and an
Almgren-Chriss impact term, but the walk already contains the temporary impact of the
trade: the displacement you pay for is exactly the depth you consume. Worse, the impact
term measured participation against the visible book rather than daily volume, so its
`Q/D` ran into the thousands, which is where the 320 bps beside 0.25 bps came from.
Impact is now only the part that persists: a share of the displacement the same walk
implies, so the two numbers come from one view of the book instead of contradicting
each other. The square-root law is kept as an independent cross-check
(`estimate_market_impact(..., model="sqrt")`), not as a second charge.

Two smaller ones went with them: fees were one OKX-shaped taker table applied to every
venue with no maker rate at all, and slippage fell back to a linear regression fitted on
four invented points whenever there was no book. The fallback is gone — with no book to
walk there is no estimate, and the app says so.

## Volatility

Volatility is measured, not dialled in. `VolatilityTracker` takes the mid of every book
the feed delivers, keeps an exponentially weighted estimate of the per-second variance
of log returns and annualises it. The slider is the fallback used until enough of the
feed has arrived; `sigma_source` in the result says which of the two you are looking at.

## Validating it against real fills

Nothing above is calibrated to this venue's own fills yet, and the code says so in
`assumptions` rather than in a footnote. `validation/` closes that gap with public data:

```bash
python -m validation.record --symbol BTC-USDT-SWAP --minutes 30
python -m validation.validate validation/data/okx.jsonl --window 2.0
```

`record` writes books and the public trade tape to JSONL. `validate` pairs each book
with the trades that followed it, walks that book for exactly the notional the tape
traded, and reports the error between predicted and realised VWAP in bps, bucketed by
order size. It writes `validation/REPORT.md`.

Read the sign of the error, not just its size. The tape's notional arrives as a stream
of separate orders with the book refilling between them, so one simultaneous sweep of
the same notional should cost at least as much as the tape did: the model is expected to
sit slightly above the tape. A model sitting **below** the tape is under-costing, and
that is a real failure whatever its average error looks like.

The same run measures the permanent share directly: it fits the mid one window later
against the displacement the walk predicted, as a least-squares slope through the origin
with its standard error. On a quiet recording the report says the share cannot be told
apart from zero rather than handing you a number; when it can, put that number in
`QTS_PERMANENT_SHARE` and the impact term stops being a literature default.

## Knobs

| variable | default | what it does |
| --- | --- | --- |
| `QTS_PERMANENT_SHARE` | 0.4 | share of book displacement assumed permanent |
| `QTS_SQRT_LAW_Y` | 0.5 | coefficient of the square-root cross-check |
| `QTS_ADV_USD` | 5e8 | daily notional used by the cross-check when the asset is unknown |
