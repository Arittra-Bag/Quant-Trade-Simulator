"""
Offline tests for the execution advisor: output schema, tools, the agent loop, the
transports and the eval graders. Run: python -m pytest -q

Nothing here touches the network or needs an API key. The live Gemini path is exercised
only through a fake client that mimics the SDK's response shape, so what is proven here
is the loop's handling of that shape, not the real API's behaviour.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor.advisor import (GeminiTransport, ModelCooldown, ReplayTransport, RuleTransport,  # noqa: E402
                             run_advisor)
from advisor.schema import STRATEGIES, strategy_label, validate_advice  # noqa: E402
from advisor.tools import MAX_TOOL_TURNS, TOOL_DECLARATIONS, BookTools  # noqa: E402
from evals.candidates import CANDIDATES, REAL_CANDIDATES  # noqa: E402
from evals.graders import grade, ground_truth  # noqa: E402
from evals.runner import SCORE_FLOOR, run_one, run_suite, summarise  # noqa: E402
from evals.scenarios import SCENARIOS, get_scenario, scenario_book  # noqa: E402

DEEP = scenario_book(get_scenario("deep_small_buy"))
THIN = scenario_book(get_scenario("thin_book_oversized_buy"))


def _advice(**overrides):
    base = {
        "sentiment": "Neutral", "strategy": "immediate_market", "order_side": "buy",
        "urgency": "high", "confidence": 0.7, "expected_cost_bps": 4.2, "slices": 1,
        "horizon_seconds": 0, "limit_price": 0, "analysis": "a", "reasoning": "b",
        "execution_approach": "c", "risks": [],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------------- schema

def test_valid_advice_passes_untouched():
    advice, errors = validate_advice(_advice(), expected_side="buy")
    assert not errors and advice["strategy"] == "immediate_market"


def test_unknown_strategy_is_rejected_outright():
    advice, errors = validate_advice(_advice(strategy="yolo"), expected_side="buy")
    assert advice is None and "not in" in errors[0]


def test_free_text_strategy_is_normalised():
    advice, errors = validate_advice(_advice(strategy="Immediate Market"), expected_side="buy")
    assert advice["strategy"] == "immediate_market" and not errors


@pytest.mark.parametrize("field,value,fixed", [
    ("confidence", 1.8, 1.0),
    ("confidence", -0.2, 0.0),
    ("expected_cost_bps", -5.0, 0.0),
])
def test_out_of_range_numbers_are_clamped_and_reported(field, value, fixed):
    advice, errors = validate_advice(_advice(**{field: value}), expected_side="buy")
    assert advice[field] == fixed and any(field in e for e in errors)


def test_side_mismatch_is_reported_but_not_fatal():
    advice, errors = validate_advice(_advice(order_side="sell"), expected_side="buy")
    assert advice is not None and any("does not match" in e for e in errors)


def test_single_clip_strategy_cannot_carry_slices():
    advice, errors = validate_advice(_advice(slices=4), expected_side="buy")
    assert advice["slices"] == 1 and any("single clip" in e for e in errors)


def test_twap_gets_a_horizon_and_a_minimum_slice_count():
    advice, errors = validate_advice(
        _advice(strategy="twap", slices=1, horizon_seconds=0), expected_side="buy")
    assert advice["slices"] >= 2 and advice["horizon_seconds"] > 0 and len(errors) == 2


def test_missing_fields_are_reported():
    _, errors = validate_advice({"strategy": "wait", "sentiment": "Neutral"}, expected_side="buy")
    assert any("missing fields" in e for e in errors)


def test_non_object_response_is_not_repairable():
    advice, errors = validate_advice("sell everything", expected_side="buy")
    assert advice is None and errors


def test_every_strategy_has_a_label():
    for strategy in STRATEGIES:
        assert strategy_label({"strategy": strategy, "slices": 3})


# -------------------------------------------------------------------------------- tools

def test_quote_order_flags_an_order_larger_than_the_book():
    tools = BookTools(THIN)
    assert tools.quote_order("buy", 400_000)["complete"] is False
    assert "larger than the visible book" in tools.quote_order("buy", 400_000)["warning"]


def test_quote_order_fills_inside_a_deep_book():
    assert BookTools(DEEP).quote_order("buy", 1_000)["complete"] is True


def test_quote_order_reads_the_side_it_is_given():
    tools = BookTools(DEEP)
    assert tools.quote_order("sell", 1_000)["fill_vwap"] < tools.quote_order("buy", 1_000)["fill_vwap"]


def test_depth_profile_accumulates_and_is_capped():
    profile = BookTools(DEEP).get_depth_profile("buy", levels=200)["levels"]
    assert len(profile) <= 50
    assert all(b["cumulative_usd"] >= a["cumulative_usd"] for a, b in zip(profile, profile[1:]))


def test_compare_schedule_does_not_record_its_internal_quote():
    tools = BookTools(THIN)
    tools.dispatch("compare_schedule", {"side": "buy", "notional_usd": 400_000, "slices": 4})
    assert tools.call_names() == ["compare_schedule"]


def test_compare_schedule_reports_a_saving_for_an_oversized_order():
    result = BookTools(THIN).compare_schedule("buy", 400_000, 4)
    assert result["saving_bps"] > 0 and result["one_shot_complete"] is False


def test_dispatch_turns_a_bad_call_into_an_error_not_an_exception():
    tools = BookTools(DEEP)
    assert "error" in tools.dispatch("get_fills", {})
    assert "error" in tools.dispatch("quote_order", {"nope": 1})
    assert "error" in tools.dispatch("quote_order", {"side": "buy", "notional_usd": -1})
    assert len(tools.calls) == 3


def test_declarations_match_the_methods_that_implement_them():
    tools = BookTools(DEEP)
    for declaration in TOOL_DECLARATIONS:
        assert callable(getattr(tools, declaration["name"]))
        assert declaration["description"] and declaration["parameters"]["type"] == "OBJECT"


# --------------------------------------------------------------------------------- loop

def test_loop_validates_the_final_object():
    result = run_advisor(ReplayTransport([{"advice": _advice()}]), DEEP, "buy", 1_000)
    assert result.ok and not result.errors and result.turns == 1


def test_loop_runs_the_tools_the_transport_asks_for():
    transport = ReplayTransport([
        {"tool_calls": [{"name": "get_book_stats", "args": {}},
                        {"name": "quote_order", "args": {"side": "buy", "notional_usd": 1_000}}]},
        {"advice": _advice()},
    ])
    result = run_advisor(transport, DEEP, "buy", 1_000)
    assert [c["name"] for c in result.tool_calls] == ["get_book_stats", "quote_order"]
    assert result.tool_calls[1]["result"]["net_cost_bps"] > 0


def test_loop_stops_at_the_turn_ceiling():
    spin = {"tool_calls": [{"name": "get_book_stats", "args": {}}]}
    result = run_advisor(ReplayTransport([spin] * 50), DEEP, "buy", 1_000)
    assert not result.ok and result.turns == MAX_TOOL_TURNS
    assert any("ceiling" in e for e in result.errors)


def test_loop_survives_a_transport_that_raises():
    class Boom:
        name, model = "boom", ""

        def propose(self, *_):
            raise RuntimeError("upstream is down")

    result = run_advisor(Boom(), DEEP, "buy", 1_000)
    assert not result.ok and "upstream is down" in result.errors[0]


def test_loop_survives_an_exhausted_script():
    result = run_advisor(ReplayTransport([]), DEEP, "buy", 1_000)
    assert not result.ok and "exhausted" in result.errors[0]


def test_loop_records_the_expected_side_for_the_validator():
    result = run_advisor(ReplayTransport([{"advice": _advice(order_side="sell")}]),
                         DEEP, "buy", 1_000)
    assert any("does not match" in e for e in result.errors)


# ----------------------------------------------------------------------- rules transport

def test_baseline_takes_a_small_order_into_a_deep_book():
    result = run_advisor(RuleTransport("buy", 1_000), DEEP, "buy", 1_000)
    assert result.ok and result.advice["strategy"] == "immediate_market"


def test_baseline_refuses_to_one_shot_an_order_past_the_book():
    result = run_advisor(RuleTransport("buy", 400_000), THIN, "buy", 400_000)
    assert result.advice["strategy"] in ("twap", "wait")
    assert "compare_schedule" in [c["name"] for c in result.tool_calls]


def test_baseline_stands_aside_when_the_book_is_hopeless():
    scenario = get_scenario("absurd_size_buy")
    book = scenario_book(scenario)
    result = run_advisor(RuleTransport("buy", scenario["notional"]), book, "buy", scenario["notional"])
    assert result.advice["strategy"] == "wait"


def test_baseline_cites_the_cost_it_quoted():
    result = run_advisor(RuleTransport("buy", 1_000), DEEP, "buy", 1_000)
    quoted = next(c for c in result.tool_calls if c["name"] == "quote_order")
    assert result.advice["expected_cost_bps"] == pytest.approx(quoted["result"]["net_cost_bps"])


def test_baseline_reads_sentiment_from_the_book_not_the_order_side():
    bid_heavy = scenario_book(get_scenario("bid_heavy_buy"))
    buy = run_advisor(RuleTransport("buy", 25_000), bid_heavy, "buy", 25_000)
    sell = run_advisor(RuleTransport("sell", 25_000), bid_heavy, "sell", 25_000)
    assert buy.advice["sentiment"] == sell.advice["sentiment"] == "Bullish"


# ---------------------------------------------------------------------- gemini transport
# A fake client standing in for google-genai. This proves the loop reads the SDK's
# response shape and the two-phase call; it says nothing about the real API.

class _FakeResponse:
    def __init__(self, text="", function_calls=None):
        self.text = text
        self.function_calls = function_calls or []
        self.candidates = [type("C", (), {"content": type("Ct", (), {"parts": []})()})()]


class _FakeCall:
    def __init__(self, name, args):
        self.name, self.args = name, args


class _NotFound(Exception):
    code = 404

    def __str__(self):
        return "404 NOT_FOUND. model is no longer available"


class _FakeModels:
    def __init__(self, turns, unavailable=()):
        self.turns, self.unavailable, self.calls = list(turns), set(unavailable), []
        self.contents = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        self.contents.append(list(contents))
        if model in self.unavailable:
            raise _NotFound()
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def _client(turns, unavailable=()):
    return type("C", (), {"models": _FakeModels(turns, unavailable)})()


def test_gemini_transport_runs_tools_then_finalises_with_the_schema():
    pytest.importorskip("google.genai")
    client = _client([
        _FakeResponse(function_calls=[_FakeCall("quote_order", {"side": "buy", "notional_usd": 1000})]),
        _FakeResponse(text="Book is deep, take it."),
        _FakeResponse(text=json.dumps(_advice())),
    ])
    result = run_advisor(GeminiTransport(client, ["m1"]), DEEP, "buy", 1_000)
    assert result.ok and [c["name"] for c in result.tool_calls] == ["quote_order"]
    assert len(client.models.calls) == 3  # tool turn, closing turn, schema turn


def test_gemini_transport_falls_through_a_retired_model_and_remembers_it():
    pytest.importorskip("google.genai")
    client = _client([_FakeResponse(text="done"), _FakeResponse(text=json.dumps(_advice()))],
                     unavailable={"dead"})
    transport = GeminiTransport(client, ["dead", "alive"])
    result = run_advisor(transport, DEEP, "buy", 1_000)
    assert result.ok and transport.model == "alive"
    assert client.models.calls == ["dead", "alive", "alive"]


def test_gemini_transport_reports_failure_when_no_model_is_available():
    pytest.importorskip("google.genai")
    client = _client([], unavailable={"dead", "also_dead"})
    result = run_advisor(GeminiTransport(client, ["dead", "also_dead"]), DEEP, "buy", 1_000)
    assert not result.ok and "NOT_FOUND" in result.errors[0]


def _three_call_run():
    return [
        _FakeResponse(function_calls=[_FakeCall("quote_order", {"side": "buy", "notional_usd": 1000})]),
        _FakeResponse(text="Book is deep, take it."),
        _FakeResponse(text=json.dumps(_advice())),
    ]


def test_gemini_rate_limit_does_not_fire_between_calls_of_one_request():
    """
    The first live eval scored 7.7%: the 5s limit was checked on every API call, so each
    scenario made one call, got tool calls back, and died on the second. One request is
    several calls by design, and the limit is per request.
    """
    pytest.importorskip("google.genai")
    client = _client(_three_call_run())
    result = run_advisor(GeminiTransport(client, ["m"], min_interval=5), DEEP, "buy", 1_000)
    assert result.ok and not result.errors, result.errors
    assert len(client.models.calls) == 3


def test_gemini_transport_honours_the_rate_limit_between_requests():
    pytest.importorskip("google.genai")
    client = _client(_three_call_run() + _three_call_run())
    transport = GeminiTransport(client, ["m"], min_interval=60)
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    second = run_advisor(transport, DEEP, "buy", 1_000)
    assert not second.ok and "Rate limited" in second.errors[0]
    assert len(client.models.calls) == 3  # the refused request never reached the API


def test_gemini_transport_starts_each_request_with_a_fresh_conversation():
    pytest.importorskip("google.genai")
    client = _client(_three_call_run() + _three_call_run())
    transport = GeminiTransport(client, ["m"])
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert len(client.models.contents[3]) == 1  # second request opens with just its prompt


class _Exhausted(Exception):
    code = 429
    details = {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}]}}

    def __str__(self):
        return "429 RESOURCE_EXHAUSTED"


def test_a_spent_quota_passes_the_call_to_the_fallback_model():
    pytest.importorskip("google.genai")
    client = _client([_Exhausted()] + _three_call_run())
    result = run_advisor(GeminiTransport(client, ["flash", "lite"]), DEEP, "buy", 1_000)
    assert result.ok and client.models.calls[:2] == ["flash", "lite"]


class _Busy(Exception):
    code = 503

    def __str__(self):
        return "503 UNAVAILABLE. This model is currently experiencing high demand."


class _BadRequest(Exception):
    code = 400

    def __str__(self):
        return "400 INVALID_ARGUMENT"


def _no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("advisor.advisor.time.sleep", slept.append)
    return slept


def test_a_busy_model_falls_through_to_the_fallback_without_moving_the_default():
    """Second live run: 7 of 8 scenarios got 503 high demand and never tried the lite model."""
    pytest.importorskip("google.genai")
    client = _client([_Busy()] + _three_call_run())
    transport = GeminiTransport(client, ["flash", "lite"])
    result = run_advisor(transport, DEEP, "buy", 1_000)
    assert result.ok and result.models_used == ["lite", "flash"]  # in answer order; only the busy call moved
    assert client.models.calls == ["flash", "lite", "flash", "flash"]
    assert transport.model == "flash"  # busy is not retired


def test_nothing_is_retried(monkeypatch):
    """A retry loop on the free tier hung a live run for 18 minutes. One attempt per model."""
    pytest.importorskip("google.genai")
    slept = _no_sleep(monkeypatch)
    client = _client([_Busy(), _Busy()])
    result = run_advisor(GeminiTransport(client, ["flash", "lite"]), DEEP, "buy", 1_000)
    assert not result.ok and result.upstream_error
    assert client.models.calls == ["flash", "lite"] and slept == []


class _Timeout(Exception):
    def __str__(self):
        return "The read operation timed out"


def test_a_call_that_times_out_is_errored_not_scored():
    pytest.importorskip("google.genai")
    result = run_advisor(GeminiTransport(_client([_Timeout()]), ["m"]), DEEP, "buy", 1_000)
    assert not result.ok and result.upstream_error


def test_a_live_run_stops_starting_scenarios_when_its_budget_is_spent(monkeypatch):
    import evals.runner as runner
    monkeypatch.setattr(runner, "LIVE_RUN_BUDGET", -1)
    monkeypatch.setattr(runner, "_live_transport_factory", lambda: lambda: ReplayTransport([]))
    saved = []
    rows = runner.run_suite([], live=True, on_row=lambda r: saved.append(len(r)))
    assert len(rows) == len(SCENARIOS) and all(r["errored"] for r in rows)
    assert saved == list(range(1, len(SCENARIOS) + 1))  # results written after every scenario
    assert summarise(rows)["gemini_live"]["score"] is None


def test_saved_results_replace_the_file_whole(tmp_path, monkeypatch):
    import evals.runner as runner
    monkeypatch.setattr(runner, "SCENARIOS", runner.SCENARIOS)  # --scenario narrows it globally
    out = tmp_path / "results.json"
    out.write_text("old")
    runner.main(["--candidates", "rules", "--scenario", SCENARIOS[0]["id"], "--json", str(out), "--quiet"])
    assert json.loads(out.read_text())["rows"]
    assert [p.name for p in tmp_path.iterdir()] == ["results.json"]  # no temp file left behind

def test_live_calls_are_paced_across_requests(monkeypatch):
    pytest.importorskip("google.genai")
    slept = _no_sleep(monkeypatch)
    client = _client(_three_call_run() + _three_call_run())
    transport = GeminiTransport(client, ["m"], call_interval=13)
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert len(slept) == 5 and all(0 < s <= 13 for s in slept)  # every call after the first


def test_a_provider_outage_is_errored_not_scored():
    pytest.importorskip("google.genai")
    result = run_advisor(GeminiTransport(_client([_Busy()]), ["m"]), DEEP, "buy", 1_000)
    assert not result.ok and result.upstream_error


def test_a_request_the_api_rejects_is_our_failure_not_an_outage():
    pytest.importorskip("google.genai")
    result = run_advisor(GeminiTransport(_client([_BadRequest()]), ["m", "lite"]), DEEP, "buy", 1_000)
    assert not result.ok and not result.upstream_error


def test_errored_scenarios_are_left_out_of_the_score():
    rows = [
        {"candidate": "gemini_live", "scenario": "a", "score": 1.0, "critical_failures": [],
         "checks": [], "latency_ms": 10.0, "errored": False},
        {"candidate": "gemini_live", "scenario": "b", "score": 0.08, "critical_failures": ["produced_advice"],
         "checks": [], "latency_ms": 10.0, "errored": True},
    ]
    entry = summarise(rows)["gemini_live"]
    assert entry["score"] == 1.0 and entry["errored"] == 1 and entry["clean_scenarios"] == 1
    assert summarise(rows[1:])["gemini_live"]["score"] is None


def test_gemini_transport_surfaces_a_429_by_default():
    pytest.importorskip("google.genai")
    client = _client([_Exhausted()])
    result = run_advisor(GeminiTransport(client, ["m"]), DEEP, "buy", 1_000)
    assert not result.ok and "RESOURCE_EXHAUSTED" in result.errors[0]


# ------------------------------------------------------------------------------ graders

def _graded(scenario_id, candidate):
    scenario = get_scenario(scenario_id)
    row = run_one(candidate, scenario)
    return {c["name"]: c for c in row["checks"]}, row


def test_baseline_passes_every_grader_on_every_scenario():
    for scenario in SCENARIOS:
        row = run_one("rules", scenario)
        failed = [c["name"] for c in row["checks"] if not c["passed"]]
        assert not failed, f"{scenario['id']}: {failed}"


@pytest.mark.parametrize("candidate,check,scenario_id", [
    ("legacy_prose", "side_fidelity", "ask_heavy_sell"),
    ("legacy_prose", "quoted_the_order", "deep_small_buy"),
    ("legacy_prose", "schema_clean", "deep_small_buy"),
    ("ungrounded", "cost_grounded", "deep_small_buy"),
    ("depth_blind", "depth_honesty", "thin_book_oversized_buy"),
    ("schema_drift", "schema_clean", "deep_small_buy"),
    ("schedule_unpriced", "schedule_grounded", "deep_small_buy"),
    ("derails", "produced_advice", "deep_small_buy"),
    ("derails", "tools_succeeded", "deep_small_buy"),
    ("derails", "tool_economy", "deep_small_buy"),
])
def test_each_grader_fires_on_the_failure_it_targets(candidate, check, scenario_id):
    """A grader that never fails is not evidence of anything."""
    checks, _ = _graded(scenario_id, candidate)
    assert check in checks and not checks[check]["passed"]


def test_legacy_contract_keeps_the_side_on_a_buy_but_loses_it_on_a_sell():
    """The old prompt hardcoded 'market BUY', so it only drifts when the order is a sell."""
    assert _graded("deep_small_buy", "legacy_prose")[0]["side_fidelity"]["passed"]
    assert not _graded("ask_heavy_sell", "legacy_prose")[0]["side_fidelity"]["passed"]


def test_ground_truth_is_computed_from_the_book_not_the_model():
    scenario = get_scenario("thin_book_oversized_buy")
    truth = ground_truth(scenario, scenario_book(scenario))
    assert truth["quote"]["complete"] is False and truth["quote"]["net_cost_bps"] > 0


def test_grade_marks_critical_failures():
    scenario = get_scenario("thin_book_oversized_buy")
    book = scenario_book(scenario)
    transport = CANDIDATES["depth_blind"](scenario)
    for turn in transport.script:
        if isinstance(turn.get("advice"), dict):
            turn["advice"]["expected_cost_bps"] = ground_truth(scenario, book)["quote"]["net_cost_bps"]
    result = run_advisor(transport, book, scenario["side"], scenario["notional"],
                         fee_tier=scenario["fee_tier"], volatility=scenario["volatility"])
    _, score, critical = grade(scenario, result, book)
    assert "depth_honesty" in critical and score < 1.0


# --------------------------------------------------------------------------------- suite

def test_the_whole_suite_runs_offline_and_the_baseline_clears_its_floor():
    summary = summarise(run_suite(list(CANDIDATES)))
    assert set(summary) == set(CANDIDATES)
    for name in REAL_CANDIDATES:
        assert summary[name]["score"] >= SCORE_FLOOR


def test_replay_candidates_score_below_the_baseline():
    summary = summarise(run_suite(list(CANDIDATES)))
    baseline = summary["rules"]["score"]
    for name in set(CANDIDATES) - REAL_CANDIDATES:
        assert summary[name]["score"] < baseline, f"{name} did not lose to the baseline"


def test_every_scenario_book_has_two_sides():
    for scenario in SCENARIOS:
        book = scenario_book(scenario)
        assert book["bids"] and book["asks"]
        assert float(book["bids"][0][0]) < float(book["asks"][0][0])


def test_published_results_are_current():
    """RESULTS.md must match what the harness produces now, not a stale run."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "evals", "RESULTS.md")
    assert os.path.exists(path), "run: python -m evals.runner --markdown evals/RESULTS.md"
    published = open(path).read()
    summary = summarise(run_suite(list(CANDIDATES)))
    for name, entry in summary.items():
        assert f"| `{name}` |" in published
        assert f"{entry['score'] * 100:.1f}%" in published, f"{name} score has moved since RESULTS.md"


