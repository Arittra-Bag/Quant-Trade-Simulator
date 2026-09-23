"""
Multi-venue L2 orderbook client.

Connects to a public orderbook WebSocket, normalises every venue to one shape and
atomically writes the latest book to a JSON file that the Dash app polls:

    {"bids": [[price, size], ...], "asks": [[price, size], ...],
     "timestamp": <exchange ms>, "local_time": <epoch s>,
     "symbol": "BTC-USDT-SWAP", "source": "OKX", "size_unit": "base"}

Sizes are always in base-asset units (OKX swap contracts are converted with the
instrument's contract value), so the app can walk the book in USD.

A small status file (feed_status.json next to the output) tells the app which venue
is live, when the last message arrived and the last error, so the UI never claims a
stream is active when it is not.

Usage (unchanged): python websocket_client.py --symbol BTC-USDT-SWAP
"""
import argparse
import asyncio
import json
import math
import os
import random
import signal
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, version

import websockets
from loguru import logger

try:
    WS_VERSION = tuple(int(p) for p in version("websockets").split(".")[:3] if p.isdigit())
except PackageNotFoundError:
    WS_VERSION = (0, 0, 0)

logger.add("websocket.log", rotation="10 MB", retention=3, level="INFO")

BOOK_DEPTH = 25            # levels kept per side
FIRST_BOOK_TIMEOUT = 10    # seconds to wait for a valid book before trying the next venue
RECV_TIMEOUT = 30          # seconds of silence before reconnecting

# Venue order when the requested venue is unreachable. SIM is never used as a silent fallback.
FALLBACK_ORDER = ["OKX", "HYPERLIQUID", "BINANCE", "KRAKEN"]

# OKX swap contract values (base units per contract), used if the REST lookup fails.
OKX_CT_VAL_DEFAULTS = {"BTC": 0.01, "ETH": 0.1, "SOL": 1.0, "XRP": 100.0, "DOGE": 1000.0}

shutdown_flag = False


# --------------------------------------------------------------------------- symbols

def base_asset(symbol):
    return (symbol or "BTC").split("-")[0].upper()


def quote_asset(symbol):
    parts = (symbol or "BTC-USDT").split("-")
    return parts[1].upper() if len(parts) > 1 else "USDT"


def normalize_symbol_for_venue(symbol, venue):
    """BTC-USDT-SWAP -> the venue's own instrument name."""
    if venue == "OKX":
        return symbol
    if venue == "HYPERLIQUID":
        return base_asset(symbol)
    if venue == "BINANCE":
        return f"{base_asset(symbol)}{quote_asset(symbol)}".lower()
    if venue == "KRAKEN":
        return f"{base_asset(symbol)}/USD"
    return symbol


def okx_contract_value(symbol):
    """Base units per contract for an OKX derivative, 1.0 for spot."""
    if not symbol.upper().endswith(("-SWAP", "-FUTURES")) and symbol.count("-") < 2:
        return 1.0
    try:
        url = f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={symbol}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp).get("data") or []
        if data and data[0].get("ctVal"):
            return float(data[0]["ctVal"])
    except Exception as e:
        logger.warning(f"OKX contract value lookup failed ({e}); using default")
    return OKX_CT_VAL_DEFAULTS.get(base_asset(symbol), 1.0)


# --------------------------------------------------------------------------- venues

def _levels(rows, mult=1.0):
    out = []
    for row in rows:
        try:
            out.append([float(row[0]), float(row[1]) * mult])
        except (TypeError, ValueError, IndexError):
            continue
    return out


class Venue:
    """One venue: URL, subscription and message parser. parse() returns a book or None."""
    name = ""

    def __init__(self, symbol):
        self.symbol = symbol
        self.venue_symbol = normalize_symbol_for_venue(symbol, self.name)

    url = ""

    def subscribe_message(self):
        return None

    def parse(self, msg):
        raise NotImplementedError


class OKXVenue(Venue):
    """OKX books5: a full top-5 snapshot every 100 ms, so no delta bookkeeping is needed."""
    name = "OKX"
    url = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, symbol, ct_val=None):
        super().__init__(symbol)
        self.ct_val = ct_val if ct_val is not None else okx_contract_value(symbol)

    def subscribe_message(self):
        return {"op": "subscribe", "args": [{"channel": "books5", "instId": self.venue_symbol}]}

    def parse(self, msg):
        if msg.get("arg", {}).get("channel") != "books5" or not msg.get("data"):
            return None
        book = msg["data"][0]
        return {
            "bids": _levels(book.get("bids", []), self.ct_val),
            "asks": _levels(book.get("asks", []), self.ct_val),
            "timestamp": int(book.get("ts", time.time() * 1000)),
        }


