"""
CHAKRA — Iceberg Order Detector
backend/chakra/modules/iceberg_detector.py

Institutions hide large orders as icebergs — small visible sizes that
repeatedly refresh at the same price level. Detects accumulation IN REAL TIME
by identifying repeated small prints at the same price with consistent sizing.

Detection signals:
  ≥5 prints within $0.02 of same price within 60 sec
  Each print ≤ 500 shares BUT total accumulated > 10,000 shares
  Print interval ±2 sec apart (algorithmic pattern)
  Prints on ASK = accumulation (BULLISH)
  Prints on BID = distribution (BEARISH)

Since we don't have real-time tape (WebSocket not deployed), this module
uses Polygon's trade endpoint to approximate iceberg detection from
recent trade history in a rolling window.

Integration:
  - UOA Detector  → upgrade with iceberg layer
  - ARKA entry    → iceberg in same direction adds +12 pts conviction
"""

import json
import logging
import httpx
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log           = logging.getLogger("chakra.iceberg")
POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")
ICEBERG_CACHE = BASE / "logs" / "chakra" / "iceberg_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]

# Detection thresholds
MIN_PRINTS          = 5       # minimum prints at same level
MIN_TOTAL_SHARES    = 10_000  # minimum accumulated shares
MAX_PRINT_SIZE      = 500     # max per-print size (iceberg characteristic)
PRICE_BUCKET        = 0.05    # $0.05 price bucket (wider than $0.02 for REST API)
WINDOW_MINUTES      = 10      # look-back window in minutes
MIN_PREMIUM         = 100_000 # $100K minimum total premium to flag


# ══════════════════════════════════════════════════════════════════════
# ICEBERG DETECTION
# ══════════════════════════════════════════════════════════════════════

