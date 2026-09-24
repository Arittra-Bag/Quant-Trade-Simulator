"""
The execution advisor: a bounded tool-calling loop over `BookTools` that ends in one
schema-validated advice object.

Shape of a run
--------------
    prompt -> transport proposes tool calls -> tools run against the real book
           -> results go back -> repeat (up to MAX_TOOL_TURNS)
           -> transport emits a final object -> validate_advice -> AdviceResult

The transport is pluggable on purpose. `GeminiTransport` talks to the live API;
`ReplayTransport` plays a recorded script so the whole path can be exercised offline in
CI with no key and no network; `RuleTransport` is a deterministic non-LLM baseline that
drives the same tools through the same graders, which is what the eval numbers are
measured against.

Note on the two-phase Gemini call: Gemini will not accept function declarations and a
structured response schema in the same request. So the loop runs with tools and free
text, and the final turn is a separate schema-constrained call that is handed the tool
transcript. That is why `GeminiTransport` has a `_finalise` step.
"""
import json
import math
import re
import sys
import threading
import time
from datetime import datetime, timedelta

from .schema import ADVICE_SCHEMA, validate_advice
from .tools import MAX_TOOL_TURNS, TOOL_DECLARATIONS, BookTools

SYSTEM_PROMPT = """You are an execution analyst on a crypto trading desk. You advise on \
how to work one specific order into one specific order book.

You have tools that read the live book. Use them. Do not estimate a cost you could have \
quoted, and do not describe liquidity you have not looked at.

Rules you are held to:
- Quote the actual order you were given with quote_order before you recommend anything.
- The order side you were given is the side you advise on. Never silently switch it.
- If quote_order returns complete=false the order is larger than the visible book. Say so, \
and do not recommend taking it in one clip.
- Before recommending twap, call compare_schedule and use the number it returns.
- expected_cost_bps must be a number a tool gave you, not a guess.
- Prefer standing aside over inventing liquidity that is not in the book."""

USER_TEMPLATE = """Order under consideration:
  instrument   {symbol} on {source}
  side         {side}
  order type   {order_type}
  notional     ${notional:,.2f}
  fee tier     {fee_tier}
  volatility   {volatility:.4f} (unitless model input, not annualised)
  book age     {age}

Work out how to execute it. Use the tools, then give your advice."""


class AdviceResult:
    """One advisor run: the advice, how it was produced, and what went wrong."""

    def __init__(self, advice=None, errors=None, tool_calls=None, transport="", model="",
                 turns=0, latency_ms=0.0, raw=None, upstream_error=False, models_used=None, exception=None):
        self.advice = advice
        self.exception = exception  # what stopped the run, for callers that classify it
        self.upstream_error = upstream_error  # the provider failed; the model never answered
        self.models_used = list(models_used or [])
        self.errors = list(errors or [])
        self.tool_calls = list(tool_calls or [])
        self.transport = transport
        self.model = model
        self.turns = turns
        self.latency_ms = latency_ms
        self.raw = raw

    @property
    def ok(self):
        return self.advice is not None

    def to_dict(self):
        return {
            "ok": self.ok,
            "advice": self.advice,
            "errors": self.errors,
            "tool_calls": [c["name"] for c in self.tool_calls],
            "tool_trace": self.tool_calls,
            "transport": self.transport,
            "model": self.model,
            "models_used": self.models_used,
            "upstream_error": self.upstream_error,
            "turns": self.turns,
            "latency_ms": round(self.latency_ms, 2),
        }


# --------------------------------------------------------------------------- transports

class Transport:
    """
    A transport turns a prompt plus a tool history into either more tool calls or a
    final advice object.

    `propose` returns {"tool_calls": [{"name", "args"}, ...]} to keep going, or
    {"advice": <dict>} to stop. Anything else ends the run with an error.
    """

    name = "abstract"
    model = ""

    def begin(self):
        """Called once at the start of every advisor request, before the first propose."""

    def propose(self, system_prompt, user_prompt, history):
        raise NotImplementedError


