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
import ssl
import time
import urllib.error
import urllib.request
import zlib
from decimal import Decimal
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


# OKX answers urllib's default "Python-urllib/3.x" agent with 403.
USER_AGENT = "quant-trade-simulator/1.0"


_CERTIFI = {}


def certifi_context():
    """An SSL context over certifi's CA bundle, or None if certifi is not installed. Cached."""
    if "ctx" not in _CERTIFI:
        try:
            import certifi
            _CERTIFI["ctx"] = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _CERTIFI["ctx"] = None
    return _CERTIFI["ctx"]


def get_json(url, timeout=10):
    """
    GET a JSON document with a real User-Agent. If the system's certificate store cannot
    verify the host, as on python.org builds for macOS until their certificate installer is
    run, retry once against certifi's bundle rather than failing every request.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.URLError as e:
        ctx = certifi_context() if isinstance(e.reason, ssl.SSLCertVerificationError) else None
        if ctx is None:
            raise
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.load(resp)


# Set once the system store has failed to verify a venue, so later connects go straight to certifi.
_WS_TLS = {"certifi": False}


async def open_ws(url):
    """
    Open a WebSocket. Like get_json, a certificate the system store cannot verify is retried
    against certifi's bundle, and later connects use it directly; a proxy that installs its
    own CA in the system store keeps working, since the system store is tried first.
    """
    kw = {"ping_interval": 20, "ping_timeout": 10, "open_timeout": 10, "max_size": 2 ** 22}
    secure = url.startswith("wss://")
    if secure and _WS_TLS["certifi"] and certifi_context() is not None:
        return await websockets.connect(url, ssl=certifi_context(), **kw)
    try:
        return await websockets.connect(url, **kw)
    except ssl.SSLCertVerificationError:
        if not secure or certifi_context() is None:
            raise
        logger.warning("The system certificate store could not verify the venue; using certifi's bundle")
        _WS_TLS["certifi"] = True
        return await websockets.connect(url, ssl=certifi_context(), **kw)


def okx_contract_value(symbol):
    """Base units per contract for an OKX derivative, 1.0 for spot."""
    if not symbol.upper().endswith(("-SWAP", "-FUTURES")) and symbol.count("-") < 2:
        return 1.0
    try:
        url = f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={symbol}"
        data = get_json(url, timeout=5).get("data") or []
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


class BookIntegrityError(ConnectionError):
    """The venue's own check (sequence or checksum) says our copy of the book is wrong: resubscribe to the same venue."""


