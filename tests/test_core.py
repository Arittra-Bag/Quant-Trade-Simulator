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
from fee_model import calculate_fees, fee_rates  # noqa: E402
from models import (VolatilityTracker, book_stats, estimate_costs,  # noqa: E402
                    estimate_market_impact, estimate_slippage, measure_volatility,
                    predict_maker_taker, timing_risk, walk_book)
from validation import record as rec  # noqa: E402
from validation.validate import build_samples, evaluate, fit_permanent_share  # noqa: E402

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
    fill = walk_book(BOOK, 10_000_000)
    assert fill["complete"] is False and fill["extrapolated"] is True
    assert fill["residual_usd"] == pytest.approx(10_000_000 - fill["visible_usd"])


def test_orders_past_the_book_are_not_priced_at_the_last_level():
    """The old model filled everything past the visible book at the last visible price,
    so the residual was free. Every unit past the book must now cost more than the one
    before it, and the fill must land beyond the last level we can see."""
    old_way = walk_book(BOOK, 1_000_000, extrapolate=False)
    new_way = walk_book(BOOK, 1_000_000)
    assert new_way["vwap"] > old_way["vwap"] > BOOK["asks"][-1][0] * 0.999
    assert new_way["vwap"] > BOOK["asks"][-1][0]
    assert new_way["slippage_bps"] > 2 * old_way["slippage_bps"]


def test_cost_per_dollar_rises_with_size_on_a_realistic_book():
    """A 25-level book with a 1 USD tick: cost in bps has to grow with the order, and a
    1M sweep has to land in single-digit bps rather than the sub-bp the old walk gave."""
    mid = 65_000.0
    book = {"symbol": "BTC-USDT-SWAP", "source": "OKX",
            "asks": [[mid + 0.5 + i, 0.2 + 0.04 * i] for i in range(25)],
            "bids": [[mid - 0.5 - i, 0.2 + 0.04 * i] for i in range(25)]}
    bps = [estimate_costs(book, q)["slippage_bps"] for q in (10_000, 100_000, 1_000_000, 10_000_000)]
    assert bps == sorted(bps)
    assert 1.0 < bps[2] < 10.0


def test_extrapolated_cost_grows_with_the_residual():
    a = walk_book(BOOK, 1_000_000)["slippage_bps"]
    b = walk_book(BOOK, 5_000_000)["slippage_bps"]
    assert b > a > 0


def test_walk_book_extrapolation_can_be_switched_off():
    off = walk_book(BOOK, 1_000_000, extrapolate=False)
    assert off["extrapolated"] is False
    assert off["vwap"] < walk_book(BOOK, 1_000_000)["vwap"]


@pytest.mark.parametrize("q", [1, 10, 100, 1_000, 100_000, 1_000_000])
def test_slippage_is_never_negative(q):
    assert estimate_slippage(BOOK, q, 0.01) >= 0


def test_slippage_without_a_book_is_zero_not_a_guess():
    assert estimate_slippage(None, 1_000) == 0.0
    assert estimate_slippage({"bids": [], "asks": []}, 1_000) == 0.0


def test_slippage_grows_with_size_and_side_uses_bids():
    small, big = estimate_slippage(BOOK, 100), estimate_slippage(BOOK, 200_000)
    assert big / 200_000 > small / 100
    assert estimate_slippage(BOOK, 100, side="sell") == pytest.approx(0.5 / 65000.5 * 100)


def test_market_orders_are_always_taker():
    assert predict_maker_taker(BOOK, 1) == 0.0
    assert 0 < predict_maker_taker(BOOK, 1_000, order_type="Limit") < 1


def test_impact_is_a_share_of_the_book_displacement():
    q = 100_000
    end_bps = walk_book(BOOK, q)["end_bps"]
    impact = estimate_market_impact(BOOK, q, 0.01)
    assert impact == pytest.approx(0.4 * end_bps / 1e4 * q)
    assert impact < estimate_slippage(BOOK, q) * 2


def test_impact_grows_with_size_and_sqrt_model_scales_with_volatility():
    assert estimate_market_impact(BOOK, 100_000, 0.01) > estimate_market_impact(BOOK, 1_000, 0.01) > 0
    hi = estimate_market_impact(BOOK, 1_000, 0.02, model="sqrt")
    lo = estimate_market_impact(BOOK, 1_000, 0.01, model="sqrt")
    assert hi == pytest.approx(2 * lo) and lo > 0


def test_impact_no_longer_dwarfs_the_fill_cost():
    """The symptom that started this: a 1M market buy showing 0.25 bps of slippage
    beside 320 bps of impact. Impact is now the permanent residue of the same walk."""
    c = estimate_costs(BOOK, 1_000_000)
    assert c["impact_bps"] < c["slippage_bps"]
    assert c["net_bps"] == pytest.approx(c["slippage_bps"] + c["fees_bps"] + c["impact_bps"])


