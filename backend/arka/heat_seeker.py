"""
CHAKRA Heat Seeker — Unusual Options Flow Scanner (Dual-Mode)
Scalp mode: 0DTE/1DTE ATM sweeps (0.38–0.65 delta, 2x volume)
Swing mode:  any DTE, OTM institutional flow (<0.40 delta, 3x volume)

v2 improvements:
- GEX-aware scoring: wall proximity penalizes, regime alignment boosts
- Bid-ask spread filter: removes untradeable wide-spread contracts
- IV context: flags expensive premium, penalizes very high IV sweeps
- Premium tier: size-classified ($25K / $100K / $500K / $1M+)
- Direction confidence: BOUGHT > LIKELY_BOUGHT in scoring
- Confirmed-sweep bonus increased vs unconfirmed
"""
import math
import os
import json
import time
import httpx
import asyncio
from datetime import datetime, timezone, date
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env", override=True)

POLYGON_KEY  = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE = "https://api.polygon.io"

# ── Dual-mode thresholds ────────────────────────────────────────────────────
SCALP_THRESHOLDS = {
    "VOLUME_MULT": 2.0,
    "OI_RATIO":    0.2,
    "MIN_PREMIUM": 25_000,
    "DELTA_MIN":   0.38,
    "DELTA_MAX":   0.65,
    "MAX_DTE":     1,
    "MIN_DTE":     0,
}
SWING_THRESHOLDS = {
    "VOLUME_MULT": 3.0,
    "OI_RATIO":    0.4,
    "MIN_PREMIUM": 10_000,
    "DELTA_MIN":   0.10,
    "DELTA_MAX":   0.40,
    "MAX_DTE":     21,
    "MIN_DTE":     1,
}

# ── Spread / IV filters ─────────────────────────────────────────────────────
MAX_SPREAD_PCT     = 0.30   # block contracts where (ask-bid)/mid > 30%
IV_HIGH_THRESHOLD  = 1.50   # IV > 150% = expensive premium, penalize -8
IV_EXTREME_THRESHOLD = 2.50 # IV > 250% = extremely expensive, penalize -15

# ── Premium tiers ───────────────────────────────────────────────────────────
PREMIUM_TIERS = [
    (1_000_000, "BLOCK 🏦", 20),   # $1M+  = block trade
    (500_000,   "LARGE 🐋", 15),   # $500K = whale
    (100_000,   "MID 🦈",   8),    # $100K = institutional
    (25_000,    "SMALL 🐟", 0),    # $25K  = retail-size
]

# ── Persistent watchlist ────────────────────────────────────────────────────
_WATCHLIST_PATH    = BASE_DIR / "logs" / "heatseeker_watchlist.json"
_DEFAULT_WATCHLIST = ["SPY", "QQQ", "IWM", "SPX", "NVDA", "TSLA", "AAPL", "MSFT"]


def load_watchlist() -> list[str]:
    try:
        if _WATCHLIST_PATH.exists():
            data = _WATCHLIST_PATH.read_text()
            parsed = json.loads(data)
            if isinstance(parsed, list):
                tickers = parsed
            else:
                tickers = parsed.get("tickers", [])
            if tickers:
                return [t.upper() for t in tickers]
    except Exception:
        pass
    save_watchlist(_DEFAULT_WATCHLIST)
    return list(_DEFAULT_WATCHLIST)


def save_watchlist(tickers: list[str]) -> None:
    _WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WATCHLIST_PATH.write_text(json.dumps(tickers))


def add_to_watchlist(ticker: str) -> list[str]:
    wl = load_watchlist()
    t  = ticker.upper().strip()
    if t and t not in wl:
        wl.append(t)
        save_watchlist(wl)
    return wl


def remove_from_watchlist(ticker: str) -> list[str]:
    wl = load_watchlist()
    t  = ticker.upper().strip()
    if t in wl:
        wl.remove(t)
        save_watchlist(wl)
    return wl


# ── Polygon options chain ───────────────────────────────────────────────────

