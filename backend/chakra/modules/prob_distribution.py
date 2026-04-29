"""
CHAKRA — Options Probability Distribution Engine
backend/chakra/modules/prob_distribution.py

The options chain encodes the market's TRUE probability estimate of where
price will be at expiry. By inverting the Black-Scholes formula across
all strikes, we can extract the risk-neutral probability density function
(the "options-implied PDF").

This PDF tells us:
  - Expected move range (1σ and 2σ)
  - Probability of price being above/below key levels (GEX walls, VWAP, etc.)
  - Tail risk: probability of a >3% move vs historical base rate

Signals:
  - High tail probability (>25% chance of >2% move) → VOLATILE_EXPECTED
  - Skewed distribution (mean ≠ spot) → DIRECTIONAL_BIAS_UP / DOWN
  - Narrow distribution (low σ) → PINNED (gamma pinning near expiry)

Integration:
  - GEX Tab     → expected move overlay on strikes
  - Daily Briefing → "Market pricing X% move today"
  - ARKA size   → shrink if tail risk elevated
  - MOC Engine  → expected move as stop reference
"""

import json
import logging
import math
import numpy as np
import httpx
import os
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv
from scipy import stats

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log         = logging.getLogger("chakra.probdist")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
PROB_CACHE  = BASE / "logs" / "chakra" / "probdist_latest.json"

TICKERS = ["SPY", "QQQ", "IWM"]


# ══════════════════════════════════════════════════════════════════════
# BLACK-SCHOLES UTILITIES
# ══════════════════════════════════════════════════════════════════════

