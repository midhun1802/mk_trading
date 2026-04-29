"""
CHAKRA — WebSocket Live Price Broadcaster
backend/chakra/price_broadcaster.py

Maintains a shared in-memory price cache fed by Polygon WebSocket stream.
dashboard_api.py imports _price_cache and broadcasts to connected clients
via /ws/prices endpoint.

Architecture:
  Polygon WS → polygon_stream.py (existing, feeds ARKA)
             → price_broadcaster.py (new, feeds dashboard /ws/prices)
  dashboard /ws/prices → browser (replaces 10s setInterval polling)
"""

import asyncio
import json
import logging
import os
import time
from typing import Set

import websockets

log = logging.getLogger('price_broadcaster')

POLYGON_API_KEY = os.getenv('POLYGON_API_KEY', '')

# Tickers to stream — matches dashboard price display
STREAM_TICKERS = [
    'SPY', 'QQQ', 'IWM', 'DIA', 'XLF', 'XLK',
    'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLB',
    'GLD', 'TLT', 'VIX'
]

# ── Shared price cache ─────────────────────────────────────────────────
# dashboard_api imports this dict directly and reads latest prices
_price_cache: dict = {t: {'price': 0, 'change_pct': 0, 'ts': 0} for t in STREAM_TICKERS}
_cache_updated_at: float = 0.0

# ── Connected dashboard WebSocket clients ──────────────────────────────
_ws_clients: Set = set()


def get_price_cache() -> dict:
    """Return current snapshot of all cached prices."""
    return dict(_price_cache)


def register_client(ws) -> None:
    _ws_clients.add(ws)
    log.debug(f"WS client connected — total: {len(_ws_clients)}")


def unregister_client(ws) -> None:
    _ws_clients.discard(ws)
    log.debug(f"WS client disconnected — total: {len(_ws_clients)}")


async def broadcast_to_clients(payload: dict) -> None:
    """Send price update to all connected dashboard clients."""
    if not _ws_clients:
        return
    msg = json.dumps(payload)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _ws_clients.discard(ws)


def update_price(ticker: str, price: float, change_pct: float = 0.0) -> None:
    """Update a single ticker's cached price."""
    global _cache_updated_at
    _price_cache[ticker] = {
        'price':      round(price, 4),
        'change_pct': round(change_pct, 4),
        'ts':         time.time(),
    }
    _cache_updated_at = time.time()


# ── Polygon WebSocket stream ───────────────────────────────────────────

POLYGON_WS_URL = 'wss://socket.polygon.io/stocks'


async def _run_polygon_stream():
    """
    Connect to Polygon WebSocket and stream real-time quotes.
    Updates _price_cache on every A (aggregate per second) event.
    Reconnects automatically on disconnect.
    """
    if not POLYGON_API_KEY:
        log.error("POLYGON_API_KEY not set — price broadcaster cannot start")
        return

    subscribe_msg = json.dumps({
        'action': 'subscribe',
        'params': ','.join(f'A.{t}' for t in STREAM_TICKERS)
    })

    while True:
        try:
            log.info(f"Connecting to Polygon WebSocket for {len(STREAM_TICKERS)} tickers...")
            async with websockets.connect(POLYGON_WS_URL, ping_interval=20) as ws:

                # Authenticate
                auth_resp = await ws.recv()
                await ws.send(json.dumps({'action': 'auth', 'params': POLYGON_API_KEY}))
                auth_result = await ws.recv()
                log.info(f"Polygon auth: {auth_result[:80]}")

                # Subscribe to per-second aggregates
                await ws.send(subscribe_msg)
                log.info(f"Subscribed to: {', '.join(STREAM_TICKERS)}")

                async for raw_msg in ws:
                    try:
                        events = json.loads(raw_msg)
                        for event in events:
                            ev_type = event.get('ev')

                            # A = aggregate (per second), AM = aggregate (per minute)
                            if ev_type in ('A', 'AM'):
                                ticker     = event.get('sym', '')
                                close      = event.get('c', 0) or event.get('vw', 0)
                                open_price = event.get('op', close)  # day open
                                change_pct = ((close - open_price) / open_price * 100) if open_price else 0

                                if ticker and close:
                                    update_price(ticker, close, change_pct)

                                    # Broadcast to dashboard clients immediately
                                    await broadcast_to_clients({
                                        'type':       'price_update',
                                        'ticker':     ticker,
                                        'price':      round(close, 4),
                                        'change_pct': round(change_pct, 4),
                                        'ts':         time.time(),
                                    })

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.debug(f"Event processing error: {e}")

        except Exception as e:
            log.warning(f"Polygon WS disconnected: {e} — reconnecting in 5s...")
            await asyncio.sleep(5)


async def start_broadcaster():
    """Start the price broadcaster as a background task."""
    asyncio.create_task(_run_polygon_stream())
    log.info("Price broadcaster started as background task")


# ── Fallback REST poller ───────────────────────────────────────────────
# Used when Polygon WS is unavailable — polls /api/prices/live every 5s

async def start_rest_poller(dashboard_base_url: str = 'http://localhost:8000'):
    """
    Fallback: poll /api/prices/live every 5s and update cache.
    Activate this if WebSocket connection fails repeatedly.
    """
    import httpx
    log.info("Starting REST price poller fallback (5s interval)")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(f'{dashboard_base_url}/api/prices/live', timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    prices = data.get('prices', data)
                    for ticker, info in prices.items():
                        if isinstance(info, dict):
                            update_price(
                                ticker,
                                info.get('price', 0),
                                info.get('change_pct', 0)
                            )
                    # Broadcast full snapshot to all clients
                    await broadcast_to_clients({
                        'type':   'price_snapshot',
                        'prices': get_price_cache(),
                        'ts':     time.time(),
                    })
            except Exception as e:
                log.debug(f"REST poller error: {e}")
            await asyncio.sleep(5)
