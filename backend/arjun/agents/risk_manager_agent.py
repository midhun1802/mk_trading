"""
ARJUN Agent 4: Risk Manager Agent
Final gate before any signal is approved.
Checks: GEX regime, curvature risk, macro events, portfolio state, position limits.
"""
import json
import os
import httpx
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")

# High-impact macro events to watch for
HIGH_IMPACT_KEYWORDS = ["FOMC", "Federal Reserve", "Fed Rate", "NFP", "Nonfarm", "CPI", "Inflation",
                         "PPI", "GDP", "Retail Sales", "Jobs Report", "Powell"]

MAX_POSITION_PCT  = 0.15   # 15% max per position
BASE_RISK_PCT     = 0.02   # 2% risk per trade
MAX_OPEN_TRADES   = 8      # Concurrent trade limit


def fetch_portfolio_state() -> dict:
    """Get current Alpaca portfolio state."""
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    try:
        acct = httpx.get(f"{ALPACA_URL}/v2/account", headers=headers, timeout=10).json()
        pos  = httpx.get(f"{ALPACA_URL}/v2/positions", headers=headers, timeout=10).json()
        orders = httpx.get(f"{ALPACA_URL}/v2/orders?status=open", headers=headers, timeout=10).json()
        portfolio_val = float(acct.get("portfolio_value", 100000))
        buying_power  = float(acct.get("buying_power", 50000))
        open_pos      = len(pos) if isinstance(pos, list) else 0
        open_orders   = len(orders) if isinstance(orders, list) else 0
        return {
            "portfolio_value": portfolio_val,
            "buying_power":    buying_power,
            "open_positions":  open_pos,
            "open_orders":     open_orders,
            "positions":       pos if isinstance(pos, list) else [],
        }
    except Exception as e:
        return {"portfolio_value": 100000, "buying_power": 50000,
                "open_positions": 0, "open_orders": 0, "positions": [], "error": str(e)}


def check_macro_events() -> dict:
    """
    Check for high-impact macro events using macro_calendar module.
    Blocks if event within 4h ahead or 2h behind (post-event volatility).
    """
    try:
        from backend.arjun.modules.macro_calendar import fetch_upcoming_events
        events = fetch_upcoming_events(hours_ahead=4, hours_before=2)
        return {
            "high_impact_events": events,
            "event_count":        len(events),
            "block_trading":      len(events) > 0,
            "next_event":         events[0].get("name", "") if events else None,
            "hours_away":         events[0].get("hours_away", None) if events else None,
        }
    except Exception as e:
        return {"high_impact_events": [], "event_count": 0, "block_trading": False, "error": str(e)}


def calculate_position_size(portfolio_value: float, entry: float, stop: float,
                             gex_regime: str, curvature_regime: str) -> dict:
    """
    Risk-based position sizing with GEX and curvature adjustments.
    Base: 2% portfolio risk per trade.
    """
    risk_per_share = abs(entry - stop) if stop > 0 else entry * 0.02
    if risk_per_share == 0:
        risk_per_share = entry * 0.02

    base_risk_dollars = portfolio_value * BASE_RISK_PCT
    base_shares       = int(base_risk_dollars / risk_per_share)
    base_dollar_value = base_shares * entry

    # Apply multipliers
    mult = 1.0
    notes = []

    if gex_regime == "NEGATIVE_GAMMA":
        mult *= 0.5
        notes.append("NEGATIVE_GAMMA: halved size")
    elif gex_regime == "POSITIVE_GAMMA":
        notes.append("POSITIVE_GAMMA: normal size")

    if curvature_regime == "HIGH_RISK":
        mult *= 0.5
        notes.append("HIGH curvature: halved size")
    elif curvature_regime == "MODERATE_RISK":
        mult *= 0.75
        notes.append("MODERATE curvature: reduced 25%")

    adjusted_shares = max(1, int(base_shares * mult))
    adjusted_dollars = adjusted_shares * entry

    # Hard cap: 15% of portfolio
    max_dollars = portfolio_value * MAX_POSITION_PCT
    if adjusted_dollars > max_dollars:
        adjusted_shares = int(max_dollars / entry)
        notes.append(f"Capped at {MAX_POSITION_PCT*100}% portfolio limit")

    return {
        "shares":         adjusted_shares,
        "dollar_value":   round(adjusted_shares * entry, 2),
        "risk_dollars":   round(adjusted_shares * risk_per_share, 2),
        "risk_pct":       round(adjusted_shares * risk_per_share / portfolio_value * 100, 2),
        "size_multiplier": round(mult, 2),
        "sizing_notes":   notes,
    }