async def fetch_option_snapshot(ticker: str, mode: str = "swing") -> list[dict]:
    """
    Fetch options snapshot from Polygon with smart pre-filtering.
    Requests near-the-money, near-expiry contracts only — not the full chain.
    """
    from datetime import timedelta

    api_key = os.getenv("POLYGON_API_KEY", "")
    today   = date.today()

    if mode == "scalp":
        exp_from = today.strftime("%Y-%m-%d")
        exp_to   = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        exp_from = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        exp_to   = (today + timedelta(days=60)).strftime("%Y-%m-%d")

    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {
        "apiKey":                api_key,
        "expiration_date.gte":   exp_from,
        "expiration_date.lte":   exp_to,
        "limit":                 250,
    }

    all_results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        next_url = None
        page     = 0
        while page < 5:
            try:
                if next_url:
                    resp = await client.get(next_url)
                else:
                    resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                all_results.extend(data.get("results", []))
                next_url = data.get("next_url")
                if not next_url:
                    break
                next_url = f"{next_url}&apiKey={api_key}"
                page += 1
            except Exception as e:
                print(f"[HeatSeeker] Polygon fetch error for {ticker}: {e}")
                break

    print(f"[HeatSeeker] {ticker} ({mode}): fetched {len(all_results)} contracts ({exp_from} to {exp_to})")
    return all_results


def _calc_dte(expiry_str: str) -> int:
    try:
        exp = date.fromisoformat(expiry_str)
        return max(0, (exp - date.today()).days)
    except Exception:
        return 999


def get_time_multiplier() -> float:
    """
    Weight signals by time of day.
    Open and power hour sweeps carry highest conviction.
    Mid-day flow is less significant.
    """
    from zoneinfo import ZoneInfo as _ZI
    _now = datetime.now(_ZI("America/New_York"))
    h, m = _now.hour, _now.minute
    if h == 9  and m >= 30: return 1.3   # opening bell
    if h == 10:              return 1.2   # continuation of open
    if h in (11, 12, 13):   return 0.85  # dead zone
    if h in (14, 15):       return 1.25  # power hour
    return 1.0


def classify_direction(trade_price: float, ask: float, bid: float) -> str:
    if ask == bid or (ask == 0 and bid == 0):
        return "UNKNOWN"
    mid = (ask + bid) / 2
    if trade_price >= ask:
        return "BOUGHT"
    elif trade_price <= bid:
        return "SOLD"
    elif trade_price > mid:
        return "LIKELY_BOUGHT"
    else:
        return "LIKELY_SOLD"


def is_bullish(direction: str, option_type: str) -> bool:
    if direction in ("BOUGHT", "LIKELY_BOUGHT") and option_type == "call":
        return True
    if direction in ("SOLD",   "LIKELY_SOLD")   and option_type == "put":
        return True
    return False


def is_bearish(direction: str, option_type: str) -> bool:
    if direction in ("BOUGHT", "LIKELY_BOUGHT") and option_type == "put":
        return True
    if direction in ("SOLD",   "LIKELY_SOLD")   and option_type == "call":
        return True
    return False


def _spread_pct(ask: float, bid: float) -> float:
    """(ask - bid) / mid — returns 0 if no valid market."""
    if ask <= 0 or bid < 0 or ask < bid:
        return 0.0
    mid = (ask + bid) / 2
    if mid <= 0:
        return 0.0
    return (ask - bid) / mid


def _premium_tier(premium: float) -> tuple[str, int]:
    """Return (label, score_bonus) for the premium size."""
    for threshold, label, bonus in PREMIUM_TIERS:
        if premium >= threshold:
            return label, bonus
    return "TINY", -5  # below all tiers — penalize tiny prints


def _gex_score_adjustment(ticker: str, signal_direction: str, spot: float) -> tuple[int, str]:
    """
    Apply GEX-aware score adjustment to a HeatSeeker signal.
    Returns (score_delta, reason_str).
    Negative delta = penalize. Positive delta = boost. 0 = no GEX data.
    """
    try:
        from backend.arka.gex_state import load_gex_state
        from backend.arka.gex_gate  import gex_gate

        gex = load_gex_state(ticker)
        if not gex:
            return 0, ""

        # Inject live spot for accurate wall-proximity calculation
        if spot > 0:
            gex["live_spot"] = spot

        # gex_gate operates on 0-100 conviction; we pass 70 as a neutral baseline
        # and measure the delta to understand how much GEX adjusts this signal.
        baseline    = 70.0
        gate_result = gex_gate(signal_direction, baseline, gex)

        if not gate_result["allow"]:
            # Hard block (near wall / extreme bias) — penalize heavily
            return -25, f"GEX BLOCK: {gate_result['reason'][:80]}"

        delta  = round(gate_result["conviction"] - baseline)
        reason = gate_result["reason"] if delta != 0 else ""
        return delta, reason

    except Exception:
        return 0, ""


# ── Core scanner ────────────────────────────────────────────────────────────

