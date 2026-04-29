"""
CHAKRA — Intraday OI Delta Tracker
File: backend/chakra/oi_tracker.py

Tracks % change in open interest per strike for SPY/QQQ every 5 minutes.
Detects institutional positioning buildup — large OI increases = conviction boost.

OI buildup rules:
  Call OI +20%+ at ATM strike + BULLISH signal  → +5 conviction
  Put  OI +20%+ at ATM strike + BEARISH signal  → +5 conviction
  Cross signal (put buildup but going LONG)      → -4 conviction
  Unusual OI spike >50% at any strike            → always logged

Wire in arka_engine.py conviction block:
    from backend.chakra.oi_tracker import get_oi_conviction_boost
    _oi_boost = get_oi_conviction_boost(ticker, direction, price)
    if _oi_boost != 0:
        score = max(0, min(100, score + _oi_boost))
        reasons.append(f"OI buildup {direction} {_oi_boost:+d}")
        comp["oi_delta"] = _oi_boost

Crontab:
    */5 9-16 * * 1-5  cd ~/trading-ai && venv/bin/python3 backend/chakra/oi_tracker.py >> logs/chakra/oi_tracker.log 2>&1
"""

import os
import json
import time
import logging
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logger = logging.getLogger("CHAKRA.OITracker")

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
BASE_URL        = "https://api.polygon.io"
ET              = ZoneInfo("America/New_York")

CACHE_FILE       = BASE / "logs" / "chakra" / "oi_snapshot_current.json"
PREV_CACHE_FILE  = BASE / "logs" / "chakra" / "oi_snapshot_prev.json"
DELTA_FILE       = BASE / "logs" / "chakra" / "oi_delta_latest.json"

SCAN_TICKERS = ["SPY", "QQQ", "IWM", "SPX"]
STRIKES_EACH_SIDE = 8   # scan ATM ±8 strikes
OI_BOOST_THRESHOLD = 0.20   # 20% OI increase = signal
OI_SPIKE_THRESHOLD = 0.50   # 50% = unusual spike
RESULT_TTL         = 360    # 6 min cache for boost reads


def _polygon_get(path: str, params: dict) -> dict:
    import requests
    params["apiKey"] = POLYGON_API_KEY
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Polygon [{path}]: {e}")
        return {}


def _get_atm_strike(ticker: str) -> float:
    """Get current underlying price from Polygon."""
    poly_ticker = "I:SPX" if ticker == "SPX" else ticker
    data = _polygon_get(f"/v2/last/trade/{poly_ticker}", {})
    return float(data.get("results", {}).get("p", 0))


def _round_strike(price: float, increment: float) -> float:
    return round(round(price / increment) * increment, 2)


def _get_strike_increment(ticker: str) -> float:
    return 5.0 if ticker in ("SPX", "NDX") else 1.0


def scan_oi_for_ticker(ticker: str) -> dict:
    """Fetch current OI for ATM ±N strikes, both calls and puts."""
    today   = date.today().isoformat()
    price   = _get_atm_strike(ticker)
    if not price:
        return {"ticker": ticker, "error": "no_price", "strikes": {}}

    incr     = _get_strike_increment(ticker)
    atm      = _round_strike(price, incr)
    strikes  = [round(atm + (i * incr), 2) for i in range(-STRIKES_EACH_SIDE, STRIKES_EACH_SIDE + 1)]

    poly_ticker = "I:SPX" if ticker == "SPX" else ticker
    # Fetch options snapshot for today's expiry
    data = _polygon_get(
        f"/v3/snapshot/options/{poly_ticker}",
        {
            "expiration_date": today,
            "limit": 250,
        }
    )
    results = data.get("results", [])

    # Build OI map: {strike_type: oi} e.g. {"550.0_call": 15000, "550.0_put": 8000}
    oi_map: dict = {}
    for contract in results:
        details = contract.get("details", {})
        strike  = float(details.get("strike_price", 0))
        ctype   = details.get("contract_type", "").lower()  # "call" or "put"
        oi      = int(contract.get("open_interest", 0) or 0)
        if strike in strikes:
            key = f"{strike}_{ctype}"
            oi_map[key] = oi

    return {
        "ticker":     ticker,
        "price":      round(price, 2),
        "atm":        atm,
        "strikes":    oi_map,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "ts":         time.time(),
    }


def scan_all() -> dict:
    """Scan all tickers and write current snapshot, compute delta vs previous."""
    # Rotate: prev ← current
    if CACHE_FILE.exists():
        try:
            PREV_CACHE_FILE.write_text(CACHE_FILE.read_text())
        except Exception:
            pass

    # Scan fresh
    results = {}
    for tk in SCAN_TICKERS:
        try:
            results[tk] = scan_oi_for_ticker(tk)
        except Exception as e:
            logger.warning(f"OI scan error {tk}: {e}")
            results[tk] = {"ticker": tk, "error": str(e), "strikes": {}}

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(results, indent=2))

    # Compute deltas
    delta_results = compute_deltas(results)
    DELTA_FILE.write_text(json.dumps(delta_results, indent=2))
    logger.info(f"OI scan complete — {len(results)} tickers")
    return delta_results


