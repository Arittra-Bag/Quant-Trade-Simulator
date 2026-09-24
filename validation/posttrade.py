"""
Post-trade scoring: replay the execution agent's plans against a recorded book and trade
tape, and compare what each plan was predicted to cost with what the recording says it
would have cost.

The recording is the one validation/record.py writes: books and public trades, with
timestamps. At each sample time t (non-overlapping windows of `horizon` seconds) an order
of `notional` USD is worked three ways:

- **TWAP**: N equal slices at t, t + H/N, ...; each slice walks the recorded book of its
  moment, through the same paper executor the agent uses. Predicted: the same slices all
  walked against the book at t, which is `compare_schedule`'s assumption that the book
  refills fully between slices (`compare_schedule` itself also charges permanent impact,
  which a paper fill cannot observe, so it is shown beside rather than scored against).
  Realised twice: against each slice's own mid (liquidity only, which isolates the refill
  assumption) and against the arrival mid (implementation shortfall, adding whatever the
  price did meanwhile).
- **Passive limit**: rest the whole order at the touch for H seconds, behind the size
  already queued there. It fills as the tape trades through that price: fully if any trade
  prints beyond it, otherwise by the volume at the price in excess of the queue ahead. The
  remainder crosses the book at t + H. Predicted three ways: the queue model's maker share,
  `quote_order`'s Limit price, and the agent's paper fill (the expected-value fill).
- **One clip** at t, as the reference: its prediction is the walk itself.

What this can and cannot say. The tape is sampled every poll, so a burst of more than 100
trades between polls is partly missed; that undercounts volume at our price and makes the
passive fill rate a floor. The queue is assumed FIFO with no cancellations ahead of us,
which also errs toward filling less. Price drift between slices is noise in either
direction; the liquidity-only TWAP score removes it, the arrival score keeps it.

Usage:
    python -m validation.posttrade validation/data/okx.jsonl [--notional 250000,2500000] [--horizon 60] \\
        [--slices 4] [--report validation/POSTTRADE.md]
"""
import argparse
import bisect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor.tools import BookTools  # noqa: E402
from agent.execution import execute  # noqa: E402
from fee_model import calculate_fees  # noqa: E402
from models import book_stats, predict_maker_taker, walk_book  # noqa: E402
from validation.validate import _stats, load_records  # noqa: E402

VENUE = "OKX"


def _book_at(books, book_ts, ts):
    """The last recorded book at or before `ts`."""
    i = bisect.bisect_right(book_ts, ts) - 1
    return books[i] if i >= 0 else None


def _two_sided(book):
    return bool(book and book.get("bids") and book.get("asks"))


def _liquidity_bps(result, side):
    """A paper fill's cost against each slice's own mid, fees included: the liquidity alone."""
    sign = 1 if side == "buy" else -1
    fills = result["fills"]
    filled = sum(f["notional"] for f in fills)
    paid = sum(sign * (f["vwap"] - f["mid"]) / f["mid"] * f["notional"] for f in fills)
    return (paid + result["fees_usd"]) / filled * 1e4


def score_twap(books, book_ts, t0, side, notional, slices, horizon, fee_tier="Tier 1"):
    """One TWAP from t0, each slice against the recorded book of its moment."""
    arrival = _book_at(books, book_ts, t0)
    times = iter([t0 + k * horizon / slices for k in range(slices)])
    plan = {"strategy": "twap", "slices": slices, "expected_cost_bps": 0.0}
    order = {"side": side, "notional": notional, "fee_tier": fee_tier}
    result = execute(order, plan, arrival, book_source=lambda: _book_at(books, book_ts, next(times)))
    if result["status"] not in ("filled", "partial"):
        return None
    # The same slices all walked against the arrival book: what "the book refills fully
    # between slices" predicts, on the same footing as the realised fill.
    refilled = execute(order, plan, arrival)
    schedule = BookTools(arrival, fee_tier=fee_tier).compare_schedule(side, notional, slices)
    return {"ts": t0, "predicted_bps": _liquidity_bps(refilled, side), "schedule_bps": schedule["sliced_net_bps"],
            "liquidity_bps": _liquidity_bps(result, side), "arrival_bps": result["shortfall_bps"],
            "complete": not result["beyond_visible_book"]}


