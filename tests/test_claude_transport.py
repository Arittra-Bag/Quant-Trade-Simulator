"""
The Claude transport, driven through the real Anthropic SDK against a mock HTTP transport:
the request bodies are what the SDK would send, the responses are parsed by the SDK, and
nothing touches the network or spends money.
"""
import json
import os
import sys

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor.advisor import is_transient, run_advisor  # noqa: E402
from advisor.claude_transport import OUTPUT_SCHEMA, TOOLS, ClaudeTransport, run_cost  # noqa: E402
from tests.test_advisor import DEEP, _advice  # noqa: E402

USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 0}


def _message(content, stop_reason, usage=USAGE):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage}


def _tool_use(name, args, use_id="toolu_1"):
    return {"type": "tool_use", "id": use_id, "name": name, "input": args}


def _client(replies, status=200):
    """An anthropic.Anthropic whose HTTP layer answers from `replies` and records each body."""
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        reply = replies.pop(0)
        if isinstance(reply, int):
            return httpx2.Response(reply, json={"type": "error", "error": {"type": "overloaded_error",
                                                                           "message": "Overloaded"}})
        return httpx2.Response(status, json=reply)

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return client, sent


def _run(replies, **kwargs):
    client, sent = _client(replies)
    transport = ClaudeTransport(client, "claude-haiku-4-5", **kwargs)
    return run_advisor(transport, DEEP, "buy", 1_000), transport, sent


def test_tools_then_schema_constrained_advice():
    result, transport, sent = _run([
        _message([_tool_use("quote_order", {"side": "buy", "notional_usd": 1000})], "tool_use"),
        _message([{"type": "text", "text": json.dumps({**_advice(), "limit_price": 0})}], "end_turn"),
    ])
    assert result.ok, result.errors
    assert [c["name"] for c in result.tool_calls] == ["quote_order"]
    assert len(sent) == 2 and transport.answered_by == ["claude-haiku-4-5"] * 2

    first, second = sent
    assert first["output_config"]["format"]["schema"] == OUTPUT_SCHEMA
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == [t["name"] for t in TOOLS]
    # The second turn carries the assistant's tool call and the matching result.
    assert second["messages"][1]["content"][0]["type"] == "tool_use"
    tool_result = second["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "net_cost_bps" in tool_result["content"]


def test_usage_is_priced():
    result, transport, _ = _run([
        _message([{"type": "text", "text": json.dumps({**_advice(), "limit_price": 0})}], "end_turn",
                 usage={**USAGE, "cache_read_input_tokens": 2000}),
    ])
    assert result.ok
    # 1000 in at $1, 2000 cached at $0.10, 200 out at $5, per million
    assert transport.cost_usd == pytest.approx((1000 * 1 + 2000 * 0.1 + 200 * 5) / 1e6)
    assert run_cost("some-unknown-model", transport.usage) is None


def test_every_object_in_the_schemas_is_closed():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(OUTPUT_SCHEMA)
    walk(TOOLS)
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
    assert all(t["input_schema"]["type"] == "object" for t in TOOLS)


def test_effort_is_sent_only_when_set():
    advice = _message([{"type": "text", "text": json.dumps({**_advice(), "limit_price": 0})}], "end_turn")
    _, _, sent = _run([dict(advice)], effort="low")
    assert sent[0]["output_config"]["effort"] == "low"
    _, _, sent = _run([dict(advice)])
    assert "effort" not in sent[0]["output_config"]


def test_an_overloaded_api_is_an_outage_not_a_score():
    result, _, _ = _run([529])
    assert not result.ok and result.upstream_error


def test_a_refusal_is_reported_not_scored_as_advice():
    result, _, _ = _run([_message([], "refusal")])
    assert not result.ok and not result.upstream_error and "declined" in result.errors[0]


def test_a_cut_off_answer_is_errored_not_scored():
    """The provider stopped the model; the row measures the output limit, not the advisor."""
    result, _, _ = _run([_message([{"type": "text", "text": '{"sentiment": "Bull'}], "max_tokens")])
    assert not result.ok and result.upstream_error and "max_tokens" in result.errors[0]


def test_the_budget_is_checked_before_every_call():
    client, sent = _client([
        _message([_tool_use("quote_order", {"side": "buy", "notional_usd": 1000})], "tool_use"),
        _message([{"type": "text", "text": "{}"}], "end_turn"),
    ])
    transport = ClaudeTransport(client, "claude-haiku-4-5", budget_usd=0.0001)
    result = run_advisor(transport, DEEP, "buy", 1_000)
    assert len(sent) == 1  # the first call spent $0.002, so the second was never sent
    assert not result.ok and result.upstream_error and "spend cap" in result.errors[0]


def test_a_refused_request_reports_no_tokens_of_its_own():
    reply = _message([{"type": "text", "text": json.dumps({**_advice(), "limit_price": 0})}], "end_turn")
    client, _ = _client([reply])
    transport = ClaudeTransport(client, "claude-haiku-4-5", min_interval=60)
    assert run_advisor(transport, DEEP, "buy", 1_000).ok and transport.usage
    refused = run_advisor(transport, DEEP, "buy", 1_000)
    assert not refused.ok and transport.usage == {} and transport.cost_usd == 0


def test_sdk_errors_classify_by_status_code():
    class _Overloaded(Exception):
        status_code = 529
    assert is_transient(_Overloaded())
