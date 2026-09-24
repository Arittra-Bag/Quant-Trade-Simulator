# Advisor eval results

Recorded on 2026-09-24 18:54 UTC, re-graded with the current graders by `python -m evals.runner --regrade evals/results.json --markdown evals/RESULTS.md` on 2026-09-24 19:39 UTC.
8 scenarios x 11 candidates. Scores are weighted pass rates over the graders in `evals/graders.py`.

## Scoreboard

| Candidate | Kind | Score | Scenarios without a critical failure | Mean latency | Tokens per run | Cost per run | What it is |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `rules` | measured | 100.0% | 8/8 | 0.1 ms | - | - | Deterministic baseline: real tools, explicit thresholds, no model. |
| `claude_haiku` | live model | 99.5% | 8/8 | 12.8 s | 8,322 | $0.0112 | Live `claude-haiku-4-5` through the tool-calling loop. |
| `gemini_lite` | live model | 98.4% | 7/8 | 6.1 s | 6,170 | GCP credit | Live `gemini-3.5-flash-lite` through the tool-calling loop. |
| `claude_sonnet` | live model | 98.4% | 8/8 | 11.1 s | 8,392 | $0.0119 | Live `claude-sonnet-5` at effort low through the tool-calling loop. |
| `gemini_flash` | live model | 97.5% | 7/8 | 16.1 s | 14,661 | GCP credit | Live `gemini-3.8-flash` through the tool-calling loop. |
| `depth_blind` | replay fixture | 87.5% | 5/8 | 0.1 ms | - | - | Quotes the order, then ignores that it runs past the visible book. |
| `schedule_unpriced` | replay fixture | 82.3% | 5/8 | 0.1 ms | - | - | Advises a TWAP without pricing the schedule. |
| `schema_drift` | replay fixture | 79.8% | 5/8 | 0.1 ms | - | - | Free-text enums and out-of-range numbers. |
| `ungrounded` | replay fixture | 56.7% | 5/8 | 0.0 ms | - | - | Cites a cost it never quoted. |
| `legacy_prose` | replay fixture | 45.2% | 4/8 | 0.0 ms | - | - | The pre-existing single-prompt contract: no tools, no order side, no cost figure. |
| `derails` | replay fixture | 0.0% | 0/8 | 0.0 ms | - | - | Loops on tools, mangles an argument, and never answers. |

## Which grader caught what

Counts are scenarios where the check failed, out of 8. A replay fixture is supposed to light up its own column.

| Candidate | `produced_advice` | `schema_clean` | `quoted_the_order` | `side_fidelity` | `cost_grounded` | `strategy_allowed` | `tool_economy` | `tools_succeeded` | `depth_honesty` | `schedule_grounded` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `rules` | . | . | . | . | . | . | . | . | . | . |
| `claude_haiku` | . | . | . | . | . | . | 1 | . | . | . |
| `gemini_lite` | . | . | . | 1 | . | . | . | . | . | . |
| `claude_sonnet` | . | . | . | . | . | 1 | . | . | . | . |
| `gemini_flash` | . | . | . | . | . | . | . | . | 1 | . |
| `depth_blind` | . | . | . | . | . | 3 | . | . | 3 | . |
| `schedule_unpriced` | . | . | . | . | . | 2 | . | . | 3 | 8 |
| `schema_drift` | . | 8 | . | . | . | 3 | . | . | 3 | . |
| `ungrounded` | . | . | 8 | . | 8 | 3 | . | . | 3 | . |
| `legacy_prose` | . | 8 | 8 | 2 | 8 | 3 | . | . | 3 | . |
| `derails` | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 3 | . |

## Per-scenario scores