class ReplayTransport(Transport):
    """
    Plays a fixed script of turns. Used by the eval harness and the tests so the full
    loop, the tool dispatch and the validator all run with no key and no network.

    A script is a list of turns, each either {"tool_calls": [...]} or {"advice": {...}}.
    """

    name = "replay"

    def __init__(self, script, model="replay"):
        self.script = list(script)
        self.model = model
        self.index = 0

    def propose(self, system_prompt, user_prompt, history):
        if self.index >= len(self.script):
            return {"error": "replay script exhausted"}
        turn = self.script[self.index]
        self.index += 1
        return turn


class RuleTransport(Transport):
    """
    Deterministic baseline. No model, no key, no network: it drives the same tools and
    emits the same schema, from explicit thresholds.

    It exists for two reasons. It is the control the LLM has to beat in the evals, and
    it is what the UI falls back to when no API key is configured, so the panel says
    something useful instead of nothing.
    """

    name = "rules"
    model = "rules-v1"

    # Thresholds, all in bps of notional unless noted.
    EXPENSIVE_BPS = 25.0        # above this a single clip is worth avoiding
    WIDE_SPREAD_BPS = 10.0      # above this resting passively earns real edge
    WORTH_SLICING_BPS = 2.0     # minimum modelled saving to justify a schedule
    STRONG_IMBALANCE = 0.15
    THIN_BOOK_RATIO = 0.10      # order > 10x visible depth is untradeable here

    def __init__(self, side, notional, order_type="Market"):
        self.side = side
        self.notional = float(notional)
        self.order_type = order_type

    def propose(self, system_prompt, user_prompt, history):
        done = [c["name"] for c in history]
        if "get_book_stats" not in done:
            return {"tool_calls": [{"name": "get_book_stats", "args": {"levels": 5}}]}
        if "quote_order" not in done:
            return {"tool_calls": [{"name": "quote_order", "args": {
                "side": self.side, "notional_usd": self.notional, "order_type": self.order_type}}]}

        stats = next(c["result"] for c in history if c["name"] == "get_book_stats")
        quote = next(c["result"] for c in history if c["name"] == "quote_order")
        incomplete = not quote.get("complete", False)
        expensive = quote.get("net_cost_bps", 0.0) > self.EXPENSIVE_BPS

        if (incomplete or expensive) and "compare_schedule" not in done:
            depth = max(stats.get("visible_depth_usd", 0.0), 1e-9)
            want = math.ceil(self.notional / (depth * 0.25)) if incomplete else 4
            return {"tool_calls": [
                {"name": "get_depth_profile", "args": {"side": self.side, "levels": 10}},
                {"name": "compare_schedule", "args": {
                    "side": self.side, "notional_usd": self.notional,
                    "slices": max(2, min(int(want), 20))}},
            ]}

        schedule = next((c["result"] for c in history if c["name"] == "compare_schedule"), None)
        return {"advice": self._decide(stats, quote, schedule)}

    def _decide(self, stats, quote, schedule):
        imbalance = stats.get("imbalance", 0.0)
        spread_bps = stats.get("spread_bps", 0.0)
        depth = stats.get("visible_depth_usd", 0.0)
        net_bps = quote.get("net_cost_bps", 0.0)
        incomplete = not quote.get("complete", False)

        sentiment = ("Bullish" if imbalance > self.STRONG_IMBALANCE else
                     "Bearish" if imbalance < -self.STRONG_IMBALANCE else "Neutral")

        risks, slices, horizon, limit_price = [], 1, 0, 0.0
        touch = stats.get("best_bid" if self.side == "buy" else "best_ask", 0.0)

        if depth > 0 and self.notional > depth / self.THIN_BOOK_RATIO:
            strategy, urgency, confidence = "wait", "low", 0.75
            reason = (f"Order is ${self.notional:,.0f} against ${depth:,.0f} of visible depth. "
                      "There is no responsible way to size this into this book.")
            risks.append("Standing aside carries the risk that the price moves away before liquidity arrives.")
        elif incomplete:
            strategy, urgency = "twap", "low"
            slices = max(2, min(schedule.get("slices", 4) if schedule else 4, 20))
            horizon = slices * 30
            confidence = 0.6
            reason = ("The order runs past the visible book, so a single clip would be filled at "
                      "prices the book does not currently show. Working it in slices lets depth replenish.")
            risks.append("Modelled slice saving assumes the book refreshes between clips; real depth recovers only partly.")
            risks.append("Spreading the order over time adds exposure to the price drifting away.")
        elif schedule and schedule.get("saving_bps", 0.0) > self.WORTH_SLICING_BPS:
            strategy, urgency = "twap", "medium"
            slices = max(2, min(schedule.get("slices", 4), 20))
            horizon = slices * 30
            confidence = 0.65
            reason = (f"A single clip costs {net_bps:.1f} bps; {slices} slices model out "
                      f"{schedule['saving_bps']:.1f} bps cheaper.")
            risks.append("The modelled saving is an upper bound and ignores timing risk.")
        elif spread_bps > self.WIDE_SPREAD_BPS:
            strategy, urgency, confidence = "passive_limit", "low", 0.55
            limit_price = touch
            reason = (f"The spread is {spread_bps:.1f} bps, so crossing it is most of the cost. "
                      "Resting at the touch captures that instead of paying it.")
            risks.append("A passive order may not fill at all if the market moves away from the touch.")
        else:
            strategy, urgency, confidence = "immediate_market", "high", 0.8
            reason = (f"The book absorbs the order in {quote.get('levels_consumed', 0)} levels at "
                      f"{net_bps:.1f} bps all-in. The spread is tight and there is nothing to gain by waiting.")
            risks.append("Immediate execution locks in the current book; a better price may appear moments later.")

        analysis = (f"Mid {stats.get('mid', 0):,.2f} with a {spread_bps:.1f} bps spread and "
                    f"${depth:,.0f} of visible depth across both sides, imbalance {imbalance:+.2f}. "
                    f"A ${self.notional:,.0f} {self.side} quotes at {net_bps:.1f} bps all-in"
                    + (", and does not fit inside the visible book." if incomplete else "."))

        return {
            "sentiment": sentiment,
            "strategy": strategy,
            "order_side": self.side,
            "urgency": urgency,
            "confidence": confidence,
            "expected_cost_bps": net_bps,
            "slices": slices,
            "horizon_seconds": horizon,
            "limit_price": limit_price,
            "analysis": analysis,
            "reasoning": reason,
            "execution_approach": _approach(strategy, slices, horizon, limit_price, self.side),
            "risks": risks,
        }