def test_costs_break_down_into_spread_and_depth():
    c = estimate_costs(BOOK, 100_000)
    assert c["spread_usd"] > 0 and c["depth_usd"] > 0
    assert c["slippage_usd"] == pytest.approx(c["spread_usd"] + c["depth_usd"])


def test_costs_flag_what_is_assumed():
    assumptions = " ".join(estimate_costs(BOOK, 1_000_000)["assumptions"])
    assert "past the visible book" in assumptions and "permanent impact" in assumptions


def test_costs_need_a_book():
    assert estimate_costs(None, 1_000) is None
    assert estimate_costs(BOOK, 0) is None


def test_timing_risk_scales_with_sigma_and_horizon():
    assert timing_risk(1_000, 0.02, 1) == pytest.approx(2 * timing_risk(1_000, 0.01, 1))
    assert timing_risk(1_000, 0.01, 4) == pytest.approx(2 * timing_risk(1_000, 0.01, 1))


def test_book_stats():
    s = book_stats(BOOK)
    assert s["mid"] == 65000.5 and s["spread"] == 1.0
    assert s["spread_bps"] == pytest.approx(1 / 65000.5 * 1e4)


# ------------------------------------------------------------------- measured volatility

def test_volatility_tracker_recovers_a_known_sigma():
    """Feed a 1% daily vol random walk sampled every second and read it back."""
    import math
    import random

    rng = random.Random(7)
    sigma_daily = 0.01
    step = sigma_daily / math.sqrt(86_400)          # per-second sigma
    tracker = VolatilityTracker(half_life_s=600.0, min_samples=20)
    price, ts = 65_000.0, 0.0
    for _ in range(4_000):
        ts += 1.0
        price *= math.exp(rng.gauss(0.0, step))
        tracker.update(price, ts)
    assert tracker.ready
    assert 0.5 * sigma_daily < tracker.sigma_daily < 2.0 * sigma_daily
    assert tracker.sigma_annual == pytest.approx(tracker.sigma_daily * math.sqrt(365))


def test_volatility_tracker_ignores_bad_samples():
    t = VolatilityTracker(min_samples=1)
    assert t.update(0, 1) is None and t.update(None, 2) is None
    t.update(100.0, 10.0)
    t.update(101.0, 10.0)        # same timestamp
    assert not t.ready
    t.update(101.0, 1_000_000.0)  # gap far beyond max_gap_s
    assert not t.ready


def test_measure_volatility_reports_its_source():
    tracker = VolatilityTracker(min_samples=3, half_life_s=10.0)
    sigma, source = measure_volatility(BOOK, fallback=0.02, tracker=tracker)
    assert (sigma, source) == (0.02, "assumed")
    for i in range(1, 8):
        book = {"bids": [[65000.0 + i, 1.0]], "asks": [[65001.0 + i, 1.0]], "timestamp": 1000 * i}
        sigma, source = measure_volatility(book, fallback=0.02, tracker=tracker)
    assert source == "measured" and sigma > 0


# ------------------------------------------------------------------------------- fees

def test_fees_differ_by_venue_and_tier():
    assert fee_rates("HYPERLIQUID", "Tier 1") != fee_rates("OKX", "Tier 1")
    assert calculate_fees(10_000, "Tier 3", "OKX") < calculate_fees(10_000, "Tier 1", "OKX")


def test_maker_orders_pay_the_maker_rate():
    maker, taker = fee_rates("OKX", "Tier 1")
    assert maker < taker
    assert calculate_fees(10_000, "Tier 1", "OKX", maker_fraction=1.0) == pytest.approx(10_000 * maker)
    assert calculate_fees(10_000, "Tier 1", "OKX", maker_fraction=0.5) == pytest.approx(
        10_000 * (maker + taker) / 2)


def test_unknown_venue_falls_back_to_okx():
    assert fee_rates("NOT-A-VENUE", "Tier 1") == fee_rates("OKX", "Tier 1")
    assert calculate_fees(1_000, "Tier 9", "OKX") == pytest.approx(1_000 * fee_rates("OKX")[1])


def test_market_order_pays_taker_and_is_charged_the_streaming_venue():
    c = estimate_costs(dict(BOOK, source="HYPERLIQUID"), 10_000)
    assert c["venue"] == "HYPERLIQUID"
    assert c["fees_usd"] == pytest.approx(10_000 * fee_rates("HYPERLIQUID", "Tier 1")[1])


# ------------------------------------------------------------------------- validation

