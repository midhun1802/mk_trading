"""
CHAKRA Stock Scanner
=====================
Scans individual stocks for high-conviction setups using the same
technical indicators as ARKA, then posts George-style alerts to Discord.

Runs at 10:00am ET daily (after open volatility settles).
Tickers: AAPL, NVDA, TSLA, MSFT, AMZN, META, GOOGL, AMD, JPM, COIN

Run manually:
    cd ~/trading-ai
    python3 backend/chakra/chakra_stock_scanner.py

Run in background:
    nohup python3 backend/chakra/chakra_stock_scanner.py > logs/chakra/chakra.log 2>&1 &
"""

import asyncio
import httpx
import pandas as pd
import numpy as np
import os
import json
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = Path(__file__).parent.parent.parent
LOG_DIR  = BASE_DIR / "logs/chakra"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_DIR / f"chakra_{date.today()}.log")),
    ]
)
log = logging.getLogger("CHAKRA.Stocks")

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY  = os.getenv("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"

# Stocks to scan — expandable
WATCHLIST = {
    "AAPL":  {"sector": "Technology",    "name": "Apple"},
    "NVDA":  {"sector": "Technology",    "name": "NVIDIA"},
    "TSLA":  {"sector": "Consumer",      "name": "Tesla"},
    "MSFT":  {"sector": "Technology",    "name": "Microsoft"},
    "AMZN":  {"sector": "Consumer",      "name": "Amazon"},
    "META":  {"sector": "Technology",    "name": "Meta"},
    "GOOGL": {"sector": "Technology",    "name": "Alphabet"},
    "AMD":   {"sector": "Technology",    "name": "AMD"},
    "JPM":   {"sector": "Finance",       "name": "JPMorgan"},
    "COIN":  {"sector": "Crypto/Finance","name": "Coinbase"},
    "SPY":   {"sector": "ETF",           "name": "S&P 500"},
    "QQQ":   {"sector": "ETF",           "name": "Nasdaq 100"},
    "IWM":   {"sector": "ETF",           "name": "Russell 2000"},
}

# Signal thresholds
CONVICTION_THRESHOLD = 58    # slightly higher than ARKA since stocks are more volatile
SCAN_INTERVAL        = 300   # scan every 5 minutes
MAX_ALERTS_PER_DAY   = 5     # don't spam Discord

# ── Helpers (same as ARKA) ────────────────────────────────────────────────────

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi_calc(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(alpha=1/n, adjust=False).mean()
    al    = loss.ewm(alpha=1/n, adjust=False).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr_calc(h, l, c, n=14) -> float:
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/n, adjust=False).mean().iloc[-1])


# ── Data fetcher ──────────────────────────────────────────────────────────────

