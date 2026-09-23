"""
Pre-trade cost models.

Books are {"bids": [[px, sz]], "asks": [[px, sz]]} with sizes in base-asset units,
best level first. Quantities are order notional in USD. Costs are returned in USD
and are always >= 0.

What is measured and what is assumed
------------------------------------
Measured from the live feed:
  * the fill price of an order, by walking the resting book level by level
  * the spread, the depth and the imbalance of the visible book
  * realised volatility, from the mid-price series the feed produces (VolatilityTracker)

Assumed, and flagged as such in every result:
  * liquidity past the last visible level. The book we receive is 25 levels deep, so
    a large order runs off the end of it. We continue the book at the average USD
    density of the visible levels rather than pretending the last price fills the
    rest (see _extrapolated_fill_price).
  * the share of the book displacement that is permanent (PERMANENT_SHARE). The
    literature puts it near a half; validation/ fits it against real fills.

Nothing here is calibrated to this venue's own fills yet. `validation/` records books
and the public trade tape and reports predicted-versus-realised error in bps; until
that report exists, treat the impact term as an assumption, not a measurement.
"""
import math
import os
import time
from collections import deque

# Share of the total book displacement assumed to persist after the trade.
# Empirical studies of impact decay put the permanent component near one half of the
# peak displacement. Overridable so validation/calibrate.py can fit it.
PERMANENT_SHARE = float(os.getenv("QTS_PERMANENT_SHARE", "0.4"))

# Square-root-law coefficient, used only for the cross-check estimate.
SQRT_LAW_Y = float(os.getenv("QTS_SQRT_LAW_Y", "0.5"))

# Daily traded notional per base asset, USD. Order-of-magnitude figures for the major
# venues, used only by the square-root cross-check. Override with adv_usd= or QTS_ADV_USD.
ADV_USD_DEFAULTS = {
    "BTC": 1.5e10, "ETH": 8.0e9, "SOL": 2.5e9, "XRP": 1.5e9, "DOGE": 8.0e8,
}
ADV_USD_FALLBACK = float(os.getenv("QTS_ADV_USD", "5.0e8"))

SECONDS_PER_DAY = 86_400.0
TRADING_DAYS = 365.0


def _best(orderbook):
    top_bid = float(orderbook["bids"][0][0])
    top_ask = float(orderbook["asks"][0][0])
    return top_bid, top_ask, (top_bid + top_ask) / 2


def _base_asset(orderbook):
    return (orderbook.get("symbol") or "").split("-")[0].upper()


def _extrapolated_fill_price(mid, last_px, side_depth_usd, residual_usd, side):
    """
    Average price for the part of an order that runs past the last visible level.

    The visible book gives us a liquidity density: `side_depth_usd` of notional rests
    inside a relative price displacement of `u_last`. We assume that density continues
    past the last level, so `residual_usd` more notional needs a further displacement
    of residual/density, and fills on average at half of it.

    This is an assumption, not a measurement, and it is the single most important one
    in this file. It is still far better than pricing the residual at the last visible
    level, which reports a 1M order into a thin book as costing a fraction of a bp.
    """
    u_last = abs(last_px - mid) / mid if mid > 0 else 0.0
    if u_last <= 0 or side_depth_usd <= 0:
        return last_px, last_px
    density = side_depth_usd / u_last          # USD of depth per unit of relative displacement
    u_end = u_last + residual_usd / density
    u_avg = (u_last + u_end) / 2
    if side == "buy":
        return mid * (1 + u_avg), mid * (1 + u_end)
    u_avg, u_end = min(u_avg, 0.99), min(u_end, 0.99)
    return mid * (1 - u_avg), mid * (1 - u_end)


