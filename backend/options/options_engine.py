"""
CHAKRA Options Engine
======================
Powered by Polygon Options Advanced.
Provides:
  1. GEX Walls — gamma exposure call/put walls
  2. Magnet Levels — EQH/EQL, BSL/SSL from open interest
  3. 0DTE Ticker Licker — best same-day options plays
  4. Opening Bell Prep — live-refreshed 9:25am game plan

Run manually:
    cd ~/trading-ai
    python3 backend/options/options_engine.py

Outputs:
    logs/options/gex_YYYY-MM-DD.json        — GEX walls per ticker
    logs/options/ticker_licker_HH-MM.json   — 0DTE plays (updates every 5min)
    logs/options/bell_prep_YYYY-MM-DD.json  — 9:25am opening bell card
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
LOG_DIR  = BASE_DIR / "logs/options"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_DIR / f"options_{date.today()}.log")),
    ]
)
log = logging.getLogger("CHAKRA.Options")

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY  = os.getenv("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"

# Tickers for 0DTE scanner
TICKER_LICKER_UNIVERSE = [
    "SPY", "QQQ", "IWM", "SPX", "DIA",          # indices
    "AAPL", "NVDA", "TSLA", "AMZN", "META",      # mega cap
    "MSFT", "GOOGL", "AMD", "JPM", "COIN",       # extended
    "GLD", "SLV", "TLT",                          # commodities/bonds
]

# GEX tickers (indices only for now)
GEX_TICKERS = ["SPY", "QQQ", "IWM", "SPX", "RUT"]

# Min premium for whale-level options ($50K+)
MIN_PREMIUM_LICKER = 5000    # $5K min for 0DTE plays
MIN_CONFIDENCE     = 60      # min confidence score to show


# ── Polygon options fetchers ──────────────────────────────────────────────────

async def fetch_options_chain(ticker: str, dte_max: int = 1) -> list[dict]:
    """Fetch options chain for a ticker — 0DTE or near-term."""
    today     = date.today()
    exp_limit = today + timedelta(days=dte_max)
    url       = f"{POLYGON_BASE}/v3/snapshot/options/{ticker}"
    params    = {
        "apiKey":           POLYGON_KEY,
        "expiration_date":  today.isoformat(),
        "limit":            250,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r    = await client.get(url, params=params)
            data = r.json()
        results = data.get("results", [])
        log.info(f"  {ticker}: {len(results)} 0DTE contracts fetched")
        return results
    except Exception as e:
        log.error(f"  {ticker} options chain error: {e}")
        return []


async def fetch_current_price(ticker: str) -> float | None:
    """Fetch price using prev-day close — works on all Polygon plans."""
    # Polygon uses I:SPX / I:RUT format for indices
    _INDEX_MAP = {"SPX": "I:SPX", "RUT": "I:RUT", "VIX": "I:VIX"}
    poly_ticker = _INDEX_MAP.get(ticker.upper(), ticker)
    url    = f"{POLYGON_BASE}/v2/aggs/ticker/{poly_ticker}/prev"
    params = {"apiKey": POLYGON_KEY, "adjusted": "true"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r    = await client.get(url, params=params)
            data = r.json()
        results = data.get("results", [])
        return float(results[0].get("c", 0)) if results else None
    except:
        return None

async def fetch_options_snapshot_all(ticker: str) -> list[dict]:
    """Fetch full options snapshot with Greeks for GEX calculation."""
    url    = f"{POLYGON_BASE}/v3/snapshot/options/{ticker}"
    params = {"apiKey": POLYGON_KEY, "limit": 250, "expiration_date": date.today().isoformat()}
    all_results = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                r    = await client.get(url, params=params)
                data = r.json()
                results = data.get("results", [])
                all_results.extend(results)
                cursor = data.get("next_url")
                if not cursor or len(all_results) >= 1000:
                    break
                params = {"apiKey": POLYGON_KEY, "cursor": cursor.split("cursor=")[-1]}
        log.info(f"  {ticker}: {len(all_results)} total contracts for GEX")
        return all_results
    except Exception as e:
        log.error(f"  {ticker} snapshot error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  1. GEX WALLS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_gex(contracts: list[dict], spot_price: float) -> dict:
    """
    Calculate Gamma Exposure (GEX) per strike.
    GEX = Gamma × OI × 100 × spot²× 0.01
    Positive GEX = dealers long gamma (price pinned)
    Negative GEX = dealers short gamma (price explosive)
    """
    strikes = {}

    for c in contracts:
        details = c.get("details", {})
        greeks  = c.get("greeks", {})
        day     = c.get("day", {})

        strike     = details.get("strike_price")
        opt_type   = details.get("contract_type", "").lower()
        gamma      = greeks.get("gamma", 0) or 0
        oi         = c.get("open_interest", 0) or 0

        if not strike or not gamma or not oi:
            continue

        # GEX formula
        gex = gamma * oi * 100 * (spot_price ** 2) * 0.01
        if opt_type == "put":
            gex = -gex  # puts create negative GEX

        strike = round(strike, 1)
        if strike not in strikes:
            strikes[strike] = {"strike": strike, "call_gex": 0, "put_gex": 0, "net_gex": 0, "oi": 0}

        if opt_type == "call":
            strikes[strike]["call_gex"] += gex
        else:
            strikes[strike]["put_gex"]  += gex
        strikes[strike]["net_gex"] += gex
        strikes[strike]["oi"]      += oi

    if not strikes:
        return {"call_wall": None, "put_wall": None, "zero_gamma": None, "strikes": []}

    df = pd.DataFrame(list(strikes.values())).sort_values("strike")

    # Call wall = strike with highest positive GEX above spot
    above = df[df["strike"] > spot_price]
    call_wall = float(above.loc[above["call_gex"].idxmax(), "strike"]) if len(above) > 0 else None

    # Put wall = strike with most negative GEX below spot
    below = df[df["strike"] < spot_price]
    put_wall = float(below.loc[below["put_gex"].idxmin(), "strike"]) if len(below) > 0 else None

    # Zero gamma line = where net GEX crosses zero
    df["cumulative_gex"] = df["net_gex"].cumsum()
    zero_cross = df[df["cumulative_gex"].diff().abs() > 0]
    zero_gamma = float(df.iloc[(df["net_gex"].abs()).idxmin()]["strike"]) if len(df) > 0 else None

    # Top 10 strikes by absolute GEX for chart
    top_strikes = df.nlargest(10, "net_gex")[["strike","call_gex","put_gex","net_gex","oi"]].to_dict("records")

    return {
        "call_wall":    call_wall,
        "put_wall":     put_wall,
        "zero_gamma":   zero_gamma,
        "spot":         spot_price,
        "top_strikes":  top_strikes,
        "total_call_gex": float(df["call_gex"].sum()),
        "total_put_gex":  float(df["put_gex"].sum()),
        "net_gex":        float(df["net_gex"].sum()),
        "regime":         "pinned" if df["net_gex"].sum() > 0 else "explosive",
    }


def calculate_magnet_levels(contracts: list[dict], spot_price: float) -> dict:
    """
    Find BSL/SSL magnet levels from open interest clustering.
    High OI strikes = liquidity pools that price is attracted to.
    """
    oi_by_strike = {}

    for c in contracts:
        details  = c.get("details", {})
        strike   = details.get("strike_price")
        opt_type = details.get("contract_type", "").lower()
        oi       = c.get("open_interest", 0) or 0

        if not strike or not oi:
            continue

        strike = round(strike, 1)
        if strike not in oi_by_strike:
            oi_by_strike[strike] = {"strike": strike, "call_oi": 0, "put_oi": 0, "total_oi": 0}

        if opt_type == "call":
            oi_by_strike[strike]["call_oi"] += oi
        else:
            oi_by_strike[strike]["put_oi"]  += oi
        oi_by_strike[strike]["total_oi"] += oi

    if not oi_by_strike:
        return {}

    df = pd.DataFrame(list(oi_by_strike.values())).sort_values("strike")

    # BSL = highest call OI above spot (buy-side liquidity)
    above = df[df["strike"] > spot_price].nlargest(3, "call_oi")
    bsl_levels = above["strike"].tolist()

    # SSL = highest put OI below spot (sell-side liquidity)
    below = df[df["strike"] < spot_price].nlargest(3, "put_oi")
    ssl_levels = below["strike"].tolist()

    # Max pain = strike where total OI loss is minimized
    max_pain_strike = None
    min_pain = float("inf")
    for s in df["strike"]:
        pain = 0
        for _, row in df.iterrows():
            if row["strike"] < s:
                pain += row["call_oi"] * (s - row["strike"])
            else:
                pain += row["put_oi"] * (row["strike"] - s)
        if pain < min_pain:
            min_pain     = pain
            max_pain_strike = s

    # Equal highs/lows (EQH/EQL) — strikes with similar OI on both sides
    df["oi_ratio"] = df["call_oi"] / (df["put_oi"] + 1)
    balanced = df[(df["oi_ratio"].between(0.8, 1.2)) & (df["total_oi"] > df["total_oi"].quantile(0.7))]
    eqh = balanced[balanced["strike"] > spot_price]["strike"].tolist()[:2]
    eql = balanced[balanced["strike"] < spot_price]["strike"].tolist()[-2:]

    return {
        "bsl":       [round(x, 2) for x in sorted(bsl_levels, reverse=True)],
        "ssl":       [round(x, 2) for x in sorted(ssl_levels, reverse=True)],
        "max_pain":  round(max_pain_strike, 2) if max_pain_strike else None,
        "eqh":       [round(x, 2) for x in eqh],
        "eql":       [round(x, 2) for x in eql],
    }


async def run_gex_analysis(tickers: list[str] = None) -> dict:
    """Run full GEX + magnet analysis for all GEX tickers."""
    tickers = tickers or GEX_TICKERS
    results = {}

    for ticker in tickers:
        log.info(f"  GEX: analyzing {ticker}...")
        spot      = await fetch_current_price(ticker)
        contracts = await fetch_options_snapshot_all(ticker)

        if not spot or not contracts:
            log.warning(f"  {ticker}: no data for GEX")
            continue

        gex     = calculate_gex(contracts, spot)
        magnets = calculate_magnet_levels(contracts, spot)

        results[ticker] = {
            "ticker":  ticker,
            "spot":    spot,
            "gex":     gex,
            "magnets": magnets,
            "updated": datetime.now(ET).strftime("%I:%M %p ET"),
        }

        log.info(f"  {ticker}: call_wall=${gex.get('call_wall')} put_wall=${gex.get('put_wall')} regime={gex.get('regime')}")

        # ── Write gex_latest_{ticker}.json for ARKA load_gex_state() ──────────
        # Translates options_engine vocabulary → gex_calculator schema so ARKA
        # gets a fresh regime every 5 min instead of relying on hourly ARJUN runs.
        _raw_regime = (gex.get("regime") or "").lower()
        if _raw_regime in ("pinned", "positive"):
            _arka_regime = "POSITIVE_GAMMA"
        elif _raw_regime in ("explosive", "negative", "trending"):
            _arka_regime = "NEGATIVE_GAMMA"
        else:
            _arka_regime = "UNKNOWN"

        _spot       = float(gex.get("spot") or spot or 0)
        _zero_gamma = float(gex.get("zero_gamma") or _spot)
        _call_wall  = float(gex.get("call_wall")  or 0)
        _put_wall   = float(gex.get("put_wall")   or 0)
        _net_gex    = float(gex.get("net_gex")    or 0)

        import time as _time
        _gex_state = {
            "ticker":           ticker,
            "spot":             _spot,
            "regime":           _arka_regime,
            "regime_call":      "SHORT_THE_POPS" if _arka_regime == "POSITIVE_GAMMA"
                                else "FOLLOW_MOMENTUM" if _arka_regime == "NEGATIVE_GAMMA"
                                else "NEUTRAL",
            "zero_gamma":       _zero_gamma,
            "call_wall":        _call_wall,
            "put_wall":         _put_wall,
            "net_gex":          _net_gex,
            "above_zero_gamma": _spot > _zero_gamma,
            "call_gex_dollars": float(gex.get("total_call_gex") or 0),
            "put_gex_dollars":  abs(float(gex.get("total_put_gex") or 0)),
            "bias_ratio":       1.0,
            "dominant_side":    "CALL" if _net_gex > 0 else "PUT" if _net_gex < 0 else "NEUTRAL",
            "accel_up":         0.0,
            "accel_down":       0.0,
            "expected_move_pts":0.0,
            "upper_1sd":        round(_spot * 1.01, 2),
            "lower_1sd":        round(_spot * 0.99, 2),
            "pin_strikes":      [],
            "cliff":            {"expires_today": False, "strike": None},
            "top_strikes":      gex.get("top_strikes", []),
            "ts":               _time.time(),
        }
        try:
            _gex_dir  = BASE_DIR / "logs/gex"
            _gex_dir.mkdir(parents=True, exist_ok=True)
            _gex_path = _gex_dir / f"gex_latest_{ticker.upper()}.json"
            _gex_path.write_text(json.dumps(_gex_state, indent=2))
            log.info(f"  💾 gex_latest_{ticker}.json → regime={_arka_regime} "
                     f"call_wall=${_call_wall} put_wall=${_put_wall} zero_gamma=${_zero_gamma}")
        except Exception as _we:
            log.warning(f"  ⚠️  Failed to write gex_latest_{ticker}.json: {_we}")

        await asyncio.sleep(2.0)

    # Save
    out = {"date": date.today().isoformat(), "generated": datetime.now(ET).strftime("%I:%M %p ET"), "tickers": results}
    path = LOG_DIR / f"gex_{date.today()}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    log.info(f"  💾 GEX saved → {path}")

    return out


# ══════════════════════════════════════════════════════════════════════════════
#  2. 0DTE TICKER LICKER
# ══════════════════════════════════════════════════════════════════════════════

def score_0dte_contract(contract: dict, spot: float, bias: str = "NEUTRAL") -> dict | None:
    """
    Score a 0DTE contract for the Ticker Licker scanner.
    Returns scored dict or None if contract doesn't qualify.
    """
    details = contract.get("details", {})
    greeks  = contract.get("greeks", {})
    day     = contract.get("day", {})
    quote   = contract.get("last_quote", {})

    strike    = details.get("strike_price")
    opt_type  = details.get("contract_type", "").lower()
    gamma     = greeks.get("gamma", 0) or 0
    delta     = greeks.get("delta", 0) or 0
    theta     = greeks.get("theta", 0) or 0
    iv        = greeks.get("implied_volatility", 0) or 0
    volume    = day.get("volume", 0) or 0
    oi        = contract.get("open_interest", 0) or 0

    ask       = quote.get("ask", 0) or 0
    bid       = quote.get("bid", 0) or 0
    mid       = (ask + bid) / 2 if ask and bid else 0

    if not strike or not mid or mid < 0.05:
        return None

    premium = mid * 100  # per contract dollar value
    if premium < MIN_PREMIUM_LICKER:
        return None

    # % from strike to spot
    pct_from_strike = (spot - strike) / spot * 100

    # Vol/OI ratio (suspicion indicator)
    vol_oi = volume / (oi + 1)

    # Confidence score
    conf = 50
    reasons = []

    # Gamma — higher = more explosive
    if gamma > 0.05:   conf += 15; reasons.append(f"high gamma {gamma:.3f}")
    elif gamma > 0.02: conf += 8

    # Delta alignment with bias
    if opt_type == "call":
        if bias == "BULLISH": conf += 10; reasons.append("aligned with bullish bias")
        elif bias == "BEARISH": conf -= 10
        if 0.3 <= abs(delta) <= 0.7: conf += 8; reasons.append("optimal delta range")
    else:  # put
        if bias == "BEARISH": conf += 10; reasons.append("aligned with bearish bias")
        elif bias == "BULLISH": conf -= 10
        if 0.3 <= abs(delta) <= 0.7: conf += 8; reasons.append("optimal delta range")

    # Volume activity
    if vol_oi > 5:  conf += 10; reasons.append(f"vol/OI {vol_oi:.1f}x")
    elif vol_oi > 2: conf += 5
    if volume > 1000: conf += 5; reasons.append(f"active volume {volume:,}")

    # Price proximity to strike
    if abs(pct_from_strike) < 0.5:   conf += 10; reasons.append("ATM — max gamma")
    elif abs(pct_from_strike) < 1.5: conf += 5
    elif abs(pct_from_strike) > 3:   conf -= 15  # too far OTM

    # IV check — not too high (decay risk)
    if iv > 1.0:   conf -= 10  # >100% IV is dangerous for 0DTE
    elif iv > 0.5: conf -= 5

    conf = max(0, min(100, conf))

    if conf < MIN_CONFIDENCE:
        return None

    return {
        "ticker":           details.get("underlying_ticker", ""),
        "contract":         details.get("ticker", ""),
        "type":             opt_type.upper(),
        "strike":           strike,
        "expiry":           details.get("expiration_date", ""),
        "spot":             round(spot, 2),
        "pct_from_strike":  round(pct_from_strike, 2),
        "entry":            round(mid, 2),
        "premium_per_lot":  round(premium, 2),
        "confidence":       round(conf, 1),
        "gamma":            round(gamma, 3),
        "delta":            round(delta, 3),
        "theta":            round(theta, 3),
        "iv":               round(iv * 100, 1),
        "volume":           volume,
        "open_interest":    oi,
        "vol_oi":           round(vol_oi, 1),
        "ask_side_pct":     100,  # will enhance with trade data
        "reasons":          reasons,
        "bias_aligned":     (opt_type == "call" and bias == "BULLISH") or
                            (opt_type == "put"  and bias == "BEARISH"),
    }


async def run_ticker_licker(tickers: list[str] = None, bias_map: dict = None) -> dict:
    """
    Run the 0DTE Ticker Licker scanner across all tickers.
    bias_map: {ticker: "BULLISH"/"BEARISH"/"NEUTRAL"}
    """
    tickers   = tickers or TICKER_LICKER_UNIVERSE
    bias_map  = bias_map or {}
    all_plays = []

    for ticker in tickers:
        log.info(f"  Ticker Licker: scanning {ticker}...")
        spot      = await fetch_current_price(ticker)
        contracts = await fetch_options_chain(ticker, dte_max=0)

        if not spot or not contracts:
            log.warning(f"  {ticker}: no 0DTE data")
            await asyncio.sleep(0.3)
            continue

        bias = bias_map.get(ticker, "NEUTRAL")

        for c in contracts:
            scored = score_0dte_contract(c, spot, bias)
            if scored:
                all_plays.append(scored)

        await asyncio.sleep(0.5)

    # Sort by confidence descending
    all_plays.sort(key=lambda x: x["confidence"], reverse=True)

    # Top 15 plays
    top_plays = all_plays[:15]

    now = datetime.now(ET)
    out = {
        "date":      date.today().isoformat(),
        "time":      now.strftime("%I:%M %p ET"),
        "total":     len(all_plays),
        "plays":     top_plays,
        "calls":     [p for p in top_plays if p["type"] == "CALL"],
        "puts":      [p for p in top_plays if p["type"] == "PUT"],
    }

    path = LOG_DIR / f"ticker_licker_{now.strftime('%H-%M')}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2)

    # Also save as latest for dashboard
    latest_path = LOG_DIR / "ticker_licker_latest.json"
    with open(latest_path, "w") as f:
        json.dump(out, f, indent=2)

    log.info(f"  🎯 Ticker Licker: {len(top_plays)} plays found (from {len(all_plays)} candidates)")

    # Post top 3 to Discord
    if top_plays:
        try:
            from backend.arka.discord_notifier import post_ticker_licker
            await post_ticker_licker(out)
        except Exception as e:
            log.error(f"  Discord post failed: {e}")

    return out


# ══════════════════════════════════════════════════════════════════════════════
#  3. OPENING BELL PREP (9:25am)
# ══════════════════════════════════════════════════════════════════════════════

async def run_opening_bell_prep() -> dict:
    """
    Runs at 9:25am — refreshes pre-market game plan with live prices.
    Detects direction flips vs 8am plan and posts final bell prep card.
    """
    # ── Dedup lock — prevents duplicate Discord posts when multiple engine instances run ──
    _lock_dir  = BASE_DIR / "logs/notifications"
    _lock_dir.mkdir(parents=True, exist_ok=True)
    _lock_file = _lock_dir / f"opening_bell_{date.today()}.lock"
    if _lock_file.exists():
        try:
            _lock_data = json.loads(_lock_file.read_text())
        except Exception:
            _lock_data = {}
        log.warning(
            f"  🔒 Opening bell prep already sent today at "
            f"{_lock_data.get('sent_at', '?')} — skipping duplicate (multiple engine instances detected)"
        )
        _existing = LOG_DIR / f"bell_prep_{date.today()}.json"
        if _existing.exists():
            with open(_existing) as _f:
                return json.load(_f)
        return {}
    # Write lock first — before any Discord call — so a racing second instance sees it immediately
    _lock_file.write_text(json.dumps({
        "sent_at": datetime.now(ET).strftime("%H:%M:%S"),
        "date":    str(date.today()),
        "pid":     __import__("os").getpid(),
    }))

    log.info("  🔔 Running Opening Bell Prep (5 min to open)...")
    now = datetime.now(ET)

    # Load 8am pre-market plan
    pm_path = BASE_DIR / f"logs/premarket/premarket_{date.today()}.json"
    pm_data = {}
    if pm_path.exists():
        with open(pm_path) as f:
            pm_data = json.load(f)

    bell_prep = {
        "date":      date.today().isoformat(),
        "time":      now.strftime("%I:%M %p ET"),
        "tickers":   {},
        "bullish":   [],
        "bearish":   [],
        "flips":     [],  # direction changes from 8am plan
    }

    tickers_to_check = ["SPY", "QQQ", "IWM", "SPX", "DIA"]

    for ticker in tickers_to_check:
        spot = await fetch_current_price(ticker)
        if not spot:
            continue

        # Get 8am bias
        pm_ticker = pm_data.get("tickers", {}).get(ticker, {})
        am_bias   = pm_ticker.get("bias", {}).get("bias", "NEUTRAL")
        am_levels = pm_ticker.get("levels", {})

        # Compute gap vs previous close
        prev_close  = am_levels.get("close", spot)
        gap_pct     = round((spot - prev_close) / prev_close * 100, 2) if prev_close else 0
        gap_dir     = "up" if gap_pct > 0 else "down" if gap_pct < 0 else "flat"

        # Refresh bias with live price
        pm_high = am_levels.get("pm_high") or am_levels.get("prev_high", spot)
        pm_low  = am_levels.get("pm_low")  or am_levels.get("prev_low",  spot)
        prev_vwap = am_levels.get("prev_vwap", spot)

        # Live bias check
        if spot > pm_high:
            live_bias = "BULLISH"
            note      = f"Trading above PM high ${pm_high:.2f}"
        elif spot < pm_low:
            live_bias = "BEARISH"
            note      = f"Trading below PM low ${pm_low:.2f}"
        elif spot > prev_vwap:
            live_bias = "BULLISH"
            note      = f"Above prev VWAP ${prev_vwap:.2f}"
        else:
            live_bias = "BEARISH"
            note      = f"Below prev VWAP ${prev_vwap:.2f}"

        # Detect flip
        flipped = am_bias != live_bias and am_bias != "NEUTRAL"

        # Get GEX walls if available
        gex_path = LOG_DIR / f"gex_{date.today()}.json"
        call_wall = put_wall = None
        if gex_path.exists():
            with open(gex_path) as f:
                gex_data = json.load(f)
            ticker_gex = gex_data.get("tickers", {}).get(ticker, {})
            call_wall  = ticker_gex.get("gex", {}).get("call_wall")
            put_wall   = ticker_gex.get("gex", {}).get("put_wall")
            magnets    = ticker_gex.get("magnets", {})
        else:
            magnets = {}

        # Build game plans for bell prep
        atr = am_levels.get("atr", spot * 0.01)
        entry_call  = round(pm_low  - atr * 0.1, 2)
        target_call = round(pm_low  + atr * 1.5, 2)
        entry_put   = round(pm_high + atr * 0.1, 2)
        target_put  = round(pm_high - atr * 1.5, 2)

        ticker_result = {
            "ticker":      ticker,
            "spot":        round(spot, 2),
            "live_bias":   live_bias,
            "am_bias":     am_bias,
            "flipped":     flipped,
            "gap_pct":     gap_pct,
            "gap_dir":     gap_dir,
            "note":        note,
            "call_wall":   call_wall,
            "put_wall":    put_wall,
            "magnets":     magnets,
            "pm_high":     pm_high,
            "pm_low":      pm_low,
            "prev_vwap":   prev_vwap,
            "plans": {
                "lows_swept": {
                    "action":  "BUY CALLS",
                    "entry":   entry_call,
                    "target":  target_call,
                    "note":    f"Sell-side swept below ${pm_low:.2f} — reversal above VWAP ${prev_vwap:.2f}",
                    "expiry":  date.today().strftime("%b %d"),
                },
                "highs_swept": {
                    "action":  "BUY PUTS",
                    "entry":   entry_put,
                    "target":  target_put,
                    "note":    f"Buy-side swept above ${pm_high:.2f} — reversal below and holds",
                    "expiry":  date.today().strftime("%b %d"),
                }
            }
        }

        bell_prep["tickers"][ticker] = ticker_result

        if flipped:
            bell_prep["flips"].append(f"{ticker}: {am_bias} → {live_bias} ({note})")

        if live_bias == "BULLISH":
            bell_prep["bullish"].append(ticker)
        else:
            bell_prep["bearish"].append(ticker)

        await asyncio.sleep(0.3)

    # Save
    path = LOG_DIR / f"bell_prep_{date.today()}.json"
    with open(path, "w") as f:
        json.dump(bell_prep, f, indent=2)
    log.info(f"  💾 Bell prep saved → {path}")

    # Post to Discord
    try:
        from backend.arka.discord_notifier import post_opening_bell_prep
        await post_opening_bell_prep(bell_prep)
    except Exception as e:
        log.error(f"  Discord bell prep failed: {e}")

    return bell_prep


# ══════════════════════════════════════════════════════════════════════════════
#  CONTINUOUS RUNNER
# ══════════════════════════════════════════════════════════════════════════════

class OptionsEngine:
    def __init__(self):
        self.gex_run_today       = False
        self.bell_prep_run_today = False
        self.last_licker_run     = None
        self.last_date           = None

    def daily_reset(self):
        today = date.today()
        if self.last_date != today:
            self.gex_run_today       = False
            self.bell_prep_run_today = False
            self.last_licker_run     = None
            self.last_date           = today
            log.info(f"\n{'='*50}")
            log.info(f"  CHAKRA OPTIONS ENGINE — {today}")
            log.info(f"{'='*50}")

    async def run(self):
        log.info("  CHAKRA OPTIONS ENGINE STARTING")
        log.info(f"  Universe: {', '.join(TICKER_LICKER_UNIVERSE)}")

        while True:
            try:
                now = datetime.now(ET)
                self.daily_reset()

                # Skip weekends
                if now.weekday() >= 5:
                    await asyncio.sleep(3600)
                    continue

                h, m = now.hour, now.minute

                # 8:00am — run GEX analysis
                if h == 8 and m >= 0 and not self.gex_run_today:
                    log.info("  📊 Running morning GEX analysis...")
                    await run_gex_analysis()
                    self.gex_run_today = True

                # 9:25am — Opening Bell Prep
                elif h == 9 and m >= 25 and m < 30 and not self.bell_prep_run_today:
                    await run_opening_bell_prep()
                    self.bell_prep_run_today = True

                # 9:30am-3:45pm — Ticker Licker every 5 min
                elif (9, 30) <= (h, m) < (15, 45):
                    now_ts = now.timestamp()
                    if not self.last_licker_run or (now_ts - self.last_licker_run) >= 300:
                        # Load bias from premarket
                        bias_map = {}
                        try:
                            pm_path = BASE_DIR / f"logs/premarket/premarket_{date.today()}.json"
                            if pm_path.exists():
                                with open(pm_path) as f:
                                    pm = json.load(f)
                                for t, td in pm.get("tickers", {}).items():
                                    bias_map[t] = td.get("bias", {}).get("bias", "NEUTRAL")
                        except:
                            pass
                        await run_ticker_licker(bias_map=bias_map)
                        self.last_licker_run = now_ts

                else:
                    log.info(f"  Waiting... {now.strftime('%H:%M ET')}")
                    await asyncio.sleep(60)
                    continue

            except Exception as e:
                log.error(f"  Options engine error: {e}", exc_info=True)

            await asyncio.sleep(60)


if __name__ == "__main__":
    import sys
    if "--gex" in sys.argv:
        asyncio.run(run_gex_analysis())
    elif "--licker" in sys.argv:
        asyncio.run(run_ticker_licker())
    elif "--bell" in sys.argv:
        asyncio.run(run_opening_bell_prep())
    else:
        asyncio.run(OptionsEngine().run())
