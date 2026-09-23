"""Offline tests for the feed parsers, cost models and exports. Run: python -m pytest -q"""
import asyncio
import base64
import io
import json
import os
import sys

import pandas as pd
import pytest
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websocket_client as wc  # noqa: E402
from export import export_orderbook_to_csv, export_orderbook_to_excel  # noqa: E402
from models import (book_stats, estimate_market_impact, estimate_slippage,  # noqa: E402
                    predict_maker_taker, walk_book)

BOOK = {
    "bids": [[65000.0, 0.5], [64999.0, 1.0], [64998.0, 2.0]],
    "asks": [[65001.0, 0.5], [65002.0, 1.0], [65003.0, 2.0]],
    "timestamp": 1,
}

OKX_MSG = {"arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
           "data": [{"asks": [["65001.0", "50", "0", "3"], ["65002.0", "100", "0", "4"]],
                     "bids": [["65000.0", "40", "0", "2"], ["64999.0", "80", "0", "5"]],
                     "ts": "1758650000000", "seqId": 1}]}


# ----------------------------------------------------------------------------- parsers

def test_okx_books5_converts_contracts_to_base():
    book = wc.validate_book(wc.OKXVenue("BTC-USDT-SWAP", ct_val=0.01).parse(OKX_MSG))
    assert book["asks"][0] == [65001.0, 0.5]
    assert book["bids"][0] == [65000.0, 0.4]
    assert book["timestamp"] == 1758650000000


def test_okx_ignores_non_book_messages():
    venue = wc.OKXVenue("BTC-USDT-SWAP", ct_val=0.01)
    assert venue.parse({"event": "subscribe", "arg": {"channel": "books5"}}) is None
    assert venue.subscribe_message()["args"][0]["channel"] == "books5"


def test_binance_depth20_uses_b_and_a():
    v = wc.BinanceVenue("BTC-USDT-SWAP")
    assert v.url.endswith("/btcusdt@depth20@100ms")
    book = wc.validate_book(v.parse({"e": "depthUpdate", "T": 5, "b": [["100", "1"]], "a": [["101", "2"]]}))
    assert book["bids"] == [[100.0, 1.0]] and book["asks"] == [[101.0, 2.0]] and book["timestamp"] == 5


def test_hyperliquid_l2book():
    msg = {"channel": "l2Book", "data": {"coin": "BTC", "time": 7, "levels": [
        [{"px": "100", "sz": "1", "n": 1}], [{"px": "101", "sz": "2", "n": 1}]]}}
    book = wc.validate_book(wc.HyperliquidVenue("BTC-USDT-SWAP").parse(msg))
    assert book["bids"] == [[100.0, 1.0]] and book["asks"] == [[101.0, 2.0]]


def test_kraken_snapshot_then_delta():
    v = wc.KrakenVenue("BTC-USDT-SWAP")
    assert v.venue_symbol == "BTC/USD"
    v.parse({"channel": "book", "type": "snapshot", "data": [{
        "bids": [{"price": 100, "qty": 1}, {"price": 99, "qty": 1}],
        "asks": [{"price": 101, "qty": 1}]}]})
    book = v.parse({"channel": "book", "type": "update", "data": [{
        "bids": [{"price": 100, "qty": 0}], "asks": [{"price": 102, "qty": 3}]}]})
    assert book["bids"] == [[99, 1]]
    assert book["asks"] == [[101, 1], [102, 3]]


def test_validate_rejects_crossed_and_empty_books():
    assert wc.validate_book({"bids": [[101, 1]], "asks": [[100, 1]], "timestamp": 1}) is None
    assert wc.validate_book({"bids": [], "asks": [[100, 1]], "timestamp": 1}) is None


def test_venue_order_puts_requested_first_and_never_falls_back_to_sim():
    assert wc.venue_order("KRAKEN")[0] == "KRAKEN"
    assert "SIM" not in wc.venue_order("OKX")


# ----------------------------------------------------------------------------- models

def test_walk_book_vwap_and_levels():
    fill = walk_book(BOOK, 65001.0 * 0.5 + 65002.0 * 0.25)
    assert fill["levels"] == 2 and fill["complete"]
    assert 65001.0 < fill["vwap"] < 65002.0


def test_walk_book_marks_orders_larger_than_visible_depth():
    assert walk_book(BOOK, 10_000_000)["complete"] is False


@pytest.mark.parametrize("q", [1, 10, 100, 1_000, 100_000, 1_000_000])
def test_slippage_is_never_negative(q):
    assert estimate_slippage(BOOK, q, 0.01) >= 0
    assert estimate_slippage(None, q, 0.001) >= 0


def test_slippage_grows_with_size_and_side_uses_bids():
    small, big = estimate_slippage(BOOK, 100), estimate_slippage(BOOK, 200_000)
    assert big / 200_000 > small / 100
    assert estimate_slippage(BOOK, 100, side="sell") == pytest.approx(0.5 / 65000.5 * 100)


def test_market_orders_are_always_taker():
    assert predict_maker_taker(BOOK, 1) == 0.0
    assert 0 < predict_maker_taker(BOOK, 1_000, order_type="Limit") < 1


def test_impact_scales_with_volatility_and_size():
    assert estimate_market_impact(BOOK, 1_000, 0.02) > estimate_market_impact(BOOK, 1_000, 0.01) > 0
    assert estimate_market_impact(BOOK, 10_000, 0.01) > estimate_market_impact(BOOK, 1_000, 0.01)