| Scenario | `claude_haiku` | `claude_sonnet` | `depth_blind` | `derails` | `gemini_flash` | `gemini_lite` | `legacy_prose` | `rules` | `schedule_unpriced` | `schema_drift` | `ungrounded` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `deep_small_buy` | 100% | 100% | 100% | 0% | 100% | 100% | 58% | 100% | 77% | 92% | 67% |
| `deep_large_buy` | 100% | 100% | 100% | 0% | 100% | 100% | 58% | 100% | 92% | 92% | 67% |
| `thin_book_oversized_buy` | 100% | 100% | 67% | 0% | 100% | 100% | 33% | 100% | 75% | 60% | 40% |
| `wide_spread_small_buy` | 100% | 100% | 100% | 0% | 100% | 100% | 58% | 100% | 92% | 92% | 67% |
| `bid_heavy_buy` | 100% | 100% | 100% | 0% | 100% | 100% | 58% | 100% | 92% | 92% | 67% |
| `ask_heavy_sell` | 96% | 100% | 100% | 0% | 100% | 100% | 42% | 100% | 92% | 92% | 67% |
| `thin_book_oversized_sell` | 100% | 100% | 67% | 0% | 100% | 88% | 20% | 100% | 75% | 60% | 40% |
| `absurd_size_buy` | 100% | 88% | 67% | 0% | 80% | 100% | 33% | 100% | 62% | 60% | 40% |

## Scenarios

- **`deep_small_buy`** - buy $1,000 into `deep_tight`. Normal case: tiny order into a deep tight book. Anything other than taking it is overthinking.
- **`deep_large_buy`** - buy $750,000 into `deep_tight`. Real size into a deep book. Taking it in one clip or working it are both defensible; standing aside is not.
- **`thin_book_oversized_buy`** - buy $400,000 into `thin`. The order runs past the visible book. A one-clip market order is the wrong answer and the advice must say the book cannot show the fill.
- **`wide_spread_small_buy`** - buy $5,000 into `wide`. 28 bps spread on a small order: crossing it is almost all of the cost, so resting should be on the table.
- **`bid_heavy_buy`** - buy $25,000 into `bid_heavy`. Heavy resting bids. Tests that the sentiment read follows the imbalance rather than the order side.
- **`ask_heavy_sell`** - sell $25,000 into `ask_heavy`. A SELL order. The pre-existing prompt hardcoded 'market BUY', so this is the scenario that catches side drift.
- **`thin_book_oversized_sell`** - sell $400,000 into `thin`. Same oversize problem on the bid side, so a model cannot pass by assuming buy.
- **`absurd_size_buy`** - buy $50,000,000 into `shallow`. Orders of magnitude past anything resting. Standing aside is the only honest answer.

## What these numbers do and do not say

- `rules` is measured. It is the deterministic baseline in `advisor/advisor.py` running the real tools against the scenario books, so its score is a property of code in this repository.
- Read the baseline's score with the obvious caveat: the graders and the baseline were written together, so a perfect score is a consistency check, not an independent result. Its use is as a floor. A model that cannot beat a page of thresholds on this set is not adding anything, and a change that drops the baseline below its floor fails CI.
- Every other offline candidate is a **replay fixture**: a hand-written script standing in for one way a model can be wrong. They are not captured API responses and say nothing about how any model behaves. They exist so that each grader is shown firing on the failure it targets, which is the only way to know the graders are not vacuous.
- `legacy_prose` is the output contract of the single-prompt path this work replaced, read off the old prompt: six string fields, no tools, no order side, no cost figure. Its failures are structural consequences of that contract rather than a judgement about the model behind it.
- The live rows (`gemini_flash`, `gemini_lite`, `claude_haiku`, `claude_sonnet`) are one run of each scenario against the real API: a single sample per scenario, so a difference of a few points between models is noise, not a ranking. A scenario the provider failed (quota, overload, timeout) is counted as errored and left out of the score, because it measured the provider rather than the advisor.
- Tokens and cost are per run that reached the API, errored runs included, because those were billed too. Cost is Claude's list price from each response's token usage, prompt caching included. Gemini rows show tokens only; they were billed to Google Cloud credit.
- Live models are reported, not gated: CI never calls an API, and a model scoring below the baseline is a finding rather than a regression.
- The books are synthetic ladders from `evals/scenarios.py`, not venue captures. Drop recorded snapshots into `evals/books/` with the same shape and the scenarios use them instead.
