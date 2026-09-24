"""
Plotly figures in the terminal theme, built as plain dicts.

The desk repaints every poll, and building go.Figure objects cost ~15 ms a chart in plotly's
validation alone, most of each poll's server time. Plain dicts are the same JSON the browser
receives, so they are built in well under a millisecond. The tests still pass every figure
through go.Figure, so a typo in a property name fails there rather than in the browser.
"""
import numpy as np

BID = "#2ebd85"
ASK = "#f6465d"
ACCENT = "#ffb000"
MUTED = "#6b7480"
GRID = "#171b21"
TEXT = "#aab2bd"
MONO = "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"


def _axis(**kw):
    axis = {"gridcolor": GRID, "zeroline": False, "linecolor": GRID, "tickfont": {"color": MUTED}}
    axis.update(kw)
    return axis


def _layout(height=None, xaxis=None, yaxis=None, **kw):
    layout = {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": MONO, "size": 10, "color": TEXT},
        "margin": {"l": 48, "r": 12, "t": 8, "b": 28},
        "showlegend": False,
        "hovermode": "x unified",
        "hoverlabel": {"bgcolor": "#0d0f12", "bordercolor": "#2a3039", "font": {"family": MONO, "size": 11}},
        "uirevision": "keep",
        "xaxis": _axis(showspikes=False, **(xaxis or {})),
        "yaxis": _axis(**(yaxis or {})),
    }
    layout.update(kw)
    if height:
        layout["height"] = height
    return layout


def _vline(x, **line):
    return {"type": "line", "xref": "x", "yref": "y domain", "x0": x, "x1": x, "y0": 0, "y1": 1, "line": line}


def _hline(y, **line):
    return {"type": "line", "xref": "x domain", "yref": "y", "x0": 0, "x1": 1, "y0": y, "y1": y, "line": line}


def empty_figure(message="Waiting for book", height=None):
    return {"data": [], "layout": _layout(
        height, xaxis={"visible": False}, yaxis={"visible": False},
        annotations=[{"text": message, "showarrow": False, "xref": "paper", "yref": "paper", "x": 0.5, "y": 0.5,
                      "font": {"family": MONO, "size": 11, "color": MUTED}}])}


def create_orderbook_depth_chart(orderbook_data, fill=None, height=None, dp=2):
    """
    Cumulative USD depth per side as step lines, with the mid and the order's fill marked.

    `dp` is the price precision the rest of the screen is using, so the axis, the hovers and
    the VWAP marker read the same number of decimals as the ladder and the header.
    """
    if not orderbook_data or not orderbook_data.get("bids") or not orderbook_data.get("asks"):
        return empty_figure(height=height)

    bids = np.array(orderbook_data["bids"], dtype=float)[:, :2]
    asks = np.array(orderbook_data["asks"], dtype=float)[:, :2]
    bids = bids[np.argsort(-bids[:, 0])]
    asks = asks[np.argsort(asks[:, 0])]
    bid_cum = np.cumsum(bids[:, 0] * bids[:, 1])
    ask_cum = np.cumsum(asks[:, 0] * asks[:, 1])
    mid = (bids[0, 0] + asks[0, 0]) / 2

    def side(prices, cum, name, color, fillcolor, label):
        return {"type": "scatter", "x": prices.tolist(), "y": cum.tolist(), "name": name, "mode": "lines",
                "line": {"color": color, "width": 1.5, "shape": "hv"}, "fill": "tozeroy", "fillcolor": fillcolor,
                "hovertemplate": f"{label} %{{x:,.{dp}f}}<br>cum $%{{y:,.0f}}<extra></extra>"}

    shapes = [_vline(mid, color=MUTED, width=1, dash="dot")]
    annotations = []
    if fill:
        shapes.append(_vline(fill["vwap"], color=ACCENT, width=1.5))
        annotations.append({"x": fill["vwap"], "y": 1, "yref": "paper", "text": f"VWAP {fill['vwap']:,.{dp}f}",
                            "showarrow": False, "xanchor": "left", "yanchor": "top", "xshift": 4,
                            "font": {"family": MONO, "size": 10, "color": ACCENT}})
    return {
        "data": [side(bids[:, 0], bid_cum, "Bids", BID, "rgba(46,189,133,0.12)", "bid"),
                 side(asks[:, 0], ask_cum, "Asks", ASK, "rgba(246,70,93,0.12)", "ask")],
        "layout": _layout(height, xaxis={"tickformat": f",.{dp}f"}, yaxis={"tickprefix": "$", "tickformat": "~s"},
                          shapes=shapes, annotations=annotations),
    }


def create_latency_time_series(latency_history, height=None, unit="µs"):
    """Calculation latency per tick with p50 and p99 guides."""
    if not latency_history:
        return empty_figure("No samples", height)
    y = np.asarray(latency_history, dtype=float)
    shapes, annotations = [], []
    for q, label, yanchor in ((50, "p50", "top"), (99, "p99", "bottom")):
        val = float(np.percentile(y, q))
        shapes.append(_hline(val, color=MUTED, width=1, dash="dot"))
        annotations.append({"x": 0, "xref": "x domain", "y": val, "yref": "y", "text": f"{label} {val:,.0f}",
                            "showarrow": False, "xanchor": "left", "yanchor": yanchor,
                            "font": {"family": MONO, "size": 9, "color": MUTED}})
    return {
        "data": [{"type": "scatter", "y": y.tolist(), "mode": "lines", "line": {"color": ACCENT, "width": 1.2},
                  "fill": "tozeroy", "fillcolor": "rgba(255,176,0,0.07)",
                  "hovertemplate": f"%{{y:,.0f}} {unit}<extra></extra>"}],
        "layout": _layout(height, xaxis={"showticklabels": False},
                          yaxis={"ticksuffix": f" {unit}", "rangemode": "tozero"},
                          shapes=shapes, annotations=annotations),
    }


def create_transaction_cost_breakdown(slippage, fees, impact, quantity=None, height=None):
    """
    Horizontal stacked bar of cost components, in bps of notional when quantity is given.

    A component worth a fraction of a percent of the total is narrower than its own label, and
    plotly silently drops text that will not fit, which left the screen showing a "cost stack"
    with the slippage component missing. The bar carries the proportions and the legend carries
    every component with its value, in the order they are stacked, so nothing can hide.
    """
    parts = [("Slippage", slippage, "#7aa2f7"), ("Impact", impact, ACCENT), ("Fees", fees, "#8b93a1")]
    scale = 1e4 / quantity if quantity else 1.0
    unit = "bps" if quantity else "$"
    data = []
    for name, val, color in parts:
        v = max(val, 0) * scale
        data.append({"type": "bar", "y": ["cost"], "x": [v], "name": f"{name} {v:,.2f}", "orientation": "h",
                     "marker": {"color": color, "line": {"width": 0}},
                     "hovertemplate": f"{name}: %{{x:,.3f}} {unit}<extra></extra>"})
    return {"data": data, "layout": _layout(
        height, barmode="stack", bargap=0.35, hovermode="closest",
        margin={"l": 8, "r": 12, "t": 4, "b": 40}, showlegend=True,
        legend={"orientation": "h", "traceorder": "normal", "yanchor": "top", "y": -0.45, "xanchor": "left", "x": 0,
                "font": {"family": MONO, "size": 9, "color": MUTED}, "itemclick": False, "itemdoubleclick": False,
                "bgcolor": "rgba(0,0,0,0)"},
        xaxis={"ticksuffix": f" {unit}" if quantity else "", "tickprefix": "" if quantity else "$"},
        yaxis={"visible": False})}