def test_app_adapter_completes_a_live_request_and_spaces_out_clicks():
    """The deployed panel had the same per-call limit, so it failed every request with a key."""
    pytest.importorskip("google.genai")
    import gemini_integration as gi
    client = _client(_three_call_run() + _three_call_run())
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = client, ["m"], "m", 5
    analyzer.cooldown = ModelCooldown()
    first = analyzer.analyze(DEEP, 1_000, side="buy")
    assert first["success"] and first["source"] == "gemini", first
    second = analyzer.analyze(DEEP, 1_000, side="buy")
    # A click inside the pacing window answers from the rules and says why, not an error.
    assert second["success"] and second["source"] == "baseline", second
    assert "paced" in second["notice"] and len(client.models.calls) == 3


def test_app_adapter_gives_each_request_its_own_conversation():
    """Two callbacks in flight must not share one transport's message history."""
    pytest.importorskip("google.genai")
    import gemini_integration as gi
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = _client([]), ["m"], "m", 0
    analyzer.cooldown = ModelCooldown()
    first = analyzer._transport("buy", 1_000, "Market")
    second = analyzer._transport("buy", 1_000, "Market")
    assert first is not second


# --------------------------------------------------------------------------- free-tier limits
class _DailyQuota(Exception):
    """What the free tier returns once a model's requests-per-day quota is spent."""
    code = 429
    details = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                      "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}

    def __str__(self):  # the SDK's message carries the response body, as on the desk
        return f"429 RESOURCE_EXHAUSTED. {self.details}"


