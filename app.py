import atexit
import base64
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from datetime import datetime

try:
    import fcntl
except ImportError:  # Windows: the thread lock alone guards the feed
    fcntl = None

import dash
import numpy as np
from dash import Input, Output, State, ctx, dcc, html
from flask_compress import Compress

from export import export_orderbook_to_csv, export_orderbook_to_excel
from gemini_integration import GeminiAnalyzer
from models import book_stats, estimate_costs, measure_volatility
from visualizations import (create_latency_time_series, create_orderbook_depth_chart,
                            create_transaction_cost_breakdown, empty_figure)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERBOOK_FILE = os.path.join(BASE_DIR, "latest_orderbook.json")
STATUS_FILE = os.path.join(BASE_DIR, "feed_status.json")
FEED_FILE = os.path.join(BASE_DIR, "feed.json")
FEED_LOCK_FILE = os.path.join(BASE_DIR, "feed.lock")
LADDER_LEVELS = 12
STALE_AFTER = 5.0  # seconds without a new book before the feed is shown as stale
NOTE_SECONDS = 4.0  # how long a click's message stays under Start and Stop

VENUES = [("OKX", "OKX"), ("Hyperliquid", "HYPERLIQUID"), ("Binance", "BINANCE"),
          ("Kraken", "KRAKEN"), ("Simulated", "SIM")]
VENUE_NAMES = {value: label for label, value in VENUES}

# ----------------------------------------------------------------------------- state
# One feed at a time; this is a single-user simulator. Whether it is running lives in
# FEED_FILE, not here, so every server process and every restart sees the same answer.
_lock = threading.Lock()  # Start and Stop
_paint_lock = threading.Lock()  # the cached book and latency history below
_watcher_lock = threading.Lock()
_watcher = None  # thread taking in books while a feed runs
WATCH_BOOKS = True
WATCH_SECONDS = 0.5
client_process = None  # the feed this process started, if any
feed_started = None  # which feed the cached book below belongs to
orderbook_data = None
data_last_modified = 0.0
update_count = 0
calc_latency_us = deque(maxlen=300)

gemini_analyzer = GeminiAnalyzer()

# ----------------------------------------------------------------------------- app
app = dash.Dash(__name__, title="Quant Trade Simulator", update_title=None,
                meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}])
server = app.server  # for gunicorn: gunicorn app:server


class StaticBundleCache:
    """
    Flask-Compress cache that keeps only Dash's JS and CSS bundles.

    Compressing plotly's multi-megabyte bundle costs over a second of a free instance's CPU,
    and every new visitor asks for it. Bundle URLs are fingerprinted, so a compressed copy per
    path and encoding stays valid for the life of the process. Keys without the bundle marker
    (every callback response) are never stored, and the store is capped, so made-up bundle
    paths cannot grow it without bound.
    """
    MARKER = "bundle:"
    MAX_ENTRIES = 32

    def __init__(self):
        """Start with an empty store."""
        self._store = OrderedDict()
        self._guard = threading.Lock()

    def get(self, key):
        """The cached compressed bytes for `key`, or None."""
        # A plain read: flask-compress calls set() after every get(), which records the use.
        return self._store.get(key)

    def set(self, key, value):
        """Store a compressed bundle, evicting the least recently used past the cap."""
        if self.MARKER not in key:
            return
        with self._guard:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.MAX_ENTRIES:
                self._store.popitem(last=False)


def bundle_cache_key(request):
    """Cache key for a Dash bundle request, or an empty key for anything else."""
    # The path alone: the query string is not part of what Dash serves. Under a URL prefix
    # the bundles move with it.
    bundles = f"{app.config.routes_pathname_prefix}_dash-component-suites/"
    if request.method == "GET" and request.path.startswith(bundles):
        return StaticBundleCache.MARKER + request.path
    return ""


# Polls are JSON a few KB to tens of KB; compressed they cross a slow link several times faster.
server.config.update(COMPRESS_CACHE_BACKEND=StaticBundleCache, COMPRESS_CACHE_KEY=bundle_cache_key,
                     COMPRESS_ALGORITHM=["br", "gzip"])
Compress(server)

app.index_string = """<!DOCTYPE html>
<html lang="en">
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
{%css%}
</head>
<body>
{%app_entry%}
<footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>"""


# ----------------------------------------------------------------------------- feed process
# The running feed is recorded in FEED_FILE (pid, venue, symbol, start time). It used to be
# a variable in this process, so a status poll answered by another worker, or by the process
# after a restart, read Idle however many times Start was clicked. Any process can now read
# the feed and stop it.
def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


@contextmanager
def feed_lock():
    """Serialise Start and Stop across threads and, where the OS allows it, processes."""
    with _lock:
        if fcntl is None:
            yield
            return
        with open(FEED_LOCK_FILE, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def pid_alive(pid):
    if not pid:
        return False
    if client_process is not None and client_process.pid == pid:
        return client_process.poll() is None
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_feed():
    """The feed recorded on disk, running or not, or None when nothing was started."""
    feed = read_json(FEED_FILE)
    return feed if isinstance(feed, dict) and feed.get("pid") else None


def running_feed():
    feed = read_feed()
    return feed if feed and pid_alive(feed["pid"]) else None


def start_feed(symbol, exchange):
    """Launch the client and record it. Raises if the process cannot be started."""
    global client_process
    remove_file(ORDERBOOK_FILE)
    remove_file(STATUS_FILE)
    cmd = [sys.executable, os.path.join(BASE_DIR, "websocket_client.py"),
           "--symbol", symbol, "--exchange", exchange, "--output", ORDERBOOK_FILE]
    process = subprocess.Popen(cmd, cwd=BASE_DIR)
    # Reap the child as soon as it exits, so other processes checking its pid see it gone
    # rather than a zombie that still answers.
    threading.Thread(target=process.wait, daemon=True).start()
    client_process = process
    feed = {"pid": process.pid, "symbol": symbol, "exchange": exchange, "started": time.time()}
    write_json(FEED_FILE, feed)
    return feed


def stop_feed():
    """Stop the recorded feed, whichever process started it. Returns whether one was running."""
    global client_process
    feed = read_feed()
    was_running = bool(feed) and pid_alive(feed["pid"])
    if was_running and (client_process is None or client_process.pid != feed["pid"]):
        stop_pid(feed["pid"])
    if client_process is not None:
        stop_websocket_client(client_process)
        client_process = None
    remove_file(FEED_FILE)
    return was_running


def stop_pid(pid):
    """Stop a feed another process started. Only a pid the feed client itself reported is
    signalled, so a stale record whose pid was reused never takes down something else."""
    if (read_json(STATUS_FILE) or {}).get("pid") != pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        return
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.05)


