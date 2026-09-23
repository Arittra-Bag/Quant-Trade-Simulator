"""
Check the predicted cost of an order against what the market actually paid.

Method
------
For every recorded book at time t we take the public trades in (t, t+window] that hit
the same side, add up their notional Q and their realised VWAP, then walk the book we
had at t for exactly Q and compare. The difference is the model's error in bps.

What this can and cannot say. The tape's Q arrives as a stream of separate orders and
the book refills between them, so a single sweep of the same Q should cost at least as
much as the tape did: the model is expected to sit a little above the tape, and a model
sitting below it is under-costing and that is a real failure. Longer windows make the
replenishment bias worse, so the default window is short.

The same samples calibrate the permanent share of impact: we compare the mid price one
window later against the displacement the walk predicted, and the median ratio is the
share that actually persisted. That number, not a literature default, is what belongs
in PERMANENT_SHARE once a recording exists.

Usage:
    python -m validation.validate validation/data/okx.jsonl [--window 2.0] [--report validation/REPORT.md]
"""
import argparse
import bisect
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import walk_book  # noqa: E402

BUCKETS = [(0, 1e3), (1e3, 1e4), (1e4, 1e5), (1e5, 1e6), (1e6, float("inf"))]

# Below this much predicted displacement the trades never left the touch, so a fitted
# permanent share is dividing price noise by nothing and means nothing.
MIN_DISPLACEMENT_BPS = 0.5


def load_records(path):
    books, trades = [], []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            (books if rec.get("type") == "book" else trades).append(rec)
    books.sort(key=lambda r: r["ts"])
    trades.sort(key=lambda r: r["ts"])
    return books, trades