class _MinuteQuota(Exception):
    code = 429
    details = {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}

    def __str__(self):
        return "429 RESOURCE_EXHAUSTED"


class _Deadline(Exception):
    """The 504 on the desk: the fallback model timed out server-side."""
    code = 504

    def __str__(self):
        return "504 DEADLINE_EXCEEDED. Deadline expired before operation could complete."


def test_a_model_out_of_daily_quota_is_not_asked_again_until_the_reset():
    """3.8 Flash at 28/20 a day was still tried first on every call of every click."""
    pytest.importorskip("google.genai")
    cooldown = ModelCooldown()
    client = _client([_DailyQuota()] + _three_call_run() + _three_call_run())
    first = run_advisor(GeminiTransport(client, ["flash", "lite"], cooldown=cooldown), DEEP, "buy", 1_000)
    assert first.ok and client.models.calls == ["flash", "lite", "lite", "lite"]
    second = run_advisor(GeminiTransport(client, ["flash", "lite"], cooldown=cooldown), DEEP, "buy", 1_000)
    assert second.ok and client.models.calls[4:] == ["lite", "lite", "lite"]  # flash never asked
    assert cooldown.resting()["flash"] > 60


def test_cooldowns_match_what_each_error_means():
    now = 1_790_000_000.0
    cooldown = ModelCooldown(clock=lambda: now)
    assert cooldown.cooldown_for(_MinuteQuota()) == 37  # Google's retryDelay
    assert cooldown.cooldown_for(_Deadline()) == ModelCooldown.BUSY_S
    assert cooldown.cooldown_for(_Busy()) == ModelCooldown.BUSY_S
    assert 60 <= cooldown.cooldown_for(_DailyQuota()) <= 25 * 3600  # until midnight Pacific
    assert cooldown.cooldown_for(_BadRequest()) == 0  # our mistake, not the model's


