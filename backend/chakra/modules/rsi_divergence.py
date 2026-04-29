"""
CHAKRA — RSI Divergence Detector
backend/chakra/modules/rsi_divergence.py

Detects bullish, bearish, hidden bullish, and hidden bearish divergence
between price and RSI over a configurable lookback window.
"""

from typing import Optional


def detect_rsi_divergence(
    prices: list,
    rsi_values: list,
    lookback: int = 14
) -> dict:
    """
    Detect RSI divergence between price and RSI.

    Args:
        prices     : list of closing prices (oldest → newest)
        rsi_values : list of RSI values aligned with prices
        lookback   : number of bars to scan for swing points (default 14)

    Returns dict:
        {
          'type'       : 'BULLISH' | 'BEARISH' | 'HIDDEN_BULL' | 'HIDDEN_BEAR' | None,
          'strength'   : 'STRONG' | 'MODERATE' | None,
          'description': str,
          'price_diff' : float,   # magnitude of price divergence
          'rsi_diff'   : float,   # magnitude of RSI divergence
        }
    """
    if len(prices) < lookback or len(rsi_values) < lookback:
        return _no_divergence("Insufficient data for divergence detection")

    p = prices[-lookback:]
    r = rsi_values[-lookback:]

    # ── Find swing LOWS (local minima) ─────────────────────────────────
    price_lows = [
        (i, p[i]) for i in range(1, len(p) - 1)
        if p[i] < p[i - 1] and p[i] < p[i + 1]
    ]
    rsi_lows = [
        (i, r[i]) for i in range(1, len(r) - 1)
        if r[i] < r[i - 1] and r[i] < r[i + 1]
    ]

    # ── Find swing HIGHS (local maxima) ────────────────────────────────
    price_highs = [
        (i, p[i]) for i in range(1, len(p) - 1)
        if p[i] > p[i - 1] and p[i] > p[i + 1]
    ]
    rsi_highs = [
        (i, r[i]) for i in range(1, len(r) - 1)
        if r[i] > r[i - 1] and r[i] > r[i + 1]
    ]

    # ── BULLISH DIVERGENCE: price Lower Low, RSI Higher Low ────────────
    if len(price_lows) >= 2 and len(rsi_lows) >= 2:
        p_low1, p_low2 = price_lows[-2][1], price_lows[-1][1]   # older, newer
        r_low1, r_low2 = rsi_lows[-2][1],   rsi_lows[-1][1]

        if p_low2 < p_low1 and r_low2 > r_low1:
            rsi_gap    = r_low2 - r_low1
            price_gap  = p_low1 - p_low2
            strength   = 'STRONG' if rsi_gap > 5 else 'MODERATE'
            return {
                'type':        'BULLISH',
                'strength':    strength,
                'description': (
                    f"Bullish divergence: price LL "
                    f"({p_low2:.2f} vs {p_low1:.2f}), "
                    f"RSI HL ({r_low2:.1f} vs {r_low1:.1f})"
                ),
                'price_diff': round(price_gap, 4),
                'rsi_diff':   round(rsi_gap, 2),
            }

    # ── BEARISH DIVERGENCE: price Higher High, RSI Lower High ──────────
    if len(price_highs) >= 2 and len(rsi_highs) >= 2:
        p_hi1, p_hi2 = price_highs[-2][1], price_highs[-1][1]
        r_hi1, r_hi2 = rsi_highs[-2][1],   rsi_highs[-1][1]

        if p_hi2 > p_hi1 and r_hi2 < r_hi1:
            rsi_gap   = r_hi1 - r_hi2
            price_gap = p_hi2 - p_hi1
            strength  = 'STRONG' if rsi_gap > 5 else 'MODERATE'
            return {
                'type':        'BEARISH',
                'strength':    strength,
                'description': (
                    f"Bearish divergence: price HH "
                    f"({p_hi2:.2f} vs {p_hi1:.2f}), "
                    f"RSI LH ({r_hi2:.1f} vs {r_hi1:.1f})"
                ),
                'price_diff': round(price_gap, 4),
                'rsi_diff':   round(rsi_gap, 2),
            }

    # ── HIDDEN BULLISH: price Higher Low, RSI Lower Low (trend continuation LONG)
    if len(price_lows) >= 2 and len(rsi_lows) >= 2:
        p_low1, p_low2 = price_lows[-2][1], price_lows[-1][1]
        r_low1, r_low2 = rsi_lows[-2][1],   rsi_lows[-1][1]

        if p_low2 > p_low1 and r_low2 < r_low1:
            return {
                'type':        'HIDDEN_BULL',
                'strength':    'MODERATE',
                'description': 'Hidden bullish divergence — trend continuation LONG',
                'price_diff':  round(p_low2 - p_low1, 4),
                'rsi_diff':    round(r_low1 - r_low2, 2),
            }

    # ── HIDDEN BEARISH: price Lower High, RSI Higher High (trend continuation SHORT)
    if len(price_highs) >= 2 and len(rsi_highs) >= 2:
        p_hi1, p_hi2 = price_highs[-2][1], price_highs[-1][1]
        r_hi1, r_hi2 = rsi_highs[-2][1],   rsi_highs[-1][1]

        if p_hi2 < p_hi1 and r_hi2 > r_hi1:
            return {
                'type':        'HIDDEN_BEAR',
                'strength':    'MODERATE',
                'description': 'Hidden bearish divergence — trend continuation SHORT',
                'price_diff':  round(p_hi1 - p_hi2, 4),
                'rsi_diff':    round(r_hi2 - r_hi1, 2),
            }

    return _no_divergence("No divergence detected")