def stop_websocket_client(process):
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        else:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def read_orderbook_data(last_modified):
    try:
        modified = os.path.getmtime(ORDERBOOK_FILE)
    except OSError:
        return None, last_modified
    if modified <= last_modified:
        return None, last_modified
    data = read_json(ORDERBOOK_FILE)
    return (data, modified) if data else (None, last_modified)


def feed_state(feed=None):
    """(state, label, detail) derived from the feed record and the status file the client writes."""
    feed = feed or read_feed()
    if not feed:
        return "idle", "Idle", "Press Start to connect a feed"
    status = read_json(STATUS_FILE) or {}
    if not pid_alive(feed["pid"]):
        code = client_process.poll() if client_process is not None and client_process.pid == feed["pid"] else None
        err = status.get("error") or (f"exited with code {code}" if code is not None else "the client exited")
        return "down", "Offline", f"Feed process stopped: {err}"
    book_time = status.get("last_book_time")
    if status.get("state") == "live" and book_time and time.time() - book_time < STALE_AFTER:
        return "live", "Live", f"{status.get('source', '')} · {status.get('books', 0):,} books"
    if book_time and time.time() - book_time >= STALE_AFTER:
        return "warn", "Stale", status.get("error") or f"No book for {time.time() - book_time:.0f}s"
    label = {"connecting": "Connecting", "reconnecting": "Reconnecting", "retrying": "Retrying"}.get(
        status.get("state"), "Starting")
    detail = status.get("error") or (f"Trying {status['source']}" if status.get("source") else "Launching client")
    return "warn", label, detail


def feed_where(feed):
    return f"{VENUE_NAMES.get(feed.get('exchange'), feed.get('exchange') or '')} {feed.get('symbol', '')}".strip()


def feed_line(state, label, detail, feed):
    """(text, tone) for the line under Start and Stop: what the feed is doing and what to press."""
    if state == "idle" or not feed:
        return "Stopped. Press Start to connect.", "idle"
    where = feed_where(feed)
    if state == "live":
        return f"Running on {where}. Press Stop to end it.", "live"
    if state == "down":
        return f"{detail}. Press Start to try again.", "error"
    if label == "Stale":
        return f"Running on {where}, but {detail[0].lower()}{detail[1:]}. Press Stop to end it.", "warn"
    if label == "Retrying":
        return f"{detail}. Press Stop to give up.", "warn"
    verb = "Reconnecting" if label == "Reconnecting" else "Connecting"
    return f"{verb} to {where}…", "busy"


def book_freshness(book, feed):
    """
    How far the numbers on screen can be trusted: ("fresh"|"stale"|"frozen"|"none", note).

    Derived from when we received the book rather than the exchange timestamp, because that
    is what "how old is what I am looking at" means, and exchange clocks drift.
    """
    if not book:
        return "none", ""
    age = max(0.0, time.time() - float(book.get("local_time") or 0))
    if feed == "live" and age < STALE_AFTER:
        return "fresh", ""
    if feed == "idle":
        return "frozen", f"Stream stopped. Showing the last book, {age_str(age)} old."
    if feed == "down":
        return "frozen", f"Feed offline. Showing the last book, {age_str(age)} old."
    return "stale", f"No new book for {age_str(age)}. Figures are from the last one received."


# ----------------------------------------------------------------------------- layout helpers
def field(label, control, hint=None, symbol=None):
    """
    A labelled control. `symbol` is rendered beside the label without the uppercasing the
    label carries, so a lowercase sigma stays a sigma instead of becoming a summation sign.
    """
    caption = [label] if symbol is None else [label, html.Span(symbol, className="field-symbol")]
    return html.Div([html.Label(caption, className="field-label"), control,
                     html.Div(hint, className="field-hint") if hint else None], className="field")


def kpi(id_, label, primary=False):
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div("—", id=f"{id_}-value", className="kpi-value"),
        html.Div("", id=f"{id_}-sub", className="kpi-sub"),
    ], className="kpi primary" if primary else "kpi")


def stat(id_, label):
    return html.Div([html.Span(label, className="stat-label"), html.Span("—", id=id_, className="stat-value")],
                    className="stat")


def panel(title, children, extra=None, className=""):
    return html.Section([
        html.Header([html.H2(title), extra], className="panel-head"),
        html.Div(children, className="panel-body"),
    ], className=f"panel {className}")


GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}

