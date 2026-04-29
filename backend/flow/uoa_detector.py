"""
CHAKRA Unusual Options Activity (UOA) Detector
Flags contracts where volume > open_interest * 3.
Fetches from Polygon Options snapshot.
"""
import os, httpx
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")


def detect_unusual_options(ticker: str, multiplier: float = 3.0) -> dict:
    """Scan options chain for volume > OI * multiplier."""
    try:
        r = httpx.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={"apiKey": POLYGON_KEY, "limit": 100,
                    "expiration_date": date.today().isoformat()},
            timeout=12,
        )
        contracts = r.json().get("results", [])
    except Exception as e:
        return {"ticker": ticker, "unusual": [], "error": str(e)}

    unusual = []
    for c in contracts:
        vol = c.get("day", {}).get("volume", 0)
        oi  = c.get("open_interest", 0) or 1
        if vol > oi * multiplier:
            details = c.get("details", {})
            unusual.append({
                "contract":    details.get("ticker", ""),
                "type":        details.get("contract_type", ""),
                "strike":      details.get("strike_price", 0),
                "expiry":      details.get("expiration_date", ""),
                "volume":      vol,
                "oi":          oi,
                "ratio":       round(vol / oi, 1),
                "mark":        c.get("day", {}).get("close", 0),
                "implied_vol": c.get("implied_volatility", 0),
            })

    unusual = sorted(unusual, key=lambda x: -x["ratio"])[:10]
    buy_pressure  = sum(1 for u in unusual if u["type"] == "call")
    sell_pressure = sum(1 for u in unusual if u["type"] == "put")

    result = {
        "ticker":        ticker,
        "unusual":       unusual,
        "count":         len(unusual),
        "buy_pressure":  buy_pressure,
        "sell_pressure": sell_pressure,
        "bias":          "BULLISH" if buy_pressure > sell_pressure
                         else "BEARISH" if sell_pressure > buy_pressure
                         else "NEUTRAL",
        "date":          date.today().isoformat(),
    }

    # ── Session 3: Iceberg upgrade ────────────────────────────────────
    try:
        from backend.chakra.modules.iceberg_detector import upgrade_uoa_with_iceberg
        result = upgrade_uoa_with_iceberg(result)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    import json
    for t in ["SPY", "QQQ"]:
        result = detect_unusual_options(t)
        print(f"{t}: {result['count']} UOA contracts | Bias: {result['bias']}")
        for u in result["unusual"][:3]:
            print(f"  {u['contract']} vol={u['volume']} oi={u['oi']} ratio={u['ratio']}x")
