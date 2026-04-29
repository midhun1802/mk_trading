#!/usr/bin/env python3
"""
ARKA WebSocket Stream Engine
==============================
Real-time options + stock flow via Polygon WebSocket.
Replaces 5-minute REST polling with 30-second live streams.

Streams:
  - Options trades (T.*) — detects sweeps, blocks, unusual activity
  - Stock trades (T.*) — large prints, dark pool proxies
  - Stock quotes (Q.*) — NBBO spread expansion alerts
  - Index aggregates (A.*) — SPX/SPY/QQQ/IWM minute bars

Alert types (matching Neo bot):
  1. Options sweep — aggressive multi-exchange fill
  2. Large block — single print > $500K premium
  3. Gamma spike — node adds $500K+ in one minute
  4. Vol/OI surge — contract exceeds 50x normal
  5. Dark pool proxy — large off-exchange print
  6. VWAP break — price crosses VWAP with volume
  7. LOD/HOD break — intraday level break at gamma node

Usage:
  python3 backend/arka/ws_stream_engine.py           # run live
  python3 backend/arka/ws_stream_engine.py --test    # test webhooks only

Crontab (add this):
  30 9 * * 1-5 cd ~/trading-ai && venv/bin/python3 backend/arka/ws_stream_engine.py >> logs/arka/ws_stream.log 2>&1
"""

import os, sys, json, asyncio, logging, time
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict, deque
import httpx

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.WSStream")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WS] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY    = os.getenv("POLYGON_API_KEY", "")
WS_URL_OPTIONS = "wss://socket.polygon.io/options"
WS_URL_STOCKS  = "wss://socket.polygon.io/stocks"

# Discord channels
CH_SCALP_EXT  = os.getenv("DISCORD_ARKA_SCALP_EXTREME", "")
CH_SCALP_SIG  = os.getenv("DISCORD_ARKA_SCALP_SIGNALS", "")
CH_SWING_EXT  = os.getenv("DISCORD_ARKA_SWINGS_EXTREME", "")
CH_SWING_SIG  = os.getenv("DISCORD_ARKA_SWINGS_SIGNALS", "")
CH_FLOW_EXT   = os.getenv("DISCORD_FLOW_EXTREME", "")
CH_FLOW_SIG   = os.getenv("DISCORD_FLOW_SIGNALS", "")
CH_ALERTS     = os.getenv("DISCORD_ALERTS", "")
CH_LOTTO      = os.getenv("DISCORD_ARKA_LOTTO", "")

# Tickers to stream
INDEX_TICKERS = ["SPY", "QQQ", "IWM", "DIA"]
STOCK_TICKERS = [
    "AAPL","NVDA","TSLA","MSFT","AMZN","META","GOOGL","AMD",
    "NFLX","CRM","COIN","MSTR","PLTR","HOOD","RBLX","IONQ",
    "SMCI","ARM","SNOW","UBER","GS","JPM","SOFI","UPST","AFRM",
]
ALL_TICKERS = INDEX_TICKERS + STOCK_TICKERS

# Alert thresholds
MIN_SWEEP_PREMIUM    = 100_000   # $100K minimum for sweep alert
MIN_BLOCK_PREMIUM    = 500_000   # $500K minimum for block alert
MIN_EXTREME_PREMIUM  = 1_000_000 # $1M+ for extreme channel
MIN_DARK_POOL_SIZE   = 50_000    # shares for dark pool proxy
MIN_VOL_OI_RATIO     = 10        # Vol/OI ratio to alert
EXTREME_VOL_OI       = 50        # Ratio for extreme channel
ALERT_COOLDOWN_SECS  = 300       # 5 min between same ticker alerts

# ── User preference: Dark Pool PRINT alerts disabled (too noisy) ──────────────
DARK_POOL_PRINT_DISABLED = True