# ----------------------------------------------------------------------------- layout
app.layout = html.Div([
    html.Header([
        html.Div([html.Span("QTS", className="brand-mark"), html.Span("Quant Trade Simulator", className="brand-name")],
                 className="brand"),
        html.Div([
            html.Span("—", id="hdr-symbol", className="hdr-symbol"),
            html.Span("—", id="hdr-venue", className="chip"),
        ], className="hdr-instrument"),
        html.Div([
            html.Span("—", id="hdr-mid", className="hdr-mid"),
            stat("hdr-spread", "SPRD"),
            stat("hdr-micro", "MICRO"),
            stat("hdr-imb", "IMB"),
            stat("hdr-age", "AGE"),
        ], className="hdr-stats"),
        html.Div([
            html.Span([html.Span(className="dot"), html.Span("Idle", id="feed-label")], id="feed-pill",
                      className="pill idle"),
            html.Span("--:--:--", id="hdr-clock", className="hdr-clock"),
        ], className="hdr-right"),
    ], className="topbar"),

    html.Div(id="data-banner", className="banner", role="status", **{"aria-live": "polite"}),

    html.Main([
        # ---------------- left: order ticket
        html.Div([
            panel("Order ticket", [
                html.Div([
                    field("Venue", dcc.Dropdown(id="exchange-dropdown", options=[{"label": l, "value": v} for l, v in VENUES],
                                                value="OKX", clearable=False, searchable=False, className="dd")),
                    field("Instrument", dcc.Input(id="asset-input", type="text", value="BTC-USDT-SWAP",
                                                  debounce=True, spellCheck=False, className="input mono")),
                ], className="row-2"),
                html.Div([
                    field("Side", dcc.RadioItems(id="side-radio", options=[{"label": "Buy", "value": "buy"},
                                                                           {"label": "Sell", "value": "sell"}],
                                                 value="buy", className="seg")),
                    field("Order type", dcc.Dropdown(id="order-type-dropdown",
                                                     options=[{"label": "Market", "value": "Market"}],
                                                     value="Market", clearable=False, searchable=False, className="dd")),
                ], className="row-2"),
                field("Notional (USD)", html.Div([
                    dcc.Input(id="quantity-input", type="number", value=100, min=1, step=1, debounce=True,
                              className="input mono"),
                    html.Div([html.Button(lbl, id={"type": "qty-chip", "value": val}, className="chip-btn")
                              for lbl, val in [("100", 100), ("1K", 1000), ("10K", 10000), ("100K", 100000), ("1M", 1000000)]],
                             className="chips"),
                ])),
                field("Volatility", html.Div([
                    dcc.Slider(id="volatility-slider", min=0.001, max=0.05, step=0.001, value=0.01,
                               marks=None, tooltip=None, updatemode="drag", className="slider"),
                    html.Span("0.010", id="volatility-readout", className="readout mono"),
                ], className="slider-row"),
                      "Dimensionless. Assumption, not a measurement from the feed.", symbol="(σ)"),
                field("Fee tier", dcc.Dropdown(id="fee-tier-dropdown",
                                               options=[{"label": t, "value": t} for t in ("Tier 1", "Tier 2", "Tier 3")],
                                               value="Tier 1", clearable=False, searchable=False, className="dd")),
                html.Div([
                    html.Button("Start stream", id="start-button", className="btn btn-go"),
                    html.Button("Stop", id="stop-button", className="btn btn-stop"),
                ], className="btn-row"),
                html.Div("Stopped. Press Start to connect.", id="feed-hint", className="feed-hint idle",
                         role="status", **{"aria-live": "polite"}),
                html.Div(id="ticket-error", className="ticket-error", role="alert"),
            ], className="ticket"),

            panel("Feed", [
                html.Div(id="status-display", className="feed-detail", role="status",
                         **{"aria-live": "polite"}),
                html.Div(id="update-time", className="feed-meta mono"),
            ]),

            panel("Export", [
                html.Div([
                    html.Button("CSV", id="export-csv-button", className="btn btn-ghost"),
                    html.Button("XLSX", id="export-excel-button", className="btn btn-ghost"),
                ], className="btn-row"),
                html.Div(id="export-note", className="field-hint", role="status",
                         **{"aria-live": "polite"}),
                dcc.Download(id="download-data"),
            ]),

            html.Details([
                html.Summary("Raw book"),
                html.Pre("No data", id="debug-info", className="raw mono"),
            ], className="panel raw-panel"),
        ], className="col col-left"),

        # ---------------- center: costs and charts
        html.Div([
            html.Div([
                kpi("netcost", "Net cost", primary=True),
                kpi("slippage", "Slippage"),
                kpi("impact", "Market impact"),
                kpi("fees", "Fees"),
                kpi("makertaker", "Maker / Taker"),
                kpi("latency", "Calc latency"),
            ], id="kpis", className="kpis"),

            panel("Depth", dcc.Graph(id="depth-chart", figure=empty_figure(), config=GRAPH_CONFIG,
                                     className="graph graph-depth", style={"height": "100%", "minHeight": "320px"}),
                  extra=html.Span("cumulative USD · VWAP of your fill", className="panel-note"),
                  className="grow"),

            html.Div([
                panel("Cost stack", dcc.Graph(id="cost-breakdown-chart", figure=empty_figure(), config=GRAPH_CONFIG,
                                              className="graph graph-small", style={"height": "140px"}),
                      extra=html.Span("bps of notional", className="panel-note")),
                panel("Latency", dcc.Graph(id="latency-chart", figure=empty_figure("No samples"), config=GRAPH_CONFIG,
                                           className="graph graph-small", style={"height": "140px"}),
                      extra=html.Span("per tick, model + render prep", className="panel-note")),
            ], className="row-2 charts-row"),

            panel("AI read", [
                dcc.Loading(html.Div(html.Span("Ask Gemini for a read on the current book and your order.",
                                               className="muted"), id="gemini-analysis", className="ai-body"),
                            type="dot", color="#ffb000"),
            ], extra=html.Button("Generate", id="generate-analysis-button", className="btn btn-ghost btn-sm")),

            panel("Execution agent", [
                dcc.Loading(html.Div(html.Span(
                    "Plan the order: the advisor proposes, every alternative is priced, a critic checks the "
                    "plan against the book, and nothing is sent until you approve it. Fills are paper.",
                    className="muted"), id="agent-body", className="ai-body"), type="dot", color="#ffb000"),
                html.Div([
                    html.Button("Approve", id="agent-approve-button", className="btn btn-sm btn-approve"),
                    html.Button("Reject", id="agent-reject-button", className="btn btn-ghost btn-sm"),
                ], id="agent-actions", className="agent-actions", style={"display": "none"}),
                dcc.Store(id="agent-thread"),
            ], extra=html.Button("Plan", id="agent-plan-button", className="btn btn-ghost btn-sm")),
        ], className="col col-center"),

        # ---------------- right: ladder
        html.Div([
            panel("Book", [
                html.Div([
                    html.Div([html.Span("Price"), html.Span("Size"), html.Span("Cum $")], className="ladder-head"),
                    html.Div(id="asks-table", className="ladder asks"),
                    html.Div(id="spread-row", className="spread-row mono"),
                    html.Div(id="bids-table", className="ladder bids"),
                    html.Div(id="ladder-depth-note", className="ladder-foot mono"),
                ], id="ladder-wrap"),
            ], extra=html.Span("", id="ladder-note", className="panel-note"), className="ladder-panel"),
        ], className="col col-right"),
    ], id="grid", className="grid"),

    # Polling is paced in the browser: the next poll goes out only once the last one has
    # answered. A fixed 500 ms poll froze the desk on a slow link, because Dash drops a
    # response that arrives after the next poll has already been sent, so none ever landed.
    dcc.Interval(id="interval-component", interval=250, n_intervals=0),
    dcc.Store(id="poll-tick"),
    dcc.Store(id="poll-ack"),
    dcc.Store(id="painted"),  # which book and order this browser last painted
    dcc.Interval(id="clock-interval", interval=1000, n_intervals=0),
    # The line under Start and Stop is drawn in the browser from these three, so a click
    # shows Starting… or Stopping… at once instead of after the server answers.
    dcc.Store(id="feed-line"),     # the feed's state, from the painter
    dcc.Store(id="feed-pending"),  # a click the server has not answered yet
    dcc.Store(id="feed-note"),     # the server's answer to the last click
], className="app")


# ----------------------------------------------------------------------------- formatting
def usd(v):
    """
    Money at a precision that follows the magnitude.

    A cost tile is one sixth of the centre column, so a fixed 4dp made a $250m impact
    truncate and a $800 fee read "$800.0000". Millions and above are abbreviated the way
    a desk shows them; the bps sub-label underneath carries the exact relative figure.
    """
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:,.2f}M"
    if a >= 1e4:
        return f"${v:,.0f}"
    if a >= 1:
        return f"${v:,.2f}"
    if a >= 0.01:
        return f"${v:,.4f}"
    return "$0.00" if v == 0 else f"${v:,.6f}"


