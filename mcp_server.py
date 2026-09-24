"""
MCP server: the desk's pricing tools and the execution agent's critic, for any MCP client.

    python mcp_server.py                      # stdio, for Claude Code, Claude Desktop, Cursor, ...

The tools are the ones the in-app advisor calls, over the same cost models, so a model in
another client can price an order against the live book (or a recorded scenario book) and
then have its plan checked by the same deterministic critic the agent uses before a human
approves anything. Every tool is read-only: nothing here places, stages or changes an
order, and the server holds no keys.

`book` selects the book: "live" reads the file the app's feed writes (latest_orderbook.json,
or QTS_BOOK_FILE), and any scenario name from `list_books` uses that recorded book, which is
what the evals run on. A live book older than MAX_LIVE_AGE_S is refused rather than priced.
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from advisor.schema import validate_advice  # noqa: E402
from advisor.tools import BookTools  # noqa: E402
from agent.critic import blocking, review  # noqa: E402
from agent.pricing import plans_to_price, price_plan  # noqa: E402
from evals.scenarios import BUILTIN_BOOKS, load_book  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOK_FILE = os.environ.get("QTS_BOOK_FILE", os.path.join(BASE_DIR, "latest_orderbook.json"))
MAX_LIVE_AGE_S = 30.0
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

server = MCPServer(
    name="quant-trade-simulator",
    instructions=(
        "Pre-trade execution tools for crypto perpetuals. Price an order with quote_order before "
        "recommending anything, compare_schedule before recommending a TWAP, and pass your final "
        "plan to review_plan: it returns the desk's blocking findings, which should be fixed before "
        "a plan is shown to a trader. All tools are read-only."
    ),
)


def _book(book):
    """The named book, or the live one if it is fresh. Raises ToolError, which the client sees."""
    if book != "live":
        if book not in BUILTIN_BOOKS:
            raise ToolError(f"unknown book {book!r}; call list_books for the names")
        return load_book(book), None
    try:
        age = time.time() - os.path.getmtime(BOOK_FILE)
        with open(BOOK_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        raise ToolError("no live book: start a stream in the app (or python websocket_client.py), "
                         "or use a scenario book from list_books") from None
    if age > MAX_LIVE_AGE_S:
        raise ToolError(f"the live book is {age:.0f}s old, past the {MAX_LIVE_AGE_S:.0f}s limit; "
                         "is the feed running?")
    if not data.get("bids") or not data.get("asks"):
        raise ToolError("the live book has an empty side")
    return data, round(age, 1)


def _tools(book, fee_tier, volatility):
    data, age = _book(book)
    return BookTools(data, fee_tier=fee_tier, volatility=volatility), data, age


def _side(side):
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ToolError("side must be 'buy' or 'sell'")
    return side


def _notional(notional_usd):
    try:
        notional = float(notional_usd)
    except (TypeError, ValueError):
        notional = float("nan")
    if not math.isfinite(notional) or notional <= 0:
        raise ToolError("notional_usd must be a positive number of USD")
    return notional


def _with_source(result, book, data, age):
    return {**result, "book": book, "venue": data.get("source") or ("scenario" if book != "live" else "unknown"),
            "symbol": data.get("symbol"), **({"book_age_s": age} if age is not None else {})}


@server.tool(annotations=READ_ONLY)
def list_books() -> dict:
    """The books the other tools can read: 'live' (if the app's feed is running) and the recorded scenario books."""
    live = {"available": False}
    try:
        data, age = _book("live")
        live = {"available": True, "age_s": age, "venue": data.get("source"), "symbol": data.get("symbol")}
    except ToolError as e:
        live["reason"] = str(e)
    return {"live": live, "scenarios": sorted(BUILTIN_BOOKS)}


@server.tool(annotations=READ_ONLY)
def book_stats(book: str = "live", levels: int = 5) -> dict:
    """Top of book: bid, ask, mid, spread in bps, imbalance over `levels`, microprice and visible depth in USD."""
    tools, data, age = _tools(book, "Tier 1", 0.01)
    return _with_source(tools.get_book_stats(levels), book, data, age)


@server.tool(annotations=READ_ONLY)
def quote_order(side: str, notional_usd: float, order_type: str = "Market", book: str = "live",
                fee_tier: str = "Tier 1", volatility: float = 0.01) -> dict:
    """Walk the book for an order: fees, slippage, impact and net cost in USD and bps, fill VWAP, and complete=false when it runs past the visible book."""
    tools, data, age = _tools(book, fee_tier, volatility)
    return _with_source(tools.quote_order(_side(side), _notional(notional_usd), order_type), book, data, age)


@server.tool(annotations=READ_ONLY)
def depth_profile(side: str, levels: int = 10, book: str = "live") -> dict:
    """Cumulative USD on the side an order would take, level by level, with each level's distance from mid in bps."""
    tools, data, age = _tools(book, "Tier 1", 0.01)
    return _with_source(tools.get_depth_profile(_side(side), levels), book, data, age)


@server.tool(annotations=READ_ONLY)
def compare_schedule(side: str, notional_usd: float, slices: int, book: str = "live",
                     fee_tier: str = "Tier 1", volatility: float = 0.01) -> dict:
    """One clip against N equal slices. Assumes the book refills between slices, which validation/POSTTRADE.md shows holds on average and fails in the tail at size."""
    tools, data, age = _tools(book, fee_tier, volatility)
    return _with_source(tools.compare_schedule(_side(side), _notional(notional_usd), slices), book, data, age)


@server.tool(annotations=READ_ONLY)
def review_plan(side: str, notional_usd: float, plan: dict, book: str = "live",
                fee_tier: str = "Tier 1", volatility: float = 0.01) -> dict:
    """
    Check an execution plan the way the desk's agent does before a human sees it.

    `plan` follows the advisor's schema: strategy (immediate_market, passive_limit, twap, iceberg
    or wait), slices, horizon_seconds, expected_cost_bps, order_side, sentiment, confidence,
    urgency, analysis, reasoning, execution_approach and risks. Returns the plan as validated,
    every alternative priced against the same book, and the critic's findings; `approvable` is
    false while any finding blocks or the schema had to repair the plan.
    """
    side, notional = _side(side), _notional(notional_usd)
    data, age = _book(book)
    advice, problems = validate_advice(plan, expected_side=side)
    if advice is None:
        return _with_source({"approvable": False, "schema_errors": problems, "findings": [], "priced": []},
                            book, data, age)
    order = {"side": side, "notional": notional, "fee_tier": fee_tier, "volatility": volatility}
    priced = [price_plan(order, data, p) for p in plans_to_price(advice)]
    findings = review(order, data, advice, priced)
    # A plan the schema had to repair is not the plan that was submitted, so it cannot be
    # approved as submitted: the client resubmits the repaired `plan`.
    return _with_source({"approvable": not blocking(findings) and not problems, "findings": findings, "priced": priced,
                         "schema_repairs": problems, "plan": advice}, book, data, age)


@server.resource("qts://evals/results", mime_type="text/markdown")
def eval_results() -> str:
    """How the live models scored on the advisor evals."""
    with open(os.path.join(BASE_DIR, "evals", "RESULTS.md")) as fh:
        return fh.read()


@server.resource("qts://validation/posttrade", mime_type="text/markdown")
def posttrade_report() -> str:
    """The agent's plans scored against a recorded OKX tape."""
    with open(os.path.join(BASE_DIR, "validation", "POSTTRADE.md")) as fh:
        return fh.read()


if __name__ == "__main__":
    server.run("stdio")
