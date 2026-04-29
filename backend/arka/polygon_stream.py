import asyncio
import websockets
import json
import os
from dotenv import load_dotenv

load_dotenv(override=True)
API_KEY = os.getenv("POLYGON_API_KEY")

async def stream_real_time_bars(tickers, on_bar_callback):
    """Subscribe to 1-second aggregate bars. Calls on_bar_callback on each bar."""
    uri = "wss://socket.polygon.io/stocks"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"action": "auth", "params": API_KEY}))
        subscriptions = [f"A.{t}" for t in tickers]
        await ws.send(json.dumps({"action": "subscribe", "params": ",".join(subscriptions)}))
        print(f"✅ WebSocket connected - streaming {tickers}")
        async for message in ws:
            for msg in json.loads(message):
                if msg.get("ev") == "A":
                    await on_bar_callback(msg)
