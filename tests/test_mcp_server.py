"""
The MCP server: its tools answer exactly what the desk's own tools answer, review_plan
blocks what the agent's critic blocks, the live book is refused when stale, and one test
drives the real server over stdio the way an MCP client does.
"""
import asyncio
import json
import os
import sys
import time

import pytest

pytest.importorskip("mcp")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mcp_server as srv  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from advisor.advisor import RuleTransport, run_advisor  # noqa: E402
from advisor.tools import BookTools  # noqa: E402
from evals.scenarios import load_book  # noqa: E402
from tests.test_advisor import _advice  # noqa: E402


def _call(name, **args):
    result = asyncio.run(srv.server.call_tool(name, args))
    return json.loads(result.content[0].text)


def test_every_tool_is_read_only():
    tools = asyncio.run(srv.server.list_tools())
    assert {t.name for t in tools} == {"list_books", "book_stats", "quote_order", "depth_profile",
                                       "compare_schedule", "review_plan"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


def test_quote_is_the_desks_own_quote():
    out = _call("quote_order", side="buy", notional_usd=400_000, book="thin")
    assert out["net_cost_bps"] == BookTools(load_book("thin")).quote_order("buy", 400_000)["net_cost_bps"]
    assert out["complete"] is False and out["book"] == "thin"


def test_review_blocks_a_single_clip_into_a_book_that_cannot_fill_it():
    out = _call("review_plan", side="buy", notional_usd=400_000, book="thin", plan=_advice(expected_cost_bps=3.0))
    assert out["approvable"] is False
    assert {"cost", "depth"} <= {f["code"] for f in out["findings"]} and len(out["priced"]) >= 4


def test_review_approves_the_rules_baseline():
    book = load_book("deep_tight")
    advice = run_advisor(RuleTransport("buy", 1_000, "Market"), book, "buy", 1_000).advice
    out = _call("review_plan", side="buy", notional_usd=1_000, book="deep_tight", plan=advice)
    assert out["approvable"] is True and not [f for f in out["findings"] if f["severity"] == "block"]


def test_a_plan_that_fails_the_schema_is_not_approvable():
    out = _call("review_plan", side="buy", notional_usd=1_000, book="deep_tight", plan={"strategy": "moon"})
    assert out["approvable"] is False and out["schema_errors"]


def test_unknown_books_and_sides_are_tool_errors():
    with pytest.raises(ToolError, match="unknown book"):
        asyncio.run(srv.server.call_tool("book_stats", {"book": "nope"}))
    with pytest.raises(ToolError, match="side must be"):
        asyncio.run(srv.server.call_tool("quote_order", {"side": "long", "notional_usd": 1, "book": "thin"}))


def test_the_live_book_is_used_fresh_and_refused_stale(tmp_path, monkeypatch):
    path = tmp_path / "latest_orderbook.json"
    path.write_text(json.dumps({**load_book("deep_tight"), "source": "OKX", "symbol": "BTC-USDT-SWAP"}))
    monkeypatch.setattr(srv, "BOOK_FILE", str(path))
    out = _call("book_stats")
    assert out["venue"] == "OKX" and out["book_age_s"] < srv.MAX_LIVE_AGE_S
    stale = time.time() - 2 * srv.MAX_LIVE_AGE_S
    os.utime(path, (stale, stale))
    with pytest.raises(ToolError, match="old"):
        asyncio.run(srv.server.call_tool("book_stats", {}))
    assert _call("list_books")["live"]["available"] is False


def test_the_server_does_not_load_langgraph():
    import subprocess
    code = "import sys, mcp_server; print('langgraph' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True).stdout.strip() == "False"


def test_a_client_over_stdio():
    """The real server process, driven by the SDK's client, as Claude Code or Cursor would."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def session():
        params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "mcp_server.py")],
                                       env={"QTS_BOOK_FILE": os.path.join(ROOT, "no-such-book.json")})
        async with stdio_client(params) as streams, ClientSession(*streams[:2]) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            quote = await s.call_tool("quote_order", {"side": "sell", "notional_usd": 25_000, "book": "ask_heavy"})
            missing = await s.call_tool("book_stats", {})
            report = await s.read_resource("qts://validation/posttrade")
            return names, quote, missing, report

    names, quote, missing, report = asyncio.run(asyncio.wait_for(session(), 60))
    assert "review_plan" in names
    assert not quote.is_error and json.loads(quote.content[0].text)["side"] == "sell"
    assert missing.is_error and "no live book" in missing.content[0].text
    assert report.contents[0].text.startswith("# Post-trade scoring")
