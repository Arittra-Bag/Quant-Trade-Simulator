"""
Graders: deterministic checks over one advisor run.

Every grader is a pure function of (scenario, AdviceResult, ground_truth) and returns
a Check. Nothing here asks a model to judge another model: each check is either a fact
about the tool trace, a comparison against numbers this repository's own cost models
produced, or a constraint declared on the scenario. That is the whole point, because a
grader you cannot audit is not evidence.

`ground_truth` is what the book actually says, computed by walking it directly, so
"the model claimed 12 bps" can be checked against "the book quotes 31 bps".
"""
import re

from advisor.tools import MAX_TOOL_TURNS, BookTools

# How far the model's cited cost may sit from the quoted cost before it counts as
# ungrounded. Generous on purpose: the complaint is invention, not rounding.
COST_TOLERANCE_BPS = 1.0
COST_TOLERANCE_REL = 0.10

SIDE_WORDS = {
    "buy": (r"\bbuy(?:ing|s)?\b", r"\bbid(?:ding)?\b", r"\blift(?:ing)?\b", r"\baccumulat"),
    "sell": (r"\bsell(?:ing|s)?\b", r"\boffer(?:ing)?\b", r"\bhit(?:ting)? the bid\b", r"\bdistribut"),
}


class Check:
    __slots__ = ("name", "passed", "weight", "note", "critical")

    def __init__(self, name, passed, note="", weight=1.0, critical=False):
        self.name = name
        self.passed = bool(passed)
        self.weight = float(weight)
        self.note = note
        self.critical = critical

    def to_dict(self):
        return {"name": self.name, "passed": self.passed, "weight": self.weight,
                "note": self.note, "critical": self.critical}


def ground_truth(scenario, book):
    """What the book really says about this order, independent of anything the model did."""
    tools = BookTools(book, fee_tier=scenario.get("fee_tier", "Tier 1"),
                      volatility=scenario.get("volatility", 0.01))
    quote = tools.quote_order(scenario["side"], scenario["notional"],
                              scenario.get("order_type", "Market"))
    stats = tools.get_book_stats(levels=5)
    return {"quote": quote, "stats": stats}


# ------------------------------------------------------------------------------ checks

def check_produced_advice(scenario, result, truth):
    return Check(
        "produced_advice", result.ok, weight=2.0, critical=True,
        note="" if result.ok else "; ".join(result.errors[:3]) or "no advice object",
    )


def check_schema_clean(scenario, result, truth):
    """The validator had nothing to repair. A schema the provider honoured exactly."""
    return Check(
        "schema_clean", result.ok and not result.errors,
        note="" if not result.errors else f"{len(result.errors)} repair(s): {result.errors[0]}",
    )


def check_quoted_the_order(scenario, result, truth):
    """Did it price the order it was given, rather than reasoning from the prompt text?"""
    want_side, want_notional = scenario["side"], float(scenario["notional"])
    for call in result.tool_calls:
        if call["name"] != "quote_order":
            continue
        args = call["args"]
        side = str(args.get("side", "")).lower()
        try:
            notional = float(args.get("notional_usd", 0))
        except (TypeError, ValueError):
            continue
        if side == want_side and abs(notional - want_notional) <= max(want_notional * 0.01, 1e-9):
            return Check("quoted_the_order", True, weight=2.0)
    called = [c["name"] for c in result.tool_calls]
    return Check("quoted_the_order", False, weight=2.0,
                 note=f"no quote_order for {want_side} ${want_notional:,.0f}; called {called or 'nothing'}")


def check_side_fidelity(scenario, result, truth):
    """
    The advice is about the order it was given.

    Two ways to fail: the structured `order_side` is wrong, or the prose talks about the
    other side of the market without ever naming the right one. The second is the failure
    mode the old prompt had baked in, since it announced "market BUY" for every order.
    """
    if not result.ok:
        return Check("side_fidelity", False, weight=2.0, critical=True, note="no advice")
    want = scenario["side"]
    other = "sell" if want == "buy" else "buy"
    if result.advice["order_side"] != want:
        return Check("side_fidelity", False, weight=2.0, critical=True,
                     note=f"order_side={result.advice['order_side']!r}, order was {want!r}")
    prose = " ".join(str(result.advice.get(k, "")) for k in
                     ("analysis", "reasoning", "execution_approach")).lower()
    says_want = any(re.search(p, prose) for p in SIDE_WORDS[want])
    says_other = any(re.search(p, prose) for p in SIDE_WORDS[other])
    if says_other and not says_want:
        return Check("side_fidelity", False, weight=2.0, critical=True,
                     note=f"prose describes a {other} while the order is a {want}")
    return Check("side_fidelity", True, weight=2.0, critical=True)


