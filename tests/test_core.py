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


# ----------------------------------------------------------------------------- gemini model fallback

class _NotFound(Exception):
    code = 404


class _FakeModels:
    def __init__(self, unavailable):
        self.unavailable, self.calls = unavailable, []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model in self.unavailable:
            raise _NotFound(f"404 NOT_FOUND. models/{model} is no longer available to new users")
        return type("R", (), {"text": '{"sentiment": "Neutral", "analysis": "ok", "strategy": "Immediate market"}'})()


def _analyzer(unavailable):
    import gemini_integration as gi
    a = gi.GeminiAnalyzer.__new__(gi.GeminiAnalyzer)
    a.client = type("C", (), {"models": _FakeModels(unavailable)})()
    a.models = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    a.model, a.last_call_time, a.min_interval = a.models[0], 0, 0
    return a


def test_gemini_default_model_is_current():
    import gemini_integration as gi
    assert gi.MODEL == "gemini-3.8-flash" or os.environ.get("GEMINI_MODEL")


def test_gemini_falls_back_when_model_is_retired_and_remembers_it():
    a = _analyzer({"gemini-3.8-flash"})
    result = a.analyze(BOOK, 100, 0.08, 0.001, 0.01)
    assert result["success"] and result["model"] == "gemini-3.5-flash-lite"
    a.analyze(BOOK, 100, 0.08, 0.001, 0.01)
    assert a.client.models.calls == ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"]


def test_gemini_reports_failure_when_every_model_is_unavailable():
    result = _analyzer({"gemini-3.8-flash", "gemini-3.5-flash-lite"}).analyze(BOOK, 100, 0.08, 0.001, 0.01)
    assert not result["success"] and "NOT_FOUND" in result["analysis"]
