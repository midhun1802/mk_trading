from typing import Dict, List

def detect_dark_pool_activity(trades: list) -> Dict:
    """Filter TRF trades and compute directional bias.
    Polygon v3/trades fields: s=size, p=price, x=exchange, c=conditions[]
    TRF exchanges: 4=FINRA ADF, 19=OTC, exchange>=50 = TRF venues
    """
    def _size(t):
        # handle both raw Polygon format (s) and pre-parsed format (size)
        if isinstance(t, dict):
            return int(t.get("s") or t.get("size") or 0)
        return 0

    def _is_dark(t):
        if not isinstance(t, dict): return False
        exch = t.get("x") or t.get("exchange") or 0
        # TRF = exchange >= 4 and not a lit exchange (NYSE=1, NASDAQ=2, etc.)
        return int(exch) not in (1, 2, 3, 8, 11, 12)

    def _side(t):
        # conditions list: 41=above ask (buy aggressor), 42=below bid (sell)
        conds = t.get("c") or t.get("conditions") or []
        if isinstance(conds, list):
            if 41 in conds: return "buy"
            if 42 in conds: return "sell"
        return t.get("side", "unknown")

    dp_trades   = [t for t in trades if _is_dark(t)]
    buy_volume  = sum(_size(t) for t in dp_trades if _side(t) == "buy")
    sell_volume = sum(_size(t) for t in dp_trades if _side(t) == "sell")
    total_vol   = sum(_size(t) for t in dp_trades)

    if buy_volume > sell_volume * 1.5:
        return {"bias": "BULLISH", "volume": buy_volume,  "total": total_vol, "score": 75}
    elif sell_volume > buy_volume * 1.5:
        return {"bias": "BEARISH", "volume": sell_volume, "total": total_vol, "score": 75}
    return {"bias": "NEUTRAL", "volume": total_vol, "total": total_vol, "score": 0}

def detect_unusual_options(options_chain: list) -> List[Dict]:
    """Flag contracts where volume > OI * 3."""
    unusual = []
    for c in options_chain:
        volume = c.get("day", {}).get("volume", 0)
        oi     = c.get("open_interest", 0)
        if oi > 0 and volume > oi * 3:
            unusual.append({"contract": c.get("ticker",""), "volume": volume,
                            "oi": oi, "ratio": round(volume / oi, 1)})
    return sorted(unusual, key=lambda x: -x["ratio"])[:5]