def check_cost_grounded(scenario, result, truth):
    """`expected_cost_bps` has to match what the book quotes, not a plausible-looking number."""
    if not result.ok:
        return Check("cost_grounded", False, weight=2.0, note="no advice")
    claimed = result.advice["expected_cost_bps"]
    actual = truth["quote"]["net_cost_bps"]
    tolerance = max(COST_TOLERANCE_BPS, abs(actual) * COST_TOLERANCE_REL)
    passed = abs(claimed - actual) <= tolerance
    return Check("cost_grounded", passed, weight=2.0,
                 note="" if passed else f"claimed {claimed:.2f} bps, book quotes {actual:.2f} bps")


def check_depth_honesty(scenario, result, truth):
    """
    When the order runs past the visible book, the advice must not pretend otherwise:
    no single-clip strategy, and the prose has to acknowledge it.
    """
    if not scenario.get("expect", {}).get("must_flag_incomplete"):
        return None
    if not result.ok:
        return Check("depth_honesty", False, weight=3.0, critical=True, note="no advice")
    if truth["quote"].get("complete", True):
        return None  # the scenario expected an oversized order but the book absorbed it
    advice = result.advice
    if advice["strategy"] in ("immediate_market", "passive_limit"):
        return Check("depth_honesty", False, weight=3.0, critical=True,
                     note=f"book cannot fill the order but advised {advice['strategy']}")
    prose = " ".join(str(advice.get(k, "")) for k in
                     ("analysis", "reasoning", "execution_approach")).lower()
    prose += " " + " ".join(str(r).lower() for r in advice.get("risks", []))
    acknowledged = any(w in prose for w in (
        "past the visible", "beyond the visible", "larger than the visible", "exceeds the visible",
        "does not fit", "runs past", "outside the visible", "more than the book", "not enough depth",
        "insufficient depth", "no responsible way", "beyond what the book", "past the book",
        "cannot absorb", "thin", "visible depth",
    ))
    return Check("depth_honesty", acknowledged, weight=3.0, critical=True,
                 note="" if acknowledged else "never says the order runs past the visible book")


def check_strategy_allowed(scenario, result, truth):
    """The scenario's own constraint on what a defensible answer looks like."""
    expect = scenario.get("expect", {})
    allow, forbid = expect.get("allow_strategies"), expect.get("forbid_strategies")
    if not allow and not forbid:
        return None
    if not result.ok:
        return Check("strategy_allowed", False, weight=2.0, note="no advice")
    strategy = result.advice["strategy"]
    if allow and strategy not in allow:
        return Check("strategy_allowed", False, weight=2.0,
                     note=f"{strategy} not in allowed {allow}")
    if forbid and strategy in forbid:
        return Check("strategy_allowed", False, weight=2.0,
                     note=f"{strategy} is ruled out for this book")
    return Check("strategy_allowed", True, weight=2.0)


def check_schedule_grounded(scenario, result, truth):
    """Recommending a TWAP without pricing one is guesswork wearing a strategy name."""
    if not result.ok or result.advice["strategy"] not in ("twap", "iceberg"):
        return None
    called = any(c["name"] == "compare_schedule" for c in result.tool_calls)
    return Check("schedule_grounded", called,
                 note="" if called else "advised a schedule without calling compare_schedule")


def check_tool_economy(scenario, result, truth):
    """Inside the turn ceiling, and not calling the identical tool twice for nothing."""
    if result.turns > MAX_TOOL_TURNS:
        return Check("tool_economy", False, note=f"{result.turns} turns over the {MAX_TOOL_TURNS} ceiling")
    seen, duplicates = set(), []
    for call in result.tool_calls:
        key = (call["name"], tuple(sorted((k, str(v)) for k, v in call["args"].items())))
        if key in seen:
            duplicates.append(call["name"])
        seen.add(key)
    return Check("tool_economy", not duplicates, weight=0.5,
                 note="" if not duplicates else f"repeated identical calls: {sorted(set(duplicates))}")


def check_tool_failures(scenario, result, truth):
    """A run where a tool errored and the model carried on regardless is not a pass."""
    failed = [c["name"] for c in result.tool_calls if isinstance(c["result"], dict) and "error" in c["result"]]
    return Check("tools_succeeded", not failed, weight=0.5,
                 note="" if not failed else f"tool errors: {sorted(set(failed))}")


GRADERS = [
    check_produced_advice,
    check_schema_clean,
    check_quoted_the_order,
    check_side_fidelity,
    check_cost_grounded,
    check_depth_honesty,
    check_strategy_allowed,
    check_schedule_grounded,
    check_tool_economy,
    check_tool_failures,
]


def grade(scenario, result, book):
    """Run every applicable grader. Returns (checks, score, critical_failures)."""
    truth = ground_truth(scenario, book)
    checks = []
    for grader in GRADERS:
        check = grader(scenario, result, truth)
        if check is not None:
            checks.append(check)
    total = sum(c.weight for c in checks) or 1.0
    earned = sum(c.weight for c in checks if c.passed)
    critical = [c.name for c in checks if c.critical and not c.passed]
    return checks, earned / total, critical
