from typing import Dict, List

def detect_smart_money_activity(ticker: str, trades: list, lookback_minutes: int = 60) -> Dict:
    """
    Scan for institutional dark pool positioning signals.
    Exchange code 4 (TRF) = dark pool trade.
    """
    dp_trades   = [t for t in trades if t.get("exchange") == 4]
    buy_volume  = sum(t["size"] for t in dp_trades if t.get("side") == "buy")
    sell_volume = sum(t["size"] for t in dp_trades if t.get("side") == "sell")

    if buy_volume > sell_volume * 1.5:
        bias       = 'BULLISH'
        conviction = min(100, int((buy_volume / max(sell_volume, 1) - 1) * 50))
    elif sell_volume > buy_volume * 1.5:
        bias       = 'BEARISH'
        conviction = min(100, int((sell_volume / max(buy_volume, 1) - 1) * 50))
    else:
        bias, conviction = 'NEUTRAL', 0

    return {
        'dark_pool_bias':   bias,
        'dark_pool_volume': buy_volume + sell_volume,
        'conviction':       conviction
    }

def detect_unusual_options(options_chain: list) -> List[Dict]:
    """Flag contracts where volume > OI * 3 (unusual activity)."""
    unusual = []
    for contract in options_chain:
        volume = contract.get("day", {}).get("volume", 0)
        oi     = contract.get("open_interest", 0)
        if oi > 0 and volume > oi * 3:
            unusual.append({
                "contract": contract.get("ticker", ""),
                "volume":   volume,
                "oi":       oi,
                "ratio":    round(volume / oi, 1)
            })
    return sorted(unusual, key=lambda x: -x["ratio"])[:5]