def _approach(strategy, slices, horizon, limit_price, side):
    if strategy == "immediate_market":
        return f"Send the full {side} as one market order now."
    if strategy == "passive_limit":
        return f"Rest the {side} at {limit_price:,.2f} and re-evaluate if the touch moves away."
    if strategy == "twap":
        return f"Work the {side} as {slices} equal clips over roughly {horizon} seconds."
    if strategy == "iceberg":
        return f"Show one clip of the {side} at a time and refill as each fills."
    return "Do not send this order into the current book."


class RateLimited(RuntimeError):
    """Our own pacing refused the request before any model was asked."""


class ModelsResting(RateLimited):
    """Every model is cooling down after a failure, so the request was not sent."""


class AdviceInvalid(ValueError):
    """The model answered, but its answer could not be parsed or failed validation."""


class ModelCooldown:
    """
    Which models to leave alone for now, shared by every request in the process.

    The free tier caps each model per minute and per day. Without this, every call of every
    request tried the spent model first: one click is up to seven calls, so a model whose
    daily quota was gone cost seven failed round trips before the fallback answered, and a
    slow failure (504) could run the click past its deadline. A model that failed is now
    skipped until it can plausibly answer again:

    - daily quota spent (429 naming a per-day quota): until the quota resets, midnight Pacific
    - per-minute quota (other 429): Google's retryDelay, or 60 s
    - busy, 5xx or timed out: 30 s
    """

    MINUTE_S = 60.0
    BUSY_S = 30.0

    def __init__(self, clock=time.time):
        self._until = {}
        self._clock = clock
        self._guard = threading.Lock()

    def ready(self, models):
        """`models` in order, without those still cooling down."""
        now = self._clock()
        with self._guard:
            return [m for m in models if self._until.get(m, 0.0) <= now]

    def record(self, model, error):
        """Put `model` on cooldown for as long as `error` says it will keep failing."""
        seconds = self.cooldown_for(error)
        if seconds <= 0:
            return
        with self._guard:
            self._until[model] = max(self._until.get(model, 0.0), self._clock() + seconds)

    def resting(self):
        """{model: seconds left} for every model still cooling down."""
        now = self._clock()
        with self._guard:
            return {m: t - now for m, t in self._until.items() if t > now}

    def cooldown_for(self, error):
        """Seconds to rest a model after `error`; 0 for an error that is ours, not the model's."""
        if not is_transient(error):
            return 0.0
        if is_daily_quota(error):
            return seconds_to_quota_reset(self._clock())
        if is_quota(error):
            delay = retry_delay(error)
            return self.MINUTE_S if delay is None else delay
        return self.BUSY_S


