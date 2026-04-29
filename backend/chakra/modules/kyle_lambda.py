"""
CHAKRA — Kyle's Lambda (Market Impact / Liquidity Gauge)
backend/chakra/modules/kyle_lambda.py

Kyle's Lambda measures how much price moves per unit of order flow.
High lambda = illiquid market = big price impact per share = DANGEROUS
Low lambda  = liquid market   = small price impact = safe to size up

Lambda = ΔPrice / ΔVolume_signed
  where signed volume approximates buy vs sell pressure (tick rule)

Practical CHAKRA use:
  Lambda EXTREME (>2σ from mean) → shrink position 50%, raise threshold +10
  Lambda HIGH    (>1σ)           → shrink position 75%
  Lambda NORMAL                  → full position
  Lambda LOW     (<0.5σ)         → expand position (extra liquidity = tighter fills)

Integration:
  - ARKA execution gate → don't size up in illiquid conditions
  - MOC Engine          → skip MOC if lambda extreme at 3:45 PM
  - Dashboard           → live liquidity gauge widget
"""

import json
import logging
import numpy as np
import httpx
import os
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log           = logging.getLogger("chakra.lambda")
POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")
LAMBDA_CACHE  = BASE / "logs" / "chakra" / "lambda_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]

# Rolling window for lambda z-score
LOOKBACK_BARS   = 30   # bars for mean/std estimation
INTRADAY_BARS   = 5    # bars per lambda estimate


# ══════════════════════════════════════════════════════════════════════
# KYLE'S LAMBDA CALCULATION
# ══════════════════════════════════════════════════════════════════════

def estimate_signed_volume(bar: dict, prev_close: float) -> float:
    """
    Approximate signed volume using tick rule:
    If close > prev_close → bullish bar → positive volume
    If close < prev_close → bearish bar → negative volume
    """
    close     = float(bar.get("c", 0) or 0)
    volume    = float(bar.get("v", 0) or 0)
    vwap      = float(bar.get("vw", close) or close)

    if prev_close <= 0:
        return 0.0

    price_dir = close - prev_close
    # Use VWAP position relative to midpoint for better direction estimate
    bar_mid   = (float(bar.get("h", close)) + float(bar.get("l", close))) / 2
    vwap_dir  = vwap - bar_mid   # positive = more buying pressure

    # Blend tick rule and VWAP position
    if price_dir > 0:
        sign = 1.0
    elif price_dir < 0:
        sign = -1.0
    else:
        sign = 1.0 if vwap_dir > 0 else -1.0

    return sign * volume


def compute_kyle_lambda(bars: list) -> dict:
    """
    Compute Kyle's Lambda from intraday bar sequence.
    Returns lambda value and regime classification.
    """
    if len(bars) < 5:
        return _empty_lambda()

    lambdas = []

    for i in range(1, len(bars)):
        prev_close = float(bars[i-1].get("c", 0) or 0)
        curr_close = float(bars[i].get("c", 0) or 0)

        if not prev_close or not curr_close:
            continue

        dp = curr_close - prev_close
        sv = estimate_signed_volume(bars[i], prev_close)

        if abs(sv) < 1000:   # skip tiny volume bars
            continue

        lam = dp / sv * 1e6  # scale: price_change per million shares
        lambdas.append(float(lam))

    if len(lambdas) < 3:
        return _empty_lambda()

    arr         = np.array(lambdas)
    lam_current = float(np.median(arr[-5:]))   # recent lambda
    lam_mean    = float(np.mean(arr))
    lam_std     = float(np.std(arr))

    if lam_std < 1e-10:
        z_score = 0.0
    else:
        z_score = (abs(lam_current) - abs(lam_mean)) / lam_std

    # Regime classification
    abs_lam = abs(lam_current)

    if z_score > 2.0:
        regime       = "EXTREME"
        label        = "🚨 Extreme Illiquidity — Lambda 2σ+"
        size_mult    = 0.5
        threshold_adj = +10
        skip_moc     = True
        color        = "FF2D55"
    elif z_score > 1.0:
        regime        = "HIGH"
        label         = "⚠️ High Illiquidity — Lambda 1-2σ"
        size_mult     = 0.75
        threshold_adj = +5
        skip_moc      = False
        color         = "FF9500"
    elif z_score < -0.5:
        regime        = "LOW"
        label         = "💧 High Liquidity — Tight spreads"
        size_mult     = 1.1
        threshold_adj = -3
        skip_moc      = False
        color         = "00D4FF"
    else:
        regime        = "NORMAL"
        label         = "✅ Normal Liquidity"
        size_mult     = 1.0
        threshold_adj = 0
        skip_moc      = False
        color         = "00FF9D"

    return {
        "lambda":         round(lam_current, 6),
        "lambda_mean":    round(lam_mean, 6),
        "lambda_std":     round(lam_std, 6),
        "z_score":        round(z_score, 3),
        "regime":         regime,
        "label":          label,
        "size_mult":      size_mult,
        "threshold_adj":  threshold_adj,
        "skip_moc":       skip_moc,
        "color":          color,
        "bars_used":      len(lambdas),
    }