def test_quota_resets_at_midnight_pacific():
    from datetime import datetime, timezone
    from advisor.advisor import seconds_to_quota_reset
    noon_utc = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc).timestamp()  # 05:00 PDT
    assert seconds_to_quota_reset(noon_utc) == pytest.approx(19 * 3600)


def test_every_model_resting_fails_fast_without_calling_the_api():
    pytest.importorskip("google.genai")
    cooldown = ModelCooldown()
    cooldown.record("flash", _DailyQuota())
    cooldown.record("lite", _Deadline())
    client = _client([])
    result = run_advisor(GeminiTransport(client, ["flash", "lite"], cooldown=cooldown), DEEP, "buy", 1_000)
    assert not result.ok and result.upstream_error and client.models.calls == []


def test_a_request_stops_when_its_deadline_passes():
    pytest.importorskip("google.genai")
    client = _client(_three_call_run())
    result = run_advisor(GeminiTransport(client, ["m"], deadline_s=1e-9), DEEP, "buy", 1_000)
    assert not result.ok and result.upstream_error and "budget" in result.errors[0]


def _live_analyzer(client, models=("flash", "lite")):
    import gemini_integration as gi
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = client, list(models), models[0], 0
    analyzer.cooldown = ModelCooldown()
    return analyzer


def test_the_panel_answers_from_the_rules_when_gemini_cannot():
    """The desk showed 'Analysis failed: ServerError: 504 DEADLINE_EXCEEDED {...}'."""
    pytest.importorskip("google.genai")
    result = _live_analyzer(_client([_DailyQuota(), _Deadline()])).analyze(DEEP, 1_000, side="buy")
    assert result["success"] and result["source"] == "baseline" and result["model"] == "rules-v1"
    assert result["notice"] == "Rules-based read: Gemini took too long to answer."