class HyperliquidVenue(Venue):
    """Hyperliquid l2Book: full snapshots, sizes already in base units."""
    name = "HYPERLIQUID"
    url = "wss://api.hyperliquid.xyz/ws"

    def subscribe_message(self):
        return {"method": "subscribe", "subscription": {"type": "l2Book", "coin": self.venue_symbol}}

    def parse(self, msg):
        if msg.get("channel") != "l2Book" or "data" not in msg:
            return None
        levels = msg["data"].get("levels", [[], []])
        return {
            "bids": _levels([[l["px"], l["sz"]] for l in levels[0]]),
            "asks": _levels([[l["px"], l["sz"]] for l in levels[1]]),
            "timestamp": int(msg["data"].get("time", time.time() * 1000)),
        }


class BinanceVenue(Venue):
    """Binance USD-M futures partial depth: 20-level snapshot every 100 ms ("b"/"a" keys)."""
    name = "BINANCE"

    @property
    def url(self):
        return f"wss://fstream.binance.com/ws/{self.venue_symbol}@depth20@100ms"

    def parse(self, msg):
        if "b" not in msg or "a" not in msg:
            return None
        return {
            "bids": _levels(msg["b"]),
            "asks": _levels(msg["a"]),
            "timestamp": int(msg.get("T") or msg.get("E") or time.time() * 1000),
        }


class KrakenVenue(Venue):
    """Kraken v2 book: snapshot then deltas, maintained locally."""
    name = "KRAKEN"
    url = "wss://ws.kraken.com/v2"

    def __init__(self, symbol):
        super().__init__(symbol)
        self.bids, self.asks = {}, {}

    def subscribe_message(self):
        return {"method": "subscribe", "params": {"channel": "book", "symbol": [self.venue_symbol], "depth": 25}}

    def parse(self, msg):
        if msg.get("channel") != "book" or not msg.get("data"):
            return None
        data = msg["data"][0]
        if msg.get("type") == "snapshot":
            self.bids, self.asks = {}, {}
        for side, store in (("bids", self.bids), ("asks", self.asks)):
            for lvl in data.get(side, []):
                px, qty = float(lvl["price"]), float(lvl["qty"])
                if qty == 0:
                    store.pop(px, None)
                else:
                    store[px] = qty
        bids = sorted(self.bids.items(), reverse=True)[:BOOK_DEPTH]
        asks = sorted(self.asks.items())[:BOOK_DEPTH]
        self.bids, self.asks = dict(bids), dict(asks)
        return {"bids": [list(b) for b in bids], "asks": [list(a) for a in asks],
                "timestamp": int(time.time() * 1000)}


VENUES = {"OKX": OKXVenue, "HYPERLIQUID": HyperliquidVenue, "BINANCE": BinanceVenue, "KRAKEN": KrakenVenue}


def venue_order(requested):
    requested = (requested or "OKX").upper()
    return [requested] + [v for v in FALLBACK_ORDER if v != requested]


def validate_book(book):
    """Sorted, crossed-book-free, depth-limited book or None."""
    if not book or not book.get("bids") or not book.get("asks"):
        return None
    bids = sorted((l for l in book["bids"] if l[1] > 0), key=lambda l: -l[0])[:BOOK_DEPTH]
    asks = sorted((l for l in book["asks"] if l[1] > 0), key=lambda l: l[0])[:BOOK_DEPTH]
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return None
    book["bids"], book["asks"] = bids, asks
    return book


# --------------------------------------------------------------------------- output

class BookWriter:
    def __init__(self, output_file, symbol, update_interval):
        self.output_file = output_file
        self.status_file = os.path.join(os.path.dirname(os.path.abspath(output_file)), "feed_status.json")
        self.symbol = symbol
        self.update_interval = update_interval
        self.last_write = 0.0
        self.status = {"state": "starting", "source": None, "symbol": symbol, "pid": os.getpid(),
                       "messages": 0, "books": 0, "last_book_time": None, "error": None,
                       "started_at": time.time()}
        self.write_status()

    @staticmethod
    def _atomic_write(path, payload):
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)

    def write_status(self, **changes):
        self.status.update(changes)
        self.status["updated_at"] = time.time()
        try:
            self._atomic_write(self.status_file, self.status)
        except OSError as e:
            logger.error(f"Could not write status file: {e}")

    def write_book(self, book, source):
        now = time.time()
        self.status["books"] += 1
        if now - self.last_write < self.update_interval:
            return
        book.update({"local_time": now, "symbol": self.symbol, "source": source, "size_unit": "base"})
        try:
            self._atomic_write(self.output_file, book)
            self.last_write = now
            self.write_status(state="live", source=source, last_book_time=now, error=None)
        except OSError as e:
            logger.error(f"Could not write orderbook file: {e}")


# --------------------------------------------------------------------------- simulator

async def run_simulator(writer, symbol, mid=65000.0):
    """Synthetic random-walk book for demos and offline testing (explicit opt-in only)."""
    writer.write_status(state="live", source="SIM")
    tick = 0.1
    while not shutdown_flag:
        mid *= math.exp(random.gauss(0, 0.00002))
        spread_ticks = random.choice([1, 1, 1, 2, 3])
        bid0 = round(math.floor(mid / tick) * tick, 1)
        ask0 = round(bid0 + spread_ticks * tick, 1)
        bids, asks, bpx, apx = [], [], bid0, ask0
        for i in range(BOOK_DEPTH):
            bids.append([round(bpx, 1), round(random.lognormvariate(-0.7, 1.0) * (1 + i / 8), 4)])
            asks.append([round(apx, 1), round(random.lognormvariate(-0.7, 1.0) * (1 + i / 8), 4)])
            bpx -= tick * random.choice([1, 1, 2, 3])
            apx += tick * random.choice([1, 1, 2, 3])
        book = validate_book({"bids": bids, "asks": asks, "timestamp": int(time.time() * 1000)})
        if book:
            writer.status["messages"] += 1
            writer.write_book(book, "SIM")
        await asyncio.sleep(0.1)


