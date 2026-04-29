import numpy as np
from scipy.interpolate import splprep, splev

def calculate_price_curvature(prices: np.ndarray, lookback: int = 20) -> float:
    """
    Calculate scalar curvature of recent price path.
    High curvature = sharp turns = elevated risk.
    Low curvature  = smooth trend = stable regime.
    """
    if len(prices) < lookback:
        return 0.0
    recent = prices[-lookback:]
    t      = np.arange(len(recent))
    try:
        tck, u        = splprep([t, recent], s=0, k=3)
        dx_du, dy_du  = splev(u, tck, der=1)
        d2x, d2y      = splev(u, tck, der=2)
        numerator     = np.abs(dx_du * d2y - dy_du * d2x)
        denominator   = (dx_du**2 + dy_du**2)**1.5
        return float(np.mean(numerator / (denominator + 1e-8)))
    except Exception:
        return 0.0

def assess_curvature_regime(prices, gex_regime="POSITIVE_GAMMA", base_size=0.02):
    """Return position size multiplier based on curvature + GEX regime."""
    curvature = calculate_price_curvature(np.array(prices))
    if curvature > 0.5:
        regime, size = 'HIGH_RISK',      0.01
    elif curvature > 0.2:
        regime, size = 'MODERATE_RISK',  0.015
    else:
        regime, size = 'LOW_RISK',       base_size

    if gex_regime == 'NEGATIVE_GAMMA':
        size *= 0.5   # Halve in negative gamma

    return {'regime': regime, 'curvature': round(curvature, 6), 'position_size': size}
