"""
CHAKRA — Sector Rotation Conviction Modifier
File: backend/chakra/sector_rotation.py

Reads sector ETF performance from Polygon (2-min cache).
Returns conviction modifier based on whether sector flow aligns with trade direction.

  Sector broadly green  + LONG  signal  → +5
  Sector broadly red    + SHORT signal  → +5
  Sector against signal              → -3
  Weak / flat sector data            →  0

Wire in arka_engine.py conviction block:
    from backend.chakra.sector_rotation import get_sector_conviction_modifier
    _sec_mod = get_sector_conviction_modifier(ticker, _signal_dir)
    if _sec_mod != 0:
        score = max(0, min(100, score + _sec_mod))
        reasons.append(f"Sector rotation {_signal_dir} {_sec_mod:+d}")
        comp["sector"] = _sec_mod
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

logger = logging.getLogger("CHAKRA.SectorRotation")

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
ET              = ZoneInfo("America/New_York")
CACHE_FILE      = Path("logs/chakra/sector_snapshot.json")
CACHE_TTL       = 120   # 2-min cache

# Sector ETF mapping — what to check for each underlying
SECTOR_MAP: dict = {
    "SPY":  ["XLK", "XLF", "XLI", "XLY", "XLV"],   # broad market basket
    "QQQ":  ["XLK", "XLC"],                           # tech + comm
    "IWM":  ["XLI", "XLF", "XLY"],                   # small-cap basket
    "SPX":  ["XLK", "XLF", "XLI", "XLY", "XLV"],
    "NVDA": "XLK",  "AMD":  "XLK",  "AAPL": "XLK",
    "MSFT": "XLK",  "GOOGL": "XLC", "META": "XLC",
    "AMZN": "XLY",  "TSLA": "XLY",  "NFLX": "XLC",
    "COIN": "XLF",  "GS":   "XLF",  "JPM":  "XLF",
    "XOM":  "XLE",  "CVX":  "XLE",
}

ALIGN_BOOST   =  5
OPPOSE_PENALTY = -3
STRONG_THRESHOLD = 0.30  # sector up/down >0.3% = meaningful
WEAK_THRESHOLD   = 0.10  # below 0.1% change = flat, no signal


def _fetch_sector_snapshot() -> dict:
    """Fetch live sector ETF data from Polygon and cache to disk."""
    import requests
    tickers = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLRE", "XLB", "XLC"]
    syms    = ",".join(tickers)
    try:
        resp = requests.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": syms, "apiKey": POLYGON_API_KEY},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        logger.warning(f"Polygon sector fetch failed: {e}")
        return {}

    out = {}
    for t in raw.get("tickers", []):
        sym   = t.get("ticker", "")
        day   = t.get("day", {})
        prev  = t.get("prevDay", {})
        price = t.get("lastTrade", {}).get("p", 0) or day.get("c", 0) or prev.get("c", 0)
        pc    = prev.get("c", 1) or 1
        chg_pct = (price - pc) / pc * 100 if pc else 0
        out[sym] = {
            "price":   round(price, 2),
            "chg_pct": round(chg_pct, 3),
            "direction": "UP" if chg_pct > WEAK_THRESHOLD else "DOWN" if chg_pct < -WEAK_THRESHOLD else "FLAT",
        }

    payload = {"data": out, "ts": time.time(), "fetched_at": datetime.now(timezone.utc).isoformat()}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        logger.debug(f"Cache write failed: {e}")
    return out


def _load_cache() -> dict:
    try:
        if not CACHE_FILE.exists():
            return {}
        payload = json.loads(CACHE_FILE.read_text())
        age = time.time() - payload.get("ts", 0)
        if age > CACHE_TTL:
            return {}
        return payload.get("data", {})
    except Exception:
        return {}


def get_sector_data(force_refresh: bool = False) -> dict:
    """Return sector ETF snapshot. Uses cache when fresh."""
    data = {} if force_refresh else _load_cache()
    if not data:
        data = _fetch_sector_snapshot()
    return data


def _score_sectors(sectors: list | str, data: dict) -> float:
    """Return average chg_pct for the given sector(s)."""
    if isinstance(sectors, str):
        sectors = [sectors]
    vals = [data[s]["chg_pct"] for s in sectors if s in data]
    return sum(vals) / len(vals) if vals else 0.0


def get_sector_conviction_modifier(ticker: str, direction: str) -> int:
    """
    Returns +5, -3, or 0 based on sector rotation alignment.
    direction: "BULLISH" or "BEARISH"
    """
    try:
        sectors = SECTOR_MAP.get(ticker.upper())
        if not sectors:
            return 0

        data     = get_sector_data()
        if not data:
            return 0

        avg_chg  = _score_sectors(sectors, data)

        # Strong sector move
        if abs(avg_chg) < STRONG_THRESHOLD:
            return 0   # too flat to matter

        sector_bullish = avg_chg > 0
        aligned = (sector_bullish and direction == "BULLISH") or \
                  (not sector_bullish and direction == "BEARISH")

        if aligned:
            return ALIGN_BOOST
        return OPPOSE_PENALTY

    except Exception as e:
        logger.debug(f"sector_rotation error [{ticker}]: {e}")
        return 0


def get_sector_state_for_dashboard() -> dict:
    """Returns full sector snapshot for dashboard. Refreshes if stale."""
    return get_sector_data(force_refresh=False)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [Sector] %(message)s", datefmt="%H:%M:%S")
    tickers = sys.argv[1:] if len(sys.argv) > 1 else list(SECTOR_MAP.keys())
    data    = get_sector_data(force_refresh=True)
    print(f"Sector Rotation — {datetime.now(ET).strftime('%H:%M ET')}\n")
    print(f"{'ETF':6s}  {'chg%':>7s}  {'dir':5s}")
    print("-" * 25)
    for sym, d in sorted(data.items()):
        print(f"{sym:6s}  {d['chg_pct']:>+6.2f}%  {d['direction']}")
    print()
    for tk in tickers:
        bull = get_sector_conviction_modifier(tk, "BULLISH")
        bear = get_sector_conviction_modifier(tk, "BEARISH")
        print(f"  {tk:6s}  BULL={bull:+d}  BEAR={bear:+d}")