def compute_deltas(current: dict) -> dict:
    """Compare current OI to previous snapshot, compute % change."""
    prev: dict = {}
    try:
        if PREV_CACHE_FILE.exists():
            prev = json.loads(PREV_CACHE_FILE.read_text())
    except Exception:
        pass

    delta_out: dict = {}
    for ticker, curr_data in current.items():
        curr_strikes = curr_data.get("strikes", {})
        prev_strikes = prev.get(ticker, {}).get("strikes", {})

        call_oi_now  = sum(v for k, v in curr_strikes.items() if k.endswith("_call"))
        put_oi_now   = sum(v for k, v in curr_strikes.items() if k.endswith("_put"))
        call_oi_prev = sum(v for k, v in prev_strikes.items() if k.endswith("_call"))
        put_oi_prev  = sum(v for k, v in prev_strikes.items() if k.endswith("_put"))

        call_delta_pct = (call_oi_now - call_oi_prev) / call_oi_prev if call_oi_prev > 0 else 0.0
        put_delta_pct  = (put_oi_now  - put_oi_prev)  / put_oi_prev  if put_oi_prev  > 0 else 0.0

        # Detect spike strikes
        spikes = []
        for key, oi_now in curr_strikes.items():
            oi_prev = prev_strikes.get(key, 0)
            if oi_prev > 100:  # minimum threshold to avoid noise
                chg = (oi_now - oi_prev) / oi_prev
                if abs(chg) >= OI_SPIKE_THRESHOLD:
                    spikes.append({"strike_key": key, "oi_prev": oi_prev,
                                   "oi_now": oi_now, "chg_pct": round(chg, 3)})

        # Determine signal
        signal = "NEUTRAL"
        if call_delta_pct >= OI_BOOST_THRESHOLD and call_delta_pct > put_delta_pct:
            signal = "BULLISH"
        elif put_delta_pct >= OI_BOOST_THRESHOLD and put_delta_pct > call_delta_pct:
            signal = "BEARISH"

        delta_out[ticker] = {
            "ticker":          ticker,
            "price":           curr_data.get("price", 0),
            "call_oi_now":     call_oi_now,
            "put_oi_now":      put_oi_now,
            "call_delta_pct":  round(call_delta_pct, 3),
            "put_delta_pct":   round(put_delta_pct, 3),
            "signal":          signal,
            "spikes":          spikes,
            "has_prev_data":   bool(prev_strikes),
            "timestamp":       datetime.now(timezone.utc).isoformat(),
            "ts":              time.time(),
        }
        if spikes:
            logger.info(f"  [OI] {ticker}: {len(spikes)} spike strike(s) detected")

    return delta_out


def _load_delta_cache() -> dict:
    try:
        if not DELTA_FILE.exists():
            return {}
        data = json.loads(DELTA_FILE.read_text())
        # Check freshness — oldest entry
        for v in data.values():
            if isinstance(v, dict):
                age = time.time() - v.get("ts", 0)
                if age > RESULT_TTL:
                    return {}
                break
        return data
    except Exception:
        return {}


def get_oi_conviction_boost(ticker: str, direction: str, price: float = 0) -> int:
    """
    Returns conviction adjustment based on OI delta signal.
    direction: "BULLISH" or "BEARISH"
    Returns: +5 (aligned buildup), -4 (opposing), 0 (neutral/no data)
    """
    try:
        data   = _load_delta_cache()
        if not data or ticker not in data:
            return 0

        state  = data[ticker]
        signal = state.get("signal", "NEUTRAL")

        if signal == "NEUTRAL":
            return 0

        call_chg = state.get("call_delta_pct", 0)
        put_chg  = state.get("put_delta_pct", 0)
        has_prev = state.get("has_prev_data", False)

        if not has_prev:
            return 0  # first scan, no comparison baseline

        aligned  = (signal == "BULLISH" and direction == "BULLISH") or \
                   (signal == "BEARISH" and direction == "BEARISH")
        opposing = (signal == "BULLISH" and direction == "BEARISH") or \
                   (signal == "BEARISH" and direction == "BULLISH")

        if aligned:
            return 5
        if opposing:
            return -4
        return 0

    except Exception as e:
        logger.debug(f"get_oi_conviction_boost [{ticker}]: {e}")
        return 0


def get_oi_state_for_dashboard(tickers=None) -> dict:
    """Returns OI delta state for dashboard endpoint."""
    data = _load_delta_cache()
    tickers = tickers or SCAN_TICKERS
    return {t: data.get(t, {"ticker": t, "signal": "UNKNOWN"}) for t in tickers}


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [OITracker] %(message)s",
        datefmt="%H:%M:%S",
    )
    tickers = sys.argv[1:] if len(sys.argv) > 1 else SCAN_TICKERS
    print(f"OI Delta Tracker — {datetime.now(ET).strftime('%H:%M ET')}\n")
    deltas = scan_all()
    print(f"{'Ticker':6s}  {'Signal':8s}  {'CallΔ':>7s}  {'PutΔ':>7s}  {'Spikes':>6s}")
    print("-" * 45)
    for tk, d in deltas.items():
        print(f"{tk:6s}  {d.get('signal','?'):8s}  "
              f"{d.get('call_delta_pct',0):>+6.1%}  "
              f"{d.get('put_delta_pct',0):>+6.1%}  "
              f"{len(d.get('spikes', [])):>6d}")
