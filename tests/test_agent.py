"""
The execution agent: the graph's routing, the revision loop and its cap, the human
approval pause and resume, paper execution, and the critic's rules. Offline throughout:
the planner is scripted or the rules baseline, so nothing calls a model.
"""
import os
import sys

import pytest

pytest.importorskip("langgraph")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.graph as graph_module  # noqa: E402
from advisor.advisor import RuleTransport  # noqa: E402
from agent.critic import blocking, review  # noqa: E402
from agent.execution import execute  # noqa: E402
from agent.graph import MAX_REVISIONS, ExecutionAgent, plans_to_price, price_plan  # noqa: E402
from agent.planners import analyzer_planner, transport_planner  # noqa: E402
from evals.scenarios import get_scenario, scenario_book  # noqa: E402
from tests.test_advisor import _advice  # noqa: E402


def _case(scenario_id):
    scenario = get_scenario(scenario_id)
    order = {"side": scenario["side"], "notional": scenario["notional"], "fee_tier": "Tier 1", "volatility": 0.01}
    return order, scenario_book(scenario)


def _one_clip_bps(order, book):
    return price_plan(order, book, {"strategy": "immediate_market", "slices": 1})["cost_bps"]


class Scripted:
    """A planner that answers from a list, one per round, and records the feedback it was given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.feedback = []

    def __call__(self, order, book, feedback):
        self.feedback.append(feedback)
        advice = self.answers[min(len(self.feedback), len(self.answers)) - 1]
        return {"advice": advice, "model": "scripted"}


def _rules_agent(**kwargs):
    return ExecutionAgent(transport_planner(lambda o, f: RuleTransport(o["side"], o["notional"], "Market")), **kwargs)


# ------------------------------------------------------------------------------ the graph

def test_a_clean_plan_is_priced_in_parallel_then_waits_for_a_human():
    order, book = _case("deep_small_buy")
    _, view = _rules_agent().start(order, book)
    assert view["status"] == "awaiting_approval" and view["revisions"] == 0
    nodes = [t["node"] for t in view["trace"]]
    assert nodes[0] == "plan" and nodes[-1] == "critic" and nodes.count("price") == len(view["priced"]) == 4
    assert sum(p["advised"] for p in view["priced"]) == 1 and view["execution"] is None


def test_a_blocked_plan_is_revised_with_the_findings():
    order, book = _case("deep_small_buy")
    good = _advice(expected_cost_bps=_one_clip_bps(order, book))
    planner = Scripted(_advice(expected_cost_bps=40.0), good)
    _, view = ExecutionAgent(planner).start(order, book)
    assert view["status"] == "awaiting_approval" and view["revisions"] == 1
    assert planner.feedback[0] is None and "expected_cost_bps is 40.00" in planner.feedback[1][0]
    assert [r["round"] for r in view["rounds"]] == [1, 2] and blocking(view["rounds"][0]["findings"])
    assert not blocking(view["findings"]) and view["advice"]["expected_cost_bps"] == good["expected_cost_bps"]


def test_revisions_are_capped_and_the_human_sees_what_is_unresolved():
    order, book = _case("deep_small_buy")
    planner = Scripted(_advice(expected_cost_bps=40.0))
    _, view = ExecutionAgent(planner).start(order, book)
    assert len(planner.feedback) == MAX_REVISIONS + 1 and view["revisions"] == MAX_REVISIONS
    assert view["status"] == "awaiting_approval" and blocking(view["findings"])


def test_approval_resumes_the_saved_run_and_executes_once():
    order, book = _case("deep_small_buy")
    agent = _rules_agent()
    thread, _ = agent.start(order, book)
    view = agent.resume(thread, approved=True)
    assert view["status"] == "executed" and view["execution"]["status"] == "filled"
    assert [t["node"] for t in view["trace"]][-2:] == ["approve", "execute"]
    again = agent.resume(thread, approved=True)  # a double click
    assert [t["node"] for t in again["trace"]].count("execute") == 1


def test_a_run_out_of_time_goes_to_the_human_without_revising():
    order, book = _case("deep_small_buy")
    planner = Scripted(_advice(expected_cost_bps=40.0))
    _, view = ExecutionAgent(planner, deadline_s=-1).start(order, book)
    assert len(planner.feedback) == 1 and view["status"] == "awaiting_approval" and blocking(view["findings"])


def test_a_rejected_plan_is_not_executed():
    order, book = _case("deep_small_buy")
    agent = _rules_agent()
    thread, _ = agent.start(order, book)
    view = agent.resume(thread, approved=False)
    assert view["status"] == "rejected" and view["execution"] is None


def test_a_stale_approval_is_refused(monkeypatch):
    order, book = _case("deep_small_buy")
    agent = _rules_agent()
    thread, _ = agent.start(order, book)
    monkeypatch.setattr(graph_module, "APPROVAL_TTL_S", -1)
    view = agent.resume(thread, approved=True)
    assert view["status"] == "expired" and view["execution"] is None


def test_old_runs_are_dropped_and_report_expired():
    order, book = _case("deep_small_buy")
    agent = _rules_agent(max_runs=2)
    first, _ = agent.start(order, book)
    agent.start(order, book)
    agent.start(order, book)
    assert agent.view(first)["status"] == "expired"
    assert agent.resume(first, approved=True)["status"] == "expired"
    assert agent.view("never-started")["status"] == "expired"


def test_no_advice_ends_the_run_before_pricing():
    order, book = _case("deep_small_buy")
    _, view = ExecutionAgent(lambda o, b, f: {"advice": None, "model": "down"}).start(order, book)
    assert view["status"] == "failed" and [t["node"] for t in view["trace"]] == ["plan"]


def test_the_advised_schedule_is_priced_alongside_the_alternatives():
    plans = plans_to_price(_advice(strategy="twap", slices=6, horizon_seconds=600))
    assert ("twap", 6) in [(p["strategy"], p["slices"]) for p in plans if p["advised"]]
    assert not any(p["advised"] for p in plans_to_price(_advice(strategy="wait", slices=1)))
    order, book = _case("thin_book_oversized_buy")
    iceberg = [price_plan(order, book, p) for p in plans_to_price(_advice(strategy="iceberg", slices=8))]
    assert [p["label"] for p in iceberg if p["advised"]] == ["iceberg x8"]


# ------------------------------------------------------------------------------ execution

def test_each_slice_fills_against_the_book_of_its_moment():
    order, book = _case("deep_large_buy")
    later = {**book, "asks": [[p * 1.001, s] for p, s in book["asks"]]}  # the offer moves up 10 bps
    books = iter([book, later, later, later])
    slept = []
    plan = _advice(strategy="twap", slices=4, horizon_seconds=600)
    result = execute(order, plan, book, book_source=lambda: next(books), pace_s=0.5, sleep=slept.append)
    assert result["status"] == "filled" and result["slices"] == 4 and slept == [0.5] * 3
    assert result["fills"][1]["vwap"] > result["fills"][0]["vwap"]
    assert result["shortfall_bps"] > execute(order, plan, book)["shortfall_bps"]  # paid for the move


def test_a_sell_pays_for_filling_below_the_mid():
    order, book = _case("ask_heavy_sell")
    result = execute(order, _advice(order_side="sell"), book)
    assert result["vwap"] < result["arrival_mid"] and result["shortfall_bps"] > 0


def test_wait_sends_nothing():
    order, book = _case("absurd_size_buy")
    assert execute(order, _advice(strategy="wait"), book)["status"] == "no_trade"


def test_a_passive_limit_rests_its_maker_share():
    order, book = _case("wide_spread_small_buy")
    result = execute(order, _advice(strategy="passive_limit"), book)
    assert result["status"] == "filled" and 0 < result["fills"][0]["maker_share"] <= 1
    assert result["shortfall_bps"] < execute(order, _advice(), book)["shortfall_bps"]  # resting beats crossing


# ------------------------------------------------------------------------------ critic

def _review(scenario_id, **advice):
    order, book = _case(scenario_id)
    advice = _advice(**{"order_side": order["side"], **advice})
    priced = [price_plan(order, book, p) for p in plans_to_price(advice)]
    return {f["code"]: f["severity"] for f in review(order, book, advice, priced)}


def test_critic_blocks_the_wrong_side():
    assert _review("ask_heavy_sell", order_side="buy")["side"] == "block"


def test_critic_blocks_a_single_clip_the_book_cannot_fill():
    order, book = _case("thin_book_oversized_buy")
    found = _review("thin_book_oversized_buy", expected_cost_bps=_one_clip_bps(order, book),
                    risks=["the order runs past the visible book"])
    assert found == {"depth": "block"}


def test_critic_blocks_trading_an_order_many_times_the_book():
    found = _review("absurd_size_buy", strategy="twap", slices=20, horizon_seconds=3600,
                    risks=["far larger than the visible book"])
    assert found["oversize"] == "block"


def test_critic_notes_a_much_cheaper_plan_without_blocking():
    order, book = _case("wide_spread_small_buy")
    found = _review("wide_spread_small_buy", expected_cost_bps=_one_clip_bps(order, book))
    rest = price_plan(order, book, {"strategy": "passive_limit", "slices": 1})["cost_bps"]
    assert (found.get("cheaper") == "warn") == (_one_clip_bps(order, book) - rest > 1.0)


def test_critic_passes_the_rules_baseline_on_every_scenario():
    from evals.scenarios import SCENARIOS
    agent = _rules_agent()
    for scenario in SCENARIOS:
        order, book = _case(scenario["id"])
        _, view = agent.start(order, book)
        assert not blocking(view["findings"]), (scenario["id"], view["findings"])


def test_critic_agrees_with_the_graders_on_the_recorded_run():
    """The numbers the README quotes: every live answer the graders failed is blocked, none that passed."""
    import json

    from evals.critic_eval import RESULTS, compare, tally
    from evals.runner import LIVE_CANDIDATES
    with open(RESULTS) as fh:
        rows = [r for r in json.load(fh)["rows"] if r["candidate"] in LIVE_CANDIDATES]
    t = tally(compare(rows))
    assert t["missed"] == 0 and t["false_alarms"] == 0 and t["caught"] == 3


# ------------------------------------------------------------------------------ planners

def test_analyzer_planner_maps_the_panel_result_and_does_not_pace_a_revision():
    calls = []

    class Analyzer:
        def analyze(self, book, notional, **kw):
            calls.append(kw)
            return {"success": True, **_advice(), "strategy": "Market, one clip", "strategy_key": "immediate_market",
                    "model": "gemini-3.5-flash-lite", "source": "gemini", "notice": "", "tool_calls": ["quote_order"]}

    plan = analyzer_planner(Analyzer())
    order, book = _case("deep_small_buy")
    out = plan(order, book, None)
    plan(order, book, ["fix the cost"])
    assert out["advice"]["strategy"] == "immediate_market" and out["model"] == "gemini-3.5-flash-lite"
    assert calls[0]["paced"] and not calls[1]["paced"] and calls[1]["feedback"] == ["fix the cost"]


# ------------------------------------------------------------------------------ panel

def _text(component):
    """All the text in a Dash component tree."""
    if component is None:
        return ""
    if isinstance(component, (str, int, float)):
        return str(component)
    if isinstance(component, (list, tuple)):
        return " ".join(_text(c) for c in component)
    return _text(getattr(component, "children", None))


def test_the_panel_renders_every_state_of_a_run():
    import app
    order, book = _case("deep_small_buy")
    agent = _rules_agent()
    thread, waiting = agent.start(order, book)
    assert "Awaiting approval" in _text(app.render_agent(waiting))
    done = _text(app.render_agent(agent.resume(thread, approved=True)))
    assert "Executed (paper)" in done and "all in" in done and "approve" in done

    blocked = ExecutionAgent(Scripted(_advice(expected_cost_bps=40.0))).start(order, book)[1]
    text = _text(app.render_agent(blocked))
    assert "Unresolved after 2 revisions" in text and "Round 1 sent back" in text

    failed = ExecutionAgent(lambda o, b, f: {"advice": None, "errors": ["Gemini is down"]}).start(order, book)[1]
    assert "Gemini is down" in _text(app.render_agent(failed))
    assert "Plan again" in _text(app.render_agent({"status": "expired"}))


def test_the_trace_collapses_parallel_branches():
    import app
    trace = [{"node": "plan", "ms": 6100.0}] + [{"node": "price", "ms": 0.3}] * 4 + [{"node": "critic", "ms": 0.2}]
    assert app._trace_line(trace) == "plan 6.1s → price x4 → critic"