def run(ticker: str, analyst_result: dict, bull_result: dict, bear_result: dict,
        gex_data: dict) -> dict:
    """
    Risk Manager: Final gate. Decides APPROVE / REDUCE_SIZE / BLOCK.
    Produces position sizing and comprehensive risk assessment.
    """
    print(f"  [Risk] Assessing risk for {ticker}...")

    ind           = analyst_result.get("indicators", {})
    price         = ind.get("price", 0)
    atr           = ind.get("atr", price * 0.01)
    bull_score    = bull_result.get("score", 50)
    bear_score    = bear_result.get("score", 50)
    bull_target   = bull_result.get("target_price", price * 1.03)
    bear_stop     = bear_result.get("stop_level", price * 0.98)
    curvature     = bear_result.get("curvature", {})
    curv_regime   = curvature.get("regime", "LOW_RISK")
    gex_regime    = gex_data.get("regime", "UNKNOWN")
    call_wall     = gex_data.get("call_wall", 0)
    put_wall      = gex_data.get("put_wall", 0)
    net_gex       = gex_data.get("net_gex", 0)

    portfolio = fetch_portfolio_state()
    macro     = check_macro_events()

    portfolio_val = portfolio.get("portfolio_value", 100000)
    open_pos      = portfolio.get("open_positions", 0)

    # ── Decision Logic ─────────────────────────────────────────────────
    blocks  = []
    reduces = []
    notes   = []

    # Hard blocks
    if macro["block_trading"]:
        name  = macro.get("next_event") or (macro["high_impact_events"][0].get("name","EVENT") if macro["high_impact_events"] else "EVENT")
        hours = macro.get("hours_away")
        hours_str = f"{abs(hours):.1f}h {'away' if hours and hours > 0 else 'ago'}" if hours is not None else "imminent"
        blocks.append(f"MACRO BLACKOUT: {name} ({hours_str}) — all entries blocked")

    if open_pos >= MAX_OPEN_TRADES:
        blocks.append(f"MAX CONCURRENT TRADES ({MAX_OPEN_TRADES}) reached — {open_pos} open")

    if gex_regime == "NEGATIVE_GAMMA" and bear_score > 65:
        blocks.append(f"NEGATIVE_GAMMA + Bear score {bear_score} — too risky for long")

    if bull_score < 35:
        blocks.append(f"Bull score too low ({bull_score}) — no edge")

    if bear_score > bull_score + 35:
        blocks.append(f"Bear overwhelms bull: {bear_score} vs {bull_score}")

    # Reduce-size triggers
    if gex_regime == "NEGATIVE_GAMMA":
        reduces.append("Negative gamma regime — 50% size")
    if curv_regime in ["HIGH_RISK", "MODERATE_RISK"]:
        reduces.append(f"Curvature {curv_regime} — reduced size")
    if macro["event_count"] > 0:
        reduces.append(f"{macro['event_count']} macro event(s) today — cautious sizing")
    if bear_score > 55:
        reduces.append(f"Bear score elevated ({bear_score}) — reduce exposure")

    # Final decision
    if blocks:
        decision = "BLOCK"
        reason   = blocks[0]
    elif reduces:
        decision = "REDUCE_SIZE"
        reason   = reduces[0]
    else:
        decision = "APPROVE"
        reason   = f"Bull {bull_score} vs Bear {bear_score} — edge confirmed"

    # Position sizing
    entry  = price
    stop   = bear_stop if bear_stop > 0 else price * 0.982
    target = bull_target if bull_target > price else price * 1.03

    sizing = calculate_position_size(
        portfolio_val, entry, stop,
        gex_regime if gex_regime != "UNKNOWN" else "POSITIVE_GAMMA",
        curv_regime
    ) if decision != "BLOCK" else {
        "shares": 0, "dollar_value": 0, "risk_dollars": 0,
        "risk_pct": 0, "size_multiplier": 0, "sizing_notes": ["BLOCKED"]
    }

    # Risk/reward
    risk   = abs(entry - stop)
    reward = abs(target - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    return {
        "ticker":          ticker,
        "decision":        decision,
        "reason":          reason,
        "position_size":   sizing,
        "entry":           round(entry, 2),
        "stop":            round(stop, 2),
        "target":          round(target, 2),
        "risk_reward":     rr,
        "regime":          gex_regime,
        "curvature":       curvature,
        "macro_events":    macro,
        "portfolio_state": {
            "value":         portfolio_val,
            "buying_power":  portfolio.get("buying_power", 0),
            "open_positions": open_pos,
        },
        "blocks":           blocks,
        "reduces":          reduces,
        "bull_score":       bull_score,
        "bear_score":       bear_score,
        "net_gex_billions": net_gex,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from analyst_agent  import run as analyst_run
    from bull_agent     import run as bull_run
    from bear_agent     import run as bear_run
    from gex_calculator import get_gex_for_ticker

    analyst = analyst_run("SPY")
    price   = analyst["indicators"]["price"]
    gex     = get_gex_for_ticker("SPY", price)
    bull    = bull_run("SPY", analyst, gex)
    bear    = bear_run("SPY", analyst, gex)
    result  = run("SPY", analyst, bull, bear, gex)
    print(json.dumps(result, indent=2))


