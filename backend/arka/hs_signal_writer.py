"""
HS Signal Writer — bridges Heat Seeker output to ARJUN intraday input.
Reads from logs/heatseeker/latest_scalp.json and latest_swing.json.
Writes to logs/arjun/hs_pending_signals.json for ARJUN to act on.
NEVER imports heat_seeker.py directly — reads cache files only.
"""
import json
import time
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

HS_SCALP_PATH  = "logs/heatseeker/latest_scalp.json"
HS_SWING_PATH  = "logs/heatseeker/latest_swing.json"
HS_OUTPUT_PATH = "logs/arjun/hs_pending_signals.json"
MAX_AGE_MIN    = 10  # ignore stale HS cache older than 10 min


def load_hs_signals(mode: str = "scalp") -> list:
    """Load Heat Seeker signals from cache. Never touches hs core."""
    path = HS_SCALP_PATH if mode == "scalp" else HS_SWING_PATH
    if not Path(path).exists():
        return []

    age_min = (time.time() - Path(path).stat().st_mtime) / 60
    if age_min > MAX_AGE_MIN:
        print(f"⚠️ HS {mode} cache is {age_min:.1f}min old — skipping")
        return []

    try:
        data = json.loads(Path(path).read_text())
        sigs = data.get("signals", data.get("top_signals", []))
        return sigs
    except Exception as e:
        print(f"❌ HS read error: {e}")
        return []


def format_for_arjun(hs_signal: dict, mode: str = "scalp") -> dict:
    """
    Convert HS signal format to ARJUN-readable trade request.

    HS actual keys: ticker, direction (BOUGHT/SOLD/LIKELY_BOUGHT/LIKELY_SOLD),
    bias (🟢 BULLISH / 🔴 BEARISH), score, vol_mult, oi_ratio, premium,
    is_sweep, gex_alignment, gex_regime, gex_call_wall, gex_put_wall
    """
    ticker   = hs_signal.get("ticker", "")
    raw_dir  = hs_signal.get("direction", "")
    bias     = (hs_signal.get("bias", "") or "").upper()
    score    = float(hs_signal.get("score", 50))
    vol_mult = float(hs_signal.get("vol_mult", 1.0))
    oi_ratio = float(hs_signal.get("oi_ratio", 0))
    premium  = float(hs_signal.get("premium", 0))
    is_sweep = bool(hs_signal.get("is_sweep", False))
    opt_type = hs_signal.get("type", "")  # CALL or PUT
    gex_align = hs_signal.get("gex_alignment", "")
    gex_regime = hs_signal.get("gex_regime", "")
    scanned_at = hs_signal.get("scanned_at", "")

    # Parse timestamp from scanned_at or fallback to now
    try:
        from datetime import datetime as _dt
        ts = _dt.fromisoformat(scanned_at.replace("Z", "+00:00")).timestamp()
    except Exception:
        ts = time.time()

    # Normalize direction — bias field is more reliable than direction
    if "BULLISH" in bias or raw_dir in ("BOUGHT", "LIKELY_BOUGHT") or opt_type == "CALL":
        direction = "BULLISH"
    elif "BEARISH" in bias or raw_dir in ("SOLD", "LIKELY_SOLD") or opt_type == "PUT":
        direction = "BEARISH"
    else:
        return {}  # Skip unclassifiable

    if not ticker:
        return {}

    gex_with = "WITH GEX" in (gex_align or "").upper()

    return {
        "ticker":        ticker,
        "direction":     direction,
        "hs_score":      round(score, 1),
        "vol_multiple":  round(vol_mult, 2),
        "oi_ratio":      round(oi_ratio, 2),
        "premium_usd":   round(premium, 2),
        "is_sweep":      is_sweep,
        "opt_type":      opt_type,
        "gex_aligned":   gex_with,
        "gex_regime":    gex_regime,
        "mode":          mode,
        "source":        "HEAT_SEEKER",
        "timestamp":     ts,
        "age_seconds":   round(time.time() - ts, 0),
        # Human-readable context block for Claude prompt
        "context": (
            f"Heat Seeker detected {direction} flow on {ticker}. "
            f"Option type: {opt_type}. "
            f"Vol multiple: {vol_mult:.1f}x avg. "
            f"OI ratio: {oi_ratio:.1f}x. "
            f"Premium: ${premium:,.0f}. "
            f"{'CONFIRMED SWEEP (multi-exchange). ' if is_sweep else ''}"
            f"{'GEX-aligned. ' if gex_with else 'Against GEX. '}"
            f"GEX regime: {gex_regime}. "
            f"HS conviction score: {score:.0f}/100."
        ),
    }


def write_pending_signals() -> list:
    """
    Main entry point. Reads HS cache, formats signals, writes pending list.
    Returns list of formatted signals written.
    """
    os.makedirs("logs/arjun", exist_ok=True)

    signals = []

    # Load scalp signals (high priority — 0DTE)
    for sig in load_hs_signals("scalp"):
        formatted = format_for_arjun(sig, "scalp")
        if formatted:
            signals.append(formatted)

    # Load swing signals (lower priority — require higher score)
    for sig in load_hs_signals("swing"):
        formatted = format_for_arjun(sig, "swing")
        if formatted and formatted.get("hs_score", 0) >= 70:
            signals.append(formatted)

    # Sort by score descending, keep top 5
    signals.sort(key=lambda s: -s.get("hs_score", 0))
    signals = signals[:5]

    output = {
        "signals":    signals,
        "count":      len(signals),
        "updated_at": time.time(),
        "datetime":   datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
    }
    Path(HS_OUTPUT_PATH).write_text(json.dumps(output, indent=2))
    print(f"✅ HS→ARJUN: {len(signals)} signals written to {HS_OUTPUT_PATH}")
    return signals


if __name__ == "__main__":
    sigs = write_pending_signals()
    for s in sigs:
        print(f"  {s['ticker']} {s['direction']} score={s['hs_score']} sweep={s['is_sweep']}")
