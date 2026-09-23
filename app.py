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
from collections import deque
from datetime import datetime, timezone

import dash
import numpy as np
from dash import Input, Output, State, ctx, dcc, html

from export import export_orderbook_to_csv, export_orderbook_to_excel
from fee_model import calculate_fees
from gemini_integration import GeminiAnalyzer
from models import (book_stats, estimate_market_impact, estimate_slippage,
                    predict_maker_taker, walk_book)
from visualizations import (create_latency_time_series, create_orderbook_depth_chart,
                            create_transaction_cost_breakdown, empty_figure)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERBOOK_FILE = os.path.join(BASE_DIR, "latest_orderbook.json")
STATUS_FILE = os.path.join(BASE_DIR, "feed_status.json")
LADDER_LEVELS = 12
STALE_AFTER = 5.0  # seconds without a new book before the feed is shown as stale

VENUES = [("OKX", "OKX"), ("Hyperliquid", "HYPERLIQUID"), ("Binance", "BINANCE"),
          ("Kraken", "KRAKEN"), ("Simulated", "SIM")]

# ----------------------------------------------------------------------------- state
# One feed per server process; this is a single-user simulator.
_lock = threading.Lock()
client_process = None
stream_meta = {}
orderbook_data = None
data_last_modified = 0.0
update_count = 0
calc_latency_us = deque(maxlen=300)

gemini_analyzer = GeminiAnalyzer()

# ----------------------------------------------------------------------------- app
app = dash.Dash(__name__, title="Quant Trade Simulator", update_title=None,
                meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}])
server = app.server  # for gunicorn: gunicorn app:server

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
def start_websocket_client(symbol, exchange="OKX"):
    for path in (ORDERBOOK_FILE, STATUS_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    cmd = [sys.executable, os.path.join(BASE_DIR, "websocket_client.py"),
           "--symbol", symbol, "--exchange", exchange, "--output", ORDERBOOK_FILE]
    return subprocess.Popen(cmd, cwd=BASE_DIR)


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


def feed_state():
    """(state, label, detail) derived from the process and the status file the client writes."""
    status = read_json(STATUS_FILE) or {}
    proc = client_process
    if proc is None:
        return "idle", "Idle", "Press Start to connect a feed"
    code = proc.poll()
    if code is not None:
        err = status.get("error") or f"exited with code {code}"
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


# ----------------------------------------------------------------------------- layout helpers
def field(label, control, hint=None):
    return html.Div([html.Label(label, className="field-label"), control,
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
                field("Volatility (σ)", html.Div([
                    dcc.Slider(id="volatility-slider", min=0.001, max=0.05, step=0.001, value=0.01,
                               marks=None, tooltip=None, updatemode="drag", className="slider"),
                    html.Span("0.010", id="volatility-readout", className="readout mono"),
                ], className="slider-row"), "Scales the market impact estimate"),
                field("Fee tier", dcc.Dropdown(id="fee-tier-dropdown",
                                               options=[{"label": t, "value": t} for t in ("Tier 1", "Tier 2", "Tier 3")],
                                               value="Tier 1", clearable=False, searchable=False, className="dd")),
                html.Div([
                    html.Button("Start stream", id="start-button", className="btn btn-go"),
                    html.Button("Stop", id="stop-button", className="btn btn-stop"),
                ], className="btn-row"),
            ], className="ticket"),

            panel("Feed", [
                html.Div(id="status-display", className="feed-detail"),
                html.Div(id="update-time", className="feed-meta mono"),
            ]),

            panel("Export", [
                html.Div([
                    html.Button("CSV", id="export-csv-button", className="btn btn-ghost"),
                    html.Button("XLSX", id="export-excel-button", className="btn btn-ghost"),
                ], className="btn-row"),
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
            ], className="kpis"),

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
        ], className="col col-center"),

        # ---------------- right: ladder
        html.Div([
            panel("Book", [
                html.Div([html.Span("Price"), html.Span("Size"), html.Span("Cum $")], className="ladder-head"),
                html.Div(id="asks-table", className="ladder asks"),
                html.Div(id="spread-row", className="spread-row mono"),
                html.Div(id="bids-table", className="ladder bids"),
            ], extra=html.Span("", id="ladder-note", className="panel-note"), className="ladder-panel"),
        ], className="col col-right"),
    ], className="grid"),

    dcc.Interval(id="interval-component", interval=500, n_intervals=0),
    dcc.Interval(id="chart-interval-component", interval=1000, n_intervals=0),
    dcc.Interval(id="clock-interval", interval=1000, n_intervals=0),
], className="app")


# ----------------------------------------------------------------------------- formatting
def usd(v, dp=2):
    return f"${v:,.{dp}f}" if abs(v) >= 0.01 or v == 0 else f"${v:,.4f}"


def bps(v, quantity):
    return f"{v / quantity * 1e4:,.2f} bps" if quantity else "—"