def test_the_notice_names_a_spent_daily_quota():
    pytest.importorskip("google.genai")
    result = _live_analyzer(_client([_DailyQuota()]), models=("flash",)).analyze(DEEP, 1_000, side="buy")
    assert result["success"] and "daily" in result["notice"]


def test_a_live_answer_carries_no_notice():
    pytest.importorskip("google.genai")
    result = _live_analyzer(_client(_three_call_run())).analyze(DEEP, 1_000, side="buy")
    assert result["success"] and result["source"] == "gemini" and result["notice"] == ""



def test_answering_while_the_default_rests_does_not_make_the_fallback_the_default():
    """One 503 on Lite used to hand the lead to 3.8 Flash, 20 requests a day, for good."""
    pytest.importorskip("google.genai")
    cooldown = ModelCooldown()
    cooldown.record("lite", _Busy())
    transport = GeminiTransport(_client(_three_call_run()), ["lite", "flash"], cooldown=cooldown)
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert transport.model == "lite" and transport.answered_by == ["flash"] * 3


def test_the_notice_says_when_every_model_is_resting():
    pytest.importorskip("google.genai")
    analyzer = _live_analyzer(_client([]))
    analyzer.cooldown.record("flash", _DailyQuota())
    analyzer.cooldown.record("lite", _Deadline())
    result = analyzer.analyze(DEEP, 1_000, side="buy")
    assert result["success"] and "resting" in result["notice"]
    assert "every model is resting" in result["gemini_error"]  # the real cause is kept


