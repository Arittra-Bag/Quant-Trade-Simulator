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
import time

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
                 turns=0, latency_ms=0.0, raw=None):
        self.advice = advice
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


class GeminiTransport(Transport):
    """
    Live Gemini transport.

    Runs the tool loop with function declarations and free text, then makes one final
    schema-constrained call, because Gemini rejects tools and a response schema together.
    Falls through `models` when a model ID is retired, and remembers what worked.

    `min_interval` is the minimum gap between advisor requests, not between API calls.
    One request is always several calls (each tool turn, the closing turn, the schema
    turn), so a per-call limit refuses every request on its second call. `max_retries`
    retries a 429 after the delay the API asks for; 0 surfaces it straight away, which
    is what a UI callback wants.

    NOT EXERCISED AGAINST THE LIVE API in this repository's tests or CI: there is no key
    in that environment. Everything offline runs through ReplayTransport.
    """

    name = "gemini"

    MAX_RETRY_DELAY = 60.0

    def __init__(self, client, models, min_interval=0.0, max_retries=0):
        self.client = client
        self.models = list(models)
        self.model = self.models[0] if self.models else ""
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request = 0.0
        self._contents = None
        self._pending = 0

    def begin(self):
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            raise RuntimeError(f"Rate limited, try again in {wait:.0f}s")
        self._last_request = time.time()
        self._contents = None
        self._pending = 0

    def _call(self, contents, config):
        candidates = [self.model] + [m for m in self.models if m != self.model]
        last, retries = None, 0
        i = 0
        while i < len(candidates):
            model = candidates[i]
            try:
                response = self.client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as e:
                last = e
                if _model_unavailable(e) and i + 1 < len(candidates):
                    i += 1
                    continue
                if _rate_limited(e) and retries < self.max_retries:
                    retries += 1
                    time.sleep(min(_retry_delay(e), self.MAX_RETRY_DELAY))
                    continue
                raise
            self.model = model
            return response
        raise last

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


def _rate_limited(error):
    return getattr(error, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(error)


def _retry_delay(error, default=10.0):
    """Seconds the API asked us to wait, from its RetryInfo detail when it sent one."""
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        for item in (details.get("error") or {}).get("details") or []:
            delay = item.get("retryDelay") if isinstance(item, dict) else None
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return max(float(delay[:-1]), 0.0)
                except ValueError:
                    pass
    return default


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


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

    errors, turns, raw = [], 0, None
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
    )