def _mid(book):
    return (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2


def _mid_after(books, book_ts, ts):
    i = bisect.bisect_left(book_ts, ts)
    return _mid(books[i]) if i < len(books) else None


def build_samples(books, trades, window_s=2.0, min_usd=500.0, side="buy"):
    """Pair each book with the trades that followed it on `side`."""
    samples = []
    trade_ts = [t["ts"] for t in trades]
    book_ts = [b["ts"] for b in books]
    for book in books:
        if not book.get("bids") or not book.get("asks"):
            continue
        t0 = book["ts"]
        lo = bisect.bisect_right(trade_ts, t0)
        hi = bisect.bisect_right(trade_ts, t0 + window_s)
        window = [t for t in trades[lo:hi] if t.get("side") == side]
        if not window:
            continue
        base = sum(t["sz"] for t in window)
        notional = sum(t["px"] * t["sz"] for t in window)
        if base <= 0 or notional < min_usd:
            continue
        fill = walk_book(book, notional, side)
        if not fill:
            continue
        mid = _mid(book)
        realised_vwap = notional / base
        sign = 1.0 if side == "buy" else -1.0
        realised_bps = sign * (realised_vwap - mid) / mid * 1e4
        mid_later = _mid_after(books, book_ts, t0 + window_s)
        samples.append({
            "ts": t0,
            "notional_usd": notional,
            "trades": len(window),
            "mid": mid,
            "realised_vwap": realised_vwap,
            "predicted_vwap": fill["vwap"],
            "realised_bps": realised_bps,
            "predicted_bps": fill["slippage_bps"],
            "error_bps": fill["slippage_bps"] - realised_bps,
            "predicted_end_bps": fill["end_bps"],
            "realised_perm_bps": (sign * (mid_later - mid) / mid * 1e4) if mid_later else None,
            "complete": fill["complete"],
            "levels": fill["levels"],
        })
    return samples


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    values.sort()
    return {
        "n": len(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p10": values[int(0.10 * (len(values) - 1))],
        "p90": values[int(0.90 * (len(values) - 1))],
    }


def evaluate(samples):
    errors = [s["error_bps"] for s in samples]
    result = {
        "n": len(samples),
        "error_bps": _stats(errors),
        "abs_error_bps": _stats([abs(e) for e in errors]),
        "predicted_bps": _stats([s["predicted_bps"] for s in samples]),
        "realised_bps": _stats([s["realised_bps"] for s in samples]),
        "under_costed_pct": 100 * sum(1 for e in errors if e < 0) / len(errors) if errors else None,
        "single_level_pct": 100 * sum(1 for s in samples if s["levels"] <= 1) / len(samples) if samples else None,
        "buckets": [],
        "permanent_share": None,
    }
    for lo, hi in BUCKETS:
        rows = [s for s in samples if lo <= s["notional_usd"] < hi]
        if rows:
            result["buckets"].append({
                "lo": lo, "hi": hi, "n": len(rows),
                "error_bps": _stats([s["error_bps"] for s in rows]),
                "predicted_bps": _stats([s["predicted_bps"] for s in rows]),
                "realised_bps": _stats([s["realised_bps"] for s in rows]),
            })
    result["permanent_share"] = fit_permanent_share(samples)
    return result


def fit_permanent_share(samples):
    """
    Fit the share of the predicted displacement that is still in the mid one window later.

    A least-squares slope through the origin, not a median of ratios: the ratios have
    tiny denominators and the mid moves for reasons that have nothing to do with the
    trade, so single samples are mostly noise. The standard error is reported with it,
    because on a quiet recording the honest answer is that the share cannot be told
    apart from zero.
    """
    pts = [(s["predicted_end_bps"], s["realised_perm_bps"]) for s in samples
           if s["realised_perm_bps"] is not None and s["predicted_end_bps"] > 1e-9]
    if len(pts) < 2:
        return None
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * y for x, y in pts)
    if sxx <= 0:
        return None
    slope = sxy / sxx
    resid = sum((y - slope * x) ** 2 for x, y in pts) / (len(pts) - 1)
    stderr = math.sqrt(resid / sxx)
    t = slope / stderr if stderr > 0 else float("inf")
    x_median = statistics.median([x for x, _ in pts])
    return {
        "n": len(pts),
        "slope": slope,
        "stderr": stderr,
        "t": t,
        "x_median_bps": x_median,
        "median_ratio": statistics.median([y / x for x, y in pts]),
        # A share above 1 means more displacement persisted than the trade caused, which
        # is the mid wandering rather than impact. Below 0 is the same thing with a sign.
        "usable": abs(t) >= 2 and 0.0 <= slope <= 1.0 and x_median >= MIN_DISPLACEMENT_BPS,
    }


def _fmt(st, unit="bps"):
    if not st:
        return "n/a"
    return f"{st['median']:+.2f} {unit} (p10 {st['p10']:+.2f}, p90 {st['p90']:+.2f}, n={st['n']})"


def format_report(result, source, window_s):
    if not result["n"]:
        return f"# Cost model validation\n\nNo usable samples in `{source}`.\n"
    lines = [
        "# Cost model validation",
        "",
        f"Source: `{source}` · window {window_s:.1f}s · {result['n']} samples",
        "",
        "Each sample walks the book we held at t for the notional the tape actually traded",
        "in the next window, and compares the two VWAPs. A positive error means the model",
        "quoted a worse price than the market paid, which is the expected direction: the",
        "tape's notional arrives as many orders with the book refilling between them.",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| predicted cost | {_fmt(result['predicted_bps'])} |",
        f"| realised cost | {_fmt(result['realised_bps'])} |",
        f"| error (predicted - realised) | {_fmt(result['error_bps'])} |",
        f"| absolute error | {_fmt(result['abs_error_bps'])} |",
        f"| samples where the model under-costed | {result['under_costed_pct']:.0f}% |",
        f"| samples filled inside the touch | {result['single_level_pct']:.0f}% |",
        "",
    ]
    if (result["single_level_pct"] or 0) > 90:
        lines += [
            "**This recording does not test the model.** Nearly every sample filled inside the",
            "best level, so the walk never left the touch and the depth extrapolation was never",
            "exercised. Record a thinner instrument or compare against larger sweeps before",
            "reading anything into the error column.",
            "",
        ]
    lines += [
        "## By order size",
        "",
        "| notional | n | predicted | realised | error |",
        "| --- | --- | --- | --- | --- |",
    ]
    for b in result["buckets"]:
        hi = "+" if b["hi"] == float("inf") else f"{b['hi']:,.0f}"
        lines.append(f"| {b['lo']:,.0f}-{hi} | {b['n']} | {b['predicted_bps']['median']:.2f} | "
                     f"{b['realised_bps']['median']:.2f} | {b['error_bps']['median']:+.2f} |")
    share = result["permanent_share"]
    lines += ["", "## Permanent impact", ""]
    if not share:
        lines.append("Not enough paired samples to measure the permanent share.")
    elif not share["usable"]:
        lines += [
            f"Fitted share: {share['slope']:.2f} ± {share['stderr']:.2f} (n={share['n']}, "
            f"median predicted displacement {share['x_median_bps']:.2f} bps).",
            "",
            "**Do not use this number.** " + (
                "The trades in this recording barely left the touch, so the fit is dividing "
                "price drift by a displacement of almost nothing."
                if share["x_median_bps"] < MIN_DISPLACEMENT_BPS else
                "The fit is inside two standard errors of zero." if abs(share["t"]) < 2 else
                "A share outside 0 to 1 means the mid wandered for reasons that have nothing "
                "to do with the trade."),
            "",
            "Leave PERMANENT_SHARE where it is and record a session whose orders actually",
            "walk the book: a thinner instrument, or a venue where the touch holds less.",
        ]
    else:
        lines += [
            f"Fitted share of the predicted displacement still in the mid one window later: "
            f"**{share['slope']:.2f} ± {share['stderr']:.2f}** (n={share['n']}, "
            f"median ratio {share['median_ratio']:.2f}).",
            "",
            "Set `QTS_PERMANENT_SHARE` to this value, or edit PERMANENT_SHARE in models.py,",
            "to replace the literature default with a number measured on this venue.",
        ]
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description="Validate the cost model against recorded market data")
    p.add_argument("path")
    p.add_argument("--window", type=float, default=2.0)
    p.add_argument("--side", default="buy", choices=["buy", "sell"])
    p.add_argument("--min-usd", type=float, default=500.0)
    p.add_argument("--report", default="validation/REPORT.md")
    a = p.parse_args()

    books, trades = load_records(a.path)
    samples = build_samples(books, trades, a.window, a.min_usd, a.side)
    result = evaluate(samples)
    report = format_report(result, a.path, a.window)
    os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
    with open(a.report, "w") as fh:
        fh.write(report)
    print(report)
    print(f"written to {a.report}")


if __name__ == "__main__":
    main()
