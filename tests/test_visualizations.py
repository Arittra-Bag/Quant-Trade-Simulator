"""The charts are plain dicts for speed; plotly still validates every one of them here."""
import plotly.graph_objs as go
import pytest

from visualizations import (create_latency_time_series, create_orderbook_depth_chart,
                            create_transaction_cost_breakdown, empty_figure)

BOOK = {"asks": [[str(100 + i), "1.5"] for i in range(1, 26)],
        "bids": [[str(100 - i), "2"] for i in range(1, 26)]}

FIGURES = [
    ("empty", lambda: empty_figure()),
    ("depth", lambda: create_orderbook_depth_chart(BOOK, fill={"vwap": 101.2}, dp=1)),
    ("depth no fill", lambda: create_orderbook_depth_chart(BOOK)),
    ("depth no book", lambda: create_orderbook_depth_chart(None)),
    ("latency", lambda: create_latency_time_series([120, 90, 300, 80])),
    ("latency empty", lambda: create_latency_time_series([])),
    ("cost bps", lambda: create_transaction_cost_breakdown(1.0, 2.0, 3.0, quantity=10_000)),
    ("cost usd", lambda: create_transaction_cost_breakdown(1.0, 2.0, 3.0)),
]


@pytest.mark.parametrize("name,build", FIGURES, ids=[n for n, _ in FIGURES])
def test_every_figure_is_valid_plotly(name, build):
    """Every figure dict passes plotly's validation."""
    go.Figure(build())  # raises on any unknown property or bad value


def test_depth_marks_mid_and_vwap():
    """The depth chart marks the mid and the fill's VWAP."""
    fig = create_orderbook_depth_chart(BOOK, fill={"vwap": 101.2}, dp=1)
    assert [s["x0"] for s in fig["layout"]["shapes"]] == [100.0, 101.2]
    assert fig["layout"]["annotations"][0]["text"] == "VWAP 101.2"


@pytest.mark.parametrize("name,build", FIGURES, ids=[n for n, _ in FIGURES])
def test_figures_are_plain_dicts(name, build):
    """go.Figure objects cost ~15 ms each in validation on every poll; dicts cost nothing."""
    fig = build()
    assert type(fig) is dict and set(fig) == {"data", "layout"}
