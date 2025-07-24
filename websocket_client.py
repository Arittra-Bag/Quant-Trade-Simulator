import asyncio
import websockets
import json
import os
import argparse
import time
import random
from loguru import logger

# Check websockets version
import pkg_resources
try:
    ws_version = pkg_resources.get_distribution("websockets").version
    WS_VERSION = tuple(map(int, ws_version.split('.')))
except:
    # Default to 10.0.0 if can't determine version
    WS_VERSION = (10, 0, 0)

logger.info(f"Using websockets version {'.'.join(map(str, WS_VERSION))}")

# Configure logging
logger.add("websocket.log", rotation="10 MB")

# Multiple WebSocket URLs with fallback support
WEBSOCKET_URLS = [
    "wss://ws.gomarket-cpp.goquant.io/ws/l2-orderbook/okx/{}", #GoQuant API
    "wss://ws.okx.com:8443/ws/v5/public",  # Official OKX API
    "wss://fstream.binance.com/ws/{}@depth20@100ms",  # Binance futures
    "wss://api.hyperliquid.xyz/ws",  # Hyperliquid (requires different handling)
    "wss://api.websocket.in/crypto/orderbook/{}",
    "wss://crypto-ws.coinapi.io/v1/", #Coin API
]

# Global flag for shutdown
shutdown_flag = False

def normalize_symbol_for_url(symbol, url_template):
    """
    Normalize symbol format for different URL patterns
    """
    if "binance" in url_template.lower():
        # Convert BTC-USDT-SWAP to btcusdt
        if "-SWAP" in symbol:
            symbol = symbol.replace("-SWAP", "")
        return symbol.replace("-", "").lower()
    elif "hyperliquid" in url_template.lower():
        # Hyperliquid uses just the base symbol (BTC-USDT-SWAP -> BTC)
        return symbol.split("-")[0] if symbol else "BTC"
    elif "okx" in url_template.lower():
        # Keep OKX format: BTC-USDT-SWAP
        return symbol
    else:
        # Default format for most aggregators
        return symbol

def transform_binance_data(data):
    """
    Transform Binance WebSocket data to expected format
    """
    if "bids" in data and "asks" in data:
        return {
            "bids": [[float(bid[0]), float(bid[1])] for bid in data["bids"]],
            "asks": [[float(ask[0]), float(ask[1])] for ask in data["asks"]],
            "timestamp": data.get("T", data.get("E", int(time.time() * 1000)))
        }
    return data