def test_book_stats():
    s = book_stats(BOOK)
    assert s["mid"] == 65000.5 and s["spread"] == 1.0
    assert s["spread_bps"] == pytest.approx(1 / 65000.5 * 1e4)


# ----------------------------------------------------------------------------- exports

def test_excel_headers_are_column_names():
    raw = base64.b64decode(export_orderbook_to_excel({**BOOK, "symbol": "BTC-USDT-SWAP", "source": "OKX"}))
    sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None)
    assert list(sheets["Bids"].columns) == ["price", "size", "type"]
    assert list(sheets["Metadata"].columns) == ["Property", "Value"]


def test_csv_export_round_trip():
    df = pd.read_csv(io.StringIO(base64.b64decode(export_orderbook_to_csv({**BOOK, "symbol": "X"})).decode()))
    assert len(df) == 6 and set(df["type"]) == {"bid", "ask"}


# ----------------------------------------------------------------------------- live client against local servers

async def _serve(handler):
    return await websockets.serve(handler, "127.0.0.1", 0)


def _port(server):
    return next(iter(server.sockets)).getsockname()[1]


def test_client_writes_book_and_status_from_okx(tmp_path, monkeypatch):
    async def okx(ws):
        await ws.recv()  # subscription
        while True:
            await ws.send(json.dumps(OKX_MSG))
            await asyncio.sleep(0.05)

    async def run():
        server = await _serve(okx)
        monkeypatch.setenv("ORDERBOOK_WS_URL_OKX", f"ws://127.0.0.1:{_port(server)}")
        monkeypatch.setattr(wc, "okx_contract_value", lambda s: 0.01)
        out = tmp_path / "book.json"
        task = asyncio.create_task(wc.connect_and_save("BTC-USDT-SWAP", str(out), 0.0, "OKX"))
        await asyncio.sleep(1.0)
        task.cancel()
        server.close()
        return out

    out = asyncio.run(run())
    book = json.loads(out.read_text())
    status = json.loads((tmp_path / "feed_status.json").read_text())
    assert book["source"] == "OKX" and book["symbol"] == "BTC-USDT-SWAP" and book["bids"][0] == [65000.0, 0.4]
    assert status["state"] == "live" and status["source"] == "OKX"


def test_client_falls_back_when_venue_accepts_but_sends_nothing(tmp_path, monkeypatch):
    async def silent(ws):
        await asyncio.sleep(60)

    async def hyperliquid(ws):
        await ws.recv()
        while True:
            await ws.send(json.dumps({"channel": "l2Book", "data": {"time": 1, "levels": [
                [{"px": "100", "sz": "1"}], [{"px": "101", "sz": "1"}]]}}))
            await asyncio.sleep(0.05)

    async def run():
        s1, s2 = await _serve(silent), await _serve(hyperliquid)
        monkeypatch.setenv("ORDERBOOK_WS_URL_OKX", f"ws://127.0.0.1:{_port(s1)}")
        monkeypatch.setenv("ORDERBOOK_WS_URL_HYPERLIQUID", f"ws://127.0.0.1:{_port(s2)}")
        monkeypatch.setattr(wc, "okx_contract_value", lambda s: 0.01)
        monkeypatch.setattr(wc, "FIRST_BOOK_TIMEOUT", 0.5)
        out = tmp_path / "book.json"
        task = asyncio.create_task(wc.connect_and_save("BTC-USDT-SWAP", str(out), 0.0, "OKX"))
        await asyncio.sleep(2.0)
        task.cancel()
        s1.close()
        s2.close()
        return out

    out = asyncio.run(run())
    assert json.loads(out.read_text())["source"] == "HYPERLIQUID"


# ------------------------------------------------------------------- advisor adapter (gemini_integration)
# The model fallback, the tool loop and the output schema are covered in tests/test_advisor.py.
# What is checked here is only the seam the Dash app calls through.

def test_adapter_falls_back_to_the_baseline_without_a_key():
    import gemini_integration as gi
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = None, ["m"], "m", 0
    result = analyzer.analyze(BOOK, 100, side="buy", fee_tier="Tier 1", volatility=0.01)
    assert result["success"] and result["source"] == "baseline"
    assert result["tool_calls"][:2] == ["get_book_stats", "quote_order"]


def test_adapter_advises_on_the_side_it_was_given():
    import gemini_integration as gi
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = None, ["m"], "m", 0
    result = analyzer.analyze(BOOK, 100, side="sell")
    assert result["order_side"] == "sell"
    assert "sell" in result["execution_approach"].lower()
    quote = next(c for c in result["tool_trace"] if c["name"] == "quote_order")
    assert quote["args"]["side"] == "sell"


def test_adapter_reports_a_missing_book_without_raising():
    import gemini_integration as gi
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = None, ["m"], "m", 0
    assert analyzer.analyze({}, 100)["success"] is False


def test_adapter_keeps_the_legacy_wrapper_shape():
    import gemini_integration as gi
    analyzer = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    analyzer.client, analyzer.models, analyzer.model, analyzer.min_interval = None, ["m"], "m", 0
    result = analyzer.get_trading_strategy(BOOK, 100, 0.08, 0.001, 0.01)
    assert {"strategy", "reasoning", "execution_approach"} <= set(result)


def test_default_model_is_current():
    import gemini_integration as gi
    assert gi.MODEL == "gemini-3.8-flash" or os.environ.get("GEMINI_MODEL")
