"""
ARKA — Dynamic Ticker Universe
File: backend/arka/dynamic_universe.py

Builds the ARKA scan universe automatically every 5 minutes from:
  1. ARJUN pipeline signals  — tickers with BUY/SELL confidence >= 0.60
  2. CHAKRA flow signals     — tickers with BULLISH/BEARISH bias (non-neutral)
  3. Swing watchlist         — arka_swings candidates with score >= 65
  4. Sweep detector          — tickers with confirmed sweep signals today
  5. Polygon top movers      — biggest % movers (up & down) from the market snapshot

Base indexes (SPY, QQQ, SPX) are always included regardless of sources.
Total universe is capped at MAX_UNIVERSE_SIZE tickers.
"""

import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

log  = logging.getLogger("ARKA.Universe")
BASE = Path(__file__).resolve().parents[2]
ET   = ZoneInfo("America/New_York")

# ── Config ───────────────────────────────────────────────────────────────────
BASE_TICKERS        = ["SPY", "QQQ", "SPX", "IWM", "DIA", "GLD", "SLV"]  # always included
MAX_UNIVERSE_SIZE   = 22                              # base (7) + up to 15 stocks
REFRESH_INTERVAL    = 300                             # rebuild every 5 minutes
ARJUN_MIN_CONF      = 0.60                            # min ARJUN confidence to include
FLOW_MIN_CONF       = 50                              # min flow confidence to include
SWING_MIN_SCORE     = 65                              # min swing score to include
TOP_MOVERS_N        = 5                               # how many Polygon movers to pull
POLYGON_MIN_MOVE    = 2.0                             # min % move to qualify as mover
SKIP_TICKERS        = {                               # never scan these
    "SPX", "NDX", "VIX", "XLF", "XLK", "XLE", "XLV",
    "XLI", "XLP", "XLY", "XLU", "XLRE", "XLB", "XLC",
    "SOXX",  # ETF, no direct options on paper account
}

# ── Internal cache ────────────────────────────────────────────────────────────
_cache: dict = {"tickers": list(BASE_TICKERS), "sources": {}, "ts": 0.0}


def _read_json(rel_path: str) -> dict | list:
    try:
        p = BASE / rel_path
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _from_arjun_pipeline() -> list[str]:
    """Tickers with ARJUN BUY/SELL signal at confidence >= 60%."""
    data = _read_json("logs/arjun/pipeline_latest.json")
    out  = []
    for ticker, sig in (data.items() if isinstance(data, dict) else []):
        if not isinstance(sig, dict):
            continue
        signal = (sig.get("signal") or "").upper()
        conf   = float(sig.get("confidence") or 0)
        if signal in ("BUY", "SELL") and conf >= ARJUN_MIN_CONF:
            out.append(ticker.upper())
    return out


def _from_flow_signals() -> list[str]:
    """Tickers with non-neutral CHAKRA flow bias."""
    data = _read_json("logs/chakra/flow_signals_latest.json")
    out  = []
    for ticker, sig in (data.items() if isinstance(data, dict) else []):
        if not isinstance(sig, dict):
            continue
        bias = (sig.get("bias") or "NEUTRAL").upper()
        conf = float(sig.get("confidence") or 0)
        if bias not in ("NEUTRAL", "") and conf >= FLOW_MIN_CONF:
            out.append(ticker.upper())
    return out


def _from_swing_watchlist() -> list[str]:
    """Top swing screener candidates."""
    data  = _read_json("logs/chakra/watchlist_latest.json")
    cands = data.get("candidates", data.get("top5", []))
    out   = []
    for c in cands:
        ticker = (c.get("ticker") or "").upper()
        score  = float(c.get("score") or 0)
        if ticker and score >= SWING_MIN_SCORE:
            out.append(ticker)
    return out


def _from_sweep_detector() -> list[str]:
    """Tickers with confirmed options sweep signals today."""
    data = _read_json("logs/chakra/sweep_latest.json")
    out  = []
    for ticker, sig in (data.items() if isinstance(data, dict) else []):
        if not isinstance(sig, dict):
            continue
        signal = (sig.get("signal") or "NEUTRAL").upper()
        if signal not in ("NEUTRAL", "UNKNOWN", ""):
            out.append(ticker.upper())
    return out