def _no_divergence(reason: str) -> dict:
    return {
        'type':        None,
        'strength':    None,
        'description': reason,
        'price_diff':  0.0,
        'rsi_diff':    0.0,
    }


def score_divergence(div: dict) -> tuple[int, Optional[str]]:
    """
    Convert a divergence result into (points, direction_override).
    Call this from within score_swing_candidate().

    Returns:
        (points_to_add, direction_override)  e.g. (20, 'CALL') or (0, None)
    """
    t = div.get('type')
    s = div.get('strength')

    if t == 'BULLISH':
        pts = 20 if s == 'STRONG' else 12
        return pts, 'CALL'
    elif t == 'BEARISH':
        pts = 20 if s == 'STRONG' else 12
        return pts, 'PUT'
    elif t in ('HIDDEN_BULL', 'HIDDEN_BEAR'):
        return 8, None   # bonus points, no direction override
    return 0, None


# ── Quick self-test ─────────────────────────────────────────────────────
if __name__ == '__main__':
    import random
    random.seed(42)

    # Synthetic bullish divergence: price LL, RSI HL
    prices = [100, 98, 96, 99, 97, 95, 98, 96, 94, 97, 95, 92, 96, 94, 91]
    rsiv   = [45,  43, 41, 44, 42, 40, 43, 41, 39, 42, 40, 38, 41, 40, 39]
    # Make RSI show higher low vs price lower low on last swing
    rsiv[-1] = 41   # RSI higher low
    prices[-1] = 89 # price lower low

    result = detect_rsi_divergence(prices, rsiv, lookback=14)
    print("Test 1 — Expected BULLISH:")
    print(f"  type={result['type']} strength={result['strength']}")
    print(f"  {result['description']}")

    pts, direction = score_divergence(result)
    print(f"  → {pts} points, direction override: {direction}")

    # Synthetic bearish divergence
    prices2 = [100, 102, 104, 101, 103, 106, 103, 105, 108, 105, 107, 111, 108, 110, 114]
    rsiv2   = [55,  57,  59,  56,  58,  62,  59,  61,  64,  61,  63,  65,  62,  64,  63]
    rsiv2[-1] = 61  # RSI lower high

    result2 = detect_rsi_divergence(prices2, rsiv2, lookback=14)
    print("\nTest 2 — Expected BEARISH:")
    print(f"  type={result2['type']} strength={result2['strength']}")
    print(f"  {result2['description']}")
    pts2, dir2 = score_divergence(result2)
    print(f"  → {pts2} points, direction override: {dir2}")

    print("\n✅ rsi_divergence.py self-test complete")
