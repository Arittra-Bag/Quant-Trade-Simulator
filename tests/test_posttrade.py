"""
Post-trade scoring on synthetic recordings whose answer is known: a book that never
changes must score a refill error of zero, a book that thins must score a positive one,
and the passive fill must follow the tape's volume at our price and the queue ahead of it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validation.posttrade import main, score, summarise  # noqa: E402

MID = 65_000.0


def _book(ts, size=2.0, levels=25, tick=0.5, mid=MID):
    return {"type": "book", "ts": ts,
            "bids": [[mid - tick * (i + 1), size] for i in range(levels)],
            "asks": [[mid + tick * (i + 1), size] for i in range(levels)]}


def _trade(ts, px, sz, side):
    return {"type": "trade", "ts": ts, "px": px, "sz": sz, "side": side, "id": str(ts)}


def _books(sizes, step=1.0, **kw):
    return [_book(i * step, size, **kw) for i, size in enumerate(sizes)]


def test_a_book_that_never_changes_has_no_refill_error():
    scored = score(_books([2.0] * 121), [], notional=250_000, horizon=60, slices=4)
    assert scored["windows"] == 2 and len(scored["twap"]) == 4  # two windows, both sides
    for s in scored["twap"]:
        assert abs(s["liquidity_bps"] - s["predicted_bps"]) < 1e-3
        assert abs(s["arrival_bps"] - s["liquidity_bps"]) < 1e-3  # no drift either
        assert s["schedule_bps"] >= s["predicted_bps"]  # compare_schedule adds permanent impact


def test_a_book_that_thins_after_the_first_slice_costs_more_than_predicted():
    sizes = [2.0] + [0.5] * 60  # depth falls to a quarter once the first slice has gone
    summary = summarise(score(_books(sizes), [], notional=250_000, horizon=60, slices=4, sides=("buy",)))
    assert summary["twap"]["refill_error_bps"]["median"] > 0


def test_drift_shows_in_the_arrival_score_but_not_the_liquidity_score():
    books = [_book(i, mid=MID + 5 * i) for i in range(61)]  # the price rises under a buyer
    s = score(books, [], notional=100_000, horizon=60, slices=4, sides=("buy",))["twap"][0]
    assert abs(s["liquidity_bps"] - s["predicted_bps"]) < 1e-3
    assert s["arrival_bps"] > s["liquidity_bps"]


def _passive(trades, notional=100_000):
    scored = score(_books([2.0] * 61), trades, notional=notional, horizon=60, slices=4, sides=("buy",))
    return scored["passive"][0]


def test_no_tape_means_no_passive_fill():
    s = _passive([])
    assert s["realised_fill"] == 0.0 and 0 < s["predicted_maker"] <= 1


def test_volume_at_our_price_fills_us_only_after_the_queue_ahead():
    bid = MID - 0.5
    queue = bid * 2.0  # the touch holds 2 BTC ahead of us
    s = _passive([_trade(10, bid, 2.0 + 50_000 / bid, "sell")])  # the queue, then $50k more
    assert abs(s["realised_fill"] - 0.5) < 1e-6 and queue > 0


def test_a_trade_through_our_price_fills_us_completely():
    s = _passive([_trade(10, MID - 1.0, 0.001, "sell")])
    assert s["realised_fill"] == 1.0
    assert s["realised_bps"] < _passive([])["realised_bps"]  # resting and filling beats crossing


def test_buyers_do_not_fill_a_resting_buy():
    assert _passive([_trade(10, MID - 0.5, 100.0, "buy")])["realised_fill"] == 0.0


def test_cli_writes_the_report(tmp_path):
    import json
    path = tmp_path / "tape.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _books([2.0] * 121) + [_trade(5, MID - 1, 0.1, "sell")]))
    report = tmp_path / "POSTTRADE.md"
    assert main([str(path), "--notional", "250000,2500000", "--report", str(report)]) == 0
    text = report.read_text()
    assert "Refill error" in text and "Filled from the tape" in text and "2 non-overlapping windows" in text
    assert "## $250,000 per order" in text and "## $2,500,000 per order" in text


def test_an_empty_recording_scores_nothing(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert main([str(path), "--report", ""]) == 1