def walk_book(orderbook, quantity, side="buy", extrapolate=True):
    """
    Fill `quantity` USD against the book.

    Returns dict(vwap, filled_usd, filled_base, levels, worst_price, end_price,
    visible_usd, residual_usd, complete, extrapolated, slippage_bps, end_bps) or None
    if the book is empty.

    `complete` is False when the order is larger than the visible book. The remainder
    is then priced by extending the book at its own average density; `residual_usd`
    says how much of the order that was, so the caller can show how much of the answer
    rests on an assumption.
    """
    if not orderbook:
        return None
    levels = orderbook.get("asks" if side == "buy" else "bids") or []
    remaining = float(quantity)
    if remaining <= 0 or not levels or not orderbook.get("bids") or not orderbook.get("asks"):
        return None
    try:
        _, _, mid = _best(orderbook)
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if mid <= 0:
        return None

    filled_usd = filled_base = 0.0
    used = 0
    last_px = float(levels[0][0])
    for px, sz in levels:
        px, sz = float(px), float(sz)
        if px <= 0 or sz <= 0:
            continue
        last_px = px
        take = min(remaining, px * sz)
        filled_usd += take
        filled_base += take / px
        remaining -= take
        used += 1
        if remaining <= 1e-9:
            break

    complete = remaining <= 1e-9
    visible_usd, residual_usd = filled_usd, 0.0
    end_price = last_px
    if not complete:
        residual_usd = remaining
        if extrapolate:
            res_px, end_price = _extrapolated_fill_price(mid, last_px, visible_usd, residual_usd, side)
        else:
            res_px = end_price = last_px
        filled_usd += residual_usd
        filled_base += residual_usd / res_px

    vwap = filled_usd / filled_base
    sign = 1.0 if side == "buy" else -1.0
    return {
        "vwap": vwap,
        "filled_usd": filled_usd,
        "filled_base": filled_base,
        "levels": used,
        "worst_price": last_px,
        "end_price": end_price,
        "visible_usd": visible_usd,
        "residual_usd": residual_usd,
        "complete": complete,
        "extrapolated": (not complete) and extrapolate,
        "slippage_bps": sign * (vwap - mid) / mid * 1e4,
        "end_bps": sign * (end_price - mid) / mid * 1e4,
    }


def estimate_slippage(orderbook, quantity, volatility=0.01, side="buy"):
    """
    Expected slippage in USD versus mid, from walking the live book.

    slippage = |VWAP - mid| / mid * quantity, which includes the half-spread because
    that is what a market order actually pays. Returns 0.0 when there is no book:
    with no depth to walk there is nothing to estimate from, and a guess would be
    worse than an honest zero.
    """
    try:
        fill = walk_book(orderbook, quantity, side)
        if not fill:
            return 0.0
        return abs(fill["slippage_bps"]) / 1e4 * float(quantity)
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


def side_depth_usd(orderbook, side="buy", levels=None):
    """USD resting on the side an order of `side` would consume."""
    rows = (orderbook.get("asks" if side == "buy" else "bids") or [])[:levels]
    return sum(float(px) * float(sz) for px, sz in rows)


def estimate_market_impact(orderbook, quantity, volatility=0.01, side="buy",
                           permanent_share=None, adv_usd=None, model="book"):
    """
    Permanent market impact in USD: the part of the price move the trade leaves behind.

    model="book" (default) derives it from liquidity we can see. Walking the book gives
    the displacement the order pushes the price to; a share of that displacement
    (PERMANENT_SHARE) is assumed to persist:

        impact = permanent_share * (end_price - mid) / mid * Q

    model="sqrt" is the textbook square-root law, kept as an independent cross-check:

        impact = Y * sigma_daily * sqrt(Q / ADV) * Q

    Only the permanent part is returned. The temporary part is already paid in the
    fill price and is reported by estimate_slippage, so adding both would double-count
    it -- that double count is what used to make a 1M order show 0.25 bps of slippage
    beside 320 bps of impact.
    """
    try:
        q = float(quantity)
        if q <= 0 or not orderbook:
            return 0.0
        if model == "sqrt":
            adv = float(adv_usd or ADV_USD_DEFAULTS.get(_base_asset(orderbook), ADV_USD_FALLBACK))
            sigma = max(float(volatility), 0.0)
            if adv <= 0:
                return 0.0
            return SQRT_LAW_Y * sigma * math.sqrt(q / adv) * q
        fill = walk_book(orderbook, q, side)
        if not fill:
            return 0.0
        share = PERMANENT_SHARE if permanent_share is None else float(permanent_share)
        return max(0.0, share * abs(fill["end_bps"]) / 1e4 * q)
    except Exception as e:
        print(f"Error estimating market impact: {e}")
        return 0.0


