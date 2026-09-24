"""
Pricing a plan against a book: the advised plan and the alternatives an approver would ask
about, each costed with the same tools the advisor has. Shared by the agent's graph, the
critic eval and the MCP server; it has no LangGraph dependency.
"""
from advisor.tools import BookTools

DEFAULT_SLICES = (4, 10)


def plans_to_price(advice):
    """The advised plan and the alternatives an approver would ask about."""
    strategy = advice["strategy"]
    slices = int(advice.get("slices") or 1)
    plans = [("immediate_market", 1), ("passive_limit", 1)] + [("twap", n) for n in DEFAULT_SLICES]
    advised = (strategy, slices if strategy in ("twap", "iceberg") else 1)
    if strategy != "wait" and advised not in plans:
        plans.append(advised)
    return [{"strategy": s, "slices": n, "advised": (s, n) == advised} for s, n in plans]


def price_plan(order, book, plan):
    """One plan's cost against the book, with the same tools the advisor has."""
    tools = BookTools(book, fee_tier=order.get("fee_tier", "Tier 1"), volatility=order.get("volatility", 0.01))
    side, notional = order["side"], order["notional"]
    if plan["strategy"] in ("twap", "iceberg"):  # an iceberg is priced as the equal clips it shows
        quote = tools.compare_schedule(side, notional, plan["slices"])
        name = "TWAP" if plan["strategy"] == "twap" else "iceberg"
        cost, complete, label = quote["sliced_net_bps"], quote["one_shot_complete"], f"{name} x{plan['slices']}"
    else:
        order_type = "Limit" if plan["strategy"] == "passive_limit" else "Market"
        quote = tools.quote_order(side, notional, order_type)
        cost, complete = quote["net_cost_bps"], quote.get("complete", False)
        label = "resting limit" if order_type == "Limit" else "one clip"
    return {**plan, "label": label, "cost_bps": round(cost, 4), "complete": bool(complete)}