def test_each_call_is_cut_to_what_is_left_of_the_deadline():
    pytest.importorskip("google.genai")
    client = _client(_three_call_run())
    seen = []
    real = client.models.generate_content

    def spy(model, contents, config):
        seen.append(config.http_options.timeout)
        return real(model=model, contents=contents, config=config)

    client.models.generate_content = spy
    assert run_advisor(GeminiTransport(client, ["m"], deadline_s=10), DEEP, "buy", 1_000).ok
    assert len(seen) == 3 and all(1000 <= t <= 10_000 for t in seen)


def test_a_zero_retry_delay_is_honoured():
    class _NoWait(_MinuteQuota):
        details = {"error": {"details": [{"retryDelay": "0s"}]}}
    assert ModelCooldown().cooldown_for(_NoWait()) == 0


def test_a_bug_is_reported_not_passed_off_as_a_gemini_outage():
    pytest.importorskip("google.genai")
    analyzer = _live_analyzer(_client([]))
    del analyzer.cooldown  # a broken analyzer: _transport raises AttributeError
    result = analyzer.analyze(DEEP, 1_000, side="buy")
    assert not result["success"] and "Analysis failed" in result["analysis"]


def test_the_label_names_the_model_that_answered():
    pytest.importorskip("google.genai")
    analyzer = _live_analyzer(_client(_three_call_run()), models=("lite", "flash"))
    analyzer.cooldown.record("lite", _Busy())
    result = analyzer.analyze(DEEP, 1_000, side="buy")
    assert result["source"] == "gemini" and result["model"] == "flash"


