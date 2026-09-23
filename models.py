"""
Pre-trade cost models.

All functions keep their original signatures. Books are {"bids": [[px, sz]], "asks": [[px, sz]]}
with sizes in base-asset units, best level first. Quantities are order notional in USD.
Costs are returned in USD and are always >= 0.
"""
import math

import numpy as np
from sklearn.linear_model import LinearRegression

# --- Fallback slippage regression -----------------------------------------------------
# Only used when there is no book to walk. Features: [order size (USD), volatility].
X_slip = np.array([[100, 0.01], [200, 0.01], [100, 0.02], [200, 0.02]])
y_slip = np.array([0.02, 0.05, 0.03, 0.07])
slippage_model = LinearRegression().fit(X_slip, y_slip)


def _best(orderbook):
    top_bid = float(orderbook["bids"][0][0])
    top_ask = float(orderbook["asks"][0][0])
    return top_bid, top_ask, (top_bid + top_ask) / 2


def walk_book(orderbook, quantity, side="buy"):
    """
    Fill `quantity` USD against the book.

    Returns dict(vwap, filled_usd, filled_base, levels, worst_price, complete) or None
    if the book is empty. When the visible book is too thin the remainder is priced at
    the last level (complete=False) so the estimate stays conservative-but-finite.
    """
    levels = orderbook.get("asks" if side == "buy" else "bids") or []
    remaining = float(quantity)
    if remaining <= 0 or not levels:
        return None
    filled_usd = filled_base = 0.0
    used = 0
    px = float(levels[0][0])
    for px, sz in levels:
        px, sz = float(px), float(sz)
        level_usd = px * sz
        take = min(remaining, level_usd)
        filled_usd += take
        filled_base += take / px
        remaining -= take
        used += 1
        if remaining <= 1e-12:
            break
    complete = remaining <= 1e-12
    if not complete:
        filled_usd += remaining
        filled_base += remaining / px
    return {
        "vwap": filled_usd / filled_base,
        "filled_usd": filled_usd,
        "filled_base": filled_base,
        "levels": used,
        "worst_price": px,
        "complete": complete,
    }


def estimate_slippage(orderbook, quantity, volatility=0.01, side="buy"):
    """
    Expected slippage in USD versus mid, from walking the live book.

    slippage = |VWAP - mid| / mid * quantity. This includes the half-spread, which is the
    cost a market order actually pays. Falls back to the regression model when no book
    is available.
    """
    try:
        fill = walk_book(orderbook, quantity, side) if orderbook else None
        if fill:
            _, _, mid = _best(orderbook)
            return abs(fill["vwap"] - mid) / mid * float(quantity)
        pred = slippage_model.predict(np.array([[float(quantity), float(volatility)]]))[0]
        return max(0.0, float(pred))
    except Exception as e:
        print(f"Error estimating slippage: {e}")
        return 0.0


def visible_depth_usd(orderbook, levels=None):
    """Total USD resting on both sides of the visible book."""
    total = 0.0
    for side in ("bids", "asks"):
        for px, sz in (orderbook.get(side) or [])[:levels]:
            total += float(px) * float(sz)
    return total


def estimate_market_impact(orderbook, quantity, volatility=0.01, T=1, gamma=0.1, eta=0.5):
    """
    Almgren-Chriss style impact scaled by volatility and visible liquidity.

        participation x = Q / D          (D = visible book depth in USD)
        permanent       = gamma * sigma * x
        temporary       = eta   * sigma * sqrt(x / T)
        impact (USD)    = (permanent + temporary) * Q

    The square-root temporary term is the empirical "square-root law" of impact.
    gamma/eta are dimensionless and should be recalibrated per venue and instrument.
    """
    try:
        q = float(quantity)
        depth = visible_depth_usd(orderbook)
        if q <= 0 or depth <= 0:
            return 0.0
        sigma = max(float(volatility), 0.0)
        x = q / depth
        permanent = gamma * sigma * x
        temporary = eta * sigma * math.sqrt(x / max(float(T), 1e-9))
        return (permanent + temporary) * q
    except Exception as e:
        print(f"Error estimating market impact: {e}")
        return 0.0


def predict_maker_taker(orderbook, quantity, order_type="Market"):
    """
    Probability that the order fills as maker (0.0-1.0).

    A market order always removes liquidity, so it is 0% maker. For a passive limit order
    at the touch, the fill probability falls as the order grows relative to the queue ahead.
    """
    try:
        if (order_type or "Market").lower() == "market":
            return 0.0
        top_bid, _, _ = _best(orderbook)
        queue_usd = float(orderbook["bids"][0][1]) * top_bid
        return float(1.0 / (1.0 + float(quantity) / max(queue_usd, 1e-9)))
    except Exception as e:
        print(f"Error predicting maker/taker proportion: {e}")
        return 0.0 if (order_type or "Market").lower() == "market" else 0.5


def book_stats(orderbook, levels=5):
    """Top-of-book stats used by the UI: mid, spread (abs and bps), imbalance, microprice."""
    top_bid, top_ask, mid = _best(orderbook)
    bid_sz = float(orderbook["bids"][0][1])
    ask_sz = float(orderbook["asks"][0][1])
    bid_vol = sum(float(s) for _, s in orderbook["bids"][:levels])
    ask_vol = sum(float(s) for _, s in orderbook["asks"][:levels])
    spread = top_ask - top_bid
    micro = (top_bid * ask_sz + top_ask * bid_sz) / (bid_sz + ask_sz) if bid_sz + ask_sz else mid
    return {
        "bid": top_bid, "ask": top_ask, "mid": mid, "spread": spread,
        "spread_bps": spread / mid * 1e4,
        "imbalance": (bid_vol - ask_vol) / (bid_vol + ask_vol) if bid_vol + ask_vol else 0.0,
        "microprice": micro,
        "depth_usd": visible_depth_usd(orderbook),
    }
