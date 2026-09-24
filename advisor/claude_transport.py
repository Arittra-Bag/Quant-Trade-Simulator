"""
Claude transport for the execution advisor, over the official Anthropic SDK.

Same contract as GeminiTransport, so `run_advisor`, the tools and the graders are shared:
`propose` returns {"tool_calls": [...]} while the model is still pricing, then
{"advice": {...}} when it is done. The differences are Claude's, not the harness's:

- One request shape for the whole run. Claude accepts tools and a JSON-schema response
  format together, so the final answer comes out of the same loop, schema-constrained,
  instead of Gemini's separate closing call.
- The system prompt and tools are marked for prompt caching; every turn of a run resends
  them, and on a cache hit they cost a tenth of the input rate.
- Every response's `usage` is added up and priced, so an eval row can say what it cost.

Used by the eval runner to compare providers. The app itself stays on Gemini.
"""
import copy
import json
import time

from .advisor import AdviceInvalid, RateLimited, Transport, _parse_json
from .schema import ADVICE_SCHEMA
from .tools import TOOL_DECLARATIONS

# USD per million tokens: (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10


def _json_schema(node):
    """Google's schema dialect (upper-case types) to JSON Schema, closed objects throughout."""
    if isinstance(node, list):
        return [_json_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "propertyOrdering":
            continue  # a Gemini-only hint
        out[key] = value.lower() if key == "type" and isinstance(value, str) else _json_schema(value)
    if out.get("type") == "object":
        out.setdefault("properties", {})
        out["additionalProperties"] = False
    return out


TOOLS = [{"name": d["name"], "description": d["description"], "input_schema": _json_schema(d["parameters"])}
         for d in TOOL_DECLARATIONS]

# Structured outputs require every property listed in `required`; limit_price is 0 when the
# strategy is not priced, which the local validator already accepts.
OUTPUT_SCHEMA = _json_schema(copy.deepcopy(ADVICE_SCHEMA))
OUTPUT_SCHEMA["required"] = list(OUTPUT_SCHEMA["properties"])


def run_cost(model, usage_totals):
    """USD for the tokens in `usage_totals`, at `model`'s list price; None for an unknown model."""
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    return (usage_totals.get("input_tokens", 0) * price_in
            + usage_totals.get("cache_creation_input_tokens", 0) * price_in * CACHE_WRITE
            + usage_totals.get("cache_read_input_tokens", 0) * price_in * CACHE_READ
            + usage_totals.get("output_tokens", 0) * price_out) / 1e6


class ClaudeTransport(Transport):
    """
    `client` is an `anthropic.Anthropic()`. `effort` sets output_config.effort on models that
    take it (not Haiku 4.5). `min_interval` spaces requests like GeminiTransport's.
    """

    name = "claude"

    def __init__(self, client, model, effort=None, max_tokens=8000, min_interval=0.0):
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.min_interval = min_interval
        self.answered_by = []
        self.usage = {}
        self._last_request = 0.0
        self._messages = None
        self._pending = []

    def begin(self):
        """Start a request: a fresh conversation and fresh usage totals."""
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            raise RateLimited(f"Rate limited, try again in {wait:.0f}s")
        self._last_request = time.time()
        self._messages = None
        self._pending = []
        self.answered_by = []
        self.usage = {}

    @property
    def cost_usd(self):
        """What this request has cost so far, at list price."""
        return run_cost(self.model, self.usage)

    def propose(self, system_prompt, user_prompt, history):
        """One model turn: tool calls to run, or the final schema-constrained advice."""
        if self._messages is None:
            self._messages = [{"role": "user", "content": user_prompt}]
        elif self._pending:
            # Every tool_use needs its tool_result, all in one user message, in order.
            dispatched = history[-len(self._pending):]
            self._messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": use_id, "content": json.dumps(call["result"], sort_keys=True),
                 **({"is_error": True} if isinstance(call["result"], dict) and call["result"].get("error") else {})}
                for use_id, call in zip(self._pending, dispatched)]})

        output_config = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
        if self.effort:
            output_config["effort"] = self.effort
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            # Tools render before the system prompt, so this one breakpoint caches both.
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            output_config=output_config,
            messages=self._messages,
        )
        self._count(response)
        self.answered_by.append(self.model)

        if response.stop_reason == "refusal":
            raise RuntimeError(f"the model declined: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise AdviceInvalid(f"the answer was cut off at max_tokens={self.max_tokens}")

        uses = [b for b in response.content if b.type == "tool_use"]
        if uses:
            # Thinking and text blocks go back unchanged with the tool calls.
            self._messages.append({"role": "assistant", "content": response.content})
            self._pending = [b.id for b in uses]
            return {"tool_calls": [{"name": b.name, "args": dict(b.input or {})} for b in uses]}
        self._pending = []
        text = "".join(b.text for b in response.content if b.type == "text")
        return {"advice": _parse_json(text)}

    def _count(self, response):
        usage = getattr(response, "usage", None)
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            self.usage[key] = self.usage.get(key, 0) + (getattr(usage, key, 0) or 0)
