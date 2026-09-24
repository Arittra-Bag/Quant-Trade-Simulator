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

Model: GEMINI_MODEL (default gemini-3.5-flash-lite), falling through GEMINI_FALLBACK_MODELS
(comma-separated, default gemini-3.8-flash) when a model is retired, busy or out of quota.
Lite leads on the live eval (evals/RESULTS.md): 95.3% in 6.1 s and 6,170 tokens a scenario,
against 3.8 Flash's 94.4% in 16.1 s and 14,661 tokens. The bigger model scored no better
and took over twice as long, so it is only the fallback. Because the demo
is public and paid, GEMINI_DAILY_REQUESTS (default 150) caps advisor requests per UTC day. A model that
fails is rested (see ModelCooldown) rather than tried first on every call. The API key is
read from GEMINI_API_KEY or GOOGLE_API_KEY and is never logged.

When Gemini cannot answer (quota, overload, timeout) the panel shows the deterministic
baseline's read with a note saying why, instead of an error.

The tests and CI never call the real API; they run through ReplayTransport and mocked
clients. The live path has been run against the real models with `python -m evals.runner
--live`; see evals/RESULTS.md.
"""
import json
import os
import sys
import threading
import time

from dotenv import load_dotenv

from advisor.advisor import (AdviceInvalid, DailyCapReached, GeminiTransport, ModelCooldown, ModelsResting,
                             RateLimited, RuleTransport, is_daily_quota, is_quota, is_timeout, is_upstream,
                             run_advisor)
from advisor.schema import strategy_label

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
FALLBACK_MODELS = [m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash").split(",")
                   if m.strip()]
# The demo is public and the key is on the paid tier, so a stranger clicking Generate spends
# real credit. This caps advisor requests per UTC day; past it the panel answers from the rules.
DAILY_REQUESTS = int(os.environ.get("GEMINI_DAILY_REQUESTS", "150"))
# The day's count lives on disk so a restarted worker does not start the day again. A host
# that wipes its disk on redeploy still resets it; the hard ceiling is a quota on the key
# itself, set in Google Cloud (see DOCUMENTATION.md).
USAGE_FILE = os.environ.get("GEMINI_USAGE_FILE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "advisor_usage.json"))
CALL_TIMEOUT = float(os.environ.get("GEMINI_CALL_TIMEOUT", "20"))  # seconds per API call
REQUEST_DEADLINE = float(os.environ.get("GEMINI_DEADLINE", "40"))  # seconds for one click, all calls
MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "5"))  # seconds between advisor requests
_PACE_LOCK = threading.Lock()

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
        self.cooldown = ModelCooldown()
        self.daily_requests = DAILY_REQUESTS
        self.usage_file = USAGE_FILE
        if API_KEY:
            try:
                from google import genai
                from google.genai import types
                self.client = genai.Client(api_key=API_KEY, http_options=types.HttpOptions(
                    timeout=int(CALL_TIMEOUT * 1000)))
            except Exception as e:
                print(f"Gemini client unavailable, falling back to the baseline: {e}")

    @property
    def enabled(self):
        """True when the live model is available. The panel works either way."""
        return self.client is not None

    def _transport(self, side, quantity, order_type, paced=True):
        if self.client is None:
            return RuleTransport(side, quantity, order_type)
        # Pacing is shared across clicks and guarded, because Dash can run two callbacks at
        # once. Conversation state is per request, so each request gets its own transport,
        # starting from whichever model last answered.
        with _PACE_LOCK:
            wait = self.min_interval - (time.time() - getattr(self, "_last_request", 0.0))
            if paced and wait > 0:
                raise RateLimited(f"Rate limited, try again in {wait:.0f}s")
            # Requests still running count too: a click takes up to 40 s, and without this
            # several clicks could pass the check before any of them was counted.
            in_flight = getattr(self, "_in_flight", 0)
            if self._requests_today() + in_flight >= self.daily_requests:
                raise DailyCapReached("Rate limited: today's advisor requests are used up")
            self._in_flight = in_flight + 1
            self._last_request = time.time()
        transport = GeminiTransport(self.client, self.models, cooldown=self.cooldown,
                                    deadline_s=REQUEST_DEADLINE, call_timeout_s=CALL_TIMEOUT)
        transport.model = self.model
        return transport

    def _usage_today(self):
        """{"day", "requests"} for today (UTC), from disk when there is a usage file."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        usage = getattr(self, "_usage", None) or {}
        if getattr(self, "usage_file", None):
            try:
                with open(self.usage_file) as fh:
                    usage = json.load(fh)
            except (OSError, ValueError):
                pass
        return usage if usage.get("day") == today else {"day": today, "requests": 0}

    def _requests_today(self):
        return self._usage_today()["requests"]

    def _count_request(self, reached=True):
        """Release a request's reserved slot, counting it against today's allowance if it reached Gemini."""
        with _PACE_LOCK:
            self._in_flight = max(getattr(self, "_in_flight", 0) - 1, 0)
            if not reached:
                return
            usage = self._usage_today()
            usage["requests"] += 1
            self._usage = usage
            if getattr(self, "usage_file", None):
                try:
                    tmp = f"{self.usage_file}.{os.getpid()}.tmp"
                    with open(tmp, "w") as fh:
                        json.dump(usage, fh)
                    os.replace(tmp, self.usage_file)
                except OSError as e:
                    print(f"Could not save advisor usage: {e!r}", file=sys.stderr, flush=True)

    def analyze(self, orderbook_data, quantity, fees=0.0, slippage=0.0, impact=0.0,
                side="buy", order_type="Market", fee_tier="Tier 1", volatility=0.01, feedback=None,
                paced=True):
        """
        Advise on one order against the current book. Never raises.

        `feedback` is a reviewer's findings on a previous answer (the execution agent's
        critic). A revision is part of the click that asked for the first answer, so it
        passes `paced=False` and is not refused by the gap between clicks; it still counts
        against the daily cap.

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

        run = dict(order_type=order_type, fee_tier=fee_tier, volatility=volatility, book_age=age, feedback=feedback)
        failure, transport = None, None
        try:
            transport = self._transport(side, quantity, order_type, paced=paced)
            result = run_advisor(transport, orderbook_data, side, quantity, **run)
            if self.client is not None and not result.ok:
                # Gemini did not produce usable advice: the provider failed, or its answer
                # failed to parse or validate. Either way the rules can still answer.
                why = result.errors[0] if result.errors else "no advice"
                if result.exception is not None:
                    failure = result.exception
                elif result.raw is not None:
                    failure = AdviceInvalid(why)  # Gemini's advice failed validation
                else:
                    failure = RuntimeError(why)  # Gemini never got as far as proposing advice
        except Exception as e:  # the panel must never take the page down
            if not is_upstream(e):
                print(f"Advisor failed: {e!r}", file=sys.stderr, flush=True)
                if isinstance(transport, GeminiTransport):
                    self._count_request(reached=bool(transport.answered_by))  # release its slot
                return {"success": False, "analysis": f"Analysis failed: {e}"}
            failure = e  # our own pacing refused the click

        if isinstance(transport, GeminiTransport):
            # The slot reserved in _transport is released, and kept only if the request
            # reached Gemini: one refused by resting models or answered by the rules costs nothing.
            self._count_request(reached=bool(transport.answered_by))

        notice, gemini_error = "", ""
        baseline = self.client is None
        if failure is not None:
            # Gemini could not answer, so answer from the rules instead of showing an error.
            # Same tools, same schema; the note says which produced it and why, and the real
            # error goes to the log and the result for diagnosis.
            gemini_error = f"{type(failure).__name__}: {failure}"
            print(f"Gemini unavailable, answering from the rules: {gemini_error}", file=sys.stderr, flush=True)
            notice = gemini_notice(failure, self.min_interval)
            result = run_advisor(RuleTransport(side, quantity, order_type), orderbook_data, side, quantity, **run)
            baseline = True

        if self.client is not None and getattr(transport, "model", None):
            # A retired default moves to its replacement even when the run then failed, or
            # every later click would pay the retired model's 404 first.
            self.model = transport.model

        if not result.ok:
            detail = result.errors[0] if result.errors else "the advisor returned nothing usable"
            return {"success": False, "analysis": f"Analysis failed: {detail}",
                    "errors": result.errors, "tool_calls": [c["name"] for c in result.tool_calls]}

        advice = dict(result.advice)
        advice.update({
            "success": True,
            "strategy_key": advice["strategy"],
            "strategy": strategy_label(advice),
            # The models that actually answered, not the default: with the default resting,
            # a read from the fallback was labelled as the default's.
            "model": "rules-v1" if baseline else (" + ".join(result.models_used) or result.model or self.model),
            "source": "baseline" if baseline else "gemini",
            "notice": notice,
            "gemini_error": gemini_error,
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


def gemini_notice(error, min_interval=MIN_INTERVAL):
    """One line for the panel on why Gemini did not answer, classified from the exception."""
    code = getattr(error, "code", None)
    if isinstance(error, DailyCapReached):
        why = "today's Gemini allowance for this public demo is used up; it resets at 00:00 UTC"
    elif isinstance(error, ModelsResting):
        why = "every Gemini model is resting after a quota or availability error"
    elif isinstance(error, RateLimited):
        why = f"Gemini is paced to one request every {min_interval:.0f}s"
    elif is_quota(error) and is_daily_quota(error):
        why = "the daily Gemini quota is used up until midnight Pacific"
    elif is_quota(error):
        why = "the per-minute Gemini quota is used up"
    elif is_timeout(error):
        why = "Gemini took too long to answer"
    elif code == 503:
        why = "Gemini is overloaded right now"
    elif isinstance(error, AdviceInvalid):
        why = "Gemini's answer did not pass validation"
    else:
        why = "Gemini is unavailable right now"
    return f"Rules-based read: {why}."


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
