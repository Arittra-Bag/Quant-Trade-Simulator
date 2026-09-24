"""
Structured output contract for the execution advisor.

The model does not return prose that the UI hopes to parse. It returns one object
matching ADVICE_SCHEMA, which is handed to the provider as a response schema and
re-validated locally on the way back, because a provider honouring a schema is a
convention and not a guarantee.

`validate_advice` is strict about the things a trading UI would act on (the enums,
the numeric ranges, the referenced order side) and forgiving about the prose. It
returns (advice, errors); advice is None when the object cannot be repaired.
"""

SENTIMENTS = ("Bullish", "Bearish", "Neutral")

# Strategy is a closed set so the UI can style it and the evals can score it.
STRATEGIES = (
    "immediate_market",      # cross the spread now, one clip
    "passive_limit",         # rest at the touch, accept fill risk
    "twap",                  # slice evenly over a horizon
    "iceberg",               # show a small clip, refill
    "wait",                  # do not trade into this book
)

URGENCIES = ("low", "medium", "high")

# compare_schedule prices at most this many slices, so a plan asking for more could not be
# priced as presented.
MAX_SLICES = 20

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "string", "enum": list(SENTIMENTS)},
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "slices": {
            "type": "integer",
            "description": "Number of child orders. 1 for a single clip; >1 only for twap or iceberg.",
        },
        "horizon_seconds": {
            "type": "integer",
            "description": "Seconds to work the order over. 0 for an immediate strategy.",
        },
        "limit_price": {
            "type": "number",
            "description": "Limit price for passive_limit or iceberg. 0 when the strategy is not priced.",
        },
        "expected_cost_bps": {
            "type": "number",
            "description": "Expected all-in cost in basis points of notional, from the quote tool, not guessed.",
        },
        "confidence": {"type": "number", "description": "0.0 to 1.0."},
        "urgency": {"type": "string", "enum": list(URGENCIES)},
        "order_side": {
            "type": "string",
            "enum": ["buy", "sell"],
            "description": "Echo of the side actually being sized. Used to catch the model drifting off the order.",
        },
        "analysis": {"type": "string", "description": "Two sentences on book shape and liquidity."},
        "reasoning": {"type": "string", "description": "Why this strategy over the alternatives."},
        "execution_approach": {"type": "string", "description": "One sentence a trader could act on."},
        "risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete risks of this plan. Empty list if genuinely none.",
        },
    },
    "required": [
        "sentiment", "strategy", "slices", "horizon_seconds", "expected_cost_bps",
        "confidence", "urgency", "order_side", "analysis", "reasoning",
        "execution_approach", "risks",
    ],
    "propertyOrdering": [
        "sentiment", "strategy", "order_side", "urgency", "confidence",
        "expected_cost_bps", "slices", "horizon_seconds", "limit_price",
        "analysis", "reasoning", "execution_approach", "risks",
    ],
}

_MULTI_CLIP = ("twap", "iceberg")