def score_passive(books, book_ts, trades, trade_ts, t0, side, notional, horizon, fee_tier="Tier 1"):
    """Rest at the touch for `horizon` seconds; fill from the tape, cross the rest at the end."""
    arrival = _book_at(books, book_ts, t0)
    end = _book_at(books, book_ts, t0 + horizon)
    if not (_two_sided(arrival) and _two_sided(end)):
        return None
    stats = book_stats(arrival)
    mid = stats["mid"]
    touch = stats["bid"] if side == "buy" else stats["ask"]
    level = (arrival["bids"] if side == "buy" else arrival["asks"])[0]
    queue_ahead = float(level[0]) * float(level[1])

    # Sellers hitting our bid (or buyers lifting our offer), at our price or through it.
    hitter = "sell" if side == "buy" else "buy"
    lo, hi = bisect.bisect_right(trade_ts, t0), bisect.bisect_right(trade_ts, t0 + horizon)
    at_price, through = 0.0, False
    for t in trades[lo:hi]:
        if t.get("side") != hitter:
            continue
        beyond = t["px"] < touch if side == "buy" else t["px"] > touch
        if beyond:
            through = True
            break
        if t["px"] == touch:
            at_price += t["px"] * t["sz"]
    fill = 1.0 if through else min(max((at_price - queue_ahead) / notional, 0.0), 1.0)

    rest = notional * (1 - fill)
    crossed = walk_book(end, rest, side) if rest > 0 else None
    base = notional * fill / touch + (crossed["filled_base"] if crossed else 0.0)
    vwap = notional / base
    sign = 1 if side == "buy" else -1
    fees = (calculate_fees(notional * fill, fee_tier, venue=VENUE, maker_fraction=1.0)
            + calculate_fees(rest, fee_tier, venue=VENUE))
    realised = sign * (vwap - mid) / mid * 1e4 + fees / notional * 1e4

    tools = BookTools(arrival, fee_tier=fee_tier)
    plan = {"strategy": "passive_limit", "expected_cost_bps": 0.0}
    paper = execute({"side": side, "notional": notional, "fee_tier": fee_tier}, plan, arrival)
    return {"ts": t0, "predicted_maker": predict_maker_taker(arrival, notional, "Limit"), "realised_fill": fill,
            "quoted_bps": tools.quote_order(side, notional, "Limit")["net_cost_bps"],
            "paper_bps": paper["shortfall_bps"], "realised_bps": realised}


def score(books, trades, notional=250_000.0, horizon=60.0, slices=4, sides=("buy", "sell")):
    """Every strategy at every non-overlapping window of the recording, both sides."""
    books = [b for b in books if _two_sided(b)]
    if not books:
        return {"windows": 0, "twap": [], "passive": [], "one_clip": []}
    book_ts = [b["ts"] for b in books]
    trade_ts = [t["ts"] for t in trades]
    out = {"twap": [], "passive": [], "one_clip": []}
    t0, last = book_ts[0], book_ts[-1] - horizon
    windows = 0
    while t0 <= last:
        windows += 1
        for side in sides:
            twap = score_twap(books, book_ts, t0, side, notional, slices, horizon)
            if twap:
                out["twap"].append({**twap, "side": side})
            passive = score_passive(books, book_ts, trades, trade_ts, t0, side, notional, horizon)
            if passive:
                out["passive"].append({**passive, "side": side})
            quote = BookTools(_book_at(books, book_ts, t0)).quote_order(side, notional)
            out["one_clip"].append({"ts": t0, "side": side, "predicted_bps": quote["net_cost_bps"],
                                    "complete": quote.get("complete", False)})
        t0 += horizon
    out["windows"] = windows
    return out