class OKXVenue(Venue):
    """
    OKX `books`: a 400-level snapshot, then deltas. Each delta names the sequence it follows,
    so a missed update is caught and the book is resubscribed rather than drifting silently.
    Messages also carry a CRC32 of the top 25 levels, which is verified when OKX sends one;
    OKX currently sends 0 there, meaning no checksum.

    `books5` (a fresh top-5 snapshot every 100 ms) is the fallback: after CHECKSUM_STRIKES
    bad deltas in a row (checksum or sequence) the venue switches to it, so a checksum bug can cost depth but
    never the feed.
    """
    name = "OKX"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    CHECKSUM_STRIKES = 3
    _strikes = {}  # per symbol, across reconnects

    def __init__(self, symbol, ct_val=None):
        super().__init__(symbol)
        self.ct_val = ct_val if ct_val is not None else okx_contract_value(symbol)
        self.channel = "books5" if OKXVenue._strikes.get(symbol, 0) >= self.CHECKSUM_STRIKES else "books"
        self._bids, self._asks, self._seq = {}, {}, None
        self._normalise = None  # which number format OKX's checksum uses, once one has matched

    def subscribe_message(self):
        return {"op": "subscribe", "args": [{"channel": self.channel, "instId": self.venue_symbol}]}

    def _strike(self):
        OKXVenue._strikes[self.symbol] = OKXVenue._strikes.get(self.symbol, 0) + 1
        if OKXVenue._strikes[self.symbol] == self.CHECKSUM_STRIKES:
            logger.warning(f"OKX: {self.CHECKSUM_STRIKES} bad deltas in a row; falling back to books5 (5 levels)")

    @staticmethod
    def _apply(side, rows):
        for row in rows:
            price, size = row[0], row[1]
            if float(size) == 0:
                side.pop(price, None)
            else:
                side[price] = size

    def _sorted(self):
        bids = sorted(self._bids.items(), key=lambda kv: -float(kv[0]))
        asks = sorted(self._asks.items(), key=lambda kv: float(kv[0]))
        return bids, asks

    @staticmethod
    def _plain(value):
        """A number string without trailing zeros or exponent: '12.50' -> '12.5', '3.0' -> '3'."""
        text = format(Decimal(value).normalize(), "f")
        return text

    @staticmethod
    def checksum(bids, asks, normalise=False):
        """
        OKX's checksum: CRC32 of the top 25 levels as bidPx:bidSz:askPx:askSz:..., signed.
        With `normalise`, the numbers are written without trailing zeros, which is how some
        OKX books render them for the checksum even when the wire strings carry them.
        """
        fmt = OKXVenue._plain if normalise else str
        parts = []
        for i in range(25):
            if i < len(bids):
                parts += [fmt(bids[i][0]), fmt(bids[i][1])]
            if i < len(asks):
                parts += [fmt(asks[i][0]), fmt(asks[i][1])]
        crc = zlib.crc32(":".join(parts).encode())
        return crc - (1 << 32) if crc >= (1 << 31) else crc

    def _checksum_ok(self, bids, asks, expected):
        """True if our book matches OKX's checksum in either number format; the one that matches is kept."""
        forms = [self._normalise] if self._normalise is not None else [False, True]
        for normalise in forms:
            if self.checksum(bids, asks, normalise) == expected:
                if self._normalise is None:
                    self._normalise = normalise
                    logger.info(f"OKX: checksum verified ({'normalised' if normalise else 'raw'} numbers)")
                return True
        return False

    def parse(self, msg):
        channel = msg.get("arg", {}).get("channel")
        if channel != self.channel or not msg.get("data"):
            return None
        data = msg["data"][0]
        if channel == "books5":
            bids, asks = data.get("bids", []), data.get("asks", [])
        else:
            if msg.get("action") == "snapshot":
                self._bids, self._asks = {}, {}
            elif self._seq is None or data.get("prevSeqId") != self._seq:
                self._strike()
                raise BookIntegrityError(f"update out of sequence ({data.get('prevSeqId')} after {self._seq})")
            self._apply(self._bids, data.get("bids", []))
            self._apply(self._asks, data.get("asks", []))
            self._seq = data.get("seqId")
            bids, asks = self._sorted()
            # OKX now sends checksum 0: the field is kept but no longer computed, so 0 means
            # "none", and the sequence check above is what catches a missed update.
            expected = int(data.get("checksum") or 0)
            if expected and not self._checksum_ok(bids, asks, expected):
                self._strike()
                top = f"bid {bids[0][:2] if bids else None} ask {asks[0][:2] if asks else None}"
                raise BookIntegrityError(f"checksum mismatch on {msg.get('action')} seq {data.get('seqId')}: "
                                         f"okx {data['checksum']}, ours {self.checksum(bids, asks)} raw / "
                                         f"{self.checksum(bids, asks, True)} normalised; {top}; {len(bids)}x{len(asks)} levels")
            if msg.get("action") == "update":
                # Only a verified delta clears the strikes: every resubscribe starts from a
                # snapshot that checks out, so resetting on snapshots would retry a broken
                # update path forever instead of falling back.
                OKXVenue._strikes[self.symbol] = 0
            bids, asks = bids[:BOOK_DEPTH], asks[:BOOK_DEPTH]
        return {
            "bids": _levels([[p, q] for p, q, *_ in bids], self.ct_val),
            "asks": _levels([[p, q] for p, q, *_ in asks], self.ct_val),
            "timestamp": int(data.get("ts", time.time() * 1000)),
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
    async with await open_ws(url) as ws:
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
            # A failed integrity check is our copy of the book, not the venue: resubscribe to the
            # same venue (after enough strikes OKX drops to books5) rather than moving on.
            worked = isinstance(e, BookIntegrityError) or (
                writer.status.get("source") == name and writer.status.get("state") == "live")
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
