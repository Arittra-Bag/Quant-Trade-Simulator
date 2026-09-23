"""
Adapter between the Dash app and the execution advisor.

The advisor itself lives in `advisor/`: the output schema in `advisor/schema.py`, the
tools the model runs against the live book in `advisor/tools.py`, and the loop and the
transports in `advisor/advisor.py`. This module only decides which transport to use and
flattens the result into the dict the callback renders.

Transport choice:
  GEMINI_API_KEY set  -> GeminiTransport, the real tool-calling loop
  no key              -> RuleTransport, the deterministic baseline

The no-key path is deliberate. The panel used to print "Set GEMINI_API_KEY" and do
nothing, which meant the deployed demo was dead for anyone without a key. The baseline
walks the same book through the same tools and returns the same schema, so the panel is
always useful and the label says which produced the read.

Model: GEMINI_MODEL (default gemini-3.8-flash), falling through GEMINI_FALLBACK_MODELS
(comma-separated, default gemini-3.5-flash-lite) when an ID is retired. The API key is
read from GEMINI_API_KEY or GOOGLE_API_KEY and is never logged.

The live Gemini path has NOT been exercised against the real API from this repository's
tests or CI; there is no key in that environment. Everything offline runs through
ReplayTransport. See evals/RESULTS.md.
"""
import os
import time

from dotenv import load_dotenv

from advisor.advisor import GeminiTransport, RuleTransport, run_advisor
from advisor.schema import strategy_label

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODELS = [m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",")
                   if m.strip()]
MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "5"))  # seconds, free-tier rate limits

if not API_KEY:
    print("GEMINI_API_KEY not set; the advisor will use the deterministic baseline.")


class GeminiAnalyzer:
    """
    Facade kept at its original name and call shape so the app and the exports do not
    have to care which transport answered.
    """

    def __init__(self):
        self.client = None
        self.models = list(dict.fromkeys([MODEL] + FALLBACK_MODELS))
        self.model = self.models[0] if self.models else ""
        self.min_interval = MIN_INTERVAL
        if API_KEY:
            try:
                from google import genai
                self.client = genai.Client(api_key=API_KEY)
            except Exception as e:
                print(f"Gemini client unavailable, falling back to the baseline: {e}")

    @property
    def enabled(self):
        """True when the live model is available. The panel works either way."""
        return self.client is not None

    def _transport(self, side, quantity, order_type):
        if self.client is None:
            return RuleTransport(side, quantity, order_type)
        transport = GeminiTransport(self.client, self.models, min_interval=self.min_interval)
        transport.model = self.model  # start from whichever model last worked
        return transport

    def analyze(self, orderbook_data, quantity, fees=0.0, slippage=0.0, impact=0.0,
                side="buy", order_type="Market", fee_tier="Tier 1", volatility=0.01):
        """
        Advise on one order against the current book. Never raises.

        `fees`, `slippage` and `impact` are accepted for backwards compatibility and are
        not used: the advisor quotes the order itself through `quote_order`, from the
        same cost models, so that the number it cites is one it actually looked up
        rather than one handed to it in the prompt.

        Returns a flat dict with `success`, the advice fields, the strategy label, the
        model or baseline that produced it, and `tool_calls` for display.
        """
        if not orderbook_data or not orderbook_data.get("bids") or not orderbook_data.get("asks"):
            return {"success": False, "analysis": "No orderbook data to analyse yet."}

        side = "sell" if str(side).lower() == "sell" else "buy"
        quantity = float(quantity or 0) or 1.0
        age = _book_age(orderbook_data)

        try:
            result = run_advisor(
                self._transport(side, quantity, order_type), orderbook_data, side, quantity,
                order_type=order_type, fee_tier=fee_tier, volatility=volatility, book_age=age,
            )
        except Exception as e:  # the panel must never take the page down
            return {"success": False, "analysis": f"Analysis failed: {e}"}

        if result.model and self.client is not None:
            self.model = result.model  # remember the model that answered

        if not result.ok:
            detail = result.errors[0] if result.errors else "the advisor returned nothing usable"
            return {"success": False, "analysis": f"Analysis failed: {detail}",
                    "errors": result.errors, "tool_calls": [c["name"] for c in result.tool_calls]}

        advice = dict(result.advice)
        advice.update({
            "success": True,
            "strategy_key": advice["strategy"],
            "strategy": strategy_label(advice),
            "model": result.model or ("rules-v1" if self.client is None else self.model),
            "source": "gemini" if self.client is not None else "baseline",
            "tool_calls": [c["name"] for c in result.tool_calls],
            "tool_trace": result.tool_calls,
            "turns": result.turns,
            "latency_ms": round(result.latency_ms, 1),
            "book_age": age,
            "warnings": result.errors,
        })
        return advice

    # Backwards-compatible wrappers ------------------------------------------------------

    def analyze_orderbook(self, orderbook_data):
        result = self.analyze(orderbook_data, 100.0)
        result.setdefault("sentiment", "Neutral")
        return result

    def get_trading_strategy(self, orderbook_data, quantity, fees, slippage, impact,
                             side="buy", order_type="Market", fee_tier="Tier 1", volatility=0.01):
        result = self.analyze(orderbook_data, quantity, fees, slippage, impact,
                              side=side, order_type=order_type, fee_tier=fee_tier, volatility=volatility)
        result.setdefault("strategy", "Unavailable")
        result.setdefault("reasoning", result.get("analysis", ""))
        result.setdefault("execution_approach", "")
        return result


def _book_age(book):
    """How stale the snapshot is, so the advice can say so instead of implying it is live."""
    ts = book.get("timestamp")
    if not ts:
        return "unknown"
    try:
        seconds = time.time() - (float(ts) / 1000.0)
    except (TypeError, ValueError):
        return "unknown"
    if seconds < 0 or seconds > 86400:
        return "unknown"
    return f"{seconds:.1f}s"
