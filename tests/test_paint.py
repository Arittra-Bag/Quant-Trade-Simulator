"""
The painter runs every poll. A poll whose book and order are unchanged must send only the
feed state and the ages, and anything that changes the picture must repaint all of it.
"""
import json
import os
import sys
import time

from collections import deque

import dash
import pytest

import models

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
    for name, value in (("orderbook_data", None), ("data_last_modified", 0.0), ("update_count", 0),
                        ("feed_started", None), ("calc_latency_us", deque(maxlen=300)),
                        ("WATCH_BOOKS", False)):
        monkeypatch.setattr(app, name, value)
    monkeypatch.setattr(models, "_tracker", models.VolatilityTracker())

    def write_book(**changes):
        book_file.write_text(json.dumps({**BOOK, **changes}))
        mtime = time.time() + app.update_count + 1  # a strictly newer file each time
        os.utime(book_file, (mtime, mtime))

    def paint(order=ORDER, painted=None):
        return dict(zip(app.PAINT_KEYS, app.update_tables(1, *order, painted), strict=True))

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
    assert sent(paint(order=(50_000, *ORDER[1:]), painted=key)) == set(app.PAINT_KEYS)


def test_idle_desk_sends_only_the_clock_after_the_first_paint(painter, monkeypatch):
    _, paint = painter
    monkeypatch.setattr(app, "read_feed", lambda: None)
    first = paint()
    assert sent(first) == set(app.PAINT_KEYS)
    assert "depth-chart.figure" not in sent(paint(painted=first["painted.data"]))


def test_paint_only_rejects_unknown_outputs():
    with pytest.raises(KeyError):
        app.paint_only({"no-such.children": 1})


def test_a_failed_paint_still_answers(painter, monkeypatch):
    """The browser waits for feed-line before its next poll, so the painter must not raise."""
    write_book, paint = painter
    write_book()

    def broken(*_):
        raise ValueError("bad book")

    monkeypatch.setattr(app, "compute", broken)
    out = paint()
    assert sent(out) == {"feed-line.data", "painted.data"}
    assert out["painted.data"] is None and "Retrying" in out["feed-line.data"]["text"]


def test_paint_all_requires_every_output():
    with pytest.raises(KeyError):
        app.paint_all({"feed-line.data": {}})


def test_watcher_takes_in_books_with_no_browser_polling(painter, monkeypatch):
    """The volatility estimate must keep up while every tab is hidden or closed."""
    write_book, _ = painter
    feed = app.read_feed()
    running = [True]
    monkeypatch.setattr(app, "running_feed", lambda: feed if running[0] else None)
    monkeypatch.setattr(app, "WATCH_BOOKS", True)
    monkeypatch.setattr(app, "WATCH_SECONDS", 0.01)
    monkeypatch.setattr(app, "_watcher", None)
    write_book(timestamp=1_000)
    app.ensure_book_watcher()
    deadline = time.time() + 5
    while app.update_count == 0 and time.time() < deadline:
        time.sleep(0.01)
    write_book(timestamp=2_000, asks=[[str(65002 + i), "1.5"] for i in range(1, 26)],
               bids=[[str(65002 - i), "2"] for i in range(1, 26)])
    while app.update_count < 2 and time.time() < deadline:
        time.sleep(0.01)
    running[0] = False
    app._watcher.join(timeout=2)
    assert app.update_count == 2
    assert models._tracker.samples == 1, "each new book is one volatility sample"
    assert not app._watcher.is_alive(), "the watcher stops with the feed"


def test_watcher_survives_a_failed_feed_read(painter, monkeypatch):
    """A transient error reading the feed record must not end the watcher."""
    calls = []

    def flaky():
        """Fail the first read, then report no feed."""
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("feed.json busy")
        return None  # then the feed is gone, so the watcher exits cleanly

    monkeypatch.setattr(app, "running_feed", flaky)
    monkeypatch.setattr(app, "WATCH_BOOKS", True)
    monkeypatch.setattr(app, "WATCH_SECONDS", 0.01)
    app.watch_books()
    assert len(calls) == 2


def test_start_launches_the_watcher(monkeypatch):
    """Books are taken in from Start, before any browser has polled."""
    started = []
    monkeypatch.setattr(app, "running_feed", lambda: None)
    monkeypatch.setattr(app, "stop_feed", lambda: False)
    monkeypatch.setattr(app, "start_feed", lambda symbol, exchange: {})
    monkeypatch.setattr(app, "ensure_book_watcher", lambda: started.append(True))
    monkeypatch.setattr(app, "fcntl", None)
    app.feed_action("start", "BTC-USDT-SWAP", "SIM")
    assert started == [True]