def test_the_deadline_never_lifts_a_call_above_its_own_timeout():
    """A 40 s budget replaced the 20 s per-call timeout, so one hung call ate the whole click."""
    pytest.importorskip("google.genai")
    client = _client(_three_call_run())
    seen = []
    real = client.models.generate_content

    def spy(model, contents, config):
        seen.append(config.http_options.timeout)
        return real(model=model, contents=contents, config=config)

    client.models.generate_content = spy
    transport = GeminiTransport(client, ["m"], deadline_s=40, call_timeout_s=20)
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert seen and all(t <= 20_000 for t in seen)


def test_a_timeout_the_deadline_forced_does_not_rest_the_model():
    pytest.importorskip("google.genai")
    cooldown = ModelCooldown()
    transport = GeminiTransport(_client([_Timeout()]), ["lite"], cooldown=cooldown, deadline_s=5, call_timeout_s=20)
    assert not run_advisor(transport, DEEP, "buy", 1_000).ok
    assert cooldown.resting() == {}  # our 5 s cut, not the model's fault


def test_only_a_retired_default_moves_the_default():
    pytest.importorskip("google.genai")
    client = _client([_Busy()] + _three_call_run(), unavailable={"b"})
    transport = GeminiTransport(client, ["a", "b", "c"])
    assert run_advisor(transport, DEEP, "buy", 1_000).ok
    assert transport.model == "a"  # b's 404 says nothing about a, which was only busy