def timing_risk(quantity, sigma_daily, horizon_s=1.0):
    """
    One-sigma price move over the execution horizon, in USD.

    This is risk, not expected cost, so callers report it as a band rather than adding
    it to the net. It is the one place the measured volatility enters the headline
    numbers directly.
    """
    try:
        sigma = max(float(sigma_daily), 0.0)
        h = max(float(horizon_s), 0.0)
        return sigma * math.sqrt(h / SECONDS_PER_DAY) * float(quantity)
    except (TypeError, ValueError):
        return 0.0


def predict_maker_taker(orderbook, quantity, order_type="Market"):
    """
    Probability that the order fills as maker (0.0-1.0).

    A market order always removes liquidity, so it is 0% maker. For a passive limit
    order at the touch, the fill probability falls as the order grows relative to the
    queue ahead of it.
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


# ----------------------------------------------------------------- realised volatility

class VolatilityTracker:
    """
    Realised volatility of the mid price, measured from the feed.

    Every book the app receives is one observation. We keep an exponentially weighted
    estimate of the per-second variance of log mid returns and annualise it, so the
    number reported is a measurement of this market right now rather than a slider
    position. `ready` is False until enough samples have arrived; callers fall back to
    their own default until then.
    """

    def __init__(self, half_life_s=60.0, min_samples=20, max_gap_s=30.0):
        self.half_life_s = float(half_life_s)
        self.min_samples = int(min_samples)
        self.max_gap_s = float(max_gap_s)
        self._var_per_s = None
        self._last = None          # (timestamp, mid)
        self.samples = 0
        self.returns = deque(maxlen=512)

    def update(self, mid, ts=None):
        """Feed one mid price. Returns the current daily sigma, or None if not ready."""
        try:
            mid = float(mid)
        except (TypeError, ValueError):
            return self.sigma_daily
        if mid <= 0:
            return self.sigma_daily
        ts = float(ts if ts is not None else time.time())
        last = self._last
        self._last = (ts, mid)
        if last is None:
            return self.sigma_daily
        dt = ts - last[0]
        if dt <= 0 or dt > self.max_gap_s:
            return self.sigma_daily
        r = math.log(mid / last[1])
        self.returns.append((ts, r))
        var_sample = r * r / dt                       # variance per second
        w = 0.5 ** (dt / self.half_life_s)            # time-weighted decay
        self._var_per_s = var_sample if self._var_per_s is None else w * self._var_per_s + (1 - w) * var_sample
        self.samples += 1
        return self.sigma_daily

    @property
    def ready(self):
        return self._var_per_s is not None and self.samples >= self.min_samples

    @property
    def sigma_per_s(self):
        return math.sqrt(self._var_per_s) if self._var_per_s is not None else None

    @property
    def sigma_daily(self):
        s = self.sigma_per_s
        return s * math.sqrt(SECONDS_PER_DAY) if s is not None else None

    @property
    def sigma_annual(self):
        s = self.sigma_daily
        return s * math.sqrt(TRADING_DAYS) if s is not None else None


_tracker = VolatilityTracker()


def measure_volatility(orderbook, fallback=0.01, tracker=None):
    """
    Update the shared volatility tracker with this book and return what to use.

    Returns (sigma_daily, source) where source is "measured" once enough of the feed
    has been seen, and "assumed" while it has not.
    """
    tracker = tracker or _tracker
    try:
        _, _, mid = _best(orderbook)
        ts = orderbook.get("timestamp")
        tracker.update(mid, float(ts) / 1000.0 if ts else None)
    except (IndexError, KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    if tracker.ready:
        return tracker.sigma_daily, "measured"
    return float(fallback or 0.0), "assumed"


# ----------------------------------------------------------------------- cost summary

def estimate_costs(orderbook, quantity, volatility=None, side="buy", order_type="Market",
                   fee_tier="Tier 1", venue=None, horizon_s=1.0, permanent_share=None):
    """
    Full pre-trade cost breakdown for one order, in USD and in bps of notional.

        net = spread + depth + fees + permanent impact

    The fill cost (spread + depth) comes from the book. The permanent impact is the
    residue the trade leaves in the price, taken as a share of the displacement the
    same walk implies -- it is not a second copy of the cost already in the fill.
    Timing risk is reported alongside as a one-sigma band, not added to the net.

    `assumptions` lists, in plain words, every part of the answer that is not measured.
    """
    from fee_model import calculate_fees, fee_rates      # local import keeps models importable alone

    q = float(quantity or 0)
    if q <= 0 or not orderbook:
        return None
    fill = walk_book(orderbook, q, side)
    if not fill:
        return None

    stats = book_stats(orderbook)
    mid = stats["mid"]
    touch = stats["ask"] if side == "buy" else stats["bid"]
    sigma, sigma_source = measure_volatility(orderbook, fallback=volatility if volatility is not None else 0.01)

    spread_usd = abs(touch - mid) / mid * q
    slippage_usd = abs(fill["slippage_bps"]) / 1e4 * q
    depth_usd_cost = max(0.0, slippage_usd - spread_usd)
    maker = predict_maker_taker(orderbook, q, order_type)
    venue = venue or orderbook.get("source") or "OKX"
    fees_usd = calculate_fees(q, fee_tier, venue=venue, maker_fraction=maker)
    impact_usd = estimate_market_impact(orderbook, q, sigma, side=side, permanent_share=permanent_share)
    impact_sqrt_usd = estimate_market_impact(orderbook, q, sigma, side=side, model="sqrt")
    risk_usd = timing_risk(q, sigma, horizon_s)
    net_usd = slippage_usd + fees_usd + impact_usd

    assumptions = []
    if fill["residual_usd"] > 0:
        pct = fill["residual_usd"] / q * 100
        assumptions.append(
            f"{pct:.0f}% of the order is past the visible book and is priced by "
            "extending it at its own average depth density")
    share = PERMANENT_SHARE if permanent_share is None else permanent_share
    assumptions.append(
        f"permanent impact is {share:.0%} of the book displacement, an uncalibrated "
        "literature value until validation/ is run against real fills")
    if sigma_source == "assumed":
        assumptions.append("volatility is the value supplied, not yet measured from the feed")

    return {
        "fill": fill,
        "stats": stats,
        "mid": mid,
        "sigma_daily": sigma,
        "sigma_source": sigma_source,
        "maker": maker,
        "venue": venue,
        "fee_rates": fee_rates(venue, fee_tier),
        "spread_usd": spread_usd,
        "depth_usd": depth_usd_cost,
        "slippage_usd": slippage_usd,
        "fees_usd": fees_usd,
        "impact_usd": impact_usd,
        "impact_sqrt_usd": impact_sqrt_usd,
        "timing_risk_usd": risk_usd,
        "net_usd": net_usd,
        "net_bps": net_usd / q * 1e4,
        "slippage_bps": slippage_usd / q * 1e4,
        "impact_bps": impact_usd / q * 1e4,
        "fees_bps": fees_usd / q * 1e4,
        "assumptions": assumptions,
    }