def _from_polygon_movers() -> list[str]:
    """Top % movers from Polygon snapshot — liquid stocks only (price > $20, volume > 500K)."""
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        return []
    try:
        import httpx
        movers = []
        for direction in ("gainers", "losers"):
            r = httpx.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}",
                params={"apiKey": key, "include_otc": "false"},
                timeout=6,
            ).json()
            for t in r.get("tickers", []):
                sym     = t.get("ticker", "").upper()
                chg_pct = abs(float(t.get("todaysChangePerc") or 0))
                price   = float(t.get("day", {}).get("c") or t.get("lastTrade", {}).get("p") or 0)
                volume  = float(t.get("day", {}).get("v") or 0)
                # Liquidity gates: skip micro-caps, ETFs, and low-volume tickers
                if not sym or sym.startswith("^"):
                    continue
                if len(sym) > 5:          # skip warrants / special classes
                    continue
                if price < 20:            # skip penny / small caps
                    continue
                if volume < 500_000:      # skip illiquid
                    continue
                if chg_pct >= POLYGON_MIN_MOVE:
                    movers.append(sym)
        return movers[:TOP_MOVERS_N * 2]
    except Exception as e:
        log.debug(f"Polygon movers fetch failed: {e}")
        return []


def build_universe(force: bool = False) -> list[str]:
    """
    Returns the current scan universe.
    Rebuilds from all sources if cache is stale (> REFRESH_INTERVAL seconds).
    """
    global _cache

    if not force and (time.time() - _cache["ts"]) < REFRESH_INTERVAL:
        return list(_cache["tickers"])

    log.info("  🔭 Universe refresh — rebuilding ticker list...")

    sources: dict[str, list[str]] = {
        "arjun":   _from_arjun_pipeline(),
        "flow":    _from_flow_signals(),
        "swings":  _from_swing_watchlist(),
        "sweeps":  _from_sweep_detector(),
        "movers":  _from_polygon_movers(),
    }

    # Merge all sources into an ordered set (preserve priority order)
    seen    = set(BASE_TICKERS)
    ordered = list(BASE_TICKERS)

    for source_name, tickers in sources.items():
        added = []
        for t in tickers:
            if t in seen or t in SKIP_TICKERS:
                continue
            # Basic sanity: only real equity symbols (1-5 alpha chars)
            if not t.isalpha() or len(t) > 6:
                continue
            seen.add(t)
            ordered.append(t)
            added.append(t)
        if added:
            log.info(f"    [{source_name:8s}] +{len(added):2d}: {', '.join(added)}")

    # Cap universe size
    universe = ordered[:MAX_UNIVERSE_SIZE]

    _cache = {"tickers": universe, "sources": sources, "ts": time.time()}

    added_stocks = [t for t in universe if t not in BASE_TICKERS]
    log.info(f"  🔭 Universe: {len(universe)} tickers | "
             f"indexes={BASE_TICKERS} | "
             f"stocks={added_stocks}")
    return universe


def get_universe() -> list[str]:
    """Convenience wrapper — returns current universe, rebuilds if stale."""
    return build_universe()


def get_universe_summary() -> dict:
    """Returns current universe state for the dashboard API."""
    build_universe()   # ensure fresh
    return {
        "tickers":     _cache["tickers"],
        "base":        BASE_TICKERS,
        "stocks":      [t for t in _cache["tickers"] if t not in BASE_TICKERS],
        "sources":     {k: v for k, v in _cache["sources"].items()},
        "last_refresh": datetime.fromtimestamp(_cache["ts"], ET).strftime("%H:%M:%S ET")
                        if _cache["ts"] else "never",
        "next_refresh": max(0, int(REFRESH_INTERVAL - (time.time() - _cache["ts"]))),
    }


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    u = build_universe(force=True)
    print("\nFinal universe:", u)
