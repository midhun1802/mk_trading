"""
GEX Calculator — Gamma Exposure from Polygon Options Chain
No ThetaData needed: uses Polygon's options chain with greeks.
"""
import os
import json
import math
import time
import httpx
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")


def fetch_options_chain(ticker: str, spot_price: float) -> list:
    """
    Fetch options chain from Polygon with greeks.
    Paginates up to MAX_PAGES to avoid the call-skew that happens when
    the API returns 250 contracts dominated by one side.
    """
    today  = datetime.now()
    exp_to   = (today + timedelta(days=45)).strftime("%Y-%m-%d")
    exp_from = today.strftime("%Y-%m-%d")

    MAX_PAGES = 4      # 4 × 250 = 1000 contracts max — covers full SPY chain
    contracts = []

    # Only fetch contracts within ±8% of spot — captures all meaningful GEX levels
    # without wading through deep OTM strikes that have near-zero gamma contribution.
    strike_lo = round(spot_price * 0.92, 0)
    strike_hi = round(spot_price * 1.08, 0)

    url    = "https://api.polygon.io/v3/snapshot/options/" + ticker
    params = {
        "expiration_date.gte": exp_from,
        "expiration_date.lte": exp_to,
        "strike_price.gte":    strike_lo,
        "strike_price.lte":    strike_hi,
        "limit":  250,
        "apiKey": POLYGON_KEY,
    }

    def _parse(results: list):
        for item in results:
            details = item.get("details", {})
            greeks  = item.get("greeks", {})
            gamma   = greeks.get("gamma", 0) or 0
            delta   = greeks.get("delta", 0) or 0
            oi      = item.get("open_interest", 0) or 0
            iv      = item.get("implied_volatility", 0) or 0
            ctype   = details.get("contract_type", "")
            strike  = details.get("strike_price", 0) or 0
            if gamma > 0 and oi > 0 and strike > 0:
                contracts.append({
                    "type":          ctype,
                    "strike":        float(strike),
                    "gamma":         float(gamma),
                    "delta":         float(delta),
                    "open_interest": int(oi),
                    "iv":            float(iv),
                    "expiration":    details.get("expiration_date", ""),
                })

    try:
        for _page in range(MAX_PAGES):
            r    = httpx.get(url, params=params, timeout=20)
            data = r.json()
            _parse(data.get("results", []))

            next_url = data.get("next_url")
            if not next_url:
                break   # no more pages
            # next_url already contains all params except apiKey
            url    = next_url
            params = {"apiKey": POLYGON_KEY}

        return contracts
    except Exception as e:
        print(f"  [GEX] Options fetch error: {e}")
        return []


def _build_strike_ladder(call_gex: dict, put_gex: dict, spot: float, window: float = 20) -> list:
    """
    Build a per-strike GEX ladder for the cockpit.
    Returns strikes within ±window points of spot, sorted high → low.
    Each row: {strike, call_gex, put_gex, net_gex}
    Values are in raw dollars (not billions) for easier bar scaling.
    """
    all_strikes = sorted(set(list(call_gex.keys()) + list(put_gex.keys())))
    ladder = []
    for s in all_strikes:
        if abs(float(s) - spot) > window:
            continue
        cg = call_gex.get(s, 0)
        pg = put_gex.get(s, 0)
        ng = cg + pg
        ladder.append({
            "strike":   round(float(s), 1),
            "call_gex": round(cg, 0),
            "put_gex":  round(pg, 0),
            "net_gex":  round(ng, 0),
        })
    return sorted(ladder, key=lambda x: x["strike"], reverse=True)


