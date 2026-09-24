"""
The painter runs every poll. A poll whose book and order are unchanged must send only the
feed state and the ages, and anything that changes the picture must repaint all of it.
"""
import json
import os
import sys
import time

import dash
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

ORDER = (10_000, 0.017, "Tier 1", "buy", "Market")
BOOK = {"symbol": "BTC-USDT-SWAP", "source": "SIM", "local_time": time.time(),
        "asks": [[str(65000 + i), "1.5"] for i in range(1, 26)],
        "bids": [[str(65000 - i), "2"] for i in range(1, 26)]}


@pytest.fixture
def painter(tmp_path, monkeypatch):
    book_file = tmp_path / "latest_orderbook.json"
    monkeypatch.setattr(app, "ORDERBOOK_FILE", str(book_file))
    monkeypatch.setattr(app, "STATUS_FILE", str(tmp_path / "feed_status.json"))
    monkeypatch.setattr(app, "FEED_FILE", str(tmp_path / "feed.json"))
    monkeypatch.setattr(app, "read_feed", lambda: {"pid": os.getpid(), "symbol": "BTC-USDT-SWAP",
                                                   "exchange": "SIM", "started": 1.0})
    for name, value in (("orderbook_data", None), ("data_last_modified", 0.0),
                        ("update_count", 0), ("feed_started", None)):
        monkeypatch.setattr(app, name, value)

    def write_book(**changes):
        book_file.write_text(json.dumps({**BOOK, **changes}))
        mtime = time.time() + app.update_count + 1  # a strictly newer file each time
        os.utime(book_file, (mtime, mtime))

    def paint(order=ORDER, painted=None):
        return dict(zip(app.PAINT_KEYS, app.update_tables(1, *order, painted)))

    return write_book, paint


def sent(out):
    return {k for k, v in out.items() if v is not dash.no_update}


def test_full_paint_returns_every_output(painter):
    write_book, paint = painter
    write_book()
    out = paint()
    assert sent(out) == set(app.PAINT_KEYS)
    assert out["depth-chart.figure"]["data"], "the depth chart must carry the book"


def test_unchanged_poll_sends_only_the_clock(painter):
    write_book, paint = painter
    write_book()
    key = paint()["painted.data"]
    out = paint(painted=json.loads(json.dumps(key)))  # as it comes back from the browser
    assert "depth-chart.figure" not in sent(out)
    assert "asks-table.children" not in sent(out)
    assert "netcost-value.children" not in sent(out)
    assert {"feed-pill.className", "update-time.children", "hdr-age.children", "feed-line.data"} <= sent(out)


def test_new_book_repaints(painter):
    write_book, paint = painter
    write_book()
    key = paint()["painted.data"]
    write_book(asks=[[str(65100 + i), "1"] for i in range(1, 26)])
    assert sent(paint(painted=key)) == set(app.PAINT_KEYS)


def test_changed_order_repaints(painter):
    write_book, paint = painter
    write_book()
    key = paint()["painted.data"]
    assert sent(paint(order=(50_000,) + ORDER[1:], painted=key)) == set(app.PAINT_KEYS)


def test_idle_desk_sends_only_the_clock_after_the_first_paint(painter, monkeypatch):
    _, paint = painter
    monkeypatch.setattr(app, "read_feed", lambda: None)
    first = paint()
    assert sent(first) == set(app.PAINT_KEYS)
    assert "depth-chart.figure" not in sent(paint(painted=first["painted.data"]))


def test_paint_only_rejects_unknown_outputs():
    with pytest.raises(KeyError):
        app.paint_only({"no-such.children": 1})
