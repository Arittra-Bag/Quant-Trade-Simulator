"""
Record books and the public trade tape so the cost model can be checked against
real fills.

Writes one JSON object per line:

    {"type": "book",  "ts": 1758650000.25, "bids": [[px, sz]], "asks": [[px, sz]]}
    {"type": "trade", "ts": 1758650000.31, "px": 65001.0, "sz": 0.4, "side": "buy", "id": "..."}

`side` is the aggressor: "buy" means someone lifted the offer. Sizes are base-asset
units, so swap contracts are converted with the instrument's contract value exactly as
the live feed does.

Usage:
    python -m validation.record --symbol BTC-USDT-SWAP --minutes 10 --out validation/data/okx.jsonl

A python.org build for macOS cannot verify OKX's certificate out of the box; requests
then retry against certifi's bundle (see websocket_client.get_json). The recorder stops
after MAX_FAILURES failed polls in a row instead of writing an empty file for the whole run.
"""
import argparse
import json
import os
import time

from websocket_client import USER_AGENT, get_json  # noqa: F401  (USER_AGENT: what OKX sees)

OKX_REST = "https://www.okx.com/api/v5"
BOOK_DEPTH = 25
MAX_FAILURES = 10


def _get(url, timeout=10):
    return get_json(url, timeout=timeout)


def contract_value(symbol):
    """Base units per contract for an OKX swap, 1.0 for spot."""
    if not symbol.endswith("-SWAP"):
        return 1.0
    try:
        from websocket_client import okx_contract_value
        return float(okx_contract_value(symbol))
    except Exception:
        return 1.0


def fetch_book(symbol, ct_val):
    data = _get(f"{OKX_REST}/market/books?instId={symbol}&sz={BOOK_DEPTH}")["data"][0]
    to_levels = lambda rows: [[float(p), float(s) * ct_val] for p, s, *_ in rows]
    return {"type": "book", "ts": float(data["ts"]) / 1000.0,
            "bids": to_levels(data["bids"]), "asks": to_levels(data["asks"])}


def fetch_trades(symbol, ct_val, seen):
    out = []
    for t in _get(f"{OKX_REST}/market/trades?instId={symbol}&limit=100")["data"]:
        if t["tradeId"] in seen:
            continue
        seen.add(t["tradeId"])
        out.append({"type": "trade", "ts": float(t["ts"]) / 1000.0, "px": float(t["px"]),
                    "sz": float(t["sz"]) * ct_val, "side": t["side"], "id": t["tradeId"]})
    return out


def record(symbol="BTC-USDT-SWAP", minutes=10.0, interval=0.5, out="validation/data/okx.jsonl"):
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    ct_val = contract_value(symbol)
    seen, deadline, books, trades, failures = set(), time.time() + minutes * 60, 0, 0, 0
    with open(out, "w") as fh:
        while time.time() < deadline:
            try:
                fh.write(json.dumps(fetch_book(symbol, ct_val)) + "\n")
                books += 1
                for tr in fetch_trades(symbol, ct_val, seen):
                    fh.write(json.dumps(tr) + "\n")
                    trades += 1
                fh.flush()
                failures = 0
            except Exception as e:                      # keep recording through a blip
                failures += 1
                print(f"record error: {e}")
                if failures >= MAX_FAILURES:
                    raise SystemExit(f"stopped: {failures} requests in a row failed, last with {e}. "
                                     "Check network access to OKX.")
            time.sleep(interval)
    print(f"wrote {books} books and {trades} trades to {out}")
    if not books:
        raise SystemExit(
            "recorded nothing: every request failed. Check network access to OKX.")
    return out


def main():
    p = argparse.ArgumentParser(description="Record OKX books and trades for cost-model validation")
    p.add_argument("--symbol", default="BTC-USDT-SWAP")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--out", default="validation/data/okx.jsonl")
    a = p.parse_args()
    record(a.symbol, a.minutes, a.interval, a.out)


if __name__ == "__main__":
    main()