def calculate_gex(contracts: list, spot_price: float) -> dict:
    """
    Calculate Gamma Exposure (GEX) from options contracts.

    Formula: GEX = Gamma × OI × 100 × Spot² × 0.01
    Calls: dealers SHORT gamma (negative for dealers = destabilizing above call wall)
    Puts:  dealers LONG gamma (positive for dealers = stabilizing above put wall)
    """
    if not contracts:
        return _empty_gex(spot_price)

    call_gex_by_strike = {}
    put_gex_by_strike  = {}

    total_call_gex = 0
    total_put_gex  = 0

    for c in contracts:
        gex_val = c["gamma"] * c["open_interest"] * 100 * (spot_price ** 2) * 0.01
        c["gamma_exposure"] = gex_val   # enrich contract for compute_directional_exposure

        if c["type"] == "call":
            # Dealers short calls → negative gamma contribution
            total_call_gex -= gex_val
            call_gex_by_strike[c["strike"]] = call_gex_by_strike.get(c["strike"], 0) - gex_val
        elif c["type"] == "put":
            # Dealers long puts → positive gamma contribution
            total_put_gex += gex_val
            put_gex_by_strike[c["strike"]] = put_gex_by_strike.get(c["strike"], 0) + gex_val

    net_gex = total_call_gex + total_put_gex

    # Find walls — strikes with highest absolute GEX concentration
    call_wall = None
    put_wall  = None
    second_call = None
    second_put  = None

    if call_gex_by_strike:
        sorted_calls = sorted(call_gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True)
        call_wall   = sorted_calls[0][0] if sorted_calls else None
        second_call = sorted_calls[1][0] if len(sorted_calls) > 1 else None

    if put_gex_by_strike:
        sorted_puts = sorted(put_gex_by_strike.items(), key=lambda x: abs(x[1]), reverse=True)
        put_wall   = sorted_puts[0][0] if sorted_puts else None
        second_put = sorted_puts[1][0] if len(sorted_puts) > 1 else None

    # Zero gamma level — approximate as where net GEX changes sign
    all_strikes = sorted(set(list(call_gex_by_strike.keys()) + list(put_gex_by_strike.keys())))
    zero_gamma = spot_price  # default
    if all_strikes:
        cumulative = 0
        for strike in all_strikes:
            cumulative += call_gex_by_strike.get(strike, 0) + put_gex_by_strike.get(strike, 0)
            if cumulative >= 0:
                zero_gamma = strike
                break

    # Regime
    regime = "POSITIVE_GAMMA" if net_gex > 0 else "NEGATIVE_GAMMA"

    # IV skew proxy — ratio of put to call OI weighted by proximity
    near_strikes = [c for c in contracts if abs(c["strike"] - spot_price) / spot_price < 0.05]
    put_oi  = sum(c["open_interest"] for c in near_strikes if c["type"] == "put")
    call_oi = sum(c["open_interest"] for c in near_strikes if c["type"] == "call")
    iv_skew = (put_oi - call_oi) / (put_oi + call_oi + 1)

    # Distance to walls
    room_to_call = round((call_wall - spot_price), 2) if call_wall else 0
    room_to_put  = round((spot_price - put_wall), 2)  if put_wall  else 0

    # ── Cliff detection — >25% of OI expires today ────────────────────────────
    today_str    = datetime.now().strftime("%Y-%m-%d")
    today_cs     = [c for c in contracts if c.get("expiration", "").startswith(today_str)]
    today_oi     = sum(c["open_interest"] for c in today_cs)
    total_oi     = sum(c["open_interest"] for c in contracts) or 1
    cliff_today  = (today_oi / total_oi) > 0.25
    cliff_strike = None
    if cliff_today and today_cs:
        cliff_strike = max(today_cs, key=lambda c: c["open_interest"])["strike"]

    # ── George insights ────────────────────────────────────────────────────────
    gex_by_strike = {
        k: call_gex_by_strike.get(k, 0) + put_gex_by_strike.get(k, 0)
        for k in set(list(call_gex_by_strike) + list(put_gex_by_strike))
    }
    directional    = compute_directional_exposure(contracts)
    regime_call    = get_regime_call(net_gex, spot_price > zero_gamma, directional["bias_ratio"])
    accel_up       = compute_acceleration(gex_by_strike, spot_price, "UP")
    accel_down     = compute_acceleration(gex_by_strike, spot_price, "DOWN")
    pin_strikes    = find_pin_strikes(contracts)

    # ATM IV for expected move (average of near-ATM contracts, fallback 15%)
    near_atm = [c for c in contracts
                if abs(c["strike"] - spot_price) / spot_price < 0.02 and c.get("iv", 0) > 0]
    atm_iv   = float(np.mean([c["iv"] for c in near_atm])) if near_atm else 0.0
    em       = compute_expected_move(spot_price, atm_iv, dte=1) if atm_iv > 0 else {}

    return {
        "spx_price":    round(spot_price, 2),
        "spot":         round(spot_price, 2),
        "net_gex":      round(net_gex / 1e9, 3),       # in billions
        "call_gex":     round(total_call_gex / 1e9, 3),
        "put_gex":      round(total_put_gex / 1e9, 3),
        "call_wall":    call_wall,
        "put_wall":     put_wall,
        "second_call":  second_call,
        "second_put":   second_put,
        "zero_gamma":   round(zero_gamma, 0),
        "regime":       regime,
        "regime_call":  regime_call,
        "iv_skew":      round(iv_skew, 4),
        "room_to_call": room_to_call,
        "room_to_put":  room_to_put,
        "bullish_bias": net_gex > 0 and spot_price < (call_wall or spot_price * 1.02),
        "bearish_bias": spot_price >= (call_wall or spot_price * 1.1),
        "contracts_used": len(contracts),
        # Cliff
        "cliff": {
            "expires_today": cliff_today,
            "strike":        cliff_strike,
        },
        # George insights
        "call_gex_dollars": directional["call_gex_dollars"],
        "put_gex_dollars":  directional["put_gex_dollars"],
        "bias_ratio":       directional["bias_ratio"],
        "dominant_side":    directional["dominant_side"],
        "accel_up":         accel_up,
        "accel_down":       accel_down,
        "expected_move_pts": em.get("expected_move_pts", 0),
        "upper_1sd":         em.get("upper_1sd", round(spot_price * 1.01, 2)),
        "lower_1sd":         em.get("lower_1sd", round(spot_price * 0.99, 2)),
        "pin_strikes":       pin_strikes,
        "updated":           datetime.now().isoformat(),
        # ── Nearby strike ladder (±20 pts from spot, sorted desc) ──────────
        # Each entry: {strike, call_gex, put_gex, net_gex}
        # Used by cockpit for visual GEX bar chart
        "top_strikes": _build_strike_ladder(
            call_gex_by_strike, put_gex_by_strike, spot_price, window=20
        ),
        "second_call": second_call,
        "second_put":  second_put,
    }


