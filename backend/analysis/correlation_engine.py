"""
CHAKRA Sector Correlation Engine
Builds correlation matrix across 15 assets, detects regime shifts.
Output: nodes + edges for D3 force graph in dashboard Physics tab.
"""
import os, json, httpx, numpy as np, pandas as pd
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")

UNIVERSE = ["SPY","XLK","XLF","XLV","XLC","XLY","XLP","XLE","XLI","XLB","XLRE","XLU","TLT","GLD","UUP"]

SECTOR_COLORS = {
    "SPY":"#6366f1","XLK":"#3b82f6","XLF":"#10b981","XLV":"#f59e0b",
    "XLC":"#8b5cf6","XLY":"#ec4899","XLP":"#6b7280","XLE":"#f97316",
    "XLI":"#14b8a6","XLB":"#84cc16","XLRE":"#ef4444","XLU":"#a78bfa",
    "TLT":"#facc15","GLD":"#fbbf24","UUP":"#94a3b8",
}


def _fetch_daily_bars(ticker: str, lookback: int = 25) -> list:
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=lookback + 10)).isoformat()
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true", "sort": "asc", "limit": 35},
            timeout=12,
        )
        return r.json().get("results", [])
    except Exception:
        return []


def build_correlation_matrix(lookback_days: int = 20) -> dict:
    prices = {}
    for ticker in UNIVERSE:
        bars = _fetch_daily_bars(ticker, lookback_days + 5)
        if len(bars) >= 5:
            prices[ticker] = [b["c"] for b in bars[-lookback_days:]]

    if len(prices) < 3:
        return {"nodes": [], "edges": [], "error": "Insufficient price data"}

    df      = pd.DataFrame(prices)
    returns = df.pct_change().dropna()
    corr    = returns.corr()

    edges = []
    tickers = list(prices.keys())
    for i, t1 in enumerate(tickers):
        for j, t2 in enumerate(tickers):
            if i >= j:
                continue
            if t1 not in corr.index or t2 not in corr.columns:
                continue
            c = round(float(corr.loc[t1, t2]), 3)
            if abs(c) < 0.4:
                continue
            edges.append({
                "source":      t1,
                "target":      t2,
                "correlation": c,
                "strength":    "STRONG" if abs(c) > 0.7 else "MODERATE",
                "direction":   "POSITIVE" if c > 0 else "NEGATIVE",
                "width":       round(abs(c) * 4, 1),
                "color":       "#10b981" if c > 0 else "#ef4444",
            })

    nodes = []
    for t in tickers:
        vals   = prices[t]
        ret_1d = round((vals[-1] - vals[-2]) / vals[-2] * 100, 3) if len(vals) >= 2 else 0
        ret_5d = round((vals[-1] - vals[-5]) / vals[-5] * 100, 3) if len(vals) >= 5 else 0
        degree = sum(1 for e in edges if e["source"] == t or e["target"] == t)
        nodes.append({
            "id":     t,
            "color":  SECTOR_COLORS.get(t, "#6366f1"),
            "ret_1d": ret_1d,
            "ret_5d": ret_5d,
            "degree": degree,
            "size":   12 + degree * 2,
        })

    return {
        "nodes":         nodes,
        "edges":         edges,
        "tickers":       tickers,
        "lookback_days": lookback_days,
        "date":          date.today().isoformat(),
        "edge_count":    len(edges),
        "node_count":    len(nodes),
    }


def detect_regime_shift(correlation_data: dict) -> dict:
    edges = correlation_data.get("edges", [])

    def get_corr(t1, t2):
        for e in edges:
            if (e["source"] == t1 and e["target"] == t2) or \
               (e["source"] == t2 and e["target"] == t1):
                return e["correlation"]
        return 0.0

    tlt_gld = get_corr("TLT", "GLD")
    xlk_xlf = get_corr("XLK", "XLF")
    spy_tlt = get_corr("SPY", "TLT")
    gld_uup = get_corr("GLD", "UUP")

    if tlt_gld > 0.7 and xlk_xlf < 0.3:
        return {"shift_detected": True, "type": "FLIGHT_TO_SAFETY",
                "action": "REDUCE_ALL_RISK_POSITIONS", "urgency": "HIGH",
                "reason": f"TLT/GLD corr={tlt_gld:.2f}, XLK/XLF diverging ({xlk_xlf:.2f})"}
    if spy_tlt < -0.6:
        return {"shift_detected": True, "type": "RISK_OFF_ROTATION",
                "action": "REDUCE_EQUITY_EXPOSURE", "urgency": "MEDIUM",
                "reason": f"SPY/TLT negative correlation ({spy_tlt:.2f})"}
    if gld_uup > 0.5:
        return {"shift_detected": True, "type": "DOLLAR_SAFE_HAVEN",
                "action": "MONITOR_CLOSELY", "urgency": "LOW",
                "reason": f"GLD/UUP rising together ({gld_uup:.2f})"}

    return {"shift_detected": False, "type": "NORMAL", "action": "NONE", "urgency": "NONE"}


if __name__ == "__main__":
    data  = build_correlation_matrix(lookback_days=20)
    shift = detect_regime_shift(data)
    print(f"Nodes: {data['node_count']} | Edges: {data['edge_count']}")
    print(f"Regime: {shift['type']} — {shift.get('reason','None')}")