def _recording():
    """Two books a second apart, with a burst of buy trades in between."""
    book = lambda ts, shift: {"type": "book", "ts": ts,
                              "bids": [[65000.0 + shift, 2.0], [64999.0 + shift, 4.0]],
                              "asks": [[65001.0 + shift, 2.0], [65002.0 + shift, 4.0]]}
    books = [book(0.0, 0.0), book(2.0, 1.0)]
    trades = [{"type": "trade", "ts": 0.5, "px": 65001.0, "sz": 1.0, "side": "buy"},
              {"type": "trade", "ts": 1.0, "px": 65002.0, "sz": 1.0, "side": "buy"},
              {"type": "trade", "ts": 1.5, "px": 65000.0, "sz": 1.0, "side": "sell"}]
    return books, trades


def test_validation_pairs_books_with_the_trades_that_followed():
    samples = build_samples(*_recording(), window_s=2.0, min_usd=100.0)
    assert len(samples) == 1
    s = samples[0]
    assert s["trades"] == 2                      # the sell is not in a buy sample
    assert s["notional_usd"] == pytest.approx(65001.0 + 65002.0)
    assert s["realised_bps"] > 0 and s["predicted_bps"] > 0
    assert s["error_bps"] == pytest.approx(s["predicted_bps"] - s["realised_bps"])
    assert s["realised_perm_bps"] == pytest.approx(1.0 / 65000.5 * 1e4)


def test_validation_report_stats_and_permanent_share():
    result = evaluate(build_samples(*_recording(), window_s=2.0, min_usd=100.0))
    assert result["n"] == 1
    assert result["error_bps"]["median"] == pytest.approx(result["error_bps"]["mean"])
    assert result["permanent_share"] is None          # one sample cannot fit a slope
    assert result["buckets"][0]["n"] == 1


def test_permanent_share_fit_recovers_a_known_slope_and_flags_noise():
    clean = [{"predicted_end_bps": x, "realised_perm_bps": 0.5 * x} for x in range(1, 40)]
    fit = fit_permanent_share(clean)
    assert fit["slope"] == pytest.approx(0.5) and abs(fit["t"]) > 2

    import random
    rng = random.Random(3)
    noisy = [{"predicted_end_bps": 1.0, "realised_perm_bps": rng.gauss(0, 5)} for _ in range(200)]
    assert abs(fit_permanent_share(noisy)["t"]) < 2


def test_permanent_share_is_rejected_when_the_trades_never_left_the_touch():
    """A real 10-minute BTC recording fitted a share of 2.6: 400k rests at the touch, so
    nothing walked the book and the fit was dividing mid drift by almost no displacement.
    A significant t-statistic is not enough to trust it."""
    import random
    rng = random.Random(5)
    flat = [{"predicted_end_bps": 0.01, "realised_perm_bps": 0.026 + rng.gauss(0, 0.001)}
            for _ in range(250)]
    fit = fit_permanent_share(flat)
    assert abs(fit["t"]) > 2 and fit["usable"] is False
    assert fit["x_median_bps"] < 0.5


def test_permanent_share_is_rejected_when_more_persists_than_the_trade_caused():
    over = [{"predicted_end_bps": x, "realised_perm_bps": 2.5 * x} for x in range(1, 40)]
    assert fit_permanent_share(over)["usable"] is False


def test_permanent_share_is_usable_when_the_book_was_actually_walked():
    good = [{"predicted_end_bps": x, "realised_perm_bps": 0.45 * x} for x in range(1, 40)]
    fit = fit_permanent_share(good)
    assert fit["usable"] is True and fit["slope"] == pytest.approx(0.45)


def test_recorder_sends_a_user_agent():
    """OKX answers urllib's default agent with 403, which made the recorder write an
    empty file and read as a quiet market rather than as a failure."""
    import io
    import urllib.request

    seen = {}

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def fake_urlopen(req, timeout=None, context=None):
        seen["ua"] = req.get_header("User-agent")
        return FakeResponse(b'{"data": []}')

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        rec._get("https://example.invalid/whatever")
    finally:
        urllib.request.urlopen = original
    assert seen["ua"] == rec.USER_AGENT and "urllib" not in seen["ua"]


