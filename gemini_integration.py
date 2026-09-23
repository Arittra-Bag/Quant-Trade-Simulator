"""
Gemini market read for the current book and order.

Uses the supported `google-genai` SDK (the older `google-generativeai` package is deprecated)
and asks for JSON output directly.

Model: GEMINI_MODEL (default gemini-3.8-flash, the newest stable Flash model). If Google
retires or restricts it, the analyzer falls through GEMINI_FALLBACK_MODELS (comma-separated,
default gemini-3.5-flash-lite) instead of failing, and remembers the model that worked.
The API key is read from GEMINI_API_KEY (or GOOGLE_API_KEY) and never logged.
"""
import json
import os
import time

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODELS = [m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",")
                   if m.strip()]

if not API_KEY:
    print("Warning: GEMINI_API_KEY not set; AI analysis is disabled.")


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def _is_model_unavailable(error):
    """True for errors that mean this model ID is retired or not offered to this key."""
    code = getattr(error, "code", None)
    text = str(error)
    return code == 404 or "NOT_FOUND" in text or "no longer available" in text


class GeminiAnalyzer:
    def __init__(self):
        self.client = None
        self.model = None
        self.models = []
        if API_KEY:
            try:
                from google import genai
                self.client = genai.Client(api_key=API_KEY)
                self.models = list(dict.fromkeys([MODEL] + FALLBACK_MODELS))
                self.model = self.models[0]
            except Exception as e:
                print(f"Gemini client unavailable: {e}")
        self.last_call_time = 0
        self.min_interval = 5  # seconds between calls, to stay inside free-tier rate limits

    @property
    def enabled(self):
        return self.client is not None

    def _generate(self, prompt):
        from google.genai import types
        wait = self.min_interval - (time.time() - self.last_call_time)
        if wait > 0:
            raise RuntimeError(f"Rate limited, try again in {wait:.0f}s")
        self.last_call_time = time.time()
        config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
        candidates = [self.model] + [m for m in self.models if m != self.model]
        for i, model in enumerate(candidates):
            try:
                response = self.client.models.generate_content(model=model, contents=prompt, config=config)
            except Exception as e:
                if _is_model_unavailable(e) and i + 1 < len(candidates):
                    print(f"Gemini model {model} unavailable ({getattr(e, 'code', '')}); trying {candidates[i + 1]}")
                    continue
                raise
            self.model = model
            break
        return _parse_json(response.text)

    def analyze(self, orderbook_data, quantity, fees, slippage, impact):
        """
        One call that returns sentiment, analysis, recommendation, strategy, reasoning and
        execution_approach, plus success. Never raises.
        """
        if not self.enabled:
            return {"success": False, "analysis": "Set GEMINI_API_KEY to enable AI analysis."}
        if not orderbook_data or not orderbook_data.get("bids") or not orderbook_data.get("asks"):
            return {"success": False, "analysis": "No orderbook data to analyse yet."}
        try:
            bids, asks = orderbook_data["bids"][:10], orderbook_data["asks"][:10]
            top_bid, top_ask = float(bids[0][0]), float(asks[0][0])
            mid = (top_bid + top_ask) / 2
            bid_vol = sum(float(b[1]) for b in bids)
            ask_vol = sum(float(a[1]) for a in asks)
            imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol) if bid_vol + ask_vol else 0
            total = fees + slippage + impact
            bps = lambda usd: usd / quantity * 1e4 if quantity else 0
            prompt = f"""You are an execution analyst on a crypto trading desk.
Instrument: {orderbook_data.get('symbol', 'BTC-USDT-SWAP')} on {orderbook_data.get('source', 'unknown venue')}
Top 10 bids [price, size in base units]: {json.dumps(bids)}
Top 10 asks [price, size in base units]: {json.dumps(asks)}
Mid {mid:.2f}, spread {top_ask - top_bid:.4f} ({(top_ask - top_bid) / mid * 1e4:.2f} bps), top-10 imbalance {imbalance:+.3f} (positive = more bid size)
Proposed order: market BUY ${quantity:,.2f}
Estimated costs: fees ${fees:.4f} ({bps(fees):.2f} bps), slippage ${slippage:.4f} ({bps(slippage):.2f} bps), impact ${impact:.4f} ({bps(impact):.2f} bps), total ${total:.4f} ({bps(total):.2f} bps)

Using only this data, return a JSON object with string fields:
sentiment (one of Bullish, Bearish, Neutral), analysis (2 sentences on book shape and liquidity),
recommendation (1 sentence), strategy (short name, e.g. "Immediate market", "Passive limit at touch", "TWAP 5 slices"),
reasoning (1-2 sentences), execution_approach (1 sentence). Be concise and quantitative."""
            result = self._generate(prompt)
            result = {k: str(v) for k, v in result.items()}
            result["success"] = True
            result["model"] = self.model
            return result
        except Exception as e:
            return {"success": False, "analysis": f"Analysis failed: {e}"}

    # Backwards-compatible wrappers ------------------------------------------------------

    def analyze_orderbook(self, orderbook_data):
        result = self.analyze(orderbook_data, 100.0, 0.0, 0.0, 0.0)
        result.setdefault("sentiment", "Neutral")
        return result

    def get_trading_strategy(self, orderbook_data, quantity, fees, slippage, impact):
        result = self.analyze(orderbook_data, quantity, fees, slippage, impact)
        result.setdefault("strategy", "Unavailable")
        result.setdefault("reasoning", result.get("analysis", ""))
        result.setdefault("execution_approach", "")
        return result