def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call price."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1  = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2  = d1 - sigma * math.sqrt(T)
    return S * stats.norm.cdf(d1) - K * math.exp(-r * T) * stats.norm.cdf(d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes put price."""
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    d1  = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2  = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)


def implied_vol_from_price(opt_price: float, S: float, K: float,
                            T: float, r: float, is_call: bool,
                            tol: float = 1e-5, max_iter: int = 100) -> float:
    """
    Newton-Raphson IV solver from option price.
    Returns IV as decimal or 0.20 if fails.
    """
    if T <= 0 or opt_price <= 0:
        return 0.20

    sigma = 0.20   # initial guess
    for _ in range(max_iter):
        price_fn = bs_call_price if is_call else bs_put_price
        price = price_fn(S, K, T, r, sigma)
        diff  = price - opt_price

        # Vega for Newton step
        d1    = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega  = S * stats.norm.pdf(d1) * math.sqrt(T)

        if abs(vega) < 1e-8:
            break
        sigma -= diff / vega
        sigma  = max(0.01, min(sigma, 5.0))   # bound to 1%-500%
        if abs(diff) < tol:
            break

    return round(sigma, 4)


# ══════════════════════════════════════════════════════════════════════
# RISK-NEUTRAL PDF EXTRACTION
# ══════════════════════════════════════════════════════════════════════

def extract_implied_pdf(strikes: list, call_ivs: list,
                        spot: float, T: float, r: float = 0.05) -> dict:
    """
    Extract risk-neutral PDF from implied volatility smile.
    Uses Breeden-Litzenberger: PDF(K) = e^(rT) * d²C/dK²

    strikes: sorted list of strikes
    call_ivs: corresponding call IVs
    spot: current price
    T: time to expiry (years)
    """
    if len(strikes) < 5:
        return _empty_pdf(spot)

    strikes  = np.array(strikes, dtype=float)
    call_ivs = np.array(call_ivs, dtype=float)

    # Compute call prices from IVs at each strike
    call_prices = np.array([
        bs_call_price(spot, K, T, r, iv)
        for K, iv in zip(strikes, call_ivs)
    ])

    # Second derivative d²C/dK² using finite differences
    dK  = np.diff(strikes)
    d2C = np.zeros(len(strikes))
    for i in range(1, len(strikes) - 1):
        dKl = strikes[i] - strikes[i-1]
        dKr = strikes[i+1] - strikes[i]
        d2C[i] = 2 * (call_prices[i+1] / dKr - call_prices[i] * (1/dKl + 1/dKr)
                      + call_prices[i-1] / dKl) / (dKl + dKr)

    # Risk-neutral PDF
    pdf = math.exp(r * T) * d2C
    pdf = np.maximum(pdf, 0)   # PDF must be non-negative

    # Normalize
    total = np.trapz(pdf, strikes)
    if total > 0:
        pdf = pdf / total

    # Statistics from PDF
    mean_price = float(np.trapz(strikes * pdf, strikes))
    var_price  = float(np.trapz((strikes - mean_price)**2 * pdf, strikes))
    std_price  = float(math.sqrt(max(0, var_price)))

    # Expected move (1σ range)
    exp_move_pct = std_price / spot * 100

    # Probability above/below current price
    idx_atm = np.searchsorted(strikes, spot)
    prob_up  = float(np.trapz(pdf[idx_atm:], strikes[idx_atm:])) if idx_atm < len(pdf) else 0.5
    prob_dn  = 1.0 - prob_up

    # Tail probabilities (>2% move either direction)
    idx_up2 = np.searchsorted(strikes, spot * 1.02)
    idx_dn2 = np.searchsorted(strikes, spot * 0.98)
    tail_up = float(np.trapz(pdf[idx_up2:], strikes[idx_up2:])) if idx_up2 < len(pdf) else 0.10
    tail_dn = float(np.trapz(pdf[:idx_dn2], strikes[:idx_dn2])) if idx_dn2 > 0 else 0.10

    return {
        "mean_price":    round(mean_price, 2),
        "std_price":     round(std_price, 2),
        "exp_move_pct":  round(exp_move_pct, 2),
        "prob_up":       round(prob_up, 4),
        "prob_dn":       round(prob_dn, 4),
        "tail_up":       round(tail_up, 4),
        "tail_dn":       round(tail_dn, 4),
        "tail_total":    round(tail_up + tail_dn, 4),
        "pdf_strikes":   strikes.tolist(),
        "pdf_values":    pdf.tolist(),
        "strikes_used":  len(strikes),
        "T_years":       round(T, 4),
    }


def _empty_pdf(spot: float = 0) -> dict:
    return {
        "mean_price": spot, "std_price": 0, "exp_move_pct": 0,
        "prob_up": 0.5, "prob_dn": 0.5,
        "tail_up": 0.10, "tail_dn": 0.10, "tail_total": 0.20,
        "pdf_strikes": [], "pdf_values": [], "strikes_used": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# SIGNALS FROM PDF
# ══════════════════════════════════════════════════════════════════════

def classify_distribution(pdf_data: dict, spot: float) -> dict:
    """Classify market state from extracted PDF."""
    exp_move  = pdf_data.get("exp_move_pct", 0)
    tail_tot  = pdf_data.get("tail_total", 0.20)
    prob_up   = pdf_data.get("prob_up", 0.5)
    mean_px   = pdf_data.get("mean_price", spot)

    # Expected move classification
    if exp_move < 0.5:
        move_label = "PINNED"
        move_desc  = f"🔒 Gamma pinned — market pricing only {exp_move:.1f}% move"
        color      = "00D4FF"
        size_adj   = 1.1    # can trade larger in pinned market
    elif exp_move > 2.0:
        move_label = "VOLATILE_EXPECTED"
        move_desc  = f"⚡ Volatile — market pricing {exp_move:.1f}% expected move"
        color      = "FF2D55"
        size_adj   = 0.75   # reduce size in high expected vol
    else:
        move_label = "NORMAL_MOVE"
        move_desc  = f"📊 Normal — {exp_move:.1f}% expected move"
        color      = "00FF9D"
        size_adj   = 1.0

    # Directional bias from distribution mean
    mean_bias_pct = (mean_px - spot) / spot * 100
    if mean_bias_pct > 0.15:
        dir_bias = "BULLISH_BIAS"
        dir_desc = f"↑ Market pricing {mean_bias_pct:+.2f}% upward drift"
    elif mean_bias_pct < -0.15:
        dir_bias = "BEARISH_BIAS"
        dir_desc = f"↓ Market pricing {mean_bias_pct:+.2f}% downward drift"
    else:
        dir_bias = "NEUTRAL_BIAS"
        dir_desc = "→ Distribution centered near spot"

    # Tail risk
    if tail_tot > 0.35:
        tail_label = "EXTREME_TAILS"
        tail_desc  = f"⚠️ Fat tails — {tail_tot*100:.0f}% probability of >2% move"
    elif tail_tot > 0.20:
        tail_label = "ELEVATED_TAILS"
        tail_desc  = f"Elevated tail risk — {tail_tot*100:.0f}% chance of >2% move"
    else:
        tail_label = "NORMAL_TAILS"
        tail_desc  = f"Normal tail risk — {tail_tot*100:.0f}% chance of >2% move"

    return {
        "move_label":   move_label,
        "move_desc":    move_desc,
        "dir_bias":     dir_bias,
        "dir_desc":     dir_desc,
        "tail_label":   tail_label,
        "tail_desc":    tail_desc,
        "exp_move_pct": exp_move,
        "size_adj":     size_adj,
        "color":        color,
        "one_sigma_low":  round(spot * (1 - exp_move/100), 2),
        "one_sigma_high": round(spot * (1 + exp_move/100), 2),
    }


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ══════════════════════════════════════════════════════════════════════

def fetch_chain_for_pdf(ticker: str, spot: float,
                         exp_date: str = None) -> tuple[list, list, float]:
    """
    Fetch options chain for PDF extraction.
    Returns (strikes, ivs, T_years)
    Uses 0DTE for intraday or next weekly for daily PDF.
    """
    today = date.today()
    if exp_date is None:
        # Use today's expiry for 0DTE intraday PDF
        exp_date = today.isoformat()

    exp_dt  = date.fromisoformat(exp_date)
    T_days  = (exp_dt - today).days
    T_years = max(T_days / 252, 1/252)   # min 1 trading day

    # Fetch chain ±8% around spot for good coverage
    low  = round(spot * 0.92)
    high = round(spot * 1.08)

    try:
        r = httpx.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={
                "apiKey":            POLYGON_KEY,
                "limit":             250,
                "expiration_date":   exp_date,
                "contract_type":     "call",
                "strike_price.gte":  low,
                "strike_price.lte":  high,
            },
            timeout=12
        )
        contracts = r.json().get("results", [])

        strikes = []
        ivs     = []

        for c in contracts:
            greeks = c.get("greeks", {})
            if not greeks or not greeks.get("delta"):
                continue
            details = c.get("details", {})
            strike  = float(details.get("strike_price", 0))
            if not strike:
                continue

            # Get IV from vega + Newton-Raphson
            vega     = greeks.get("vega", 0) or 0
            lq       = c.get("last_quote", {})
            midpoint = lq.get("midpoint", 0) or ((lq.get("bid", 0) + lq.get("ask", 0)) / 2)

            if midpoint > 0:
                iv = implied_vol_from_price(midpoint, spot, strike, T_years,
                                             0.05, is_call=True)
            elif vega > 0:
                # Fallback: estimate from vega
                iv = max(0.10, abs(vega) * 10)
            else:
                continue

            if 0.05 <= iv <= 3.0:
                strikes.append(strike)
                ivs.append(iv)

        # Sort by strike
        if strikes:
            paired  = sorted(zip(strikes, ivs))
            strikes = [p[0] for p in paired]
            ivs     = [p[1] for p in paired]

        return strikes, ivs, T_years

    except Exception as e:
        log.warning(f"PDF chain fetch {ticker}: {e}")
        return [], [], T_years


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

def compute_and_cache_probdist() -> dict:
    """
    Compute options-implied probability distribution.
    Run at 8:30 AM and every 30 min.
    """
    result = {
        "date":     date.today().isoformat(),
        "computed": datetime.now().strftime("%H:%M ET"),
        "tickers":  {},
    }

    for ticker in TICKERS:
        spot = get_spot(ticker)
        if not spot:
            result["tickers"][ticker] = {**_empty_pdf(), **{"ticker": ticker}}
            continue

        strikes, ivs, T = fetch_chain_for_pdf(ticker, spot)

        if len(strikes) < 5:
            log.warning(f"  PDF {ticker}: insufficient strikes ({len(strikes)})")
            result["tickers"][ticker] = {
                **_empty_pdf(spot), "ticker": ticker, "spot": spot
            }
            continue

        pdf_data  = extract_implied_pdf(strikes, ivs, spot, T)
        signal    = classify_distribution(pdf_data, spot)

        entry = {
            "ticker": ticker,
            "spot":   spot,
            **pdf_data,
            **signal,
        }
        result["tickers"][ticker] = entry

        log.info(f"  PDF {ticker}: exp_move={pdf_data['exp_move_pct']:.2f}% "
                 f"prob_up={pdf_data['prob_up']:.1%} "
                 f"tail={pdf_data['tail_total']:.1%} "
                 f"[{signal['move_label']}] {signal['dir_bias']}")

    PROB_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROB_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_probdist_cache(ticker: str = "SPY") -> dict:
    """Load cached probability distribution."""
    try:
        if PROB_CACHE.exists():
            import time
            age = time.time() - PROB_CACHE.stat().st_mtime
            if age < 1800:
                with open(PROB_CACHE) as f:
                    data = json.load(f)
                t = data.get("tickers", {}).get(ticker)
                if t:
                    return t
    except Exception:
        pass
    result = compute_and_cache_probdist()
    return result.get("tickers", {}).get(ticker, _empty_pdf())


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_expected_move(ticker: str = "SPY") -> dict:
    """ARKA + MOC: expected move range and size adjustment."""
    pd  = load_probdist_cache(ticker)
    return {
        "exp_move_pct":   pd.get("exp_move_pct", 1.0),
        "one_sigma_low":  pd.get("one_sigma_low", 0),
        "one_sigma_high": pd.get("one_sigma_high", 0),
        "size_adj":       pd.get("size_adj", 1.0),
        "move_label":     pd.get("move_label", "NORMAL_MOVE"),
        "prob_up":        pd.get("prob_up", 0.5),
    }


def get_pdf_for_gex_chart(ticker: str = "SPY") -> dict:
    """GEX tab overlay — PDF curve data for chart."""
    pd = load_probdist_cache(ticker)
    return {
        "strikes":        pd.get("pdf_strikes", []),
        "probabilities":  pd.get("pdf_values", []),
        "one_sigma_low":  pd.get("one_sigma_low", 0),
        "one_sigma_high": pd.get("one_sigma_high", 0),
        "exp_move_pct":   pd.get("exp_move_pct", 0),
        "dir_bias":       pd.get("dir_bias", "NEUTRAL_BIAS"),
    }


def get_prob_briefing_line(ticker: str = "SPY") -> str:
    """One-liner for Daily Briefing."""
    pd = load_probdist_cache(ticker)
    return (f"Expected move: ±{pd.get('exp_move_pct', 0):.1f}%  |  "
            f"Prob up: {pd.get('prob_up', 0.5):.0%}  |  "
            f"Tail risk: {pd.get('tail_total', 0):.0%}  |  "
            f"{pd.get('move_desc', '')}")


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_probdist()
    print(f"\n── Options Probability Distribution ({result['computed']}) ──────")
    for ticker, pd in result.get("tickers", {}).items():
        if pd.get("strikes_used", 0) == 0:
            print(f"  {ticker}: no data")
            continue
        print(f"  {ticker:5s}  "
              f"E[move]=±{pd.get('exp_move_pct', 0):.2f}%  "
              f"P(up)={pd.get('prob_up', 0):.1%}  "
              f"tail={pd.get('tail_total', 0):.1%}  "
              f"1σ=[{pd.get('one_sigma_low', 0):.2f}, {pd.get('one_sigma_high', 0):.2f}]")
        print(f"         {pd.get('move_desc', '')}  |  {pd.get('dir_desc', '')}")