async def scan_ticker(ticker: str, mode: str = "swing") -> list[dict]:
    """
    Scan options chain for unusual activity.
    mode='scalp' → 0DTE/1DTE, ATM (delta 0.38–0.65), 2x volume
    mode='swing' → any DTE, OTM (delta <0.40), 3x volume
    Returns up to 25 signals sorted by score descending.
    """
    th     = SCALP_THRESHOLDS if mode == "scalp" else SWING_THRESHOLDS
    chain  = await fetch_option_snapshot(ticker, mode=mode)
    signals: list[dict] = []

    for contract in chain:
        details = contract.get("details", {})
        greeks  = contract.get("greeks", {})
        day     = contract.get("day", {})
        last_q  = contract.get("last_quote", {})
        last_t  = contract.get("last_trade", {})

        opt_type = details.get("contract_type", "").lower()
        strike   = details.get("strike_price", 0)
        expiry   = details.get("expiration_date", "")
        vol      = day.get("volume", 0) or 0
        oi       = contract.get("open_interest", 0)
        vwap     = day.get("vwap", 0) or 0
        ask      = last_q.get("ask", 0) or 0
        bid      = last_q.get("bid", 0) or 0
        trade_px = last_t.get("price", vwap) or vwap
        iv       = contract.get("implied_volatility", 0) or greeks.get("implied_volatility", 0) or 0
        delta    = abs(greeks.get("delta", 0) or 0)
        # Underlying spot from greeks / day close (best estimate in snapshot)
        spot     = float(greeks.get("underlying_price", 0) or day.get("close", 0) or 0)

        if vol < 10 or not opt_type or not strike or trade_px <= 0:
            continue

        # ── Stale / illiquid contract filter ──────────────────────────────
        if delta == 0 and iv == 0:
            continue
        if ask == 0 and bid == 0:
            continue

        # ── Bid-ask spread filter — skip untradeable wide spreads ─────────
        spread = _spread_pct(ask, bid)
        if ask > 0 and bid >= 0 and spread > MAX_SPREAD_PCT:
            continue

        # ── DTE filter ────────────────────────────────────────────────────
        dte = _calc_dte(expiry)
        if dte < 0 or dte > th["MAX_DTE"] or dte < th["MIN_DTE"]:
            continue

        # ── Delta filter ─────────────────────────────────────────────────
        if mode == "scalp" and (not delta or delta < th["DELTA_MIN"] or delta > th["DELTA_MAX"]):
            continue
        elif mode != "scalp" and delta and (delta < th["DELTA_MIN"] or delta > th["DELTA_MAX"]):
            continue

        # ── Premium filter ────────────────────────────────────────────────
        premium = round(trade_px * vol * 100, 2)
        if premium < th["MIN_PREMIUM"]:
            continue

        # ── OI ratio ─────────────────────────────────────────────────────
        if oi == 0:
            oi_ratio = 999
        else:
            oi_ratio = vol / oi

        if oi > 0 and oi_ratio < th["OI_RATIO"]:
            continue

        # ── DTE-aware volume proxy ────────────────────────────────────────
        if dte == 0:
            avg_vol_proxy = max(oi * 0.15, 1.0)
        elif dte <= 7:
            avg_vol_proxy = max(oi * 0.08, 1.0)
        elif dte <= 21:
            avg_vol_proxy = max(oi * 0.04, 1.0)
        else:
            avg_vol_proxy = max(oi * 0.02, 1.0)
        vol_mult = vol / avg_vol_proxy
        if vol_mult < th["VOLUME_MULT"]:
            continue

        # ── Hedge filter: deep ITM large block ────────────────────────────
        if delta > 0.85 and vol > 500:
            continue

        # ── Sweep detection ───────────────────────────────────────────────
        conditions = last_t.get("conditions", []) or []
        is_sweep   = bool(conditions and any(c in [14, 15, 37, 38, 41] for c in conditions))

        # ── Direction inference ───────────────────────────────────────────
        direction       = classify_direction(trade_px, ask, bid)
        bullish         = is_bullish(direction, opt_type)
        bearish         = is_bearish(direction, opt_type)
        bias            = "🟢 BULLISH" if bullish else ("🔴 BEARISH" if bearish else "⚪ NEUTRAL")
        direction_label = "SWEEP" if is_sweep else direction

        # ── Conviction score (0–100) ──────────────────────────────────────
        score = 0

        # Volume multiple — log scale to prevent one blowout contract dominating
        score += min(30, int(math.log1p(vol_mult) * 4))

        # OI ratio — log scale
        score += min(15, int(math.log1p(min(oi_ratio, 999)) * 2)) if oi > 0 else 0

        # Direction confidence: confirmed aggressor scores higher than inferred
        if direction in ("BOUGHT", "SOLD"):
            score += 20
        elif direction in ("LIKELY_BOUGHT", "LIKELY_SOLD"):
            score += 10
        else:
            score += 2   # UNKNOWN — minimal contribution

        # Sweep bonus — confirmed conditions code = real institutional sweep
        if is_sweep:
            score += 15

        # Mode-specific bonuses
        if mode == "scalp":
            if dte == 0:
                score += 15   # 0DTE urgency
            if th["DELTA_MIN"] <= delta <= th["DELTA_MAX"]:
                score += 5    # ATM confirmation
        else:
            if delta < 0.40:
                score += 10   # OTM directional bet

        # ── Premium tier bonus/penalty ────────────────────────────────────
        tier_label, tier_bonus = _premium_tier(premium)
        score += tier_bonus

        # ── IV context: penalize extremely expensive premium ──────────────
        iv_penalty = 0
        iv_label   = ""
        if iv > IV_EXTREME_THRESHOLD:
            iv_penalty = -15
            iv_label   = f"IV {iv*100:.0f}% ⚠️ VERY EXPENSIVE"
        elif iv > IV_HIGH_THRESHOLD:
            iv_penalty = -8
            iv_label   = f"IV {iv*100:.0f}% ⚠️ EXPENSIVE"
        score += iv_penalty

        # ── Bid-ask spread quality: tight spread = more tradeable ─────────
        if spread > 0:
            if spread < 0.05:
                score += 5    # very tight spread — liquid, easy fill
            elif spread > 0.20:
                score -= 5    # wide but under MAX_SPREAD_PCT — less tradeable

        # ── Time-of-day weighting ─────────────────────────────────────────
        time_mult = get_time_multiplier()
        score     = int(score * time_mult)

        # ── GEX-aware score adjustment ────────────────────────────────────
        gex_direction  = "CALL" if opt_type == "call" else "PUT"
        gex_delta, gex_reason = _gex_score_adjustment(ticker, gex_direction, spot)
        score += gex_delta

        score = max(0, min(100, score))

        from zoneinfo import ZoneInfo as _ZI
        signals.append({
            "ticker":        ticker.upper(),
            "type":          opt_type.upper(),
            "strike":        strike,
            "expiry":        expiry,
            "dte":           dte,
            "volume":        int(vol),
            "oi":            int(oi),
            "oi_ratio":      round(oi_ratio, 2),
            "vol_mult":      round(vol_mult, 1),
            "premium":       premium,
            "premium_tier":  tier_label,
            "trade_px":      round(trade_px, 2),
            "iv":            round(iv * 100, 1) if iv else 0,
            "iv_label":      iv_label,
            "delta":         round(delta, 2),
            "spread_pct":    round(spread * 100, 1),
            "direction":     direction_label,
            "bias":          bias,
            "score":         score,
            "gex_adj":       gex_delta,
            "gex_reason":    gex_reason,
            "mode":          mode,
            "is_sweep":      is_sweep,
            "time_weight":   round(time_mult, 2),
            "scan_hour":     datetime.now(_ZI("America/New_York")).hour,
            "scanned_at":    datetime.now(timezone.utc).isoformat(),
        })

    return sorted(signals, key=lambda x: x["score"], reverse=True)[:25]