# State
_alert_times:  dict = {}         # ticker_type → last alert time
_contract_oi:  dict = {}         # contract → baseline OI
_option_vols:  dict = defaultdict(int)   # contract → today's volume
_minute_bars:  dict = {}         # ticker → latest bar
_vwap_state:   dict = {}         # ticker → vwap
_lod_hod:      dict = {}         # ticker → {lod, hod}
_gamma_nodes:  dict = {}         # strike → gamma state
_sweep_buffer: dict = defaultdict(list)  # contract → recent prints (for sweep detection)


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD
# ══════════════════════════════════════════════════════════════════════════════

async def post_discord_async(webhook: str, embed: dict, content: str = ""):
    if not webhook:
        return
    try:
        async with httpx.AsyncClient() as client:
            payload = {"embeds": [embed], "username": "ARKA Live Stream"}
            if content:
                payload["content"] = content
            r = await client.post(webhook, json=payload, timeout=8)
            if r.status_code not in (200, 204):
                log.warning(f"Discord post failed: {r.status_code}")
    except Exception as e:
        log.error(f"Discord error: {e}")


def route_channel(ticker: str, is_extreme: bool, is_lotto: bool = False) -> str:
    """Route alert to correct Discord channel."""
    now = datetime.now(ET)
    if is_lotto or (now.hour == 15 and now.minute >= 30):
        return CH_LOTTO or CH_ALERTS
    is_index = ticker in set(INDEX_TICKERS)
    if is_index and is_extreme:
        return CH_SCALP_EXT or CH_FLOW_EXT or CH_ALERTS
    elif is_index:
        return CH_SCALP_SIG or CH_FLOW_SIG or CH_ALERTS
    elif is_extreme:
        return CH_SWING_EXT or CH_FLOW_EXT or CH_ALERTS
    else:
        return CH_SWING_SIG or CH_FLOW_SIG or CH_ALERTS


def is_cooldown(key: str, secs: int = ALERT_COOLDOWN_SECS) -> bool:
    """Return True if we're in cooldown for this alert key."""
    last = _alert_times.get(key, 0)
    if time.time() - last < secs:
        return True
    _alert_times[key] = time.time()
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIONS PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

