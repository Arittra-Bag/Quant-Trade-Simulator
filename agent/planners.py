"""
Planners: the advisor, adapted to the graph's `planner(order, book, feedback)` call.

`transport_planner` drives `run_advisor` with any transport, which is what the tests and
the evals use. `analyzer_planner` goes through the app's GeminiAnalyzer, so the agent
gets the same daily cap, model fallback and rules baseline as the Generate button.
"""
from advisor.advisor import run_advisor
from advisor.schema import ADVICE_SCHEMA

ADVICE_KEYS = tuple(ADVICE_SCHEMA["properties"])


def transport_planner(make_transport):
    """`make_transport(order, round_feedback)` returns a fresh transport for each plan call."""

    def plan(order, book, feedback):
        result = run_advisor(make_transport(order, feedback), book, order["side"], order["notional"],
                             order_type=order.get("order_type", "Market"), fee_tier=order.get("fee_tier", "Tier 1"),
                             volatility=order.get("volatility", 0.01), feedback=feedback)
        return {"advice": result.advice, "model": result.model or result.transport,
                "tool_calls": [c["name"] for c in result.tool_calls], "errors": result.errors}

    return plan


def analyzer_planner(analyzer):
    """The GeminiAnalyzer as a planner. A revision is not paced: it belongs to the same click."""

    def plan(order, book, feedback):
        out = analyzer.analyze(book, order["notional"], side=order["side"], order_type=order.get("order_type", "Market"),
                               fee_tier=order.get("fee_tier", "Tier 1"), volatility=order.get("volatility", 0.01),
                               feedback=feedback, paced=not feedback)
        if not out.get("success"):
            return {"advice": None, "model": "", "errors": [out.get("analysis", "no advice")]}
        advice = {k: out[k] for k in ADVICE_KEYS if k in out}
        advice["strategy"] = out["strategy_key"]  # `strategy` is the display label by now
        return {"advice": advice, "model": out.get("model", ""), "source": out.get("source", ""),
                "notice": out.get("notice", ""), "tool_calls": out.get("tool_calls", [])}

    return plan
