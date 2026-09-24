"""
Paper execution: work an approved plan against the book as it is when each child order
goes, and report what it cost against the mid the plan was made at.

No order leaves the process. Each child is filled by walking the latest book the feed
has (the same `walk_book` the cost model uses), so a TWAP's later slices see the book as
it has moved since the plan. Two simplifications, stated rather than hidden:

- The horizon is compressed. Slices go `pace_s` apart (a fraction of a second in the
  app), not over the advised horizon, so a demo does not take ten minutes.
- A passive limit is filled at its expected value: the maker share the queue model gives
  rests and fills at the touch, the rest crosses the book.

`shortfall_bps` is the implementation shortfall: the fill against the arrival mid, plus
fees, in bps of the filled notional. It is directly comparable to the plan's quoted cost.
"""
import time

from fee_model import calculate_fees
from models import book_stats, predict_maker_taker, walk_book


def _child(book, side, notional, strategy, fee_tier, venue):
    """Fill one child order against `book`. Returns the fill, or None when the book is empty."""
    stats = book_stats(book)
    if strategy == "passive_limit":
        maker = predict_maker_taker(book, notional, "Limit")
        touch = stats["bid"] if side == "buy" else stats["ask"]
        crossed = walk_book(book, notional * (1 - maker), side) if maker < 1 else None
        base = notional * maker / touch + (crossed["filled_base"] if crossed else 0.0)
        return {"notional": notional, "base": base, "vwap": notional / base if base else touch,
                "maker_share": maker, "mid": stats["mid"], "complete": crossed["complete"] if crossed else True,
                "fees": calculate_fees(notional, fee_tier, venue=venue, maker_fraction=maker)}
    fill = walk_book(book, notional, side)
    if fill is None:
        return None
    return {"notional": notional, "base": fill["filled_base"], "vwap": fill["vwap"], "maker_share": 0.0,
            "mid": stats["mid"], "complete": fill["complete"],
            "fees": calculate_fees(notional, fee_tier, venue=venue)}


def execute(order, plan, arrival_book, book_source=None, pace_s=0.0, sleep=time.sleep):
    """
    Work `plan` (the approved advice) for `order` and return the fills and the shortfall.

    `book_source()` returns the latest book, or None to reuse the last one; without it every
    child fills against `arrival_book`.
    """
    side, notional = order["side"], float(order["notional"])
    strategy = plan["strategy"]
    arrival_mid = book_stats(arrival_book)["mid"]
    if strategy == "wait":
        return {"status": "no_trade", "arrival_mid": arrival_mid, "fills": [],
                "note": "The plan was to stand aside, so nothing was sent."}

    slices = max(int(plan.get("slices") or 1), 1) if strategy in ("twap", "iceberg") else 1
    venue = (arrival_book or {}).get("source") or "OKX"
    fee_tier = order.get("fee_tier", "Tier 1")
    fills, book = [], arrival_book
    for i in range(slices):
        if i and pace_s:
            sleep(pace_s)
        book = (book_source() if book_source else None) or book
        fill = _child(book, side, notional / slices, strategy, fee_tier, venue)
        if fill is None:
            break
        fills.append({"slice": i + 1, **{k: round(v, 8) if isinstance(v, float) else v for k, v in fill.items()}})

    filled = sum(f["notional"] for f in fills)
    if not filled:
        return {"status": "failed", "arrival_mid": arrival_mid, "fills": [],
                "note": "The book was empty when the order went; nothing filled."}
    base = sum(f["base"] for f in fills)
    vwap = filled / base
    sign = 1 if side == "buy" else -1
    fees = sum(f["fees"] for f in fills)
    shortfall = sign * (vwap - arrival_mid) / arrival_mid * 1e4 + fees / filled * 1e4
    return {
        "status": "filled" if filled >= notional * (1 - 1e-9) else "partial",
        "strategy": strategy,
        "slices": len(fills),
        "filled_notional": round(filled, 2),
        "vwap": round(vwap, 8),
        "arrival_mid": round(arrival_mid, 8),
        "fees_usd": round(fees, 6),
        "shortfall_bps": round(shortfall, 4),
        "planned_bps": plan.get("expected_cost_bps"),
        "vs_plan_bps": round(shortfall - float(plan.get("expected_cost_bps") or 0.0), 4),
        "beyond_visible_book": any(not f["complete"] for f in fills),
        "fills": fills,
    }