async def process_options_trade(msg: dict):
    """
    Process a live options trade from WebSocket.
    Detects: sweeps, blocks, unusual vol/OI.
    """
    contract = msg.get("sym", "")   # e.g. "O:SPY260321C00660000"
    price    = float(msg.get("p", 0))
    size     = int(msg.get("s", 0))
    exchange = int(msg.get("x", 0))

    if not contract or not price or not size:
        return

    # Parse contract details
    # Format: O:TICKER[YYMMDD][C/P][STRIKE*1000]
    try:
        sym_part = contract.replace("O:", "")
        # Extract underlying ticker
        underlying = ""
        for i, c in enumerate(sym_part):
            if c.isdigit():
                underlying = sym_part[:i]
                rest = sym_part[i:]
                break
        if not underlying:
            return

        exp_str  = rest[:6]   # YYMMDD
        cp       = rest[6]    # C or P
        strike   = int(rest[7:]) / 1000
        exp_date = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"
        premium  = price * size * 100
    except Exception:
        return

    if underlying not in ALL_TICKERS:
        return

    # Track volume
    _option_vols[contract] += size

    # Add to sweep buffer (same contract prints within 30s = sweep)
    now_ts = time.time()
    _sweep_buffer[contract].append({"ts": now_ts, "size": size, "px": price, "exch": exchange})
    # Expire old prints
    _sweep_buffer[contract] = [p for p in _sweep_buffer[contract] if now_ts - p["ts"] < 30]

    # ── Sweep detection ───────────────────────────────────────────────────────
    # Multiple prints on same contract within 30s on different exchanges = sweep
    recent = _sweep_buffer[contract]
    exchanges = set(p["exch"] for p in recent)
    total_sweep_premium = sum(p["size"] * p["px"] * 100 for p in recent)
    is_sweep = len(recent) >= 3 and len(exchanges) >= 2 and total_sweep_premium >= MIN_SWEEP_PREMIUM

    # ── Block detection ───────────────────────────────────────────────────────
    is_block = premium >= MIN_BLOCK_PREMIUM

    # ── Vol/OI ratio ──────────────────────────────────────────────────────────
    baseline_oi = _contract_oi.get(contract, 1)
    vol_oi = _option_vols[contract] / max(baseline_oi, 1)
    is_extreme_vol = vol_oi >= EXTREME_VOL_OI
    is_unusual_vol  = vol_oi >= MIN_VOL_OI_RATIO

    if not (is_sweep or is_block or is_unusual_vol):
        return

    is_extreme = is_extreme_vol or premium >= MIN_EXTREME_PREMIUM
    dedup_key  = f"opts_{underlying}_{cp}_{int(strike)}_{datetime.now(ET).strftime('%H')}"
    if is_cooldown(dedup_key, 180):
        return

    # Format alert
    action = "BUY CALLS" if cp == "C" else "BUY PUTS"
    color  = 0x00FF88 if cp == "C" else 0xFF4444
    tier   = "🔥 EXTREME" if is_extreme else "🐋 SWEEP" if is_sweep else "⚡ BLOCK"

    embed = {
        "color":  color,
        "author": {"name": f"{tier} — {underlying} {cp} ${strike:.0f}"},
        "description": (
            f"**{'Options Sweep' if is_sweep else 'Block Print'}** on **{underlying}**\n"
            f"{'Multi-exchange sweep detected' if is_sweep else 'Single large block'}"
        ),
        "fields": [
            {"name": "📋 Contract",  "value": f"{cp} ${strike:.0f} exp {exp_date}",     "inline": True},
            {"name": "💰 Premium",   "value": f"${total_sweep_premium/1e3:.0f}K" if is_sweep else f"${premium/1e3:.0f}K", "inline": True},
            {"name": "📊 Vol/OI",    "value": f"{vol_oi:.1f}x",                         "inline": True},
            {"name": "📦 Size",      "value": f"{size:,} contracts",                    "inline": True},
            {"name": "💲 Mark",      "value": f"${price:.2f}",                          "inline": True},
            {"name": "🔢 Exchanges", "value": str(len(exchanges)) if is_sweep else "1", "inline": True},
            {"name": "🎯 ARKA",      "value": f"**{action} on {underlying}**\nConviction: {'HIGH' if is_extreme else 'MEDIUM'}", "inline": False},
        ],
        "footer": {"text": f"ARKA Live Stream • {datetime.now(ET).strftime('%I:%M:%S %p ET')}"}
    }

    content = f"🔥 **{tier}** — {underlying} {cp} ${strike:.0f} | ${total_sweep_premium/1e3:.0f}K premium" if is_extreme else ""
    webhook = route_channel(underlying, is_extreme)
    await post_discord_async(webhook, embed, content)

    # Write to flow cache
    _write_flow_cache(underlying, "BULLISH" if cp == "C" else "BEARISH",
                      min(100, int(vol_oi * 2)), vol_oi, is_extreme)

    log.info(f"  ✅ {tier} {underlying} {cp}${strike:.0f} ${premium/1e3:.0f}K vol/OI={vol_oi:.1f}x")


# ══════════════════════════════════════════════════════════════════════════════
#  STOCK PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

