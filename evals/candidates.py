"""
Systems under test.

A candidate is a function(scenario) -> Transport. Two kinds live here.

`rules` is real: it is the deterministic baseline from advisor/advisor.py driving the
real tools against the real book, so its scores are measurements, not illustrations.

Everything else is a REPLAY candidate: a hand-authored script standing in for a way a
model can get this wrong. These are not captured API responses and no claim is made
about how any particular model behaves. Their job is to show that each grader actually
fires on the failure it is supposed to catch, so a green scoreboard means the harness
works rather than that the graders are asleep. `legacy_prose` is the informative one:
its script is the output contract of the pre-existing single-prompt path, read straight
off the old prompt, with no tools, no order side and no cost figure to cite.
"""
from advisor.advisor import ReplayTransport, RuleTransport


def rules(scenario):
    return RuleTransport(scenario["side"], scenario["notional"],
                         scenario.get("order_type", "Market"))


def legacy_prose(scenario):
    """
    The old path's contract: one prompt, no tools, six string fields, and the order
    described to the model as "market BUY" whatever the user actually selected.
    """
    return ReplayTransport([
        {"advice": {
            "sentiment": "Bullish",
            "analysis": ("Bids outweigh asks across the top ten levels and the spread is tight, "
                         "which points to short-term buying pressure."),
            "recommendation": "Execute promptly while liquidity is present.",
            "strategy": "Immediate market",
            "reasoning": "The book looks liquid enough to absorb the order without much slippage.",
            "execution_approach": "Send the buy as a single market order.",
        }},
    ], model="legacy-prose")


def ungrounded(scenario):
    """Looks at the top of book, then cites a cost figure it never quoted."""
    return ReplayTransport([
        {"tool_calls": [{"name": "get_book_stats", "args": {"levels": 5}}]},
        {"advice": {
            "sentiment": "Neutral",
            "strategy": "immediate_market",
            "order_side": scenario["side"],
            "urgency": "medium",
            "confidence": 0.8,
            "expected_cost_bps": 3.5,
            "slices": 1,
            "horizon_seconds": 0,
            "limit_price": 0,
            "analysis": "The spread is tight and there is size on both sides of the book.",
            "reasoning": "Costs look modest for an order of this size.",
            "execution_approach": f"Send the {scenario['side']} as one market order.",
            "risks": [],
        }},
    ], model="ungrounded")


def depth_blind(scenario):
    """Quotes the order correctly, then ignores complete=false and takes it in one clip."""
    return ReplayTransport([
        {"tool_calls": [
            {"name": "get_book_stats", "args": {"levels": 5}},
            {"name": "quote_order", "args": {"side": scenario["side"],
                                             "notional_usd": scenario["notional"],
                                             "order_type": scenario.get("order_type", "Market")}},
        ]},
        {"advice": _echo_quote(scenario, strategy="immediate_market")},
    ], model="depth-blind")


def schema_drift(scenario):
    """Right instincts, invalid object: free-text enum, out-of-range confidence, slices on a single clip."""
    advice = _echo_quote(scenario, strategy="Immediate Market")
    advice.update({"confidence": 1.8, "slices": 4, "urgency": "urgent", "risks": "none"})
    return ReplayTransport([
        {"tool_calls": [
            {"name": "get_book_stats", "args": {"levels": 5}},
            {"name": "quote_order", "args": {"side": scenario["side"],
                                             "notional_usd": scenario["notional"]}},
        ]},
        {"advice": advice},
    ], model="schema-drift")


def schedule_unpriced(scenario):
    """Recommends a TWAP without ever calling compare_schedule to price one."""
    advice = _echo_quote(scenario, strategy="twap")
    advice.update({"slices": 5, "horizon_seconds": 150, "urgency": "low"})
    return ReplayTransport([
        {"tool_calls": [
            {"name": "get_book_stats", "args": {"levels": 5}},
            {"name": "quote_order", "args": {"side": scenario["side"],
                                             "notional_usd": scenario["notional"]}},
        ]},
        {"advice": advice},
    ], model="schedule-unpriced")


def derails(scenario):
    """
    Loops on the same call, mangles an argument, and never commits. Stands in for a model
    that keeps calling tools until the turn ceiling instead of answering.
    """
    spin = {"tool_calls": [{"name": "get_book_stats", "args": {"levels": 5}}]}
    bad = {"tool_calls": [{"name": "quote_order", "args": {"side": scenario["side"],
                                                          "notional_usd": -1}}]}
    return ReplayTransport([spin, bad] * 6, model="derails")


def _echo_quote(scenario, strategy):
    """
    An advice body whose cost figure is filled in by the runner from the real quote, so
    these candidates fail on the thing under test rather than on an unrelated number.
    """
    side = scenario["side"]
    return {
        "sentiment": "Neutral",
        "strategy": strategy,
        "order_side": side,
        "urgency": "high",
        "confidence": 0.7,
        "expected_cost_bps": "__QUOTE__",
        "slices": 1,
        "horizon_seconds": 0,
        "limit_price": 0,
        "analysis": "The book shows size on both sides and the order prices out reasonably.",
        "reasoning": "Taking it now avoids exposure to the price moving.",
        "execution_approach": f"Send the {side} as one market order.",
        "risks": ["The current book may not hold."],
    }


CANDIDATES = {
    "rules": rules,
    "legacy_prose": legacy_prose,
    "ungrounded": ungrounded,
    "depth_blind": depth_blind,
    "schema_drift": schema_drift,
    "schedule_unpriced": schedule_unpriced,
    "derails": derails,
}

# Candidates whose scores are measurements of real code rather than of a written-out script.
REAL_CANDIDATES = {"rules"}

DESCRIPTIONS = {
    "rules": "Deterministic baseline: real tools, explicit thresholds, no model.",
    "legacy_prose": "The pre-existing single-prompt contract: no tools, no order side, no cost figure.",
    "ungrounded": "Cites a cost it never quoted.",
    "depth_blind": "Quotes the order, then ignores that it runs past the visible book.",
    "schema_drift": "Free-text enums and out-of-range numbers.",
    "schedule_unpriced": "Advises a TWAP without pricing the schedule.",
    "derails": "Loops on tools, mangles an argument, and never answers.",
}