def detect_iceberg_from_trades(trades: list, ticker: str) -> dict:
    """
    Detect iceberg patterns from a list of recent trades.

    trades: list of {price, size, timestamp, conditions}
    Returns detected iceberg info or {"detected": False}
    """
    if not trades:
        return {"detected": False, "ticker": ticker}

    # Group trades into price buckets
    buckets = defaultdict(list)
    for t in trades:
        price  = float(t.get("price", 0))
        size   = int(t.get("size", 0))
        ts     = t.get("timestamp", 0)

        if not price or not size:
            continue

        # Round to nearest $0.05 bucket
        bucket = round(round(price / PRICE_BUCKET) * PRICE_BUCKET, 2)
        buckets[bucket].append({"price": price, "size": size, "ts": ts})

    # Analyze each bucket for iceberg pattern
    icebergs = []

    for price_level, prints in buckets.items():
        if len(prints) < MIN_PRINTS:
            continue

        # Only count small prints (iceberg characteristic)
        small_prints = [p for p in prints if p["size"] <= MAX_PRINT_SIZE]
        if len(small_prints) < MIN_PRINTS:
            continue

        total_size = sum(p["size"] for p in small_prints)
        if total_size < MIN_TOTAL_SHARES:
            continue

        # Check if prints are spread across timestamps (algorithmic refresh)
        timestamps = sorted([p["ts"] for p in small_prints])
        if len(timestamps) >= 2:
            avg_interval = (timestamps[-1] - timestamps[0]) / len(timestamps)
        else:
            avg_interval = 0

        # Determine direction from price relative to recent VWAP
        # If prints are near/at ask = buying (bullish)
        # Use position relative to price range as proxy
        prices   = [p["price"] for p in prints]
        price_rng = max(prices) - min(prices) if prices else 0

        # Estimate premium
        avg_price = sum(prices) / len(prices) if prices else price_level
        premium   = total_size * avg_price

        if premium < MIN_PREMIUM:
            continue

        icebergs.append({
            "price_level": price_level,
            "total_size":  total_size,
            "print_count": len(small_prints),
            "avg_interval_ms": round(avg_interval / 1e6, 0) if avg_interval > 0 else 0,
            "premium":     round(premium, 0),
            "premium_k":   round(premium / 1000, 1),
            "algorithmic": 2 <= avg_interval / 1e6 <= 5000,  # 2ms-5s interval
        })

    if not icebergs:
        return {"detected": False, "ticker": ticker}

    # Sort by total size (largest accumulation first)
    icebergs.sort(key=lambda x: -x["total_size"])
    top = icebergs[0]

    # Direction: above-midpoint prints = ask-side = bullish
    # (simplified heuristic since we don't have bid/ask per trade)
    all_prices = [p["price"] for t_list in buckets.values() for p in t_list]
    if all_prices:
        midpoint  = (max(all_prices) + min(all_prices)) / 2
        above_mid = sum(1 for p in all_prices if p >= midpoint)
        direction = "BULLISH" if above_mid >= len(all_prices) * 0.55 else "BEARISH"
    else:
        direction = "NEUTRAL"

    return {
        "detected":    True,
        "ticker":      ticker,
        "direction":   direction,
        "price_level": top["price_level"],
        "total_size":  top["total_size"],
        "print_count": top["print_count"],
        "premium":     top["premium"],
        "premium_k":   top["premium_k"],
        "algorithmic": top["algorithmic"],
        "iceberg_count": len(icebergs),
        "label":       f"🧊 {'Bullish' if direction == 'BULLISH' else 'Bearish'} Iceberg @ ${top['price_level']}",
        "color":       "00FF9D" if direction == "BULLISH" else "FF2D55",
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER — Polygon REST trade endpoint
# ══════════════════════════════════════════════════════════════════════

def fetch_recent_trades(ticker: str, minutes_back: int = 10) -> list:
    """
    Fetch recent trades from Polygon's trade endpoint.
    Uses v3/trades which returns individual prints.
    """
    try:
        # Polygon v3 trades — last N minutes
        now        = datetime.now()
        ts_start   = int((now - timedelta(minutes=minutes_back)).timestamp() * 1e9)

        r = httpx.get(
            f"https://api.polygon.io/v3/trades/{ticker}",
            params={
                "apiKey":        POLYGON_KEY,
                "timestamp.gte": ts_start,
                "limit":         1000,
                "sort":          "timestamp",
                "order":         "asc",
            },
            timeout=12
        )
        results = r.json().get("results", [])
        return [
            {
                "price":      r.get("price", 0),
                "size":       r.get("size", 0),
                "timestamp":  r.get("sip_timestamp", 0),
                "conditions": r.get("conditions", []),
            }
            for r in results
        ]

    except Exception as e:
        log.warning(f"Iceberg trade fetch {ticker}: {e}")
        return []


def fetch_trades_from_aggs(ticker: str, spot: float) -> list:
    """
    Fallback: reconstruct approximate trade tape from 1-min OHLCV bars.
    Less precise but works without WebSocket.
    """
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=1)).isoformat()

        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 20},
            timeout=12
        )
        bars    = r.json().get("results", [])
        trades  = []
        for b in bars[-10:]:  # last 10 bars
            # Approximate: VWAP trades at bar close
            trades.append({
                "price":     b.get("vw", b.get("c", spot)),
                "size":      int(b.get("v", 0) / 10),  # approximate
                "timestamp": b.get("t", 0) * 1_000_000,
                "conditions": [],
            })
        return trades

    except Exception as e:
        log.warning(f"Iceberg agg fallback {ticker}: {e}")
        return []


