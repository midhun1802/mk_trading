"""
CHAKRA — MOC Imbalance Late-Day Modifier
File: backend/chakra/moc_imbalance.py

Reads Market-On-Close imbalance from Polygon equity closing print conditions.
Active ONLY during 3:00–3:58 PM ET (lotto engine / power hour window).

Conviction modifiers:
  MOC Buy  + BULLISH signal  → +10
  MOC Sell + BEARISH signal  → +10
  MOC Buy  + BEARISH signal  →  -8  (headwind)
  MOC Sell + BULLISH signal  →  -8  (headwind)
  Outside window / low conf  →   0

Integration in arka_engine.py and lotto_engine.py:
    from backend.chakra.moc_imbalance import get_moc_conviction_modifier
    if ticker in ("SPY", "QQQ", "SPX", "IWM"):
        conviction += get_moc_conviction_modifier(ticker, direction)

Dashboard endpoint (add to dashboard_api.py):
    from backend.chakra.moc_imbalance import get_moc_state_for_dashboard
    @app.get("/api/moc/imbalance")
    async def moc_imbalance():
        return {"results": get_moc_state_for_dashboard()}
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
BASE_URL        = "https://api.polygon.io"
ET              = ZoneInfo("America/New_York")

# Closing print condition codes (equity SIP)
MOC_CONDITIONS = {8: "Closing Print", 15: "Market Center Official Close", 19: "Market Center Closing Trade"}

# Power hour window
MOC_WINDOW_START  = (15, 0)    # 3:00 PM ET
MOC_WINDOW_END    = (15, 58)   # 3:58 PM ET

MOC_ALIGN_BOOST    = 10
MOC_OPPOSE_PENALTY = -8

MOC_SUPPORTED_TICKERS = {"SPY", "QQQ", "IWM", "DIA", "SPX", "NDX"}

_moc_cache: dict = {}
_CACHE_TTL = 120  # 2 minutes


def _polygon_get(path: str, params: dict) -> dict:
    params["apiKey"] = POLYGON_API_KEY
    try:
        resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=8)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"Polygon error [{path}]: {e}")
        return {}


def _is_moc_window() -> bool:
    now = datetime.now(ET)
    sh, sm = MOC_WINDOW_START
    eh, em = MOC_WINDOW_END
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def _cache_get(ticker: str):
    entry = _moc_cache.get(ticker)
    if entry and (time.time() - entry["_cached_at"]) < _CACHE_TTL:
        return entry
    return None


def _cache_set(ticker: str, state: dict) -> None:
    state["_cached_at"] = time.time()
    _moc_cache[ticker] = state


class MOCScanner:
    def get_imbalance_state(self, ticker: str) -> dict:
        cached = _cache_get(ticker)
        if cached:
            return {**cached, "source": "cache"}

        poly_ticker = "I:SPX" if ticker == "SPX" else ticker
        since_ns = int((time.time() - 300) * 1e9)

        data   = _polygon_get(f"/v3/trades/{poly_ticker}",
                               {"timestamp.gte": since_ns, "limit": 1000, "order": "desc"})
        trades = data.get("results", [])

        if not trades:
            state = self._unknown_state(ticker)
            _cache_set(ticker, state)
            return state

        buy_vol = sell_vol = total = 0.0
        for trade in trades:
            conditions = trade.get("conditions", [])
            if not any(c in MOC_CONDITIONS for c in conditions):
                continue
            notional = trade.get("price", 0) * trade.get("size", 0)
            is_aggressive = 14 in conditions or 219 in conditions
            exchange = trade.get("exchange", 0)

            if is_aggressive:
                buy_vol  += notional
            elif exchange % 2 == 0:
                buy_vol  += notional * 0.55
                sell_vol += notional * 0.45
            else:
                sell_vol += notional * 0.55
                buy_vol  += notional * 0.45
            total += notional

        if total == 0:
            state = self._unknown_state(ticker)
            _cache_set(ticker, state)
            return state

        buy_ratio = buy_vol / total
        if buy_ratio >= 0.62:
            imbalance  = "BUY"
            confidence = min(0.95, 0.60 + (buy_ratio - 0.62) * 2.5)
        elif buy_ratio <= 0.38:
            imbalance  = "SELL"
            confidence = min(0.95, 0.60 + (0.38 - buy_ratio) * 2.5)
        else:
            imbalance  = "NONE"
            confidence = 0.50

        state = {
            "ticker":         ticker,
            "imbalance":      imbalance,
            "confidence":     round(confidence, 3),
            "close_vol_buy":  round(buy_vol, 2),
            "close_vol_sell": round(sell_vol, 2),
            "buy_ratio":      round(buy_ratio, 3),
            "source":         "polygon_conditions",
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
        _cache_set(ticker, state)
        logger.info(f"MOC [{ticker}] {imbalance} conf={confidence:.2f} buy_ratio={buy_ratio:.2f}")
        return state

    @staticmethod
    def _unknown_state(ticker: str) -> dict:
        return {
            "ticker":         ticker,
            "imbalance":      "UNKNOWN",
            "confidence":     0.0,
            "close_vol_buy":  0.0,
            "close_vol_sell": 0.0,
            "buy_ratio":      0.5,
            "source":         "polygon_conditions",
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }


_scanner = MOCScanner()


def get_moc_conviction_modifier(ticker: str, direction: str) -> int:
    """
    Returns +10, -8, or 0 based on MOC imbalance vs signal direction.
    Only active 3:00–3:58 PM ET.
    """
    if not _is_moc_window() or ticker not in MOC_SUPPORTED_TICKERS:
        return 0
    try:
        state = _scanner.get_imbalance_state(ticker)
    except Exception as e:
        logger.warning(f"MOC error {ticker}: {e}")
        return 0

    imbalance  = state.get("imbalance", "UNKNOWN")
    confidence = state.get("confidence", 0.0)
    if confidence < 0.65:
        return 0

    if imbalance == "BUY"  and direction == "BULLISH": return MOC_ALIGN_BOOST
    if imbalance == "SELL" and direction == "BEARISH": return MOC_ALIGN_BOOST
    if imbalance == "BUY"  and direction == "BEARISH": return MOC_OPPOSE_PENALTY
    if imbalance == "SELL" and direction == "BULLISH": return MOC_OPPOSE_PENALTY
    return 0


def get_moc_state_for_dashboard(tickers=None) -> list:
    tickers = tickers or list(MOC_SUPPORTED_TICKERS)
    return [_scanner.get_imbalance_state(t) for t in tickers]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [MOC] %(message)s", datefmt="%H:%M:%S")
    tickers  = sys.argv[1:] if len(sys.argv) > 1 else list(MOC_SUPPORTED_TICKERS)
    in_win   = _is_moc_window()
    print(f"MOC Scanner — {datetime.now(ET).strftime('%H:%M ET')} | window_active={in_win}\n")
    for t in tickers:
        s  = _scanner.get_imbalance_state(t)
        bm = get_moc_conviction_modifier(t, "BULLISH")
        berm = get_moc_conviction_modifier(t, "BEARISH")
        print(f"  {t:6s}  {s['imbalance']:7s}  conf={s['confidence']:.2f}  "
              f"buy_ratio={s['buy_ratio']:.2f}  BULL={bm:+d}  BEAR={berm:+d}")