def bps(v, quantity):
    return f"{v / quantity * 1e4:,.2f} bps" if quantity else "—"


def price_decimals(px):
    return 1 if px >= 1000 else 2 if px >= 10 else 4


def age_str(seconds):
    """
    Compact age used by the header stat and the stale-data banner.

    Minutes carry their seconds: rounding to whole minutes showed a 90-second-old book as
    "2m", which overstates how stale the screen is.
    """
    if seconds < 1:
        return f"{seconds * 1000:,.0f}ms"
    if seconds < 60:
        return f"{seconds:,.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def size_fmt(sz):
    return f"{sz:,.4f}" if sz < 100 else f"{sz:,.1f}" if sz < 1e5 else f"{sz:,.0f}"


def build_ladder(levels, side, fill_levels, max_cum):
    rows, cum = [], 0.0
    for i, (px, sz) in enumerate(levels[:LADDER_LEVELS]):
        cum += px * sz
        width = min(100.0, cum / max_cum * 100) if max_cum else 0
        rows.append(html.Div([
            html.Div(className="bar", style={"width": f"{width:.1f}%"}),
            html.Span(f"{px:,.{price_decimals(px)}f}", className="px"),
            html.Span(size_fmt(sz), className="sz"),
            html.Span(f"{cum:,.0f}", className="cum"),
        ], className="lvl hit" if i < fill_levels else "lvl"))
    return rows[::-1] if side == "asks" else rows


RAW_BOOK_CHARS = 6000


def raw_book_text(book):
    """Pretty-printed book, with the cut marked rather than stopping mid-token."""
    text = json.dumps(book, indent=1)
    if len(text) <= RAW_BOOK_CHARS:
        return text
    return f"{text[:RAW_BOOK_CHARS]}\n… truncated, {len(text) - RAW_BOOK_CHARS:,} more characters"


def clean_symbol(symbol):
    symbol = (symbol or "").strip().upper()
    return symbol if re.fullmatch(r"[A-Z0-9]{2,12}(-[A-Z0-9]{2,12}){1,2}", symbol) else None


def compute(book, quantity, volatility, fee_tier, side, order_type):
    """Cost breakdown for one order. The volatility argument is only a fallback: once
    enough of the feed has arrived, estimate_costs uses volatility measured from it."""
    t0 = time.perf_counter()
    c = estimate_costs(book, quantity, volatility, side=side, order_type=order_type,
                       fee_tier=fee_tier, venue=book.get("source"))
    elapsed_us = (time.perf_counter() - t0) * 1e6
    if not c:
        return dict(fill=None, slippage=0.0, fees=0.0, impact=0.0, maker=0.0,
                    stats=book_stats(book), net=0.0, elapsed_us=elapsed_us, cost=None)
    return dict(fill=c["fill"], slippage=c["slippage_usd"], fees=c["fees_usd"], impact=c["impact_usd"],
                maker=c["maker"], stats=c["stats"], net=c["net_usd"], elapsed_us=elapsed_us, cost=c)