def get_spot(ticker: str) -> float:
    """Get current spot price."""
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=8
        )
        t = r.json().get("ticker", {})
        return float(t.get("lastTrade", {}).get("p", 0) or
                     t.get("day", {}).get("c", 0) or 0)
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def scan_for_icebergs() -> dict:
    """
    Scan all tickers for iceberg patterns.
    Runs every 5 min via cron during market hours.
    """
    result = {
        "date":     date.today().isoformat(),
        "computed": datetime.now().strftime("%H:%M ET"),
        "tickers":  {},
        "detected_count": 0,
    }

    for ticker in TICKERS:
        # Try real trade endpoint first
        trades = fetch_recent_trades(ticker, minutes_back=WINDOW_MINUTES)

        # Fallback to agg-based approximation
        if not trades:
            spot   = get_spot(ticker)
            trades = fetch_trades_from_aggs(ticker, spot)

        iceberg = detect_iceberg_from_trades(trades, ticker)
        result["tickers"][ticker] = iceberg

        if iceberg.get("detected"):
            result["detected_count"] += 1
            log.info(f"  🧊 ICEBERG {ticker}: {iceberg['direction']} "
                     f"@ ${iceberg['price_level']} "
                     f"size={iceberg['total_size']:,} "
                     f"premium=${iceberg['premium_k']:.0f}K")
        else:
            log.info(f"  {ticker}: no iceberg detected "
                     f"({len(trades)} trades analyzed)")

    ICEBERG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(ICEBERG_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_iceberg_cache(ticker: str = "SPY") -> dict:
    """Load latest iceberg scan for a ticker."""
    try:
        if ICEBERG_CACHE.exists():
            import time
            age = time.time() - ICEBERG_CACHE.stat().st_mtime
            if age < 600:  # 10 min max age
                with open(ICEBERG_CACHE) as f:
                    data = json.load(f)
                return data.get("tickers", {}).get(ticker,
                                                   {"detected": False, "ticker": ticker})
    except Exception:
        pass
    result = scan_for_icebergs()
    return result.get("tickers", {}).get(ticker, {"detected": False, "ticker": ticker})


# ══════════════════════════════════════════════════════════════════════
# ARKA + UOA INTEGRATION
# ══════════════════════════════════════════════════════════════════════

def get_iceberg_conviction_boost(ticker: str, trade_direction: str) -> dict:
    """
    Returns conviction boost for ARKA if iceberg aligns with trade.
    +12 pts if iceberg direction matches trade direction.
    -6 pts if iceberg direction opposes trade direction.
    """
    ice = load_iceberg_cache(ticker)

    if not ice.get("detected"):
        return {"boost": 0, "reason": "No iceberg detected", "iceberg": ice}

    ice_dir   = ice.get("direction", "NEUTRAL")
    premium_k = ice.get("premium_k", 0)

    if ((trade_direction == "LONG"  and ice_dir == "BULLISH") or
        (trade_direction == "SHORT" and ice_dir == "BEARISH")):
        boost  = 12
        reason = (f"🧊 Iceberg {ice_dir} @ ${ice['price_level']} "
                  f"${premium_k:.0f}K premium aligns {trade_direction} ✅")
    elif ((trade_direction == "LONG"  and ice_dir == "BEARISH") or
          (trade_direction == "SHORT" and ice_dir == "BULLISH")):
        boost  = -6
        reason = (f"🧊 Iceberg {ice_dir} @ ${ice['price_level']} "
                  f"opposes {trade_direction} ⚠️")
    else:
        boost  = 0
        reason = f"Iceberg NEUTRAL for {trade_direction}"

    return {"boost": boost, "reason": reason, "iceberg": ice}


def upgrade_uoa_with_iceberg(uoa_result: dict) -> dict:
    """
    Enhance UOA detector output with iceberg context.
    Called from detect_unusual_options() return path.
    """
    ticker  = uoa_result.get("ticker", "SPY")
    ice     = load_iceberg_cache(ticker)

    uoa_result["iceberg"] = ice
    if ice.get("detected"):
        # If iceberg direction matches UOA bias, raise confidence
        uoa_bias  = uoa_result.get("bias", "NEUTRAL")
        ice_dir   = ice.get("direction", "NEUTRAL")
        if ((uoa_bias == "BULLISH" and ice_dir == "BULLISH") or
            (uoa_bias == "BEARISH" and ice_dir == "BEARISH")):
            uoa_result["iceberg_confirms"] = True
            uoa_result["bias_confidence"]  = "HIGH"
        else:
            uoa_result["iceberg_confirms"] = False
            uoa_result["bias_confidence"]  = "MIXED"

    return uoa_result


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = scan_for_icebergs()
    print(f"\n── Iceberg Scan ({result['computed']}) ──────────────────────────")
    for ticker, ice in result.get("tickers", {}).items():
        if ice.get("detected"):
            print(f"  🧊 {ticker}: {ice['direction']} @ ${ice['price_level']} "
                  f"| {ice['total_size']:,} shares "
                  f"| ${ice['premium_k']:.0f}K premium "
                  f"| {ice['print_count']} prints "
                  f"| algo={ice['algorithmic']}")
        else:
            print(f"  ✓  {ticker}: clean — no iceberg")

    print(f"\n  Total detected: {result['detected_count']}")