def is_daily_quota(error):
    """A 429 for a per-day quota, which will not clear until the daily reset."""
    text = f"{error} {getattr(error, 'details', '')}"
    return "PerDay" in text or "per day" in text.lower()


def retry_delay(error):
    """The retryDelay Google attaches to a 429, in seconds, or None."""
    match = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", f"{error} {getattr(error, 'details', '')}")
    return float(match.group(1)) if match else None


def _pacific():
    """US Pacific time, from the OS database or the pinned tzdata package."""
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/Los_Angeles")


def seconds_to_quota_reset(now):
    """Seconds from `now` (epoch) to the next midnight Pacific, when free-tier daily quotas reset."""
    local = datetime.fromtimestamp(now, _pacific())
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, midnight.timestamp() - now)


class GeminiTransport(Transport):
    """
    Live Gemini transport.

    Runs the tool loop with function declarations and free text, then makes one final
    schema-constrained call, because Gemini rejects tools and a response schema together.
    Falls through `models` when a model ID is retired, and remembers what worked.

    `min_interval` is the minimum gap between advisor requests, not between API calls.
    One request is always several calls (each tool turn, the closing turn, the schema
    turn), so a per-call limit refuses every request on its second call.

    Nothing is retried. A busy model (503) or a spent quota (429) passes that one call to
    the next model, which on the free tier has its own quota; if none answers, the error
    ends the request. Only a retired model moves the default for later requests, and
    `answered_by` records which model answered each call of the current request.

    With a `cooldown` (a ModelCooldown shared across requests), a model that failed is
    skipped until it can plausibly answer again, and a request with every model resting
    fails at once without calling the API. Skipping a resting model does not move the
    default. `deadline_s` bounds one request: each call's timeout is cut to what is left.

    `call_interval` spaces API calls by at least that many seconds, for batch runs on the
    free tier (5 calls a minute per model). The client itself should carry a timeout, so
    one unanswered call cannot hang a run.

    NOT EXERCISED AGAINST THE LIVE API in this repository's tests or CI: there is no key
    in that environment. Everything offline runs through ReplayTransport.
    """

    name = "gemini"

    def __init__(self, client, models, min_interval=0.0, call_interval=0.0, cooldown=None, deadline_s=None,
                 call_timeout_s=None):
        self.client = client
        self.call_timeout_s = call_timeout_s  # the client's per-call timeout, kept under the deadline
        self.cooldown = cooldown  # a ModelCooldown shared across requests, or None
        self.deadline_s = deadline_s  # wall-clock budget for one request, or None
        self._deadline = None
        self.models = list(models)
        self.model = self.models[0] if self.models else ""
        self.min_interval = min_interval
        self.call_interval = call_interval
        self.answered_by = []
        self._last_request = 0.0
        self._last_attempt = 0.0
        self._contents = None
        self._pending = 0

    def begin(self):
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            raise RateLimited(f"Rate limited, try again in {wait:.0f}s")
        self._last_request = time.time()
        self._deadline = time.monotonic() + self.deadline_s if self.deadline_s else None
        self._contents = None
        self._pending = 0
        self.answered_by = []

    def _pace(self):
        wait = self.call_interval - (time.time() - self._last_attempt)
        if wait > 0:
            time.sleep(wait)
        self._last_attempt = time.time()

    def _call(self, contents, config):
        candidates = [self.model] + [m for m in self.models if m != self.model]
        if self.cooldown is not None:
            candidates = self.cooldown.ready(candidates)
            if not candidates:
                raise ModelsResting("Rate limited: every model is resting after hitting its free-tier limit")
        last, default_retired = None, False
        for i, model in enumerate(candidates):
            self._pace()
            try:
                call_config, cut_short = self._within_deadline(config)
            except TimeoutError:
                if last is not None:
                    raise last from None  # the model's own failure is the real cause, not the budget
                raise
            try:
                response = self.client.models.generate_content(model=model, contents=contents, config=call_config)
            except Exception as e:
                last = e
                default_retired = default_retired or (model == self.model and _model_unavailable(e))
                # A timeout we imposed to meet the request's deadline is not the model's fault.
                if self.cooldown is not None and not (cut_short and is_timeout(e)):
                    try:
                        self.cooldown.record(model, e)
                    except Exception as bookkeeping:  # never hide the model's error behind ours
                        print(f"Model cooldown: {bookkeeping!r}", file=sys.stderr, flush=True)
                if (_model_unavailable(e) or is_transient(e)) and i + 1 < len(candidates):
                    continue  # next model
                raise
            if default_retired:
                self.model = model  # the default is retired and not coming back; a busy or resting one is
            self.answered_by.append(model)
            return response
        raise last

    def _within_deadline(self, config):
        """
        (`config`, cut_short): the config with its HTTP timeout cut to what is left of the
        request's budget, and whether that cut it below the normal per-call timeout.

        Checking the deadline only before each call let one slow call run the click past it
        by a whole call timeout; the remaining budget now bounds the call itself, and never
        lifts it above the per-call timeout.
        """
        if self._deadline is None:
            return config, False
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"the {self.deadline_s:.0f}s budget for this request ran out")
        timeout_ms = int((left if self.call_timeout_s is None else min(left, self.call_timeout_s)) * 1000)
        if timeout_ms < 1000:
            # Lifting a sub-second remainder to the 1 s floor would run the call past the deadline.
            raise TimeoutError(f"the {self.deadline_s:.0f}s budget for this request ran out")
        from google.genai import types
        call = config.model_copy(update={"http_options": types.HttpOptions(timeout=timeout_ms)})
        return call, self.call_timeout_s is not None and left < self.call_timeout_s

    def propose(self, system_prompt, user_prompt, history):
        from google.genai import types

        if self._contents is None:
            self._contents = [types.Content(role="user", parts=[types.Part(text=user_prompt)])]
        elif history:
            # Feed back everything dispatched since the previous turn.
            parts = [types.Part.from_function_response(name=c["name"], response={"result": c["result"]})
                     for c in history[-self._pending:]] if self._pending else []
            if parts:
                self._contents.append(types.Content(role="user", parts=parts))

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._call(self._contents, config)
        calls = _function_calls(response)
        if calls:
            self._contents.append(response.candidates[0].content)
            self._pending = len(calls)
            return {"tool_calls": calls}
        self._pending = 0
        return {"advice": self._finalise(system_prompt, history, getattr(response, "text", ""))}

    def _finalise(self, system_prompt, history, closing_text):
        """Second phase: schema-constrained call, no tools, handed the tool transcript."""
        from google.genai import types

        transcript = "\n".join(
            f"{c['name']}({json.dumps(c['args'], sort_keys=True)}) -> {json.dumps(c['result'], sort_keys=True)}"
            for c in history
        ) or "(no tools were called)"
        prompt = (
            "These are the tool calls you made against the live book and what they returned:\n\n"
            f"{transcript}\n\n"
            f"Your closing assessment was:\n{closing_text or '(none)'}\n\n"
            "Return your advice as a single JSON object matching the schema. Every number must "
            "come from the tool results above."
        )
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=ADVICE_SCHEMA,
        )
        response = self._call([types.Content(role="user", parts=[types.Part(text=prompt)])], config)
        return _parse_json(getattr(response, "text", ""))


