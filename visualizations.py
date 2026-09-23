"""Plotly figures in the terminal theme. Function names and required arguments are unchanged."""
import numpy as np
import plotly.graph_objs as go

BID = "#2ebd85"
ASK = "#f6465d"
ACCENT = "#ffb000"
MUTED = "#6b7480"
GRID = "#171b21"
TEXT = "#aab2bd"
MONO = "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace"


def _base_layout(fig, height=None, **kw):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=MONO, size=10, color=TEXT),
        margin=dict(l=48, r=12, t=8, b=28),
        showlegend=False,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#0d0f12", bordercolor="#2a3039", font=dict(family=MONO, size=11)),
        uirevision="keep",
        **kw,
    )
    if height:
        fig.update_layout(height=height)
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, tickfont=dict(color=MUTED), showspikes=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, tickfont=dict(color=MUTED))
    return fig


def empty_figure(message="Waiting for book", height=None):
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font=dict(family=MONO, size=11, color=MUTED),
                       xref="paper", yref="paper", x=0.5, y=0.5)
    _base_layout(fig, height)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


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

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=bids[:, 0], y=bid_cum, name="Bids", mode="lines", line=dict(color=BID, width=1.5, shape="hv"),
        fill="tozeroy", fillcolor="rgba(46,189,133,0.12)",
        hovertemplate=f"bid %{{x:,.{dp}f}}<br>cum $%{{y:,.0f}}<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=asks[:, 0], y=ask_cum, name="Asks", mode="lines", line=dict(color=ASK, width=1.5, shape="hv"),
        fill="tozeroy", fillcolor="rgba(246,70,93,0.12)",
        hovertemplate=f"ask %{{x:,.{dp}f}}<br>cum $%{{y:,.0f}}<extra></extra>"))
    fig.add_vline(x=mid, line=dict(color=MUTED, width=1, dash="dot"))
    if fill:
        fig.add_vline(x=fill["vwap"], line=dict(color=ACCENT, width=1.5))
        fig.add_annotation(x=fill["vwap"], y=1, yref="paper", text=f"VWAP {fill['vwap']:,.{dp}f}", showarrow=False,
                           xanchor="left", yanchor="top", xshift=4,
                           font=dict(family=MONO, size=10, color=ACCENT))
    _base_layout(fig, height)
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    fig.update_xaxes(tickformat=f",.{dp}f")
    return fig


def create_latency_time_series(latency_history, height=None, unit="µs"):
    """Calculation latency per tick with p50 and p99 guides."""
    if not latency_history:
        return empty_figure("No samples", height)
    y = np.asarray(latency_history, dtype=float)
    p50, p99 = np.percentile(y, 50), np.percentile(y, 99)
    fig = go.Figure()
    fig.add_trace(go.Scatter(y=y, mode="lines", line=dict(color=ACCENT, width=1.2),
                             fill="tozeroy", fillcolor="rgba(255,176,0,0.07)",
                             hovertemplate=f"%{{y:,.0f}} {unit}<extra></extra>"))
    for val, label, pos in ((p50, "p50", "bottom left"), (p99, "p99", "top left")):
        fig.add_hline(y=val, line=dict(color=MUTED, width=1, dash="dot"),
                      annotation_text=f"{label} {val:,.0f}", annotation_position=pos,
                      annotation_font=dict(family=MONO, size=9, color=MUTED))
    _base_layout(fig, height)
    fig.update_xaxes(showticklabels=False)
    fig.update_yaxes(ticksuffix=f" {unit}", rangemode="tozero")
    return fig


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
    fig = go.Figure()
    for name, val, color in parts:
        v = max(val, 0) * scale
        fig.add_trace(go.Bar(y=["cost"], x=[v], name=f"{name} {v:,.2f}", orientation="h",
                             marker=dict(color=color, line_width=0),
                             hovertemplate=f"{name}: %{{x:,.3f}} {unit}<extra></extra>"))
    _base_layout(fig, height, barmode="stack", bargap=0.35)
    fig.update_layout(hovermode="closest", margin=dict(l=8, r=12, t=4, b=40), showlegend=True,
                      legend=dict(orientation="h", traceorder="normal", yanchor="top", y=-0.45,
                                  xanchor="left", x=0, font=dict(family=MONO, size=9, color=MUTED),
                                  itemclick=False, itemdoubleclick=False, bgcolor="rgba(0,0,0,0)"))
    fig.update_yaxes(visible=False)
    fig.update_xaxes(ticksuffix=f" {unit}" if quantity else "", tickprefix="" if quantity else "$")
    return fig