def price_decimals(px):
    return 1 if px >= 1000 else 2 if px >= 10 else 4


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


def clean_symbol(symbol):
    symbol = (symbol or "").strip().upper()
    return symbol if re.fullmatch(r"[A-Z0-9]{2,12}(-[A-Z0-9]{2,12}){1,2}", symbol) else None


def compute(book, quantity, volatility, fee_tier, side, order_type):
    t0 = time.perf_counter()
    fill = walk_book(book, quantity, side)
    slippage = estimate_slippage(book, quantity, volatility, side=side)
    fees = calculate_fees(quantity, fee_tier)
    impact = estimate_market_impact(book, quantity, volatility)
    maker = predict_maker_taker(book, quantity, order_type)
    stats = book_stats(book)
    elapsed_us = (time.perf_counter() - t0) * 1e6
    return dict(fill=fill, slippage=slippage, fees=fees, impact=impact, maker=maker, stats=stats,
                net=slippage + fees + impact, elapsed_us=elapsed_us)


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


@app.callback(Output("hdr-clock", "children"), Input("clock-interval", "n_intervals"))
def tick_clock(_):
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


@app.callback(
    Output("status-display", "children"),
    Output("chart-interval-component", "disabled"),
    Input("start-button", "n_clicks"),
    Input("stop-button", "n_clicks"),
    State("asset-input", "value"),
    State("exchange-dropdown", "value"),
    prevent_initial_call=True,
)
def handle_stream_control(start_clicks, stop_clicks, asset, exchange):
    global client_process, stream_meta, orderbook_data, data_last_modified, update_count
    with _lock:
        if ctx.triggered_id == "start-button":
            symbol = clean_symbol(asset)
            if not symbol:
                return html.Span("Instrument must look like BTC-USDT-SWAP", className="neg"), False
            stop_websocket_client(client_process)
            orderbook_data, data_last_modified, update_count = None, 0.0, 0
            calc_latency_us.clear()
            client_process = start_websocket_client(symbol, exchange or "OKX")
            stream_meta = {"symbol": symbol, "exchange": exchange, "started": time.time()}
            return f"Requested {exchange} feed for {symbol}", False
        if ctx.triggered_id == "stop-button":
            stop_websocket_client(client_process)
            client_process = None
            return "Stream stopped. Last book kept for review.", True
    return dash.no_update, dash.no_update


@app.callback(
    Output("feed-pill", "className"),
    Output("feed-label", "children"),
    Output("update-time", "children"),
    Output("hdr-symbol", "children"),
    Output("hdr-venue", "children"),
    Output("hdr-mid", "children"),
    Output("hdr-spread", "children"),
    Output("hdr-micro", "children"),
    Output("hdr-imb", "children"),
    Output("hdr-imb", "className"),
    Output("hdr-age", "children"),
    Output("netcost-value", "children"), Output("netcost-sub", "children"),
    Output("slippage-value", "children"), Output("slippage-sub", "children"),
    Output("impact-value", "children"), Output("impact-sub", "children"),
    Output("fees-value", "children"), Output("fees-sub", "children"),
    Output("makertaker-value", "children"), Output("makertaker-sub", "children"),
    Output("latency-value", "children"), Output("latency-sub", "children"),
    Output("asks-table", "children"),
    Output("bids-table", "children"),
    Output("spread-row", "children"),
    Output("ladder-note", "children"),
    Output("debug-info", "children"),
    Input("interval-component", "n_intervals"),
    Input("quantity-input", "value"),
    Input("volatility-slider", "value"),
    Input("fee-tier-dropdown", "value"),
    Input("side-radio", "value"),
    Input("order-type-dropdown", "value"),
)
def update_tables(_, quantity, volatility, fee_tier, side, order_type):
    global orderbook_data, data_last_modified, update_count
    state, label, detail = feed_state()
    pill = f"pill {state}"

    new_data, modified = read_orderbook_data(data_last_modified)
    if new_data and client_process is not None:
        orderbook_data, data_last_modified = new_data, modified
        update_count += 1

    book = orderbook_data
    feed_meta = f"{detail}\nUpdate #{update_count} · {datetime.now().strftime('%H:%M:%S')}"
    if not book:
        blank = "—"
        return (pill, label, feed_meta, stream_meta.get("symbol", "—"), stream_meta.get("exchange") or "—",
                blank, blank, blank, blank, "stat-value", blank,
                blank, "", blank, "", blank, "", blank, "", blank, "", blank, "",
                [], [], html.Span("No book yet", className="muted"), "", "No data")

    quantity = float(quantity or 0) or 1.0
    volatility = float(volatility or 0.01)
    r = compute(book, quantity, volatility, fee_tier, side or "buy", order_type)
    calc_latency_us.append(r["elapsed_us"])
    s, fill = r["stats"], r["fill"]
    dp = price_decimals(s["mid"])

    now_ms = time.time() * 1000
    age_ms = max(0.0, now_ms - float(book.get("timestamp") or now_ms))
    lat = np.asarray(calc_latency_us)

    levels_hit = fill["levels"] if fill else 0
    ladder_note = ""
    if fill:
        ladder_note = f"fill {levels_hit} lvl · VWAP {fill['vwap']:,.{dp}f}"
        if not fill["complete"]:
            ladder_note += " · exceeds visible depth"
    asks_levels = [(float(p), float(q)) for p, q in book["asks"]]
    bids_levels = [(float(p), float(q)) for p, q in book["bids"]]
    max_cum = max(sum(p * q for p, q in asks_levels[:LADDER_LEVELS]),
                  sum(p * q for p, q in bids_levels[:LADDER_LEVELS]), 1.0)
    side = side or "buy"
    asks = build_ladder(asks_levels, "asks", levels_hit if side == "buy" else 0, max_cum)
    bids = build_ladder(bids_levels, "bids", levels_hit if side == "sell" else 0, max_cum)
    spread_row = [html.Span(f"{s['mid']:,.{dp}f}", className="spread-mid"),
                  html.Span(f"spread {s['spread']:,.{dp}f} · {s['spread_bps']:.2f} bps", className="muted")]

    imb_class = "stat-value pos" if s["imbalance"] > 0.1 else "stat-value neg" if s["imbalance"] < -0.1 else "stat-value"
    maker_pct = r["maker"] * 100

    return (
        pill, label, feed_meta,
        book.get("symbol") or stream_meta.get("symbol", "—"),
        book.get("source") or stream_meta.get("exchange") or "—",
        f"{s['mid']:,.{dp}f}",
        f"{s['spread_bps']:.2f}bp",
        f"{s['microprice']:,.{dp}f}",
        f"{s['imbalance']:+.2f}", imb_class,
        f"{age_ms:,.0f}ms",
        usd(r["net"], 4), bps(r["net"], quantity),
        usd(r["slippage"], 4), f"{bps(r['slippage'], quantity)} · {levels_hit} lvl",
        usd(r["impact"], 4), bps(r["impact"], quantity),
        usd(r["fees"], 4), f"{bps(r['fees'], quantity)} · {fee_tier}",
        f"{maker_pct:.0f} / {100 - maker_pct:.0f}", "market orders always take" if order_type == "Market" else "est. passive fill",
        f"{r['elapsed_us']:,.0f}µs", f"p50 {np.percentile(lat, 50):,.0f} · p99 {np.percentile(lat, 99):,.0f}µs",
        asks, bids, spread_row, ladder_note,
        json.dumps(book, indent=1)[:6000],
    )


