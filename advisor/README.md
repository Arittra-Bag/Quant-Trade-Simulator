# Execution advisor

The AI panel used to be one prompt. It was handed a prose summary of the book, told the
order was a `market BUY` whatever the user had actually selected, and asked for six
strings back. Nothing checked the shape of what came out and nothing measured whether it
was any good.

This package replaces that with three things a reviewer can check: a typed output
contract, tools the model runs against the real book, and an eval suite that scores the
whole path offline.

## Layout

| File | What it holds |
| --- | --- |
| `advisor/schema.py` | The output contract. `ADVICE_SCHEMA` goes to the provider as a response schema; `validate_advice` re-checks it locally and reports every repair. |
| `advisor/tools.py` | Four read-only tools over a book snapshot, and the dispatcher that records every call. |
| `advisor/advisor.py` | The bounded agent loop, plus the Gemini, replay and rules transports. |
| `../gemini_integration.py` | Thin adapter: picks a transport and flattens the result for the Dash callback. |
| `../evals/` | Scenarios, graders, candidates, runner, and the published results. |

## The output contract

The model returns one object, not prose:

```json
{
  "sentiment": "Neutral", "strategy": "twap", "order_side": "sell",
  "urgency": "low", "confidence": 0.6, "expected_cost_bps": 170.6,
  "slices": 4, "horizon_seconds": 120, "limit_price": 0,
  "analysis": "...", "reasoning": "...", "execution_approach": "...",
  "risks": ["..."]
}
```

`strategy` is a closed set (`immediate_market`, `passive_limit`, `twap`, `iceberg`,
`wait`) so the UI can style it and the graders can score it. `order_side` is an echo of
the order being sized, which is how side drift gets caught.

`validate_advice` is strict about what a trading UI would act on and forgiving about the
prose: an unknown strategy is fatal, but an out-of-range `confidence` is clamped and
reported rather than blanking the panel. A provider honouring a response schema is a
convention, not a guarantee, so the check runs locally either way.

## Tools

The model is not told what the book looks like. It looks.

| Tool | Returns |
| --- | --- |
| `get_book_stats` | Best bid/ask, mid, spread in bps, imbalance, microprice, visible depth. |
| `quote_order` | The given order walked level by level: fees, slippage, impact, net in USD and bps, fill VWAP, levels consumed, and `complete=false` when the order runs past the visible book. |
| `get_depth_profile` | Cumulative USD per level with distance from mid in bps. |
| `compare_schedule` | One clip against N equal children, with the modelled saving and the assumption that makes it an upper bound. |

Every tool is a pure read over the book dict plus the project's own cost models, so a
call has no side effects and replays deterministically. The loop is capped at
`MAX_TOOL_TURNS` and a tool that fails comes back as `{"error": ...}` rather than taking
the panel down.

The tool names the model actually called are surfaced in the UI, so the read is
auditable rather than a paragraph you either trust or don't.

## Transports

`GeminiTransport` is the live path. Gemini will not accept function declarations and a
structured response schema in the same request, so the loop runs with tools and free
text and then makes one separate schema-constrained call handed the tool transcript.
That is the `_finalise` step. It falls through `GEMINI_FALLBACK_MODELS` when a model ID
is retired, and remembers what worked. A busy model (503) or a spent quota (429) also
falls through, for that call only, with no retry.

`ReplayTransport` plays a fixed script, which is how the loop, the dispatch and the
validator are exercised in CI with no key and no network.

`RuleTransport` is a deterministic baseline: explicit thresholds, no model, driving the
same tools and emitting the same schema. It is the control the LLM has to beat, and it
is also what the panel falls back to when no API key is configured, so the deployed demo
says something useful instead of printing `Set GEMINI_API_KEY`.

## Evals

```bash
python -m evals.runner                                     # all candidates, offline
python -m evals.runner --candidates rules --scenario deep_small_buy
python -m evals.runner --markdown evals/RESULTS.md --json evals/results.json
python -m evals.runner --live                              # adds the real API, needs a key
```

A live run is built for the Gemini free tier (5 calls a minute per model). Calls are
spaced `GEMINI_CALL_INTERVAL` seconds apart (default 13), each call gives up after
`GEMINI_CALL_TIMEOUT` seconds (default 60), and no new scenario starts after
`GEMINI_RUN_BUDGET` seconds (default 900). Nothing is retried: a busy or rate-limited call
goes once to the fallback model, and if that fails the scenario is marked errored and left
out of the score, since it measured the provider rather than the advisor. One line prints
per scenario and `evals/results.json` is rewritten after each, so a run stopped early keeps
what finished. A full run is about 10 minutes; `--scenario <id>` runs one in about a minute.

Eight scenarios covering a deep tight book, real size, orders that run past the visible
book on both sides, a wide spread, an imbalanced book, a sell order, and a size nothing
can absorb. Ten graders, each a fact about the tool trace or a comparison against
numbers this repository's own cost models produced. No model grades another model.

The runner exits 1 when a measured candidate drops below `SCORE_FLOOR`, so it can gate a
merge. Published results are in [`../evals/RESULTS.md`](../evals/RESULTS.md), and
`tests/test_advisor.py` fails if that file goes stale.

## What has not been tested

**The live Gemini path has never run against the real API from this repository.** There
is no key in the test or CI environment. `GeminiTransport` is covered only by a fake
client that mimics the SDK's response shape, which proves the loop reads that shape and
handles the two-phase call, and proves nothing about the API's behaviour. Every offline
number comes from `RuleTransport` or a replay fixture.

The scenario books are synthetic ladders, not venue captures. Drop a recorded snapshot
into `evals/books/<name>.json` with the same shape and the scenarios use it instead.

The replay candidates other than `rules` are hand-written scripts standing in for ways a
model can be wrong. They exist to show each grader firing on the failure it targets. They
are not captured responses and say nothing about how any model behaves.
