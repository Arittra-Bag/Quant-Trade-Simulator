"""
Start and Stop. The feed's state lives on disk, so a status poll answered by another server
process, or by this one after a restart, sees the same feed the Start click launched.
"""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

# Stands in for websocket_client.py: reports its pid the way the real client does, then idles.
FAKE_CLIENT = ("import json, os, sys, time\n"
               "json.dump({'pid': os.getpid(), 'state': 'connecting', 'source': 'OKX'}, open(sys.argv[1], 'w'))\n"
               "time.sleep(60)\n")


@pytest.fixture
def feed_dir(tmp_path, monkeypatch):
    status = str(tmp_path / "feed_status.json")
    monkeypatch.setattr(app, "ORDERBOOK_FILE", str(tmp_path / "latest_orderbook.json"))
    monkeypatch.setattr(app, "STATUS_FILE", status)
    monkeypatch.setattr(app, "FEED_FILE", str(tmp_path / "feed.json"))
    monkeypatch.setattr(app, "FEED_LOCK_FILE", str(tmp_path / "feed.lock"))
    monkeypatch.setattr(app, "client_process", None)
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        proc = real_popen([sys.executable, "-c", FAKE_CLIENT, status])
        deadline = time.time() + 5
        while not os.path.exists(status) and time.time() < deadline:
            time.sleep(0.02)
        return proc

    monkeypatch.setattr(app.subprocess, "Popen", fake_popen)
    yield tmp_path
    if app.client_process is not None:
        app.stop_websocket_client(app.client_process)
    feed = app.read_feed()
    if feed and app.pid_alive(feed["pid"]):
        app.stop_pid(feed["pid"])


def as_another_process():
    """Forget the child handle, as a different worker or a restarted server would have."""
    app.client_process = None


def test_a_started_feed_is_seen_by_a_process_that_did_not_start_it(feed_dir):
    error, note, tone = app.feed_action("start", "BTC-USDT-SWAP", "OKX")
    assert error == "" and note == "Connecting to OKX BTC-USDT-SWAP…" and tone == "busy"
    pid = app.read_feed()["pid"]

    as_another_process()
    assert app.running_feed()["pid"] == pid
    state, label, detail = app.feed_state()
    assert state != "idle"
    assert app.feed_line(state, label, detail, app.read_feed())[0] == "Connecting to OKX BTC-USDT-SWAP…"


def test_another_process_can_stop_the_feed(feed_dir):
    app.feed_action("start", "BTC-USDT-SWAP", "OKX")
    pid = app.read_feed()["pid"]
    as_another_process()

    assert app.feed_action("stop") == ("", "Stopped.", "idle")
    assert not app.pid_alive(pid)
    assert app.read_feed() is None
    assert app.feed_state()[0] == "idle"


def test_start_on_the_running_feed_does_not_restart_it(feed_dir):
    app.feed_action("start", "BTC-USDT-SWAP", "OKX")
    pid = app.read_feed()["pid"]

    error, note, tone = app.feed_action("start", "btc-usdt-swap", "OKX")
    assert error == "" and tone == "warn"
    assert note == "Already running on OKX BTC-USDT-SWAP. Press Stop first to restart it."
    assert app.read_feed()["pid"] == pid and app.pid_alive(pid)


def test_start_on_another_instrument_switches_and_says_so(feed_dir):
    app.feed_action("start", "BTC-USDT-SWAP", "OKX")
    old = app.read_feed()["pid"]

    error, note, _ = app.feed_action("start", "ETH-USDT", "BINANCE")
    assert error == ""
    assert note == "Switched from OKX BTC-USDT-SWAP to Binance ETH-USDT. Connecting…"
    assert not app.pid_alive(old)
    assert app.read_feed()["symbol"] == "ETH-USDT"


def test_stop_while_stopped_says_so(feed_dir):
    assert app.feed_action("stop") == ("", "Already stopped. Press Start to connect.", "warn")


def test_a_failed_start_is_reported_not_left_on_idle(feed_dir, monkeypatch):
    def broken(*_, **__):
        raise OSError("no such interpreter")

    monkeypatch.setattr(app.subprocess, "Popen", broken)
    error, note, tone = app.feed_action("start", "BTC-USDT-SWAP", "OKX")
    assert "no such interpreter" in error
    assert note == "Could not start the feed on OKX BTC-USDT-SWAP." and tone == "error"
    assert app.read_feed() is None


def test_a_feed_that_died_says_what_to_press(feed_dir):
    app.feed_action("start", "BTC-USDT-SWAP", "OKX")
    app.stop_websocket_client(app.client_process)
    state, label, detail = app.feed_state()
    assert state == "down"
    text, tone = app.feed_line(state, label, detail, app.read_feed())
    assert text.endswith("Press Start to try again.") and tone == "error"


def test_the_line_under_the_buttons_names_the_feed_and_the_next_step():
    feed = {"exchange": "OKX", "symbol": "BTC-USDT-SWAP"}
    assert app.feed_line("idle", "Idle", "", None) == ("Stopped. Press Start to connect.", "idle")
    assert app.feed_line("live", "Live", "", feed) == ("Running on OKX BTC-USDT-SWAP. Press Stop to end it.", "live")
    assert app.feed_line("warn", "Reconnecting", "", feed)[0] == "Reconnecting to OKX BTC-USDT-SWAP…"
