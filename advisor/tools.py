"""
Tools the advisor can call against the live book.

The point of this module is that the model is not handed a prose summary of the
market and asked to reason about numbers it cannot check. It is handed an order and
a set of read-only functions, and it queries the book itself: it can quote the order
it was actually given, quote a hypothetical schedule, and look at depth beyond the
top of book before it commits to a strategy.

Every tool is a pure read over the book dict plus the project's own cost models, so a
tool call has no side effects and an eval can replay one deterministically.
"""
import json

from fee_model import calculate_fees
from models import (book_stats, estimate_market_impact, estimate_slippage,
                    predict_maker_taker, visible_depth_usd, walk_book)

MAX_TOOL_TURNS = 6  # hard ceiling on the agent loop, so a confused model cannot spin

# Google's function-calling schema dialect: OpenAPI subset, upper-case type names.
TOOL_DECLARATIONS = [
    {
        "name": "get_book_stats",
        "description": (
            "Top-of-book state: best bid/ask, mid, spread in absolute and bps, size "
            "imbalance over the first N levels, microprice, and total visible depth in USD. "
            "Call this first to see what kind of book you are trading into."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "levels": {
                    "type": "INTEGER",
                    "description": "How many levels per side to include in the imbalance. Default 5.",
                }
            },
        },
    },
    {
        "name": "quote_order",
        "description": (
            "Price a specific order against the current book by walking it level by level. "
            "Returns fees, slippage, market impact and net cost in USD and bps, the fill VWAP, "
            "how many levels it consumes, and complete=false when the order is larger than the "
            "visible book. Use this for the order you were given before recommending anything, "
            "and use it again to price alternatives."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "side": {"type": "STRING", "enum": ["buy", "sell"], "description": "Order side."},
                "notional_usd": {"type": "NUMBER", "description": "Order size in USD notional."},
                "order_type": {
                    "type": "STRING",
                    "enum": ["Market", "Limit"],
                    "description": "Market crosses the spread; Limit rests. Default Market.",
                },
            },
            "required": ["side", "notional_usd"],
        },
    },
    {
        "name": "get_depth_profile",
        "description": (
            "Cumulative USD available on one side of the book, level by level, with the price "
            "at each level and its distance from mid in bps. Use it to see where the liquidity "
            "actually sits and whether the order runs past it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "side": {"type": "STRING", "enum": ["buy", "sell"],
                         "description": "Side being taken: buy reads asks, sell reads bids."},
                "levels": {"type": "INTEGER", "description": "Levels to return. Default 10, max 50."},
            },
            "required": ["side"],
        },
    },
    {
        "name": "compare_schedule",
        "description": (
            "Compare executing the order in one clip against splitting it into N equal child "
            "orders. Returns the modelled net cost in bps for each and the saving. Assumes the "
            "book refreshes between slices, so it is an upper bound on the benefit of slicing, "
            "not a promise. Use it before recommending a TWAP."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "side": {"type": "STRING", "enum": ["buy", "sell"]},
                "notional_usd": {"type": "NUMBER", "description": "Total order size in USD notional."},
                "slices": {"type": "INTEGER", "description": "Number of equal child orders, 2 to 20."},
            },
            "required": ["side", "notional_usd", "slices"],
        },
    },
]


def _bps(usd, notional):
    return (usd / notional * 1e4) if notional else 0.0