def _empty_lambda() -> dict:
    return {
        "lambda": 0, "lambda_mean": 0, "lambda_std": 0, "z_score": 0,
        "regime": "NORMAL", "label": "Lambda unavailable",
        "size_mult": 1.0, "threshold_adj": 0,
        "skip_moc": False, "color": "888888", "bars_used": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch_intraday_bars(ticker: str, interval_min: int = 5,
                        bars: int = 40) -> list:
    """Fetch recent 5-min bars for lambda computation."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=3)).isoformat()

        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/"
            f"{interval_min}/minute/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 200},
            timeout=12
        )
        results = r.json().get("results", [])
        return results[-bars:]   # most recent bars

    except Exception as e:
        log.warning(f"Lambda fetch {ticker}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_lambda() -> dict:
    """
    Compute Kyle's Lambda for all tickers.
    Runs every 5 min during market hours.
    """
    result = {
        "date":     date.today().isoformat(),
        "computed": datetime.now().strftime("%H:%M ET"),
        "tickers":  {},
        "market":   {},
    }

    for ticker in TICKERS:
        bars = fetch_intraday_bars(ticker, interval_min=5, bars=40)
        lam  = compute_kyle_lambda(bars)
        lam.update({"ticker": ticker})
        result["tickers"][ticker] = lam

        log.info(f"  Lambda {ticker}: {lam['regime']:8s} "
                 f"λ={lam['lambda']:+.4f} z={lam['z_score']:+.2f} "
                 f"size={lam['size_mult']}x {lam['label']}")

    # Market-wide: use SPY as primary
    spy = result["tickers"].get("SPY", _empty_lambda())
    result["market"] = {
        "regime":        spy.get("regime", "NORMAL"),
        "size_mult":     spy.get("size_mult", 1.0),
        "threshold_adj": spy.get("threshold_adj", 0),
        "skip_moc":      spy.get("skip_moc", False),
        "label":         spy.get("label", ""),
        "z_score":       spy.get("z_score", 0),
    }

    LAMBDA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(LAMBDA_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_lambda_cache(ticker: str = "SPY") -> dict:
    """Load cached lambda. Recomputes if > 10 min old."""
    try:
        if LAMBDA_CACHE.exists():
            import time
            age = time.time() - LAMBDA_CACHE.stat().st_mtime
            if age < 600:
                with open(LAMBDA_CACHE) as f:
                    data = json.load(f)
                t = data.get("tickers", {}).get(ticker)
                if t:
                    return t
    except Exception:
        pass
    result = compute_and_cache_lambda()
    return result.get("tickers", {}).get(ticker, _empty_lambda())


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_lambda_arka_params(ticker: str = "SPY") -> dict:
    """ARKA execution gate — size_mult and threshold_adj."""
    lam = load_lambda_cache(ticker)
    return {
        "size_mult":     lam.get("size_mult", 1.0),
        "threshold_adj": lam.get("threshold_adj", 0),
        "regime":        lam.get("regime", "NORMAL"),
        "label":         lam.get("label", ""),
        "z_score":       lam.get("z_score", 0),
    }


def should_skip_moc_lambda(ticker: str = "SPY") -> bool:
    """Returns True if MOC should be skipped due to extreme illiquidity."""
    return load_lambda_cache(ticker).get("skip_moc", False)


def get_lambda_dashboard_data(ticker: str = "SPY") -> dict:
    """Dashboard widget data — liquidity gauge."""
    lam = load_lambda_cache(ticker)
    return {
        "lambda":    lam.get("lambda", 0),
        "z_score":   lam.get("z_score", 0),
        "regime":    lam.get("regime", "NORMAL"),
        "label":     lam.get("label", ""),
        "color":     lam.get("color", "888888"),
        "size_mult": lam.get("size_mult", 1.0),
    }


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_lambda()
    print(f"\n── Kyle's Lambda ({result['computed']}) ──────────────────────────")
    for ticker, lam in result.get("tickers", {}).items():
        print(f"  {ticker:5s}  [{lam['regime']:8s}]  "
              f"λ={lam['lambda']:+.4f}  z={lam['z_score']:+.2f}  "
              f"size={lam['size_mult']}x  thr={lam['threshold_adj']:+d}  "
              f"{lam['label']}")
    mkt = result.get("market", {})
    print(f"\n  Market: [{mkt.get('regime')}] "
          f"skip_moc={mkt.get('skip_moc')} "
          f"size={mkt.get('size_mult')}x")
