"""
CHAKRA — Options Sweep Detector
File: backend/chakra/sweep_detector.py

Detects aggressive options sweeps via Polygon condition codes 37/38/41.
Runs every 5 minutes via crontab during market hours, caches results to
logs/chakra/sweep_latest.json for consumption by arka_engine.py.

Condition codes:
  37 = Intermarket Sweep Order (ISO) — aggressive cross-exchange sweep
  38 = ISO Cross                     — large cross-exchange fill
  41 = Single-Leg Auction Non-ISO    — large single-leg aggressive fill

Conviction boosts:
  +7  multi-exchange sweep (code 37 or 38) confirmed in signal direction
  +4  single-exchange sweep (code 41) in signal direction
  -4  sweep strongly against signal direction (high confidence)

Integration:
    from backend.chakra.sweep_detector import get_sweep_boost
    conviction += get_sweep_boost(ticker, direction)  # direction: "BULLISH" or "BEARISH"

Crontab:
    */5 9-16 * * 1-5  cd ~/trading-ai && python3 backend/chakra/sweep_detector.py >> logs/chakra/sweep_detector.log 2>&1
"""

import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env", override=True)

logger = logging.getLogger("CHAKRA.SweepDetector")

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
BASE_URL        = "https://api.polygon.io"
ET              = ZoneInfo("America/New_York")

CACHE_FILE = BASE_DIR / "logs" / "chakra" / "sweep_latest.json"
CACHE_TTL  = 360  # 6 minutes — valid for two 5-min cron cycles

# Condition codes for aggressive sweeps
SWEEP_CONDITIONS_MULTI  = {37, 38}  # multi-exchange ISO
SWEEP_CONDITIONS_SINGLE = {41}      # single-leg aggressive

SWEEP_BOOST_MULTI  = 7
SWEEP_BOOST_SINGLE = 4
SWEEP_PENALTY      = -4

# Tickers to scan by default (cron mode)
SWEEP_TICKERS = ["SPY", "QQQ", "IWM", "SPX", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META"]

# Options contract lookback (seconds)
LOOKBACK_SECS = 600  # last 10 minutes


def _polygon_get(path: str, params: dict) -> dict:
    params["apiKey"] = POLYGON_API_KEY
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"Polygon error [{path}]: {e}")
        return {}


def scan_sweeps_for_ticker(ticker: str) -> dict:
    """
    Scan recent options trades for sweep conditions.
    Returns dict with: ticker, call_sweep, put_sweep, call_volume, put_volume,
    multi_exchange, confidence, signal, timestamp
    """
    now_ns      = int(time.time() * 1e9)
    lookback_ns = int((time.time() - LOOKBACK_SECS) * 1e9)

    # Query recent options trades via Polygon options trades endpoint
    data = _polygon_get(
        f"/v3/trades/O:{ticker}",
        {
            "timestamp.gte": lookback_ns,
            "timestamp.lte": now_ns,
            "limit": 500,
            "order": "desc",
        }
    )
    trades = data.get("results", [])

    if not trades:
        # Fallback: try without O: prefix (some index tickers)
        data = _polygon_get(
            f"/v3/trades/{ticker}",
            {"timestamp.gte": lookback_ns, "limit": 200, "order": "desc"}
        )
        trades = data.get("results", [])

    call_vol_multi   = 0.0
    put_vol_multi    = 0.0
    call_vol_single  = 0.0
    put_vol_single   = 0.0

    for trade in trades:
        conditions  = set(trade.get("conditions", []))
        size        = trade.get("size", 0)
        price       = trade.get("price", 0.0)
        symbol      = trade.get("symbol", trade.get("ticker", ""))
        notional    = size * price * 100  # options: 100 shares per contract

        # Determine call or put from symbol
        is_call = "C" in symbol.upper().split(ticker.upper())[-1][:2] if ticker.upper() in symbol.upper() else None
        if is_call is None:
            continue

        has_multi  = bool(conditions & SWEEP_CONDITIONS_MULTI)
        has_single = bool(conditions & SWEEP_CONDITIONS_SINGLE)

        if has_multi:
            if is_call:  call_vol_multi  += notional
            else:        put_vol_multi   += notional
        elif has_single:
            if is_call:  call_vol_single += notional
            else:        put_vol_single  += notional

    total_call = call_vol_multi + call_vol_single
    total_put  = put_vol_multi  + put_vol_single
    total      = total_call + total_put

    # Determine signal direction
    signal     = "NEUTRAL"
    confidence = 0.0
    is_multi   = False

    if total > 0:
        call_ratio = total_call / total
        if call_ratio >= 0.65:
            signal     = "BULLISH"
            confidence = min(0.95, 0.60 + (call_ratio - 0.65) * 2.0)
            is_multi   = call_vol_multi > call_vol_single
        elif call_ratio <= 0.35:
            signal     = "BEARISH"
            confidence = min(0.95, 0.60 + (0.35 - call_ratio) * 2.0)
            is_multi   = put_vol_multi > put_vol_single

    result = {
        "ticker":          ticker,
        "signal":          signal,
        "confidence":      round(confidence, 3),
        "multi_exchange":  is_multi,
        "call_volume":     round(total_call, 0),
        "put_volume":      round(total_put, 0),
        "call_vol_multi":  round(call_vol_multi, 0),
        "put_vol_multi":   round(put_vol_multi, 0),
        "total_volume":    round(total, 0),
        "trade_count":     len(trades),
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        f"[Sweep] {ticker:6s} signal={signal:8s} conf={confidence:.2f} "
        f"multi={is_multi} call=${total_call/1e6:.1f}M put=${total_put/1e6:.1f}M"
    )
    return result