async def scan_all_watchlist(mode: str = "swing") -> dict:
    """Scan every ticker on the watchlist, merge and sort results."""
    wl          = load_watchlist()
    all_signals: list[dict] = []

    for t in wl:
        try:
            sigs = await scan_ticker(t, mode=mode)
            all_signals.extend(sigs)
        except Exception as e:
            print(f"  [HeatSeeker] ⚠️  {t}: {e}")

    all_signals.sort(key=lambda x: x["score"], reverse=True)

    # ── Cross-ticker bias summary ─────────────────────────────────────────
    bull_count = sum(1 for s in all_signals if "BULL" in (s.get("bias") or "").upper())
    bear_count = sum(1 for s in all_signals if "BEAR" in (s.get("bias") or "").upper())
    total      = len(all_signals)
    if total > 0:
        bull_pct = round(bull_count / total * 100)
        bear_pct = round(bear_count / total * 100)
        if bull_pct >= 65:
            market_bias = f"🟢 BULLISH FLOW ({bull_pct}%)"
        elif bear_pct >= 65:
            market_bias = f"🔴 BEARISH FLOW ({bear_pct}%)"
        else:
            market_bias = f"⚪ MIXED ({bull_pct}% bull / {bear_pct}% bear)"
    else:
        market_bias = "—"

    return {
        "signals":     all_signals,
        "watchlist":   wl,
        "mode":        mode,
        "scanned_at":  datetime.now(timezone.utc).isoformat(),
        "count":       len(all_signals),
        "market_bias": market_bias,
    }