def _num(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default, False
    if out != out or out in (float("inf"), float("-inf")):
        return default, False
    return out, True


def validate_advice(raw, expected_side=None):
    """
    Check and normalise one advice object.

    Returns (advice, errors). `advice` is None only when `raw` is not a mapping or a
    required enum is unrecoverable; everything else is repaired to a safe default and
    reported in `errors`, so a single sloppy field does not blank the panel.
    """
    errors = []
    if not isinstance(raw, dict):
        return None, ["response is not a JSON object"]

    missing = [k for k in ADVICE_SCHEMA["required"] if k not in raw]
    if missing:
        errors.append(f"missing fields: {', '.join(sorted(missing))}")

    sentiment = str(raw.get("sentiment", "")).strip().title()
    if sentiment not in SENTIMENTS:
        errors.append(f"sentiment {raw.get('sentiment')!r} not in {SENTIMENTS}")
        sentiment = "Neutral"

    strategy = str(raw.get("strategy", "")).strip().lower().replace(" ", "_").replace("-", "_")
    if strategy not in STRATEGIES:
        errors.append(f"strategy {raw.get('strategy')!r} not in {STRATEGIES}")
        return None, errors

    urgency = str(raw.get("urgency", "")).strip().lower()
    if urgency not in URGENCIES:
        errors.append(f"urgency {raw.get('urgency')!r} not in {URGENCIES}")
        urgency = "medium"

    side = str(raw.get("order_side", "")).strip().lower()
    if side not in ("buy", "sell"):
        errors.append(f"order_side {raw.get('order_side')!r} not in ('buy', 'sell')")
        side = expected_side or "buy"
    elif expected_side and side != expected_side:
        errors.append(f"order_side {side!r} does not match the order being sized ({expected_side!r})")

    confidence, ok = _num(raw.get("confidence"), 0.0)
    if not ok:
        errors.append("confidence is not a number")
    if not 0.0 <= confidence <= 1.0:
        errors.append(f"confidence {confidence} outside 0.0-1.0")
        confidence = min(max(confidence, 0.0), 1.0)

    cost_bps, ok = _num(raw.get("expected_cost_bps"), 0.0)
    if not ok:
        errors.append("expected_cost_bps is not a number")
    if cost_bps < 0:
        errors.append(f"expected_cost_bps {cost_bps} is negative")
        cost_bps = 0.0

    try:
        slices = int(raw.get("slices", 1))
    except (TypeError, ValueError):
        errors.append("slices is not an integer")
        slices = 1
    if slices < 1:
        errors.append(f"slices {slices} below 1")
        slices = 1
    if strategy not in _MULTI_CLIP and slices != 1:
        errors.append(f"strategy {strategy} is a single clip but slices is {slices}")
        slices = 1
    if strategy in _MULTI_CLIP and slices < 2:
        errors.append(f"strategy {strategy} needs at least 2 slices, got {slices}")
        slices = 2
    if slices > MAX_SLICES:
        errors.append(f"slices {slices} above the {MAX_SLICES} the schedule can be priced at")
        slices = MAX_SLICES

    horizon, ok = _num(raw.get("horizon_seconds"), 0.0)
    if not ok:
        errors.append("horizon_seconds is not a number")
    horizon = int(max(horizon, 0))
    if strategy == "immediate_market" and horizon:
        errors.append(f"immediate_market with a {horizon}s horizon")
        horizon = 0
    if strategy in _MULTI_CLIP and horizon <= 0:
        errors.append(f"strategy {strategy} needs a positive horizon_seconds")
        horizon = 60

    limit_price, ok = _num(raw.get("limit_price"), 0.0)
    if not ok:
        errors.append("limit_price is not a number")
    if limit_price < 0:
        errors.append(f"limit_price {limit_price} is negative")
        limit_price = 0.0

    risks = raw.get("risks", [])
    if isinstance(risks, str):
        risks = [risks]
    if not isinstance(risks, (list, tuple)):
        errors.append("risks is not a list")
        risks = []
    risks = [str(r).strip() for r in risks if str(r).strip()]

    advice = {
        "sentiment": sentiment,
        "strategy": strategy,
        "order_side": side,
        "urgency": urgency,
        "confidence": round(confidence, 3),
        "expected_cost_bps": round(cost_bps, 4),
        "slices": slices,
        "horizon_seconds": horizon,
        "limit_price": round(limit_price, 8),
        "analysis": str(raw.get("analysis", "")).strip(),
        "reasoning": str(raw.get("reasoning", "")).strip(),
        "execution_approach": str(raw.get("execution_approach", "")).strip(),
        "risks": risks,
    }
    return advice, errors


STRATEGY_LABELS = {
    "immediate_market": "Immediate market",
    "passive_limit": "Passive limit at touch",
    "twap": "TWAP",
    "iceberg": "Iceberg",
    "wait": "Stand aside",
}


def strategy_label(advice):
    """Human label for the UI, with the slice count folded in where it matters."""
    label = STRATEGY_LABELS.get(advice.get("strategy"), advice.get("strategy", ""))
    if advice.get("strategy") in _MULTI_CLIP:
        return f"{label} {advice.get('slices')} slices"
    return label