# --------------------------------------------------------------------------- live feed

async def stream_venue(venue, writer):
    """Stream one venue until it fails. Returns True if it ever produced a valid book."""
    url = os.environ.get("ORDERBOOK_WS_URL_" + venue.name) or venue.url
    logger.info(f"Connecting to {venue.name} {url} for {venue.venue_symbol}")
    writer.write_status(state="connecting", source=venue.name)
    got_book = False
    async with websockets.connect(url, ping_interval=20, ping_timeout=10, open_timeout=10,
                                  max_size=2 ** 22) as ws:
        sub = venue.subscribe_message()
        if sub:
            await ws.send(json.dumps(sub))
        connected_at = time.time()
        while not shutdown_flag:
            timeout = RECV_TIMEOUT if got_book else max(0.1, FIRST_BOOK_TIMEOUT - (time.time() - connected_at))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                reason = "no data" if got_book else "no valid book"
                raise ConnectionError(f"{venue.name}: {reason} within {timeout:.0f}s")
            writer.status["messages"] += 1
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(msg, dict) and msg.get("event") == "error":
                raise ConnectionError(f"{venue.name} error: {msg.get('msg') or msg}")
            book = validate_book(venue.parse(msg) if isinstance(msg, dict) else None)
            if book:
                if not got_book:
                    logger.info(f"{venue.name}: first book, {len(book['bids'])}x{len(book['asks'])} levels")
                got_book = True
                writer.write_book(book, venue.name)
    return got_book


async def connect_and_save(symbol, output_file="latest_orderbook.json", update_interval=0.25, exchange="OKX"):
    """
    Stream the requested venue, falling back through the others if it cannot deliver
    a valid book. A venue that worked is retried first after a disconnect.
    """
    writer = BookWriter(output_file, symbol, update_interval)
    if (exchange or "").upper() == "SIM":
        await run_simulator(writer, symbol)
        return

    order = venue_order(exchange)
    ct_val = okx_contract_value(symbol) if "OKX" in order else None
    idx, delay = 0, 1.0
    while not shutdown_flag:
        name = order[idx]
        venue = OKXVenue(symbol, ct_val) if name == "OKX" else VENUES[name](symbol)
        try:
            worked = await stream_venue(venue, writer)
        except asyncio.CancelledError:
            return
        except Exception as e:
            worked = writer.status.get("source") == name and writer.status.get("state") == "live"
            logger.warning(f"{name} failed: {e}")
            writer.write_status(state="reconnecting", error=f"{name}: {e}")
        if shutdown_flag:
            break
        if worked:
            delay = 1.0          # retry the venue that was working
        else:
            idx = (idx + 1) % len(order)
            if idx == 0:
                wait = delay + random.uniform(0, 0.5) * delay
                logger.info(f"All venues failed, retrying in {wait:.1f}s")
                writer.write_status(state="retrying", error=f"All venues unreachable, retrying in {wait:.0f}s")
                await asyncio.sleep(wait)
                delay = min(30.0, delay * 1.5)
                continue
        await asyncio.sleep(0.2)
    writer.write_status(state="stopped")


async def watch_parent(parent_pid, interval=1.0):
    """Return when the launching process goes away, so the feed never outlives the app."""
    while not shutdown_flag:
        if os.getppid() != parent_pid:
            logger.info("Parent process exited; stopping feed")
            return
        await asyncio.sleep(interval)


def _handle_signal(*_):
    global shutdown_flag
    shutdown_flag = True
    raise KeyboardInterrupt


async def main():
    parser = argparse.ArgumentParser(description="Multi-venue L2 orderbook WebSocket client")
    parser.add_argument("--symbol", type=str, default="BTC-USDT-SWAP", help="Trading pair symbol")
    parser.add_argument("--output", type=str, default="latest_orderbook.json", help="Output file path")
    parser.add_argument("--interval", type=float, default=0.25, help="Minimum seconds between file writes")
    parser.add_argument("--exchange", type=str, default="OKX",
                        help="Preferred venue: OKX, HYPERLIQUID, BINANCE, KRAKEN or SIM")
    args = parser.parse_args()

    logger.info(f"websockets {'.'.join(map(str, WS_VERSION))}; {args.exchange} {args.symbol} -> {args.output}")
    feed = asyncio.create_task(connect_and_save(args.symbol, args.output, args.interval, args.exchange))
    watchdog = asyncio.create_task(watch_parent(os.getppid()))
    _, pending = await asyncio.wait({feed, watchdog}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")
