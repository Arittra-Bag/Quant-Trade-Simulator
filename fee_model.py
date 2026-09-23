"""
Per-venue fee schedule.

The old model applied one OKX-shaped taker table to every venue and had no maker rate
at all, so a Binance book and a Kraken book were charged identically and a passive
order was charged as if it crossed the spread. Rates below are the published base-tier
schedules for the perpetual/swap products this app streams, read from each venue's own
fee page. They are a snapshot, not a live lookup: FEE_SCHEDULE_CHECKED says when, and
the UI shows the venue and tier next to the number so nobody mistakes one venue's
schedule for another's. The base tier of each venue is the rate to trust here; the two
tiers below it follow each venue's published ladder and are worth re-reading before
anyone quotes them.

Tier 1/2/3 are the three lowest public tiers of each venue's own ladder (VIP 0/1/2 or
the equivalent), kept under generic names because the ladders do not line up across
venues.
"""
FEE_SCHEDULE_CHECKED = "2026-09-23"

# venue -> tier -> (maker rate, taker rate), as fractions of notional
FEE_SCHEDULE = {
    "OKX": {
        "Tier 1": (0.00020, 0.00050),
        "Tier 2": (0.00016, 0.00045),
        "Tier 3": (0.00015, 0.00036),
    },
    "BINANCE": {
        "Tier 1": (0.00020, 0.00050),
        "Tier 2": (0.00018, 0.00050),
        "Tier 3": (0.00016, 0.00040),
    },
    "KRAKEN": {
        "Tier 1": (0.00020, 0.00050),
        "Tier 2": (0.000175, 0.00045),
        "Tier 3": (0.00015, 0.00040),
    },
    "HYPERLIQUID": {
        "Tier 1": (0.00015, 0.00045),
        "Tier 2": (0.00012, 0.00040),
        "Tier 3": (0.00008, 0.00035),
    },
}

DEFAULT_VENUE = "OKX"
DEFAULT_TIER = "Tier 1"
TIERS = ("Tier 1", "Tier 2", "Tier 3")


def _venue_key(venue):
    key = (venue or DEFAULT_VENUE).strip().upper()
    return key if key in FEE_SCHEDULE else DEFAULT_VENUE


def fee_rates(venue=DEFAULT_VENUE, fee_tier=DEFAULT_TIER):
    """Return (maker, taker) rates for a venue and tier, falling back to OKX Tier 1."""
    table = FEE_SCHEDULE[_venue_key(venue)]
    return table.get(fee_tier or DEFAULT_TIER, table[DEFAULT_TIER])


def calculate_fees(quantity, fee_tier=DEFAULT_TIER, venue=DEFAULT_VENUE, maker_fraction=0.0):
    """
    Expected exchange fee in USD.

    maker_fraction is the share of the order expected to rest rather than cross; a
    market order is 0.0 and pays the taker rate outright. Blending the two rates means
    a passive order is no longer charged as if it lifted the offer.

    >>> round(calculate_fees(1000, "Tier 1", "OKX"), 4)
    0.5
    """
    try:
        maker, taker = fee_rates(venue, fee_tier)
        m = min(max(float(maker_fraction or 0.0), 0.0), 1.0)
        return float(quantity) * (m * maker + (1 - m) * taker)
    except Exception as e:
        print(f"Error calculating fees: {e}")
        return 0.0