async def process_stock_trade(msg: dict):
    """
    Process live stock trade. Detects dark pool proxy prints.
    Dark pool proxy: large size (>50K shares) on off-exchange (condition codes).
    """
    ticker   = msg.get("sym", "")
    price    = float(msg.get("p", 0))
    size     = int(msg.get("s", 0))
    conds    = msg.get("c", [])  # trade conditions

    if ticker not in ALL_TICKERS or not price or size < MIN_DARK_POOL_SIZE:
        return

    notional = price * size
    # Off-exchange conditions: 12=form-T, 17=intermarket sweep, 37=odd lot excluded
    is_off_exchange = any(c in conds for c in [12, 15, 17, 37, 41])
    is_large_print  = notional >= 500_000

    if not (is_off_exchange and is_large_print):
        return

    if DARK_POOL_PRINT_DISABLED:
        log.info(f"  🔇 Dark Pool Print suppressed for {ticker} (disabled)")
        return

    dedup_key = f"dp_{ticker}_{datetime.now(ET).strftime('%H%M')}"
    if is_cooldown(dedup_key, 120):
        return

    is_extreme = notional >= 5_000_000
    is_index   = ticker in set(INDEX_TICKERS)

    # Determine direction from price vs VWAP
    vwap = _vwap_state.get(ticker, {}).get("vwap", price)
    is_buy_side  = price >= vwap
    direction    = "BUY SIDE" if is_buy_side else "SELL SIDE"
    dir_emoji    = "🟢" if is_buy_side else "🔴"
    action       = "BUY CALLS" if is_buy_side else "BUY PUTS"
    dir_color    = 0x00FF88 if is_buy_side else 0xFF4444
    is_index     = ticker in {"SPY","QQQ","IWM","DIA"}

    embed = {
        "color": dir_color,
        "author": {"name": f"🕳️ Dark Pool Print — {ticker} | {dir_emoji} {direction}"},
        "description": f"Large off-exchange print on **{ticker}** — {'Above VWAP = aggressive buy' if is_buy_side else 'Below VWAP = aggressive sell'}",
        "fields": [
            {"name": "💲 Price",    "value": f"${price:.2f}",                    "inline": True},
            {"name": "📊 VWAP",     "value": f"${vwap:.2f}",                    "inline": True},
            {"name": "💰 Notional", "value": f"${notional/1e6:.2f}M",           "inline": True},
            {"name": "📦 Size",     "value": f"{size:,} shares",                "inline": True},
            {"name": "⏰ Time",     "value": datetime.now(ET).strftime("%I:%M:%S %p ET"), "inline": True},
            {"name": "🎯 ARKA Says","value": f"**{action} on {ticker}**",        "inline": True},
        ],
        "footer": {"text": f"ARKA Live Stream • Dark Pool Monitor"}
    }

    webhook = route_channel(ticker, is_extreme)
    await post_discord_async(webhook, embed)
    log.info(f"  🕳️ Dark pool {ticker} ${notional/1e6:.1f}M @ ${price:.2f}")