def _function_calls(response):
    """Pull function calls out of a Gemini response in either SDK shape."""
    calls = getattr(response, "function_calls", None)
    if calls:
        return [{"name": c.name, "args": dict(c.args or {})} for c in calls]
    out = []
    for candidate in getattr(response, "candidates", None) or []:
        for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
            fc = getattr(part, "function_call", None)
            if fc is not None:
                out.append({"name": fc.name, "args": dict(getattr(fc, "args", None) or {})})
    return out


def _model_unavailable(error):
    code = getattr(error, "code", None)
    text = str(error)
    return code == 404 or "NOT_FOUND" in text or "no longer available" in text


TRANSIENT_CODES = (429, 500, 502, 503, 504)


def is_timeout(error):
    """The call ran out of time: our HTTP timeout, the request budget, or the server's 504."""
    return (isinstance(error, TimeoutError) or "Timeout" in type(error).__name__
            or "timed out" in str(error).lower() or getattr(error, "code", None) == 504)


def is_quota(error):
    """A 429: the model's per-minute or per-day quota is spent."""
    return getattr(error, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(error)


def is_transient(error):
    """A provider-side failure: quota, overload, a 5xx, or a call that timed out."""
    return (getattr(error, "code", None) in TRANSIENT_CODES or is_quota(error) or is_timeout(error)
            or "UNAVAILABLE" in str(error))


def is_upstream(error):
    """The provider, or our own request throttle, stopped the run before the model answered."""
    return is_transient(error) or isinstance(error, RateLimited)



def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AdviceInvalid("no JSON object in response")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AdviceInvalid(f"response is not valid JSON: {e}") from e


# ------------------------------------------------------------------------------- driver

def run_advisor(transport, book, side, notional, *, order_type="Market", fee_tier="Tier 1",
                volatility=0.01, max_turns=MAX_TOOL_TURNS, book_age=None):
    """
    Drive one advisor run to a validated AdviceResult. Never raises.

    `book_age` is a human string like "0.4s"; it is put in the prompt so the model can
    say the read is stale rather than pretending the snapshot is live.
    """
    started = time.perf_counter()
    side = "sell" if str(side).lower() == "sell" else "buy"
    tools = BookTools(book, fee_tier=fee_tier, volatility=volatility)
    user_prompt = USER_TEMPLATE.format(
        symbol=book.get("symbol", "unknown"),
        source=book.get("source", "unknown venue"),
        side=side.upper(),
        order_type=order_type,
        notional=float(notional),
        fee_tier=fee_tier,
        volatility=float(volatility),
        age=book_age or "unknown",
    )

    errors, turns, raw, upstream, failure = [], 0, None, False, None
    try:
        begin = getattr(transport, "begin", None)  # optional, for duck-typed transports
        if begin is not None:
            begin()
        for turns in range(1, max_turns + 1):
            turn = transport.propose(SYSTEM_PROMPT, user_prompt, tools.calls)
            if not isinstance(turn, dict):
                errors.append("transport returned a non-object turn")
                break
            if turn.get("error"):
                errors.append(str(turn["error"]))
                break
            if "advice" in turn:
                raw = turn["advice"]
                break
            calls = turn.get("tool_calls") or []
            if not calls:
                errors.append("transport proposed neither tool calls nor advice")
                break
            for call in calls:
                tools.dispatch(call.get("name", ""), call.get("args"))
        else:
            errors.append(f"hit the {max_turns}-turn ceiling without producing advice")
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")
        upstream = is_upstream(e)
        failure = e

    advice = None
    if raw is not None:
        advice, problems = validate_advice(raw, expected_side=side)
        errors.extend(problems)

    return AdviceResult(
        advice=advice,
        errors=errors,
        tool_calls=tools.calls,
        transport=getattr(transport, "name", "unknown"),
        model=getattr(transport, "model", ""),
        turns=turns,
        latency_ms=(time.perf_counter() - started) * 1e3,
        raw=raw,
        upstream_error=upstream,
        models_used=list(dict.fromkeys(getattr(transport, "answered_by", None) or [])),  # answer order
        exception=failure,
    )