@app.callback(
    Output("depth-chart", "figure"),
    Output("latency-chart", "figure"),
    Output("cost-breakdown-chart", "figure"),
    Input("chart-interval-component", "n_intervals"),
    Input("quantity-input", "value"),
    Input("volatility-slider", "value"),
    Input("fee-tier-dropdown", "value"),
    Input("side-radio", "value"),
)
def update_chart_displays(_, quantity, volatility, fee_tier, side):
    book = orderbook_data
    if not book:
        return empty_figure(), empty_figure("No samples"), empty_figure()
    quantity = float(quantity or 0) or 1.0
    r = compute(book, quantity, float(volatility or 0.01), fee_tier, side or "buy", "Market")
    return (
        create_orderbook_depth_chart(book, fill=r["fill"]),
        create_latency_time_series(list(calc_latency_us)),
        create_transaction_cost_breakdown(r["slippage"], r["fees"], r["impact"], quantity=quantity),
    )


@app.callback(
    Output("gemini-analysis", "children"),
    Input("generate-analysis-button", "n_clicks"),
    State("quantity-input", "value"),
    State("volatility-slider", "value"),
    State("fee-tier-dropdown", "value"),
    State("side-radio", "value"),
    prevent_initial_call=True,
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
    return html.Div([
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


@app.callback(
    Output("download-data", "data"),
    Input("export-csv-button", "n_clicks"),
    Input("export-excel-button", "n_clicks"),
    prevent_initial_call=True,
)
def export_data(csv_clicks, excel_clicks):
    book = orderbook_data
    if not book:
        return dash.no_update
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if ctx.triggered_id == "export-csv-button":
        content = export_orderbook_to_csv(book)
        if content:
            return dcc.send_string(base64.b64decode(content).decode("utf-8"), filename=f"orderbook_{stamp}.csv")
    elif ctx.triggered_id == "export-excel-button":
        content = export_orderbook_to_excel(book)
        if content:
            return dcc.send_bytes(base64.b64decode(content), filename=f"orderbook_{stamp}.xlsx")
    return dash.no_update


def _shutdown(*_):
    stop_websocket_client(client_process)
    sys.exit(0)


atexit.register(lambda: stop_websocket_client(client_process))

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        port = int(os.environ.get("PORT", 8050))
        app.run(debug=False, use_reloader=False, host="0.0.0.0", port=port)
    finally:
        stop_websocket_client(client_process)