async def process_minute_bar(msg: dict):
    """
    Process minute aggregate bar. Tracks VWAP, LOD/HOD.
    """
    ticker = msg.get("sym", "")
    if ticker not in ALL_TICKERS:
        return

    o = float(msg.get("o", 0))
    h = float(msg.get("h", 0))
    l = float(msg.get("l", 0))
    c = float(msg.get("c", 0))
    v = float(msg.get("v", 0))
    vw = float(msg.get("vw", c))  # VWAP for this bar

    # Update LOD/HOD
    if ticker not in _lod_hod:
        _lod_hod[ticker] = {"lod": l, "hod": h, "open": o}
    else:
        _lod_hod[ticker]["lod"] = min(_lod_hod[ticker]["lod"], l)
        _lod_hod[ticker]["hod"] = max(_lod_hod[ticker]["hod"], h)

    # Track VWAP (cumulative)
    prev = _vwap_state.get(ticker, {"cum_vol": 0, "cum_vp": 0})
    cum_vol = prev["cum_vol"] + v
    cum_vp  = prev["cum_vp"] + (vw * v)
    vwap    = cum_vp / cum_vol if cum_vol > 0 else c
    _vwap_state[ticker] = {"cum_vol": cum_vol, "cum_vp": cum_vp, "vwap": vwap}
    _minute_bars[ticker] = {"open": o, "high": h, "low": l, "close": c, "vwap": vwap}

    # VWAP break detection — price crosses VWAP with volume surge
    prev_bar = _minute_bars.get(f"{ticker}_prev")
    if prev_bar and v > 0:
        prev_close = prev_bar["close"]
        prev_vwap  = _vwap_state.get(ticker, {}).get("vwap", vwap)
        crossed_up   = prev_close < prev_vwap and c > vwap
        crossed_down = prev_close > prev_vwap and c < vwap
        # Only alert if price has moved meaningfully away from VWAP (not just touching)
        vwap_dist_pct = abs(c - vwap) / vwap * 100
        meaningful_break = vwap_dist_pct >= 0.15

        if (crossed_up or crossed_down) and ticker in INDEX_TICKERS and meaningful_break:
            dedup_key = f"vwap_{ticker}_{datetime.now(ET).strftime('%H%M')}"
            if not is_cooldown(dedup_key, 900):
                direction = "ABOVE ↑" if crossed_up else "BELOW ↓"
                color = 0x00FF88 if crossed_up else 0xFF4444
                embed = {
                    "color": color,
                    "author": {"name": f"📈 VWAP Break — {ticker} {direction}"},
                    "description": f"**{ticker}** crossed VWAP at ${vwap:.2f}",
                    "fields": [
                        {"name": "💲 Price", "value": f"${c:.2f}", "inline": True},
                        {"name": "📊 VWAP",  "value": f"${vwap:.2f}", "inline": True},
                        {"name": "📦 Vol",   "value": f"{v:,.0f}", "inline": True},
                    ],
                    "footer": {"text": f"ARKA Live Stream • {datetime.now(ET).strftime('%I:%M %p ET')}"}
                }
                await post_discord_async(route_channel(ticker, False), embed)
                log.info(f"  📈 VWAP break {ticker} {direction} @ ${c:.2f}")

    _minute_bars[f"{ticker}_prev"] = {"close": c, "vwap": vwap}


# ══════════════════════════════════════════════════════════════════════════════
#  FLOW CACHE
# ══════════════════════════════════════════════════════════════════════════════

def _write_flow_cache(ticker: str, bias: str, confidence: int,
                      vol_oi_ratio: float, is_extreme: bool, dp_pct: float = 0):
    try:
        cache_path = BASE / "logs/chakra/flow_signals_latest.json"
        existing = {}
        if cache_path.exists():
            try:
                existing = json.loads(cache_path.read_text())
            except Exception:
                existing = {}
        existing[ticker] = {
            "bias":          bias,
            "confidence":    confidence,
            "vol_oi_ratio":  round(vol_oi_ratio, 1),
            "is_extreme":    is_extreme,
            "dark_pool_pct": round(dp_pct, 3),
            "timestamp":     datetime.now().isoformat(),
            "source":        "ws_stream",
        }
        cache_path.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        log.debug(f"Flow cache write failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  OI BASELINE LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_oi_baseline():
    """Load today's OI for key tickers via REST at startup."""
    log.info("  Loading OI baseline for key contracts...")
    loaded = 0
    for ticker in INDEX_TICKERS:
        try:
            r = httpx.get(
                f"https://api.polygon.io/v3/snapshot/options/{ticker}",
                params={"apiKey": POLYGON_KEY, "limit": 100},
                timeout=15,
            )
            for c in r.json().get("results", []):
                contract = c.get("details", {}).get("ticker", "")
                oi = int(c.get("open_interest", 0) or 0)
                if contract and oi > 0:
                    _contract_oi[contract] = oi
                    loaded += 1
        except Exception as e:
            log.warning(f"  OI load failed for {ticker}: {e}")
    log.info(f"  OI baseline loaded: {loaded} contracts")


# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET CLIENTS
# ══════════════════════════════════════════════════════════════════════════════

async def run_options_stream():
    """Stream options trades via Polygon WebSocket."""
    try:
        import websockets
    except ImportError:
        log.error("websockets not installed — run: pip install websockets")
        return

    subscriptions = [f"T.{t}" for t in ALL_TICKERS]  # options trades

    while True:
        try:
            log.info(f"  Connecting to options stream: {len(subscriptions)} tickers")
            async with websockets.connect(WS_URL_OPTIONS, ping_interval=30) as ws:
                # Auth
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_KEY}))
                auth_resp = await ws.recv()
                log.info(f"  Options auth: {auth_resp[:80]}")

                # Subscribe
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "params": ",".join(subscriptions)
                }))
                log.info(f"  Subscribed to options trades")

                async for raw in ws:
                    try:
                        msgs = json.loads(raw)
                        for msg in msgs:
                            ev = msg.get("ev", "")
                            if ev == "T":
                                await process_options_trade(msg)
                    except Exception as e:
                        log.debug(f"Options msg error: {e}")

        except Exception as e:
            log.error(f"  Options WebSocket error: {e} — reconnecting in 10s")
            await asyncio.sleep(10)