def test_an_invalid_gemini_answer_falls_back_to_the_rules():
    pytest.importorskip("google.genai")
    client = _client([_FakeResponse(text="done"), _FakeResponse(text="not json at all")])
    result = _live_analyzer(client, models=("m",)).analyze(DEEP, 1_000, side="buy")
    assert result["success"] and result["source"] == "baseline"
    assert result["notice"] == "Rules-based read: Gemini's answer did not pass validation."


def test_a_spent_budget_does_not_hide_the_models_own_error(monkeypatch):
    pytest.importorskip("google.genai")
    transport = GeminiTransport(_client([_DailyQuota()]), ["flash", "lite"], deadline_s=30)
    real = transport._within_deadline
    calls = []

    def budget_gone_after_first(config):
        calls.append(1)
        if len(calls) > 1:
            raise TimeoutError("budget ran out")
        return real(config)

    monkeypatch.setattr(transport, "_within_deadline", budget_gone_after_first)
    result = run_advisor(transport, DEEP, "buy", 1_000)
    assert isinstance(result.exception, _DailyQuota)


def test_a_retired_default_is_replaced_even_when_the_run_then_fails():
    pytest.importorskip("google.genai")
    client = _client([_FakeResponse(text="done"), _BadRequest()], unavailable={"old"})
    analyzer = _live_analyzer(client, models=("old", "new"))
    analyzer.analyze(DEEP, 1_000, side="buy")
    assert analyzer.model == "new"