async def fetch_daily_bars(ticker: str, days: int = 60) -> pd.DataFrame | None:
    """Fetch daily bars for swing analysis."""
    end   = date.today()
    start = end - timedelta(days=days + 10)
    url   = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 100, "apiKey": POLYGON_KEY}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r    = await client.get(url, params=params)
            data = r.json()
        results = data.get("results", [])
        if not results:
            log.warning(f"  {ticker}: no daily bars returned")
            return None
        df = pd.DataFrame(results).rename(columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low", "c": "close", "v": "volume", "vw": "vwap"
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df if len(df) >= 20 else None
    except Exception as e:
        log.error(f"  {ticker} fetch error: {e}")
        return None


async def fetch_intraday_bars(ticker: str, minutes: int = 60) -> pd.DataFrame | None:
    """Fetch recent 5-minute bars for intraday context."""
    end   = datetime.now(ET)
    start = end - timedelta(minutes=minutes + 30)
    url   = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/5/minute/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
    params = {"adjusted": "true", "sort": "asc", "limit": 100, "apiKey": POLYGON_KEY}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r    = await client.get(url, params=params)
            data = r.json()
        results = data.get("results", [])
        if not results:
            return None
        df = pd.DataFrame(results).rename(columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low", "c": "close", "v": "volume"
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df if len(df) >= 5 else None
    except Exception as e:
        return None


# ── Signal generator ──────────────────────────────────────────────────────────

def analyze_stock(ticker: str, daily: pd.DataFrame, intraday: pd.DataFrame | None) -> dict | None:
    """
    Generate a signal for a stock based on daily + intraday data.
    Returns signal dict or None if no setup found.
    """
    c = daily["close"]
    h = daily["high"]
    l = daily["low"]
    v = daily["volume"]

    # Indicators
    rsi14   = rsi_calc(c, 14)
    e9      = ema(c, 9)
    e20     = ema(c, 20)
    e50     = ema(c, 50)
    macd_l  = ema(c, 12) - ema(c, 26)
    macd_s  = ema(macd_l, 9)
    macd_h  = macd_l - macd_s
    atr14   = atr_calc(h, l, c, 14)
    vol_ma  = v.rolling(20).mean()

    # Bollinger
    bb_mid  = c.rolling(20).mean()
    bb_std  = c.rolling(20).std()
    bb_up   = bb_mid + 2 * bb_std
    bb_lo   = bb_mid - 2 * bb_std
    pct_b   = (c - bb_lo) / (bb_up - bb_lo + 1e-9)

    # Last values
    price      = float(c.iloc[-1])
    rsi_val    = float(rsi14.iloc[-1])
    e9_val     = float(e9.iloc[-1])
    e20_val    = float(e20.iloc[-1])
    e50_val    = float(e50.iloc[-1])
    macd_h_val = float(macd_h.iloc[-1])
    macd_h_prv = float(macd_h.iloc[-2]) if len(macd_h) > 1 else 0
    vol_now    = float(v.iloc[-1])
    vol_avg    = float(vol_ma.iloc[-1])
    vol_ratio  = vol_now / (vol_avg + 1e-9)
    pct_b_val  = float(pct_b.iloc[-1])
    atr_val    = float(atr14)

    # Intraday context
    intra_above_vwap = False
    if intraday is not None and len(intraday) > 0:
        last_close = float(intraday["close"].iloc[-1])
        last_vwap  = float(intraday.get("vwap", intraday["close"]).iloc[-1]) if "vwap" in intraday.columns else last_close
        intra_above_vwap = last_close > last_vwap

    # ── Score components ───────────────────────────────────────────────────
    score    = 50  # neutral start
    reasons  = []
    comp     = {}

    # Trend (EMA stack)
    if e9_val > e20_val > e50_val and price > e9_val:
        score += 15; reasons.append("EMA bullish stack"); comp["trend"] = +15
    elif e9_val > e20_val and price > e9_val:
        score += 8;  comp["trend"] = +8
    elif e9_val < e20_val < e50_val and price < e9_val:
        score -= 15; comp["trend"] = -15
    else:
        comp["trend"] = 0

    # RSI
    if 50 < rsi_val < 70:
        score += 10; reasons.append(f"RSI {rsi_val:.0f} bullish zone"); comp["rsi"] = +10
    elif rsi_val >= 70:
        score -= 5;  reasons.append(f"RSI {rsi_val:.0f} overbought");   comp["rsi"] = -5
    elif rsi_val < 40:
        score -= 10; comp["rsi"] = -10
    elif rsi_val < 50:
        score -= 5;  comp["rsi"] = -5
    else:
        comp["rsi"] = 0

    # MACD
    if macd_h_val > 0 and macd_h_prv <= 0:
        score += 12; reasons.append("MACD cross up"); comp["macd"] = +12
    elif macd_h_val > 0:
        score += 6;  comp["macd"] = +6
    elif macd_h_val < 0 and macd_h_prv >= 0:
        score -= 12; comp["macd"] = -12
    elif macd_h_val < 0:
        score -= 6;  comp["macd"] = -6
    else:
        comp["macd"] = 0

    # Volume
    if vol_ratio >= 1.5:
        score += 8;  reasons.append(f"Volume surge {vol_ratio:.1f}x"); comp["volume"] = +8
    elif vol_ratio >= 1.0:
        score += 3;  comp["volume"] = +3
    elif vol_ratio < 0.7:
        score -= 5;  comp["volume"] = -5
    else:
        comp["volume"] = 0

    # Bollinger position
    if 0.4 <= pct_b_val <= 0.8:
        score += 5;  reasons.append("BB mid zone"); comp["bb"] = +5
    elif pct_b_val > 0.9:
        score -= 5;  comp["bb"] = -5
    elif pct_b_val < 0.1:
        score -= 5;  comp["bb"] = -5
    else:
        comp["bb"] = 0

    # Intraday VWAP
    if intra_above_vwap:
        score += 5;  reasons.append("Above intraday VWAP"); comp["vwap"] = +5
    else:
        comp["vwap"] = 0

    score = max(0, min(100, score))

    # ── Determine signal ───────────────────────────────────────────────────
    if score >= CONVICTION_THRESHOLD:
        signal_type = "BUY"
    elif score <= (100 - CONVICTION_THRESHOLD):
        signal_type = "SELL"
    else:
        return None  # no strong signal — skip

    # Entry / stop / target
    entry  = round(price, 2)
    stop   = round(price - atr_val * 1.5, 2)
    target = round(price + atr_val * 2.5, 2)

    return {
        "ticker":       ticker,
        "signal":       signal_type,
        "price":        price,
        "entry":        entry,
        "stop":         stop,
        "target":       target,
        "confidence":   round(score, 1),
        "rsi":          round(rsi_val, 1),
        "volume_ratio": round(vol_ratio, 2),
        "macd_bullish": macd_h_val > 0,
        "reasons":      reasons,
        "components":   comp,
        "atr":          round(atr_val, 4),
        "sector":       WATCHLIST.get(ticker, {}).get("sector", "Unknown"),
        "name":         WATCHLIST.get(ticker, {}).get("name", ticker),
    }


# ── Scanner engine ────────────────────────────────────────────────────────────

class CHAKRAScanner:
    def __init__(self):
        self.alerted_today  = set()   # don't double-alert same ticker
        self.alerts_today   = 0
        self.last_scan_date = None

    def daily_reset(self):
        today = date.today()
        if self.last_scan_date != today:
            log.info(f"\n{'='*50}")
            log.info(f"  CHAKRA Stock Scanner — {today}")
            log.info(f"  Watching {len(WATCHLIST)} tickers")
            log.info(f"{'='*50}")
            self.alerted_today  = set()
            self.alerts_today   = 0
            self.last_scan_date = today

    async def scan_all(self):
        now = datetime.now(ET)
        log.info(f"\n─── CHAKRA Scan {now.strftime('%H:%M:%S')} ─────────────────────")

        signals_found = []

        for ticker in WATCHLIST:
            if ticker in self.alerted_today:
                continue

            try:
                daily    = await fetch_daily_bars(ticker, days=60)
                intraday = await fetch_intraday_bars(ticker, minutes=60)

                if daily is None:
                    continue

                signal = analyze_stock(ticker, daily, intraday)

                if signal:
                    log.info(f"  🟢 SIGNAL  {ticker}  {signal['signal']}  "
                             f"conv={signal['confidence']}  rsi={signal['rsi']}")
                    signals_found.append(signal)
                else:
                    log.info(f"  ⏸  {ticker}: no setup")

                await asyncio.sleep(0.5)   # rate limit

            except Exception as e:
                log.error(f"  ❌ {ticker} error: {e}")

        # Post top signals (sorted by conviction)
        signals_found.sort(key=lambda x: x["confidence"], reverse=True)

        for signal in signals_found:
            if self.alerts_today >= MAX_ALERTS_PER_DAY:
                log.info(f"  Max daily alerts ({MAX_ALERTS_PER_DAY}) reached")
                break

            try:
                from backend.arka.discord_notifier import post_chakra_stock
                await post_chakra_stock(signal)
                self.alerted_today.add(signal["ticker"])
                self.alerts_today += 1

                # Save to log
                log_path = LOG_DIR / f"signals_{date.today()}.json"
                existing = []
                if log_path.exists():
                    with open(log_path) as f:
                        existing = json.load(f)
                existing.append({**signal, "time": now.strftime("%H:%M")})
                with open(log_path, "w") as f:
                    json.dump(existing, f, indent=2)

            except Exception as e:
                log.error(f"  Discord post failed: {e}")

        if not signals_found:
            log.info("  No signals this scan")

    async def run(self):
        log.info("\n" + "="*50)
        log.info("  CHAKRA STOCK SCANNER STARTING")
        log.info(f"  Tickers: {', '.join(WATCHLIST.keys())}")
        log.info(f"  Conviction threshold: {CONVICTION_THRESHOLD}")
        log.info("="*50)

        # Announce startup
        try:
            from backend.arka.discord_notifier import post_system_alert
            await post_system_alert(
                "CHAKRA Stock Scanner Started",
                f"📊 Scanning {len(WATCHLIST)} stocks every 5 minutes\n"
                f"Tickers: {', '.join(WATCHLIST.keys())}\n"
                f"Conviction threshold: {CONVICTION_THRESHOLD}",
                level="info"
            )
        except:
            pass

        while True:
            try:
                now = datetime.now(ET)
                self.daily_reset()

                # Only scan during market hours (9:30am - 3:45pm ET)
                if now.weekday() >= 5:
                    log.info("Weekend — sleeping")
                    await asyncio.sleep(3600)
                    continue

                t = (now.hour, now.minute)
                if not ((9, 30) <= t < (15, 45)):
                    next_str = "09:30 ET"
                    log.info(f"Market closed — next scan at {next_str}")
                    await asyncio.sleep(300)
                    continue

                await self.scan_all()

            except Exception as e:
                log.error(f"  Scan error: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    asyncio.run(CHAKRAScanner().run())