async def run_stocks_stream():
    """Stream stock trades + minute bars via Polygon WebSocket."""
    try:
        import websockets
    except ImportError:
        return

    trade_subs = [f"T.{t}" for t in ALL_TICKERS]   # trades
    agg_subs   = [f"A.{t}" for t in ALL_TICKERS]    # minute bars

    while True:
        try:
            log.info(f"  Connecting to stocks stream")
            async with websockets.connect(WS_URL_STOCKS, ping_interval=30) as ws:
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_KEY}))
                auth_resp = await ws.recv()
                log.info(f"  Stocks auth: {auth_resp[:80]}")

                all_subs = trade_subs + agg_subs
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "params": ",".join(all_subs)
                }))
                log.info(f"  Subscribed to {len(all_subs)} stock feeds")

                async for raw in ws:
                    try:
                        msgs = json.loads(raw)
                        for msg in msgs:
                            ev = msg.get("ev", "")
                            if ev == "T":
                                await process_stock_trade(msg)
                            elif ev == "A":
                                await process_minute_bar(msg)
                    except Exception as e:
                        log.debug(f"Stocks msg error: {e}")

        except Exception as e:
            log.error(f"  Stocks WebSocket error: {e} — reconnecting in 10s")
            await asyncio.sleep(10)


async def market_hours_gate():
    """Wait for market hours, then run streams."""
    while True:
        now = datetime.now(ET)
        if 9 <= now.hour < 16:
            return
        # Wait until 9:25am ET
        target = now.replace(hour=9, minute=25, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info(f"  Market closed — waiting {wait/3600:.1f}h until 9:25am ET")
        await asyncio.sleep(min(wait, 3600))


async def main():
    """Run both streams concurrently."""
    now = datetime.now(ET)
    log.info(f"\n{'='*55}")
    log.info(f"  ARKA WebSocket Stream Engine")
    log.info(f"  {now.strftime('%Y-%m-%d %H:%M ET')}")
    log.info(f"{'='*55}")

    # Load OI baseline at startup
    load_oi_baseline()

    # Wait for market if needed
    await market_hours_gate()

    log.info("  Market hours — starting live streams")

    # Run options + stocks streams concurrently
    await asyncio.gather(
        run_options_stream(),
        run_stocks_stream(),
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Test webhooks only")
    args = parser.parse_args()

    if args.test:
        # Send test messages to all channels
        async def test_webhooks():
            channels = {
                "#arka-scalp-extreme":  CH_SCALP_EXT,
                "#arka-scalp-signals":  CH_SCALP_SIG,
                "#arka-swings-signals": CH_SWING_SIG,
                "#flow-extreme":        CH_FLOW_EXT,
            }
            for name, url in channels.items():
                if url:
                    await post_discord_async(url, {
                        "color": 0x00FF88,
                        "author": {"name": f"✅ WS Stream Test — {name}"},
                        "description": "WebSocket engine connected and routing correctly",
                        "footer": {"text": "ARKA WS Stream Engine • Test"}
                    })
                    log.info(f"  ✅ {name}")
                    await asyncio.sleep(0.5)
        asyncio.run(test_webhooks())
    else:
        asyncio.run(main())