async def connect_and_save(symbol, output_file="latest_orderbook.json", update_interval=1.0):
    """
    Connect to orderbook WebSocket with fallback support and save latest data to a file.
    
    Args:
        symbol: Trading pair symbol (e.g., BTC-USDT-SWAP)
        output_file: Path to save the latest orderbook data
        update_interval: Minimum time between file writes (to avoid excessive writes)
    """
    last_write_time = 0
    reconnect_delay = 1  # Start with 1 second delay
    max_reconnect_delay = 30  # Maximum delay between reconnection attempts
    current_url_index = 0  # Track which URL we're currently using
    
    while not shutdown_flag:
        # Try each URL in sequence
        url_template = WEBSOCKET_URLS[current_url_index]
        normalized_symbol = normalize_symbol_for_url(symbol, url_template)
        url = url_template.format(normalized_symbol)
        
        try:
            logger.info(f"Connecting to {url} (URL {current_url_index + 1}/{len(WEBSOCKET_URLS)})")
            # Configure WebSocket with appropriate parameters based on version
            ws_kwargs = {}
            
            # Newer versions support ping_interval and ping_timeout
            if WS_VERSION >= (10, 0, 0):
                ws_kwargs.update({
                    "ping_interval": 20,
                    "ping_timeout": 10
                })
            
            # Connect with appropriate parameters
            async with websockets.connect(url, **ws_kwargs) as ws:
                logger.info(f"Connected to {url}")
                # Reset reconnect delay and URL index on successful connection
                reconnect_delay = 1
                current_url_index = 0  # Reset to primary URL on success
                
                # Send subscription message if needed (for direct exchange APIs)
                if "okx.com" in url:
                    subscription_msg = {
                        "op": "subscribe",
                        "args": [{"channel": "books", "instId": symbol}]
                    }
                    await ws.send(json.dumps(subscription_msg))
                    logger.info(f"Sent subscription message for {symbol}")
                elif "binance" in url:
                    subscription_msg = {
                        "method": "SUBSCRIBE",
                        "params": [f"{normalized_symbol}@depth"],
                        "id": 1
                    }
                    await ws.send(json.dumps(subscription_msg))
                    logger.info(f"Sent Binance subscription message for {normalized_symbol}")
                elif "hyperliquid" in url:
                    # Convert symbol format for Hyperliquid (BTC-USDT-SWAP -> BTC)
                    hl_symbol = symbol.split("-")[0] if symbol else "BTC"
                    subscription_msg = {
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": hl_symbol}
                    }
                    await ws.send(json.dumps(subscription_msg))
                    logger.info(f"Sent Hyperliquid subscription message for {hl_symbol}")
                
                # Handler for WebSocket disconnect
                async def check_connection():
                    while not shutdown_flag:
                        try:
                            # Different versions have different ways to check connection state
                            if WS_VERSION >= (10, 0, 0):
                                # Try to ping to check if connection is still alive
                                pong_waiter = await ws.ping()
                                await asyncio.wait_for(pong_waiter, timeout=5)
                            else:
                                # For older versions, can only catch exceptions
                                dummy = ws.transport.get_extra_info('socket')
                                if dummy is None:
                                    raise ConnectionError("Transport socket is None")
                        except Exception as e:
                            logger.warning(f"Connection check failed: {str(e)}")
                            return
                        await asyncio.sleep(5)
                
                # Start connection monitoring task
                monitor_task = asyncio.create_task(check_connection())
                
                # Set connection state monitoring flag
                connection_active = True
                
                while connection_active and not shutdown_flag:
                    try:
                        # Set a timeout for receiving messages
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        
                        # Handle different response formats
                        if "binance" in url and "result" in data:
                            data = transform_binance_data(data["result"])
                        elif "okx.com" in url and "data" in data:
                            # Handle OKX official API response
                            if data.get("arg", {}).get("channel") == "books":
                                book_data = data["data"][0] if data["data"] else {}
                                data = {
                                    "bids": book_data.get("bids", []),
                                    "asks": book_data.get("asks", []),
                                    "timestamp": book_data.get("ts", int(time.time() * 1000))
                                }
                        elif "hyperliquid" in url and "channel" in data:
                            # Handle Hyperliquid response format
                            if data.get("channel") == "l2Book" and "data" in data:
                                book_data = data["data"]
                                levels = book_data.get("levels", [[], []])
                                data = {
                                    "bids": [[level["px"], level["sz"]] for level in levels[0]],
                                    "asks": [[level["px"], level["sz"]] for level in levels[1]],
                                    "timestamp": book_data.get("time", int(time.time() * 1000))
                                }
                        
                        # Basic validation
                        if all(k in data for k in ("bids", "asks", "timestamp")):
                            logger.info(f"Valid orderbook data found with {len(data['bids'])} bids, {len(data['asks'])} asks")
                            
                            # Add a local timestamp
                            data["local_time"] = time.time()
                            
                            # Only write to file if enough time has passed since last write
                            current_time = time.time()
                            if current_time - last_write_time >= update_interval:
                                # Write to temp file then rename to avoid partial reads
                                temp_file = f"{output_file}.tmp"
                                try:
                                    # First try to write to the temp file
                                    with open(temp_file, 'w') as f:
                                        json.dump(data, f)
                                    
                                    # Then try to replace the existing file
                                    try:
                                        os.replace(temp_file, output_file)
                                        last_write_time = current_time
                                        logger.debug(f"Updated {output_file}")
                                    except PermissionError:
                                        # If we can't replace due to permission error, try a direct write
                                        logger.warning(f"Permission error replacing file, trying direct write")
                                        with open(output_file, 'w') as f:
                                            json.dump(data, f)
                                        last_write_time = current_time
                                        logger.debug(f"Directly updated {output_file}")
                                    except OSError as e:
                                        logger.error(f"OS error replacing file: {str(e)}")
                                        # Try a direct write as a fallback
                                        try:
                                            with open(output_file, 'w') as f:
                                                json.dump(data, f)
                                            last_write_time = current_time
                                            logger.debug(f"Fallback: Directly updated {output_file}")
                                        except Exception as inner_e:
                                            logger.error(f"Failed to update orderbook file: {str(inner_e)}")
                                except Exception as e:
                                    logger.error(f"Error writing orderbook data: {str(e)}")
                        else:
                            logger.warning(f"Missing expected fields in data: {data.keys()}")
                    except asyncio.TimeoutError:
                        logger.warning("Message receive timeout, checking connection...")
                        connection_active = False
                    except asyncio.CancelledError:
                        logger.info("Task cancelled, closing connection")
                        connection_active = False
                    except Exception as e:
                        logger.error(f"Error processing message: {str(e)}")
                        # Don't break immediately on error, let the check_connection handle it
                
                # Cancel monitor task and ensure connection is properly closed
                if not monitor_task.done():
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
                
                # Ensure connection is properly closed based on version
                try:
                    if hasattr(ws, 'close') and callable(getattr(ws, 'close')):
                        await ws.close()
                    logger.info("WebSocket connection closed properly")
                except Exception as e:
                    logger.warning(f"Error while closing connection: {str(e)}")
                
        except asyncio.CancelledError:
            logger.info("Client cancelled")
            return
        except Exception as e:
            if shutdown_flag:
                logger.info("Shutdown in progress, stopping reconnection attempts")
                return
                
            logger.error(f"WebSocket connection error: {str(e)}. Trying next URL...")
            
            # Try next URL in the list
            current_url_index = (current_url_index + 1) % len(WEBSOCKET_URLS)
            
            # If we've tried all URLs, wait before starting over
            if current_url_index == 0:
                logger.info(f"Tried all URLs, waiting {reconnect_delay}s before retrying...")
                # Wait with exponential backoff and jitter
                jitter = random.uniform(0, 0.5) * reconnect_delay
                await asyncio.sleep(reconnect_delay + jitter)
                
                # Increase delay for next reconnection attempt
                reconnect_delay = min(max_reconnect_delay, reconnect_delay * 1.5)

async def main():
    parser = argparse.ArgumentParser(description='Multi-Exchange Orderbook WebSocket Client')
    parser.add_argument('--symbol', type=str, default="BTC-USDT-SWAP", help='Trading pair symbol')
    parser.add_argument('--output', type=str, default="latest_orderbook.json", help='Output file path')
    parser.add_argument('--interval', type=float, default=0.5, help='Update interval (seconds)')
    
    args = parser.parse_args()
    
    print(f"Starting WebSocket client for {args.symbol}")
    print(f"Data will be saved to {args.output} every {args.interval} seconds")
    print(f"Will try {len(WEBSOCKET_URLS)} different endpoints with automatic fallback")
    print("Press Ctrl+C to stop")
    
    try:
        await connect_and_save(args.symbol, args.output, args.interval)
    except KeyboardInterrupt:
        global shutdown_flag
        shutdown_flag = True
        print("Stopping gracefully...")
        # Allow time for cleanup
        await asyncio.sleep(1)
        print("Stopped by user")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user") 