def scan_all_tickers(tickers=None) -> dict:
    """Scan all tickers and write cache file. Returns mapping ticker→result."""
    tickers = tickers or SWEEP_TICKERS
    results = {}
    for t in tickers:
        try:
            results[t] = scan_sweeps_for_ticker(t)
        except Exception as e:
            logger.warning(f"[Sweep] Error scanning {t}: {e}")
            results[t] = {
                "ticker": t, "signal": "UNKNOWN", "confidence": 0.0,
                "multi_exchange": False, "call_volume": 0, "put_volume": 0,
                "total_volume": 0, "trade_count": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    payload = {
        "results":    results,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "tickers":    tickers,
    }
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(payload, indent=2))
        logger.info(f"[Sweep] Cache written → {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"[Sweep] Cache write failed: {e}")
    return results


def _load_cache() -> dict:
    """Load sweep cache if fresh enough."""
    try:
        if not CACHE_FILE.exists():
            return {}
        data = json.loads(CACHE_FILE.read_text())
        scanned_str = data.get("scanned_at", "")
        if scanned_str:
            scanned_dt = datetime.fromisoformat(scanned_str)
            age = (datetime.now(timezone.utc) - scanned_dt).total_seconds()
            if age > CACHE_TTL:
                return {}  # stale
        return data.get("results", {})
    except Exception:
        return {}


def get_sweep_boost(ticker: str, direction: str) -> int:
    """
    Returns conviction adjustment based on sweep signal.
    direction: "BULLISH" or "BEARISH"
    Returns: +7 (multi-exchange aligned), +4 (single aligned), -4 (opposing), 0 (neutral)
    """
    try:
        results = _load_cache()
        if not results or ticker not in results:
            return 0

        state      = results[ticker]
        signal     = state.get("signal", "NEUTRAL")
        confidence = state.get("confidence", 0.0)
        is_multi   = state.get("multi_exchange", False)

        if confidence < 0.65 or signal == "NEUTRAL" or signal == "UNKNOWN":
            return 0

        aligned = (signal == "BULLISH" and direction == "BULLISH") or \
                  (signal == "BEARISH" and direction == "BEARISH")
        opposing = (signal == "BULLISH" and direction == "BEARISH") or \
                   (signal == "BEARISH" and direction == "BULLISH")

        if aligned:
            return SWEEP_BOOST_MULTI if is_multi else SWEEP_BOOST_SINGLE
        if opposing and confidence >= 0.80:
            return SWEEP_PENALTY
        return 0

    except Exception as e:
        logger.debug(f"[Sweep] get_sweep_boost error: {e}")
        return 0


def get_sweep_state_for_dashboard(tickers=None) -> list:
    """Returns sweep state for all tickers — used by dashboard API."""
    results = _load_cache()
    tickers = tickers or SWEEP_TICKERS
    out = []
    for t in tickers:
        if t in results:
            out.append(results[t])
        else:
            out.append({
                "ticker": t, "signal": "UNKNOWN", "confidence": 0.0,
                "multi_exchange": False, "call_volume": 0, "put_volume": 0,
                "total_volume": 0, "trade_count": 0, "timestamp": None,
            })
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [SweepDetector] %(message)s",
        datefmt="%H:%M:%S",
    )
    tickers = sys.argv[1:] if len(sys.argv) > 1 else SWEEP_TICKERS
    print(f"Options Sweep Detector — {datetime.now(ET).strftime('%H:%M ET')}\n")
    results = scan_all_tickers(tickers)
    print("\nResults:")
    for t, r in results.items():
        boost_b = get_sweep_boost(t, "BULLISH")
        boost_s = get_sweep_boost(t, "BEARISH")
        print(
            f"  {t:6s}  {r['signal']:8s}  conf={r['confidence']:.2f}  "
            f"multi={r['multi_exchange']}  "
            f"call=${r['call_volume']/1e6:.1f}M  put=${r['put_volume']/1e6:.1f}M  "
            f"BULL={boost_b:+d}  BEAR={boost_s:+d}"
        )