def summarise(scored):
    twap, passive = scored["twap"], scored["passive"]
    return {
        "windows": scored["windows"],
        "twap": {
            "n": len(twap),
            "predicted_bps": _stats([s["predicted_bps"] for s in twap]),
            "schedule_bps": _stats([s["schedule_bps"] for s in twap]),
            "liquidity_bps": _stats([s["liquidity_bps"] for s in twap]),
            "arrival_bps": _stats([s["arrival_bps"] for s in twap]),
            "refill_error_bps": _stats([s["liquidity_bps"] - s["predicted_bps"] for s in twap]),
        },
        "passive": {
            "n": len(passive),
            "predicted_maker": _stats([s["predicted_maker"] for s in passive]),
            "realised_fill": _stats([s["realised_fill"] for s in passive]),
            "quoted_bps": _stats([s["quoted_bps"] for s in passive]),
            "paper_bps": _stats([s["paper_bps"] for s in passive]),
            "realised_bps": _stats([s["realised_bps"] for s in passive]),
        },
        "one_clip": {"n": len(scored["one_clip"]),
                     "predicted_bps": _stats([s["predicted_bps"] for s in scored["one_clip"]])},
    }


def _row(label, st, unit="bps", digits=2):
    if not st:
        return f"| {label} | - | - | - | - |"
    f = f"{{:.{digits}f}}"
    return (f"| {label} | {st['n']} | {f.format(st['median'])} {unit} | "
            f"{f.format(st['p10'])} to {f.format(st['p90'])} | {f.format(st['mean'])} |")


def _section(summary, notional, horizon, slices):
    t, p = summary["twap"], summary["passive"]
    head = "| Measure | n | Median | p10 to p90 | Mean |\n| --- | ---: | ---: | ---: | ---: |"
    return [
        f"## ${notional:,.0f} per order",
        "",
        f"### TWAP x{slices} over {horizon:.0f} s",
        "",
        head,
        _row("Predicted, book refills fully between slices", t["predicted_bps"]),
        _row("`compare_schedule` (adds permanent impact)", t["schedule_bps"]),
        _row("Realised, liquidity only (vs each slice's mid)", t["liquidity_bps"]),
        _row("Realised, vs arrival mid (with drift)", t["arrival_bps"]),
        _row("Refill error (liquidity realised - predicted)", t["refill_error_bps"]),
        "",
        f"### Passive limit at the touch for {horizon:.0f} s, remainder crossed at the end",
        "",
        head,
        _row("Maker share, queue model", p["predicted_maker"], unit="", digits=3),
        _row("Filled from the tape", p["realised_fill"], unit="", digits=3),
        _row("Quoted (`quote_order` Limit)", p["quoted_bps"]),
        _row("Agent's paper fill (expected value)", p["paper_bps"]),
        _row("Realised from the tape", p["realised_bps"]),
        "",
        "### One clip, for reference",
        "",
        head,
        _row("Predicted (`quote_order` Market)", summary["one_clip"]["predicted_bps"]),
        "",
    ]


def format_report(sections, source, horizon, slices):
    """`sections` is a list of (notional, summary), one per order size."""
    windows = sections[0][1]["windows"] if sections else 0
    sizes = ", ".join(f"${n:,.0f}" for n, _ in sections)
    lines = [
        "# Post-trade scoring",
        "",
        f"Source: `{source}`. {windows} non-overlapping windows of {horizon:.0f} s, both sides, orders of "
        f"{sizes}. Generated by `python -m validation.posttrade`; the method, and what it cannot say, is in "
        "that module's docstring.",
        "",
        "A positive refill error means the book had not refilled between slices as the schedule assumes. "
        "The tape's passive fill is a floor: trades between polls can be missed, and the queue ahead is "
        "assumed never to cancel.",
        "",
    ]
    for notional, summary in sections:
        lines += _section(summary, notional, horizon, slices)
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Score the execution agent's plans against a recorded tape")
    p.add_argument("path")
    p.add_argument("--notional", default="250000",
                   help="USD per order; comma-separated for several sizes (default 250000)")
    p.add_argument("--horizon", type=float, default=60.0, help="seconds per plan (default 60)")
    p.add_argument("--slices", type=int, default=4)
    p.add_argument("--report", default="validation/POSTTRADE.md")
    a = p.parse_args(argv)
    books, trades = load_records(a.path)
    sizes = [float(n) for n in str(a.notional).split(",") if n.strip()]
    sections = [(n, summarise(score(books, trades, n, a.horizon, a.slices))) for n in sizes]
    report = format_report(sections, a.path, a.horizon, a.slices)
    if a.report:
        with open(a.report, "w") as fh:
            fh.write(report)
    print(report)
    return 0 if sections and sections[0][1]["windows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