class BookTools:
    """
    Bound set of tools over one book snapshot and one fee tier.

    `calls` records every dispatch in order, which is what the eval graders score and
    what the UI shows as the advisor's working.
    """

    def __init__(self, book, fee_tier="Tier 1", volatility=0.01):
        self.book = book
        self.fee_tier = fee_tier
        # Fees are per venue, so the advisor quotes the schedule of the venue actually
        # streaming rather than OKX's for everyone.
        self.venue = (book or {}).get("source") or "OKX"
        self.volatility = float(volatility)
        self.calls = []

    # --- individual tools -------------------------------------------------------------

    def get_book_stats(self, levels=5):
        levels = max(1, min(int(levels or 5), 50))
        stats = book_stats(self.book, levels=levels)
        return {
            "best_bid": round(stats["bid"], 8),
            "best_ask": round(stats["ask"], 8),
            "mid": round(stats["mid"], 8),
            "spread": round(stats["spread"], 8),
            "spread_bps": round(stats["spread_bps"], 4),
            "imbalance": round(stats["imbalance"], 4),
            "imbalance_note": "positive means more size resting on the bid",
            "microprice": round(stats["microprice"], 8),
            "visible_depth_usd": round(stats["depth_usd"], 2),
            "levels_used": levels,
            "bid_levels": len(self.book.get("bids") or []),
            "ask_levels": len(self.book.get("asks") or []),
        }

    def quote_order(self, side, notional_usd, order_type="Market"):
        side = "sell" if str(side).lower() == "sell" else "buy"
        notional = max(float(notional_usd), 0.0)
        order_type = "Limit" if str(order_type).lower() == "limit" else "Market"
        if notional <= 0:
            return {"error": "notional_usd must be positive"}

        fill = walk_book(self.book, notional, side)
        slippage = estimate_slippage(self.book, notional, self.volatility, side=side)
        fees = calculate_fees(notional, self.fee_tier, venue=self.venue)
        impact = estimate_market_impact(self.book, notional, self.volatility)
        maker = predict_maker_taker(self.book, notional, order_type)
        net = slippage + fees + impact
        stats = book_stats(self.book)

        out = {
            "side": side,
            "order_type": order_type,
            "notional_usd": round(notional, 2),
            "mid": round(stats["mid"], 8),
            "fees_usd": round(fees, 6),
            "slippage_usd": round(slippage, 6),
            "impact_usd": round(impact, 6),
            "net_cost_usd": round(net, 6),
            "fees_bps": round(_bps(fees, notional), 4),
            "slippage_bps": round(_bps(slippage, notional), 4),
            "impact_bps": round(_bps(impact, notional), 4),
            "net_cost_bps": round(_bps(net, notional), 4),
            "maker_probability": round(maker, 4),
        }
        if fill:
            out.update({
                "fill_vwap": round(fill["vwap"], 8),
                "levels_consumed": fill["levels"],
                "worst_price": round(fill["worst_price"], 8),
                "complete": bool(fill["complete"]),
            })
            if not fill["complete"]:
                out["warning"] = (
                    "Order is larger than the visible book. The remainder is priced at the last "
                    "visible level, so this net cost is a floor and the true cost is higher."
                )
        else:
            out["complete"] = False
            out["warning"] = "No book on that side; nothing could be walked."
        return out

    def get_depth_profile(self, side, levels=10):
        side = "sell" if str(side).lower() == "sell" else "buy"
        levels = max(1, min(int(levels or 10), 50))
        rows = (self.book.get("asks" if side == "buy" else "bids") or [])[:levels]
        if not rows:
            return {"error": f"no levels on the {'ask' if side == 'buy' else 'bid'} side"}
        stats = book_stats(self.book)
        mid = stats["mid"]
        profile, cumulative = [], 0.0
        for price, size in rows:
            price, size = float(price), float(size)
            cumulative += price * size
            profile.append({
                "price": round(price, 8),
                "size_base": round(size, 8),
                "level_usd": round(price * size, 2),
                "cumulative_usd": round(cumulative, 2),
                "distance_bps": round(abs(price - mid) / mid * 1e4, 4),
            })
        return {
            "side": side,
            "reading": "asks" if side == "buy" else "bids",
            "mid": round(mid, 8),
            "levels": profile,
            "total_visible_usd": round(cumulative, 2),
            "levels_available": len(self.book.get("asks" if side == "buy" else "bids") or []),
        }

    def compare_schedule(self, side, notional_usd, slices):
        side = "sell" if str(side).lower() == "sell" else "buy"
        notional = max(float(notional_usd), 0.0)
        slices = max(2, min(int(slices or 2), 20))
        if notional <= 0:
            return {"error": "notional_usd must be positive"}

        # Called directly rather than through dispatch: the nested quote is an
        # implementation detail of this tool, not a call the model made.
        one_shot = self.quote_order(side, notional)

        child = notional / slices
        child_slip = estimate_slippage(self.book, child, self.volatility, side=side)
        child_impact = estimate_market_impact(self.book, child, self.volatility)
        child_fees = calculate_fees(child, self.fee_tier, venue=self.venue)
        sliced_net = (child_slip + child_impact + child_fees) * slices

        return {
            "side": side,
            "notional_usd": round(notional, 2),
            "slices": slices,
            "child_notional_usd": round(child, 2),
            "one_shot_net_bps": one_shot["net_cost_bps"],
            "sliced_net_bps": round(_bps(sliced_net, notional), 4),
            "saving_bps": round(one_shot["net_cost_bps"] - _bps(sliced_net, notional), 4),
            "one_shot_complete": one_shot.get("complete", False),
            "assumption": (
                "Each child order is priced against the current book, i.e. full replenishment "
                "between slices. Real depth recovers partially, so treat saving_bps as an upper bound. "
                "Slicing also adds timing risk, which this model does not price."
            ),
        }

    # --- dispatch ---------------------------------------------------------------------

    def dispatch(self, name, args):
        """Run one tool by name. Never raises: a failed tool comes back as {'error': ...}."""
        args = dict(args or {})
        handler = getattr(self, name, None)
        if name not in {d["name"] for d in TOOL_DECLARATIONS} or handler is None:
            result = {"error": f"unknown tool {name!r}"}
        else:
            try:
                result = handler(**args)
            except TypeError as e:
                result = {"error": f"bad arguments for {name}: {e}"}
            except Exception as e:  # a tool must never take the panel down
                result = {"error": f"{name} failed: {e}"}
        self.calls.append({"name": name, "args": args, "result": result})
        return result

    def call_names(self):
        return [c["name"] for c in self.calls]

    def transcript(self):
        """The tool exchange as text, for logs and for transports without a native tool loop."""
        return "\n".join(
            f"{c['name']}({json.dumps(c['args'], sort_keys=True)}) -> {json.dumps(c['result'], sort_keys=True)}"
            for c in self.calls
        )