# ----------------------------------------------------------------------------- callbacks
@app.callback(
    Output("quantity-input", "value"),
    Input({"type": "qty-chip", "value": dash.ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def pick_quantity(_):
    if not ctx.triggered_id or not any(t["value"] for t in ctx.triggered):
        return dash.no_update
    return ctx.triggered_id["value"]


@app.callback(Output("volatility-readout", "children"), Input("volatility-slider", "value"))
def show_volatility(v):
    return f"{(v or 0):.3f}"


app.clientside_callback(
    """
    function(_) {
        return new Date().toISOString().slice(11, 19) + " UTC";
    }
    """,
    Output("hdr-clock", "children"),
    Input("clock-interval", "n_intervals"),
)

app.clientside_callback(
    """
    function(_) {
        const s = window.qtsPoll = window.qtsPoll || {inflight: false, sent: 0, wait: 5000};
        const now = Date.now();
        // A hidden tab polls every 5 s instead of twice a second, to spare the server's CPU.
        const gap = document.hidden ? 5000 : 500;
        if (now - s.sent < gap) {
            return window.dash_clientside.no_update;
        }
        if (s.inflight) {
            // Wait for the answer. One that never comes (a dropped connection, a restarted
            // worker) is given up after `wait`, which doubles each time, so a server slower
            // than any fixed limit still gets a poll through instead of having each dropped.
            if (now - s.sent < s.wait) {
                return window.dash_clientside.no_update;
            }
            s.wait = Math.min(s.wait * 2, 60000);
        }
        s.inflight = true;
        s.sent = now;
        return now;
    }
    """,
    Output("poll-tick", "data"),
    Input("interval-component", "n_intervals"),
)

app.clientside_callback(
    """
    function(_) {
        // The poll has answered. Marked here, not in the pacer above, because an input on
        // the pacer from the poll's own output would be a loop Dash never fires.
        const s = window.qtsPoll = window.qtsPoll || {inflight: false, sent: 0, wait: 5000};
        s.inflight = false;
        s.wait = 5000;
        return Date.now();
    }
    """,
    Output("poll-ack", "data"),
    Input("feed-line", "data"),
)


def feed_action(action, asset=None, exchange=None):
    """
    Carry out Start or Stop and say what happened: (ticket_error, note, tone).

    Start on the feed that is already running leaves it alone rather than restarting it,
    which used to look like the click did nothing. Start on another venue or instrument
    switches. A start that fails says why instead of leaving the desk on Idle.
    """
    with feed_lock():
        feed = running_feed()
        if action == "start":
            symbol = clean_symbol(asset)
            if not symbol:
                return "Instrument must look like BTC-USDT-SWAP.", "", "idle"
            exchange = exchange or "OKX"
            where = feed_where({"exchange": exchange, "symbol": symbol})
            if feed and feed.get("symbol") == symbol and feed.get("exchange") == exchange:
                return "", f"Already running on {where}. Press Stop first to restart it.", "warn"
            try:
                stop_feed()
                start_feed(symbol, exchange)
                ensure_book_watcher()  # take in books from the start, before any tab polls
            except Exception as e:
                print(f"Could not start the feed on {where}: {e!r}", file=sys.stderr, flush=True)
                return f"Could not start the feed: {e}", f"Could not start the feed on {where}.", "error"
            if feed:
                return "", f"Switched from {feed_where(feed)} to {where}. Connecting…", "busy"
            return "", f"Connecting to {where}…", "busy"
        if not stop_feed():
            return "", "Already stopped. Press Start to connect.", "warn"
        return "", "Stopped.", "idle"


@app.callback(
    Output("ticket-error", "children"),
    Output("asset-input", "className"),
    Output("feed-note", "data"),
    Input("start-button", "n_clicks"),
    Input("stop-button", "n_clicks"),
    State("asset-input", "value"),
    State("exchange-dropdown", "value"),
    prevent_initial_call=True,
)
def handle_stream_control(start_clicks, stop_clicks, asset, exchange):
    """
    Start and stop the feed. A rejected instrument is reported on the field itself; what the
    click did goes in the line under the buttons. `seq` tells the browser which click this
    answers, so it can stop showing Starting… or Stopping….
    """
    if ctx.triggered_id not in ("start-button", "stop-button"):
        return dash.no_update, dash.no_update, dash.no_update
    action = "start" if ctx.triggered_id == "start-button" else "stop"
    error, note, tone = feed_action(action, asset, exchange)
    invalid = action == "start" and error and not note
    return (error, "input mono invalid" if invalid else "input mono",
            {"seq": (start_clicks or 0) + (stop_clicks or 0), "text": note, "tone": tone,
             "until": time.time() + NOTE_SECONDS})


app.clientside_callback(
    """
    function(startClicks, stopClicks) {
        const trig = (window.dash_clientside.callback_context.triggered || [])[0] || {};
        const stopping = (trig.prop_id || "").indexOf("stop-button") === 0;
        const text = stopping ? "Stopping…" : "Starting…";
        return [{seq: (startClicks || 0) + (stopClicks || 0), at: Date.now(), text: text},
                text, "feed-hint busy"];
    }
    """,
    # Painted here as well as below: Dash holds the callback below until the server has
    # answered the click, so without this Starting… would never be seen.
    Output("feed-pending", "data"),
    Output("feed-hint", "children", allow_duplicate=True),
    Output("feed-hint", "className", allow_duplicate=True),
    Input("start-button", "n_clicks"),
    Input("stop-button", "n_clicks"),
    prevent_initial_call=True,
)

app.clientside_callback(
    """
    function(line, pending, note) {
        line = line || {text: "Stopped. Press Start to connect.", tone: "idle", now: 0};
        if (pending && (!note || pending.seq > note.seq)) {
            if (Date.now() - pending.at < 15000) {
                return [pending.text, "feed-hint busy"];
            }
            return ["No answer from the server. Check the connection and try again.", "feed-hint error"];
        }
        if (note && note.text && line.now < note.until) {
            return [note.text, "feed-hint " + note.tone];
        }
        return [line.text, "feed-hint " + line.tone];
    }
    """,
    Output("feed-hint", "children"),
    Output("feed-hint", "className"),
    Input("feed-line", "data"),
    Input("feed-pending", "data"),
    Input("feed-note", "data"),
)


# What the painter returns, in order. Named so a poll whose book and order are unchanged
# can send only the parts that move with the clock, and leave the rest where it is.
PAINT_OUTPUTS = [
    Output("feed-pill", "className"),
    Output("feed-label", "children"),
    Output("status-display", "children"),
    Output("update-time", "children"),
    Output("hdr-symbol", "children"),
    Output("hdr-venue", "children"),
    Output("hdr-mid", "children"),
    Output("hdr-spread", "children"),
    Output("hdr-micro", "children"),
    Output("hdr-imb", "children"),
    Output("hdr-imb", "className"),
    Output("hdr-age", "children"),
    Output("netcost-value", "children"),
    Output("netcost-sub", "children"),
    Output("slippage-value", "children"),
    Output("slippage-sub", "children"),
    Output("impact-value", "children"),
    Output("impact-sub", "children"),
    Output("fees-value", "children"),
    Output("fees-sub", "children"),
    Output("makertaker-value", "children"),
    Output("makertaker-sub", "children"),
    Output("latency-value", "children"),
    Output("latency-sub", "children"),
    Output("asks-table", "children"),
    Output("bids-table", "children"),
    Output("spread-row", "children"),
    Output("ladder-note", "children"),
    Output("ladder-depth-note", "children"),
    Output("debug-info", "children"),
    Output("data-banner", "children"),
    Output("data-banner", "className"),
    Output("kpis", "className"),
    Output("ladder-wrap", "className"),
    Output("grid", "className"),
    Output("depth-chart", "figure"),
    Output("latency-chart", "figure"),
    Output("cost-breakdown-chart", "figure"),
    Output("feed-line", "data"),
    Output("painted", "data"),
]
PAINT_KEYS = [f"{o.component_id}.{o.component_property}" for o in PAINT_OUTPUTS]
PAINT_KEY_SET = frozenset(PAINT_KEYS)


TILES = ("netcost", "slippage", "impact", "fees", "makertaker", "latency")


def paint_only(values):
    """The painter's tuple with `values` (keyed "id.prop") set and everything else untouched."""
    unknown = values.keys() - PAINT_KEY_SET
    if unknown:
        raise KeyError(f"not painter outputs: {sorted(unknown)}")
    return tuple(values[k] if k in values else dash.no_update for k in PAINT_KEYS)


def paint_all(values):
    """The painter's tuple for a full paint, which must set every output."""
    missing = PAINT_KEY_SET - values.keys()
    if missing:
        raise KeyError(f"full paint is missing: {sorted(missing)}")
    return paint_only(values)


@app.callback(
    *PAINT_OUTPUTS,
    Input("poll-tick", "data"),
    Input("quantity-input", "value"),
    Input("volatility-slider", "value"),
    Input("fee-tier-dropdown", "value"),
    Input("side-radio", "value"),
    Input("order-type-dropdown", "value"),
    State("painted", "data"),
)
def update_tables(_, quantity, volatility, fee_tier, side, order_type, painted):
    """
    Paint the whole desk from one book and one set of numbers.

    The figures used to have their own interval and their own call to compute(), so the cost
    stack and the tiles reported different market impact and the depth chart's VWAP disagreed
    with the ladder's. Returning them from the same callback means one response carries one
    self-consistent view.

    It always answers: the browser sends its next poll only once feed-line comes back, so an
    exception here would stall the desk. A failed paint says so and clears `painted`, which
    makes the next poll repaint in full.
    """
    try:
        return paint_desk(quantity, volatility, fee_tier, side, order_type, painted)
    except Exception as e:
        print(f"Could not paint the desk: {e!r}", file=sys.stderr, flush=True)
        return paint_only({"feed-line.data": {"text": "Could not update the desk. Retrying…", "tone": "warn",
                                              "now": time.time()},
                           "painted.data": None})


def ingest_book(feed):
    """
    Take in the newest book, if there is one. Called with _paint_lock held.

    Each new book also goes to the volatility estimate here, not only when a browser paints
    it, so the estimate keeps up while nobody is looking.
    """
    global orderbook_data, data_last_modified, update_count, feed_started
    # A feed started since the cached book was read, here or in another process: the old
    # book belongs to the previous instrument, so drop it rather than paint it as current.
    if feed and feed.get("started") != feed_started:
        feed_started = feed.get("started")
        orderbook_data, data_last_modified, update_count = None, 0.0, 0
        calc_latency_us.clear()

    new_data, modified = read_orderbook_data(data_last_modified)
    if new_data and feed is not None:
        orderbook_data, data_last_modified = new_data, modified
        update_count += 1
        measure_volatility(new_data)


def watch_books():
    """Ingest books as they land for as long as a feed runs, whether or not a tab is polling."""
    while WATCH_BOOKS:
        try:
            feed = running_feed()
            if not feed:
                return
            with _paint_lock:
                ingest_book(feed)
        except Exception as e:
            print(f"Book watcher: {e!r}", file=sys.stderr, flush=True)
        time.sleep(WATCH_SECONDS)


def ensure_book_watcher():
    """Start the book watcher unless one is already running."""
    global _watcher
    with _watcher_lock:
        if WATCH_BOOKS and (_watcher is None or not _watcher.is_alive()):
            _watcher = threading.Thread(target=watch_books, name="book-watcher", daemon=True)
            _watcher.start()


def paint_desk(quantity, volatility, fee_tier, side, order_type, painted):
    """Build the painter's outputs: the whole desk, or only the clock parts when unchanged."""
    side = side or "buy"
    feed = read_feed()
    state, label, detail = feed_state(feed)
    line_text, line_tone = feed_line(state, label, detail, feed)
    line = {"text": line_text, "tone": line_tone, "now": time.time()}
    meta = feed or {}

    if feed and state != "down":
        ensure_book_watcher()
    # Polls run on several threads. The cached book, its counters and the latency history
    # change together, so each poll updates and reads them as one step.
    with _paint_lock:
        ingest_book(feed)
        book, count = orderbook_data, update_count
        book_key = [feed_started, data_last_modified]

        # This browser already shows this book for this order: send the feed state and the
        # ages, not the ladder, tiles and charts again. Keyed per browser, since each tab
        # paints alone.
        key = (["no book", meta.get("symbol"), meta.get("exchange")] if not book else
               [*book_key, quantity, volatility, fee_tier, side, order_type])
        r = None
        if book and key != painted:
            quantity = float(quantity or 0) or 1.0
            r = compute(book, quantity, float(volatility or 0.01), fee_tier, side, order_type)
            calc_latency_us.append(r["elapsed_us"])
            latency = list(calc_latency_us)

    freshness, banner = book_freshness(book, state)
    shaded = freshness not in ("fresh", "none")
    clock = {
        "feed-pill.className": f"pill {state}", "feed-label.children": label, "status-display.children": detail,
        "update-time.children": f"Update #{count} · {datetime.now().strftime('%H:%M:%S')}",
        "data-banner.children": banner,
        "data-banner.className": f"banner show {freshness}" if shaded else "banner",
        "kpis.className": f"kpis {freshness}" if shaded else "kpis",
        "ladder-wrap.className": freshness if shaded else "",
        "grid.className": f"grid {freshness}" if shaded else "grid",
        "feed-line.data": line, "painted.data": key,
    }
    if book:
        # Age of what is on screen, measured from when we received the book. The exchange
        # timestamp is kept in the raw panel; exchange clocks drift and would make this lie.
        clock["hdr-age.children"] = age_str(max(0.0, time.time() - float(book.get("local_time") or time.time())))
    if key == painted:
        return paint_only(clock)

    if not book:
        blank = "—"
        return paint_all({
            **clock, "hdr-symbol.children": meta.get("symbol", blank), "hdr-venue.children": meta.get("exchange") or blank,
            "hdr-mid.children": blank, "hdr-spread.children": blank, "hdr-micro.children": blank,
            "hdr-imb.children": blank, "hdr-imb.className": "stat-value", "hdr-age.children": blank,
            **{f"{tile}-value.children": blank for tile in TILES}, **{f"{tile}-sub.children": "" for tile in TILES},
            "asks-table.children": [], "bids-table.children": [],
            "spread-row.children": html.Span("No book yet", className="muted"),
            "ladder-note.children": "", "ladder-depth-note.children": "", "debug-info.children": "No data",
            "depth-chart.figure": empty_figure(), "latency-chart.figure": empty_figure("No samples"),
            "cost-breakdown-chart.figure": empty_figure(),
        })

    s, fill = r["stats"], r["fill"]
    dp = price_decimals(s["mid"])
    lat = np.asarray(latency)

    levels_hit = fill["levels"] if fill else 0
    ladder_note = ""
    if fill:
        ladder_note = f"fill {levels_hit} lvl · VWAP {fill['vwap']:,.{dp}f}"
        if not fill["complete"]:
            ladder_note += " · exceeds visible depth"
    # The ladder only draws LADDER_LEVELS rows. Say so when the order eats past them, rather
    # than letting the highlight run off the bottom with nothing to explain it.
    depth_note = ""
    if fill and not fill["complete"]:
        depth_note = "Order exceeds the visible book. The remainder is priced at the last level."
    elif levels_hit > LADDER_LEVELS:
        depth_note = f"Fill reaches level {levels_hit}; {LADDER_LEVELS} shown."
    asks_levels = [(float(p), float(q)) for p, q in book["asks"]]
    bids_levels = [(float(p), float(q)) for p, q in book["bids"]]
    max_cum = max(sum(p * q for p, q in asks_levels[:LADDER_LEVELS]),
                  sum(p * q for p, q in bids_levels[:LADDER_LEVELS]), 1.0)
    asks = build_ladder(asks_levels, "asks", levels_hit if side == "buy" else 0, max_cum)
    bids = build_ladder(bids_levels, "bids", levels_hit if side == "sell" else 0, max_cum)
    spread_row = [html.Span(f"{s['mid']:,.{dp}f}", className="spread-mid"),
                  html.Span(f"spread {s['spread']:,.{dp}f} · {s['spread_bps']:.2f} bps", className="muted")]

    imb_class = "stat-value pos" if s["imbalance"] > 0.1 else "stat-value neg" if s["imbalance"] < -0.1 else "stat-value"
    maker_pct = r["maker"] * 100

    return paint_all({
        **clock,
        "hdr-symbol.children": book.get("symbol") or meta.get("symbol", "—"),
        "hdr-venue.children": book.get("source") or meta.get("exchange") or "—",
        "hdr-mid.children": f"{s['mid']:,.{dp}f}",
        "hdr-spread.children": f"{s['spread_bps']:.2f} bps",
        "hdr-micro.children": f"{s['microprice']:,.{dp}f}",
        "hdr-imb.children": f"{s['imbalance']:+.2f}", "hdr-imb.className": imb_class,
        "netcost-value.children": usd(r["net"]), "netcost-sub.children": bps(r["net"], quantity),
        "slippage-value.children": usd(r["slippage"]),
        "slippage-sub.children": f"{bps(r['slippage'], quantity)} · {levels_hit} lvl",
        "impact-value.children": usd(r["impact"]), "impact-sub.children": bps(r["impact"], quantity),
        "fees-value.children": usd(r["fees"]), "fees-sub.children": f"{bps(r['fees'], quantity)} · {fee_tier}",
        "makertaker-value.children": f"{maker_pct:.0f} / {100 - maker_pct:.0f}",
        "makertaker-sub.children": "market orders always take" if order_type == "Market" else "est. passive fill",
        "latency-value.children": f"{r['elapsed_us']:,.0f}µs",
        "latency-sub.children": f"p50 {np.percentile(lat, 50):,.0f} · p99 {np.percentile(lat, 99):,.0f}µs",
        "asks-table.children": asks, "bids-table.children": bids, "spread-row.children": spread_row,
        "ladder-note.children": ladder_note, "ladder-depth-note.children": depth_note,
        "debug-info.children": raw_book_text(book),
        "depth-chart.figure": create_orderbook_depth_chart(book, fill=fill, dp=dp),
        "latency-chart.figure": create_latency_time_series(latency),
        "cost-breakdown-chart.figure": create_transaction_cost_breakdown(r["slippage"], r["fees"], r["impact"],
                                                                         quantity=quantity),
    })

@app.callback(
    Output("gemini-analysis", "children"),
    Input("generate-analysis-button", "n_clicks"),
    State("quantity-input", "value"),
    State("volatility-slider", "value"),
    State("fee-tier-dropdown", "value"),
    State("side-radio", "value"),
    prevent_initial_call=True,
    # One request at a time: a second click while Gemini is thinking would only spend the
    # free tier's per-minute quota on the same question.
    running=[(Output("generate-analysis-button", "disabled"), True, False),
             (Output("generate-analysis-button", "children"), "Thinking…", "Generate")],
)
def generate_gemini_analysis(_, quantity, volatility, fee_tier, side):
    book = orderbook_data
    if not book:
        return html.Span("No orderbook data available for analysis. Start a stream first.", className="warn")
    quantity = float(quantity or 0) or 1.0
    r = compute(book, quantity, float(volatility or 0.01), fee_tier, side or "buy", "Market")
    # The advisor re-quotes the order through its own tools; side and tier are passed so it
    # advises on the order actually selected rather than assuming a market buy.
    result = gemini_analyzer.analyze(book, quantity, r["fees"], r["slippage"], r["impact"],
                                     side=side or "buy", order_type="Market", fee_tier=fee_tier,
                                     volatility=float(volatility or 0.01))
    if not result.get("success"):
        return html.Span(result.get("analysis", "Analysis unavailable"), className="warn")
    sentiment = result.get("sentiment", "Neutral")
    tone = "pos" if sentiment == "Bullish" else "neg" if sentiment == "Bearish" else "muted"
    # When Gemini could not answer, the read comes from the rules; say so above it.
    notice = result.get("notice")
    return html.Div([
        html.P(notice, className="warn ai-notice") if notice else None,
        html.Div([html.Span(sentiment, className=f"tag {tone}"),
                  html.Span(result.get("strategy", ""), className="ai-strategy"),
                  html.Span(f"{result.get('expected_cost_bps', 0):.1f} bps quoted", className="muted mono"),
                  html.Span(f"{result.get('model', '')} · {datetime.now():%H:%M:%S}", className="muted mono")],
                 className="ai-head"),
        html.P(result.get("analysis", "")),
        html.P([html.Strong("Execution "), result.get("execution_approach", "")]),
        html.P([html.Strong("Why "), result.get("reasoning", "")], className="muted"),
        html.P([html.Strong("Risks "), "; ".join(result.get("risks", []) or ["none flagged"])],
               className="muted"),
        html.P(f"Tools used: {', '.join(result.get('tool_calls', [])) or 'none'} · book age {result.get('book_age', '?')}",
               className="muted mono"),
    ])


# ----------------------------------------------------------------------------- execution agent
_agent = None
_agent_lock = threading.Lock()
AGENT_PACE_S = 0.25   # seconds between a schedule's paper slices: the horizon, compressed for a demo
AGENT_DEADLINE_S = 45  # past this, a blocked plan goes to the approver rather than round again


def get_agent():
    """The execution agent, built on first use so LangGraph is only imported when someone plans."""
    global _agent
    with _agent_lock:
        if _agent is None:
            from agent.graph import ExecutionAgent
            from agent.planners import analyzer_planner
            _agent = ExecutionAgent(analyzer_planner(gemini_analyzer), book_source=lambda: orderbook_data,
                                    pace_s=AGENT_PACE_S, deadline_s=AGENT_DEADLINE_S)
        return _agent


AGENT_STATUS = {
    "awaiting_approval": ("Awaiting approval", "warn"),
    "executed": ("Executed (paper)", "pos"),
    "rejected": ("Rejected", "muted"),
    "expired": ("Expired: plan again", "muted"),
    "failed": ("No plan", "neg"),
}


def _trace_line(trace):
    """The graph's path as one line: consecutive price branches collapse to 'price x4'."""
    parts, i = [], 0
    while i < len(trace):
        step = trace[i]
        if step["node"] == "price":
            n = 0
            while i + n < len(trace) and trace[i + n]["node"] == "price":
                n += 1
            parts.append(f"price x{n}")
            i += n
            continue
        parts.append(f"{step['node']} {step['ms'] / 1000:.1f}s" if step["ms"] >= 100 else step["node"])
        i += 1
    return " \u2192 ".join(parts)


def render_agent(view):
    """The agent panel for one run's view."""
    from advisor.schema import strategy_label
    label, tone = AGENT_STATUS.get(view["status"], (view["status"], "muted"))
    if view["status"] == "expired" and not view.get("advice"):
        return html.Span("That plan is no longer held (it expired or the server restarted). Plan again.",
                         className="muted")
    advice, meta = view.get("advice") or {}, view.get("advisor") or {}
    rows = [html.P(meta["notice"], className="warn ai-notice") if meta.get("notice") else None]
    if not advice:
        rows.append(html.P("; ".join(meta.get("errors") or ["The advisor returned no plan."]), className="warn"))
        return html.Div(rows)
    revisions = view.get("revisions", 0)
    rows.append(html.Div([
        html.Span(label, className=f"tag {tone}"),
        html.Span(strategy_label(advice), className="ai-strategy"),
        html.Span(f"{advice.get('expected_cost_bps', 0):.2f} bps planned", className="muted mono"),
        html.Span(f"{meta.get('model', '')} · {revisions} revision{'s' if revisions != 1 else ''}",
                  className="muted mono"),
    ], className="ai-head"))
    rows.append(html.P(_trace_line(view.get("trace", [])), className="muted mono agent-trace"))

    priced = sorted(view.get("priced", []), key=lambda p: p["cost_bps"])
    rows.append(html.Table([
        html.Thead(html.Tr([html.Th("Plan"), html.Th("Cost bps"), html.Th("Book fills it")])),
        html.Tbody([html.Tr([
            html.Td(("\u25b8 " if p["advised"] else "") + p["label"], className="agent-advised" if p["advised"] else ""),
            html.Td(f"{p['cost_bps']:.2f}", className="mono"),
            html.Td("yes" if p["complete"] else "no, runs past it", className="" if p["complete"] else "warn"),
        ]) for p in priced]),
    ], className="agent-table"))

    findings = view.get("findings", [])
    blocked = [f for f in findings if f["severity"] == "block"]
    if blocked:
        rows.append(html.P(f"Unresolved after {revisions} revision{'s' if revisions != 1 else ''}: approve only "
                           "if you disagree with the critic.", className="neg agent-finding"))
    for f in findings:
        rows.append(html.P([html.Strong("Blocked " if f["severity"] == "block" else "Note "), f["message"]],
                           className=f"agent-finding {'neg' if f['severity'] == 'block' else 'muted'}"))
    for past in view.get("rounds", [])[:-1]:
        fixed = "; ".join(f["message"] for f in past["findings"] if f["severity"] == "block")
        rows.append(html.P(f"Round {past['round']} sent back: {fixed}", className="muted agent-finding"))
    if not findings and not view.get("rounds", [])[:-1]:
        rows.append(html.P("Critic: nothing to flag.", className="muted agent-finding"))

    result = view.get("execution")
    if result and result.get("status") in ("filled", "partial"):
        delta = result["vs_plan_bps"]
        rows.append(html.P([
            html.Strong("Filled "),
            f"${result['filled_notional']:,.0f} in {result['slices']} slice{'s' if result['slices'] != 1 else ''} at "
            f"{result['vwap']:,.6g} vs arrival mid {result['arrival_mid']:,.6g}: ",
            html.Span(f"{result['shortfall_bps']:.2f} bps", className="mono"),
            f" all in, {'+' if delta >= 0 else ''}{delta:.2f} bps against plan",
            " (part of it priced past the visible book)" if result.get("beyond_visible_book") else "",
            ".",
        ]))
    elif result:
        rows.append(html.P(result.get("note", ""), className="muted"))
    return html.Div(rows)


@app.callback(
    Output("agent-body", "children"),
    Output("agent-thread", "data"),
    Output("agent-actions", "style"),
    Input("agent-plan-button", "n_clicks"),
    State("quantity-input", "value"),
    State("volatility-slider", "value"),
    State("fee-tier-dropdown", "value"),
    State("side-radio", "value"),
    prevent_initial_call=True,
    running=[(Output("agent-plan-button", "disabled"), True, False),
             (Output("agent-plan-button", "children"), "Planning…", "Plan")],
)
def plan_execution(_, quantity, volatility, fee_tier, side):
    hidden = {"display": "none"}
    book = orderbook_data
    if not book:
        return html.Span("No book to plan against yet. Start a stream first.", className="warn"), None, hidden
    order = {"side": side or "buy", "notional": float(quantity or 0) or 1.0, "order_type": "Market",
             "fee_tier": fee_tier, "volatility": float(volatility or 0.01)}
    try:
        thread, view = get_agent().start(order, book)
    except Exception as e:  # the panel must never take the page down
        print(f"Execution agent failed: {e!r}", file=sys.stderr, flush=True)
        return html.Span(f"Planning failed: {e}", className="neg"), None, hidden
    waiting = view["status"] == "awaiting_approval"
    return render_agent(view), thread if waiting else None, {} if waiting else hidden


@app.callback(
    Output("agent-body", "children", allow_duplicate=True),
    Output("agent-thread", "data", allow_duplicate=True),
    Output("agent-actions", "style", allow_duplicate=True),
    Input("agent-approve-button", "n_clicks"),
    Input("agent-reject-button", "n_clicks"),
    State("agent-thread", "data"),
    prevent_initial_call=True,
    running=[(Output("agent-approve-button", "disabled"), True, False),
             (Output("agent-reject-button", "disabled"), True, False)],
)
def decide_execution(_approve, _reject, thread):
    if not thread:
        return dash.no_update, None, {"display": "none"}
    try:
        view = get_agent().resume(thread, approved=ctx.triggered_id == "agent-approve-button")
    except Exception as e:
        print(f"Execution agent failed: {e!r}", file=sys.stderr, flush=True)
        return html.Span(f"Execution failed: {e}", className="neg"), None, {"display": "none"}
    return render_agent(view), None, {"display": "none"}


@app.callback(
    Output("download-data", "data"),
    Output("export-note", "children"),
    Input("export-csv-button", "n_clicks"),
    Input("export-excel-button", "n_clicks"),
    prevent_initial_call=True,
)
def export_data(csv_clicks, excel_clicks):
    """Export the current book, and say so when there is nothing to export."""
    book = orderbook_data
    if not book:
        return dash.no_update, html.Span("Nothing to export yet. Start a stream first.", className="warn")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if ctx.triggered_id == "export-csv-button":
        content = export_orderbook_to_csv(book)
        if content:
            return (dcc.send_string(base64.b64decode(content).decode("utf-8"), filename=f"orderbook_{stamp}.csv"),
                    f"Saved orderbook_{stamp}.csv")
    elif ctx.triggered_id == "export-excel-button":
        content = export_orderbook_to_excel(book)
        if content:
            return (dcc.send_bytes(base64.b64decode(content), filename=f"orderbook_{stamp}.xlsx"),
                    f"Saved orderbook_{stamp}.xlsx")
    return dash.no_update, html.Span("Export failed. See the server log.", className="neg")


def stop_own_feed():
    """On shutdown, stop the feed this process started and drop its record."""
    if client_process is None:
        return
    feed = read_feed()
    if feed and feed.get("pid") == client_process.pid:
        remove_file(FEED_FILE)
    stop_websocket_client(client_process)


def _shutdown(*_):
    stop_own_feed()
    sys.exit(0)


# A record left by a previous run whose client is gone would show the feed as Offline on a
# fresh start; the desk simply starts stopped.
if read_feed() and not running_feed():
    remove_file(FEED_FILE)
atexit.register(stop_own_feed)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        port = int(os.environ.get("PORT", 8050))
        app.run(debug=False, use_reloader=False, host="0.0.0.0", port=port)
    finally:
        stop_own_feed()