def get_gex_for_ticker(ticker: str, spot_price: float) -> dict:
    """Main entry point: fetch chain, calculate GEX, write state + intraday snapshot."""
    print(f"  [GEX] Fetching options chain for {ticker} @ ${spot_price}...")
    contracts = fetch_options_chain(ticker, spot_price)
    if not contracts:
        print(f"  [GEX] No contracts found, using empty GEX")
        return _empty_gex(spot_price)
    print(f"  [GEX] Got {len(contracts)} contracts, calculating GEX...")
    result = calculate_gex(contracts, spot_price)
    try:
        write_gex_state(ticker, result)
        snapshot_gex_intraday(result, ticker)
    except Exception as _e:
        print(f"  [GEX] State write error: {_e}")
    return result


def _empty_gex(spot_price: float) -> dict:
    return {
        "spx_price":    spot_price,
        "spot":         spot_price,
        "net_gex":      0,
        "call_gex":     0,
        "put_gex":      0,
        "call_wall":    round(spot_price * 1.02, 0),
        "put_wall":     round(spot_price * 0.97, 0),
        "second_call":  None,
        "second_put":   None,
        "zero_gamma":   spot_price,
        "regime":       "UNKNOWN",
        "regime_call":  "NEUTRAL",
        "iv_skew":      0,
        "room_to_call": 0,
        "room_to_put":  0,
        "bullish_bias": False,
        "bearish_bias": False,
        "contracts_used": 0,
        "cliff": {"expires_today": False, "strike": None},
        "call_gex_dollars": 0,
        "put_gex_dollars":  0,
        "bias_ratio":       1.0,
        "dominant_side":    "NEUTRAL",
        "accel_up":         0,
        "accel_down":       0,
        "expected_move_pts": 0,
        "upper_1sd":         round(spot_price * 1.01, 2),
        "lower_1sd":         round(spot_price * 0.99, 2),
        "pin_strikes":       [],
        "updated":           datetime.now().isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# GEORGE INSIGHTS — Phase 7A functions
# ══════════════════════════════════════════════════════════════════════════════

def get_regime_call(net_gex: float, above_zero_gamma: bool,
                    bias_ratio: float = 1.0) -> str:
    """
    Generate top-level directional trading bias — George's "Short the Pops".

    - Positive gamma + above zero gamma → dealers fade upside → SHORT_THE_POPS
    - Positive gamma + below zero gamma → dealers support downside → BUY_THE_DIPS
    - Negative gamma → dealers amplify all moves → FOLLOW_MOMENTUM
    """
    if net_gex > 0 and above_zero_gamma:
        return "SHORT_THE_POPS"
    elif net_gex > 0 and not above_zero_gamma:
        return "BUY_THE_DIPS"
    elif net_gex < 0:
        return "FOLLOW_MOMENTUM"
    return "NEUTRAL"


def compute_directional_exposure(options_chain: list) -> dict:
    """
    Calculate gross gamma exposure by direction in dollars.
    George: "$5.5B puts vs $1.3B calls" — the ratio matters more than net GEX.
    Uses pre-computed gamma_exposure set on each contract by calculate_gex().
    """
    call_gex_total = 0.0
    put_gex_total  = 0.0

    for c in options_chain:
        gex_val = abs(c.get("gamma_exposure", 0))
        ctype   = c.get("type", "") or c.get("contract_type", "")
        if ctype == "call":
            call_gex_total += gex_val
        elif ctype == "put":
            put_gex_total += gex_val

    bias_ratio = put_gex_total / call_gex_total if call_gex_total > 0.01 else 99.0

    if bias_ratio > 1.5:
        dominant = "PUT"
    elif bias_ratio < 0.67:
        dominant = "CALL"
    else:
        dominant = "NEUTRAL"

    return {
        "call_gex_dollars": call_gex_total,
        "put_gex_dollars":  put_gex_total,
        "bias_ratio":       round(bias_ratio, 2),
        "dominant_side":    dominant,
    }


def compute_acceleration(gex_by_strike: dict, spot: float,
                          direction: str) -> float:
    """
    Measure gamma concentration gradient near spot price.
    Higher score = faster expected price movement in that direction.
    George: "+21 acceleration upside" or "+7 downside".
    """
    nearby = {k: v for k, v in gex_by_strike.items() if abs(float(k) - spot) <= 10}
    if direction == "UP":
        relevant = {k: v for k, v in nearby.items() if float(k) > spot}
    else:
        relevant = {k: v for k, v in nearby.items() if float(k) < spot}

    if not relevant:
        return 0.0

    nearby_gex = sum(abs(v) for v in relevant.values())
    total_gex  = sum(abs(v) for v in gex_by_strike.values()) or 1.0
    # Score = % of total chain GEX concentrated in the directional nearby zone.
    # George reference: >15 = high acceleration. SPY typically 20-60 near walls.
    acceleration = (nearby_gex / total_gex) * 100
    return round(acceleration, 1)


def compute_expected_move(spot: float, iv: float, dte: int = 1) -> dict:
    """
    Calculate IV-implied expected move (1 standard deviation).
    Formula: EM = Spot × IV × sqrt(DTE / 252)

    ARKA should never buy strikes outside this range for 0DTE.
    Probability of reaching outside 1SD ≈ 16%.
    """
    if iv <= 0 or spot <= 0:
        return {"expected_move_pts": 0, "upper_1sd": spot, "lower_1sd": spot}
    daily_move = spot * iv * math.sqrt(dte / 252)
    return {
        "expected_move_pts": round(daily_move, 2),
        "upper_1sd":         round(spot + daily_move, 2),
        "lower_1sd":         round(spot - daily_move, 2),
    }


def find_pin_strikes(options_chain: list, min_combined_oi: int = 15000) -> list:
    """
    Identify pin levels where price tends to oscillate.
    George: "You sweep that strike once down, once up — that's where you look for reversal."

    Pin = strike with elevated OI on BOTH call and put sides.
    Different from walls: walls = one-time bounce, pins = oscillation zones.
    """
    strikes: dict = {}
    for c in options_chain:
        strike = c.get("strike_price") or c.get("strike")
        if not strike:
            continue
        strike = float(strike)
        if strike not in strikes:
            strikes[strike] = {"call_oi": 0, "put_oi": 0}
        oi    = c.get("open_interest", 0) or 0
        ctype = c.get("type", "") or c.get("contract_type", "")
        if ctype == "call":
            strikes[strike]["call_oi"] += oi
        else:
            strikes[strike]["put_oi"] += oi

    pins = []
    for strike, d in strikes.items():
        combined = d["call_oi"] + d["put_oi"]
        if d["call_oi"] > 5000 and d["put_oi"] > 5000 and combined >= min_combined_oi:
            pins.append({
                "strike":   strike,
                "call_oi":  d["call_oi"],
                "put_oi":   d["put_oi"],
                "strength": combined,
            })

    return sorted(pins, key=lambda x: -x["strength"])[:5]


# ══════════════════════════════════════════════════════════════════════════════
# State persistence — called by get_gex_for_ticker() after every compute
# ══════════════════════════════════════════════════════════════════════════════

def write_gex_state(ticker: str, gex_result: dict) -> None:
    """
    Write full GEX state to logs/gex/gex_latest_{ticker}.json.
    Includes all George-insight fields for gex_state.py to read.
    """
    log_dir = BASE / "logs/gex"
    log_dir.mkdir(parents=True, exist_ok=True)

    spot      = float(gex_result.get("spx_price") or gex_result.get("spot") or 0)
    call_wall = float(gex_result.get("call_wall") or 0)
    put_wall  = float(gex_result.get("put_wall")  or 0)
    zero_gam  = float(gex_result.get("zero_gamma", spot))

    state = {
        "ticker":      ticker.upper(),
        "spot":        spot,
        "regime":      gex_result.get("regime", "UNKNOWN"),
        "regime_call": gex_result.get("regime_call", "NEUTRAL"),
        "zero_gamma":  zero_gam,
        "call_wall":   call_wall,
        "put_wall":    put_wall,
        "net_gex":     gex_result.get("net_gex", 0),
        "above_zero_gamma": spot > zero_gam,
        # Dollar exposure (George)
        "call_gex_dollars": gex_result.get("call_gex_dollars", 0),
        "put_gex_dollars":  gex_result.get("put_gex_dollars", 0),
        "bias_ratio":       gex_result.get("bias_ratio", 1.0),
        "dominant_side":    gex_result.get("dominant_side", "NEUTRAL"),
        # Acceleration
        "accel_up":   gex_result.get("accel_up", 0),
        "accel_down": gex_result.get("accel_down", 0),
        # Expected move
        "expected_move_pts": gex_result.get("expected_move_pts", 0),
        "upper_1sd":         gex_result.get("upper_1sd", round(spot * 1.01, 2)),
        "lower_1sd":         gex_result.get("lower_1sd", round(spot * 0.99, 2)),
        # Pins
        "pin_strikes": gex_result.get("pin_strikes", []),
        # Cliff (nested dict for gex_state.py compatibility)
        "cliff": gex_result.get("cliff", {
            "expires_today": bool(gex_result.get("cliff_today", False)),
            "strike": None,
        }),
        # Strike ladder — nearby strikes ±20 pts from spot
        "top_strikes":  gex_result.get("top_strikes", []),
        "second_call":  gex_result.get("second_call", 0),
        "second_put":   gex_result.get("second_put",  0),
        "ts": time.time(),
    }
    path = log_dir / f"gex_latest_{ticker.upper()}.json"
    path.write_text(json.dumps(state, indent=2))


def snapshot_gex_intraday(gex_result: dict, ticker: str) -> None:
    """
    Append one GEX snapshot to the intraday timeline log.
    File: logs/gex/gex_intraday_{TICKER}_{YYYY-MM-DD}.json
    """
    log_dir = BASE / "logs/gex"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path  = log_dir / f"gex_intraday_{ticker.upper()}_{today}.json"

    entry = {
        "ts":          time.time(),
        "datetime":    datetime.now().isoformat(),
        "zero_gamma":  gex_result.get("zero_gamma", 0),
        "call_wall":   gex_result.get("call_wall", 0),
        "put_wall":    gex_result.get("put_wall", 0),
        "net_gex":     gex_result.get("net_gex", 0),
        "regime":      gex_result.get("regime", "UNKNOWN"),
        "regime_call": gex_result.get("regime_call", "NEUTRAL"),
        "spot":        gex_result.get("spx_price") or gex_result.get("spot", 0),
        "bias_ratio":  gex_result.get("bias_ratio", 1.0),
        "accel_up":    gex_result.get("accel_up", 0),
        "accel_down":  gex_result.get("accel_down", 0),
    }

    data: list = []
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = []
    data.append(entry)
    path.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    result = get_gex_for_ticker("SPY", 680.0)
    print(json.dumps(result, indent=2))