def test_an_unverifiable_certificate_is_retried_against_certifi(monkeypatch):
    """python.org builds for macOS fail every request until pointed at a CA bundle."""
    import io
    import ssl
    import urllib.error
    import urllib.request

    import websocket_client as wc
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(context)
        if context is None:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        return io.BytesIO(b'{"data": [{"ctVal": "0.01"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert wc.okx_contract_value("BTC-USDT-SWAP") == 0.01
    assert calls[0] is None and isinstance(calls[1], ssl.SSLContext)


def test_contract_value_lookup_sends_a_real_user_agent(monkeypatch):
    """OKX answers urllib's default agent with 403, which silently fell back to a default."""
    import io
    import urllib.request

    import websocket_client as wc
    agents = []

    def fake_urlopen(req, timeout=None, context=None):
        agents.append(req.get_header("User-agent"))
        return io.BytesIO(b'{"data": [{"ctVal": "0.1"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert wc.okx_contract_value("ETH-USDT-SWAP") == 0.1 and agents == [wc.USER_AGENT]


def test_the_recorder_stops_when_every_request_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "fetch_book", lambda *a: (_ for _ in ()).throw(OSError("unreachable")))
    monkeypatch.setattr(rec, "contract_value", lambda s: 0.01)
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="10 requests in a row failed"):
        rec.record(minutes=60, out=str(tmp_path / "x.jsonl"))


def test_validation_ignores_windows_with_no_trades():
    books, trades = _recording()
    assert build_samples(books, [t for t in trades if t["side"] == "sell"], 2.0, 100.0) == []
    assert build_samples(books, trades, 2.0, min_usd=10_000_000) == []


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
    """Lite leads (fastest, fewest tokens, no worse on the live eval); 3.8 Flash is the fallback."""
    import gemini_integration as gi
    assert gi.MODEL == "gemini-3.5-flash-lite" or os.environ.get("GEMINI_MODEL")
    assert "gemini-3.8-flash" in gi.FALLBACK_MODELS or os.environ.get("GEMINI_FALLBACK_MODELS")


# --------------------------------------------------------------------------- UI formatting
def test_money_precision_follows_magnitude():
    """A cost tile is one sixth of a column; a fixed 4dp truncated large numbers on screen."""
    from app import usd
    assert usd(0) == "$0.00"
    assert usd(0.0008) == "$0.000800"
    assert usd(0.5) == "$0.5000"
    assert usd(800) == "$800.00"
    assert usd(2652.3588) == "$2,652.36"
    assert usd(32039.45) == "$32,039"
    assert usd(250_513_734.57) == "$250.51M"
    assert usd(2_505_137_345.7) == "$2.51B"
    assert usd(-2652.36) == "$-2,652.36"


@pytest.mark.parametrize("v", [0, 0.004, 1, 999.99, 1e4, 1e6, 1e9, 1e12])
def test_money_never_outgrows_a_tile(v):
    from app import usd
    assert len(usd(v)) <= 12


def test_age_string_units():
    from app import age_str
    assert age_str(0.192) == "192ms"
    assert age_str(2.6) == "2.6s"
    assert age_str(90) == "1m 30s"
    assert age_str(180) == "3m 00s"
    assert age_str(3725) == "1h 02m"


def test_raw_book_marks_the_cut():
    from app import RAW_BOOK_CHARS, raw_book_text
    small = {"bids": [[1, 1]], "asks": [[2, 1]]}
    assert raw_book_text(small) == json.dumps(small, indent=1)
    big = {"bids": [[i, 1] for i in range(4000)], "asks": [[1, 1]]}
    out = raw_book_text(big)
    assert "truncated" in out and len(out) < RAW_BOOK_CHARS + 80


# --------------------------------------------------------------------------- feed freshness
def test_book_freshness_reports_what_the_screen_can_be_trusted_to_show():
    import time as _t

    from app import book_freshness
    fresh = {"local_time": _t.time()}
    old = {"local_time": _t.time() - 90}
    assert book_freshness(None, "idle")[0] == "none"
    assert book_freshness(fresh, "live")[0] == "fresh"
    assert book_freshness(old, "live")[0] == "stale"
    state, note = book_freshness(old, "idle")
    assert state == "frozen" and "1m 30s" in note
    state, note = book_freshness(old, "down")
    assert state == "frozen" and "offline" in note.lower()


# --------------------------------------------------------------------------- one book per paint
def test_tiles_and_figures_are_built_from_one_computation():
    """
    The figures used to run their own interval and their own compute(), so the cost stack and
    the impact tile disagreed on screen. They must come from a single callback.
    """
    import app as app_module
    outputs = [o.component_id for cb in app_module.app.callback_map.values()
               for o in (cb["output"] if isinstance(cb["output"], list) else [cb["output"]])]
    painter = [cb for cb in app_module.app.callback_map.values()
               if any(getattr(o, "component_id", None) == "netcost-value"
                      for o in (cb["output"] if isinstance(cb["output"], list) else [cb["output"]]))]
    assert len(painter) == 1, "the tiles must be painted by exactly one callback"
    painter_outputs = {o.component_id for o in painter[0]["output"]}
    for figure_id in ("depth-chart", "cost-breakdown-chart", "latency-chart"):
        assert figure_id in painter_outputs, f"{figure_id} must be returned beside the tiles"
    assert outputs.count("depth-chart") == 1
