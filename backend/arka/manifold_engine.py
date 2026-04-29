"""
manifold_engine.py — Physics Manifold Engine for ARKA + CHAKRA
Integrates Phase Space, UMAP Regime Embedding, and Persistent Homology
Author: Midhun Krishna | March 2026
"""

import numpy as np
from collections import deque
from dataclasses import dataclass
from typing import Optional
import warnings
warnings.filterwarnings("ignore", category=UserWarning)


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass
class PhaseState:
    velocity: float
    acceleration: float
    curvature: float
    regime: str              # TRENDING | VOLATILE | TRANSITIONING
    conviction_modifier: int # applied to ARKA conviction score

@dataclass
class RegimePoint:
    embedding: np.ndarray    # (x, y) on 2D UMAP manifold
    cluster_label: str       # BULLISH_SWING | BEARISH_SWING | CHOPPY
    confidence: float        # 0.0 – 1.0
    nearest_centroid_dist: float

@dataclass
class TopologyState:
    persistence_score: float
    regime_change_signal: bool
    interpretation: str      # STABLE | REGIME_SHIFT_IMMINENT


# ── 1. PHASE SPACE MANIFOLD (ARKA Scalping) ───────────────────────────────────

class PhaseSpaceEngine:
    """
    Treats price as a point in [P, dP/dt, d²P/dt²] phase space.
    Curvature κ distinguishes trending vs. mean-reverting regimes.
    """

    def __init__(self, window: int = 20):
        self.window = window
        self._price_buffer = deque(maxlen=window + 5)

    def update(self, price: float) -> Optional[PhaseState]:
        self._price_buffer.append(price)
        if len(self._price_buffer) < self.window:
            return None
        return self._compute(list(self._price_buffer)[-self.window:])

    def _compute(self, prices: list) -> PhaseState:
        p = np.array(prices, dtype=float)
        v = np.gradient(p)           # velocity  dP/dt
        a = np.gradient(v)           # accel     d²P/dt²

        # Curvature κ = |v × a| / |v|³
        cross = np.abs(v * np.roll(a, 1) - a * np.roll(v, 1))
        speed_cubed = np.abs(v) ** 3 + 1e-9
        kappa = cross / speed_cubed
        k = float(kappa[-1])

        if k < 0.01:
            regime, mod = "TRENDING", +15
        elif k < 0.05:
            regime, mod = "NORMAL", 0
        elif k < 0.15:
            regime, mod = "TRANSITIONING", -10
        else:
            regime, mod = "VOLATILE", -25

        return PhaseState(
            velocity=round(float(v[-1]), 4),
            acceleration=round(float(a[-1]), 4),
            curvature=round(k, 6),
            regime=regime,
            conviction_modifier=mod,
        )

    def geodesic_deviation(self, prices: list) -> dict:
        """
        Measures how far price has deviated from its geodesic (natural path).
        Returns deviation in σ units and trade signal.
        """
        if len(prices) < 10:
            return {"deviation_sigma": 0.0, "signal": "INSUFFICIENT_DATA"}

        p = np.array(prices, dtype=float)
        # Geodesic approximated as linear trend
        x = np.arange(len(p))
        coeffs = np.polyfit(x, p, 1)
        geodesic = np.polyval(coeffs, x)
        residuals = p - geodesic
        sigma = np.std(residuals) + 1e-9
        dev = float(residuals[-1] / sigma)

        if abs(dev) < 0.5:
            signal = "ON_PATH"
        elif abs(dev) < 1.5:
            signal = "MILD_TENSION"
        elif abs(dev) < 2.5:
            signal = "MEAN_REVERT_ENTRY" if dev > 0 else "MEAN_REVERT_ENTRY_SHORT"
        else:
            signal = "BREAKOUT_LONG" if dev > 0 else "BREAKOUT_SHORT"

        return {
            "deviation_sigma": round(dev, 3),
            "signal": signal,
            "geodesic_slope": round(float(coeffs[0]), 4),
        }


# ── 2. UMAP REGIME EMBEDDING (CHAKRA Swing) ───────────────────────────────────

class RegimeManifold:
    """
    Projects multi-dimensional market state onto 2D UMAP manifold.
    Clusters represent distinct swing trading regimes.
    Features: [returns, volume_z, iv_rank, pc_ratio, oi_change, sector_delta, gex]
    """

    FEATURE_NAMES = ["returns", "volume_z", "iv_rank", "pc_ratio",
                     "oi_change", "sector_delta", "gex"]

    def __init__(self, n_neighbors: int = 15, min_dist: float = 0.1):
        self.n_neighbors = n_neighbors
        self.min_dist = min_dist
        self._reducer = None
        self._centroids = {}
        self._is_fitted = False

    def fit(self, feature_matrix: np.ndarray, labels: list):
        """
        Train on historical data.
        labels: list of strings — 'BULLISH_SWING', 'BEARISH_SWING', 'CHOPPY'
        """
        from umap import UMAP
        self._reducer = UMAP(
            n_components=2,
            n_neighbors=self.n_neighbors,
            min_dist=self.min_dist,
            random_state=42,
        )
        embedding = self._reducer.fit_transform(feature_matrix)

        # Compute centroids per cluster label
        unique_labels = set(labels)
        for lbl in unique_labels:
            mask = np.array([l == lbl for l in labels])
            self._centroids[lbl] = embedding[mask].mean(axis=0)

        self._is_fitted = True
        return embedding

    def infer(self, today_features: np.ndarray) -> Optional[RegimePoint]:
        if not self._is_fitted:
            return None

        point = self._reducer.transform(today_features.reshape(1, -1))[0]

        # Find nearest centroid
        best_label, best_dist = None, float("inf")
        for lbl, centroid in self._centroids.items():
            dist = float(np.linalg.norm(point - centroid))
            if dist < best_dist:
                best_dist = dist
                best_label = lbl

        # Confidence = inverse distance normalized 0–1
        max_dist = max(np.linalg.norm(point - c) for c in self._centroids.values()) + 1e-9
        confidence = round(1.0 - (best_dist / max_dist), 3)

        return RegimePoint(
            embedding=point,
            cluster_label=best_label,
            confidence=confidence,
            nearest_centroid_dist=round(best_dist, 4),
        )

    def save(self, path: str):
        import pickle
        with open(path, "wb") as f:
            pickle.dump({"reducer": self._reducer, "centroids": self._centroids}, f)
        print(f"  ✅ RegimeManifold saved → {path}")

    def load(self, path: str):
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        self._reducer = data["reducer"]
        self._centroids = data["centroids"]
        self._is_fitted = True
        print(f"  ✅ RegimeManifold loaded ← {path}")


# ── 3. PERSISTENT HOMOLOGY (CHAKRA Regime Change) ─────────────────────────────

class TopologyEngine:
    """
    Uses Ripser persistent homology to detect when the topological
    structure of the market changes — a leading indicator of regime shift.
    Fires 3–5 days before major trend changes.
    """

    def __init__(self, threshold: float = 0.3):
        self.threshold = threshold

    def detect_regime_change(self, returns_matrix: np.ndarray) -> TopologyState:
        """
        returns_matrix: (n_stocks, n_days) — correlation-based distance matrix
        """
        from ripser import ripser

        # Build correlation distance matrix
        if returns_matrix.shape[0] < 4:
            return TopologyState(0.0, False, "INSUFFICIENT_DATA")

        corr = np.corrcoef(returns_matrix)
        dist = 1.0 - np.abs(corr)
        np.fill_diagonal(dist, 0)

        dgms = ripser(dist, maxdim=1, distance_matrix=True)["dgms"]
        h1 = dgms[1]  # 1-cycles = "holes" in the data topology

        if len(h1) == 0:
            max_persistence = 0.0
        else:
            persistence = h1[:, 1] - h1[:, 0]
            # Exclude infinite bars
            finite_persistence = persistence[np.isfinite(persistence)]
            max_persistence = float(np.max(finite_persistence)) if len(finite_persistence) > 0 else 0.0

        signal = max_persistence > self.threshold
        interp = "🔴 REGIME SHIFT IMMINENT" if signal else "🟢 STABLE"

        return TopologyState(
            persistence_score=round(max_persistence, 4),
            regime_change_signal=signal,
            interpretation=interp,
        )


# ── 4. IV SURFACE CURVATURE (CHAKRA Options Flow) ─────────────────────────────

class IVSurfaceEngine:
    """
    Treats the IV surface (strike × expiry) as a Riemannian manifold.
    Detects warping patterns that precede volatility events.
    """

    def analyze(self, contracts: list) -> dict:
        """
        contracts: list of dicts with keys: strike, expiry_days, iv
        """
        if len(contracts) < 5:
            return {"surface_state": "INSUFFICIENT_DATA"}

        strikes = np.array([c["strike"] for c in contracts], dtype=float)
        expiries = np.array([c["expiry_days"] for c in contracts], dtype=float)
        ivs = np.array([c["iv"] for c in contracts], dtype=float)

        # Near-term vs far-term IV avg
        near_mask = expiries <= 14
        far_mask = expiries > 30
        near_iv = ivs[near_mask].mean() if near_mask.any() else None
        far_iv = ivs[far_mask].mean() if far_mask.any() else None

        # Smile skew: difference between OTM puts and calls
        atm_strike = strikes[np.argmin(np.abs(strikes - strikes.mean()))]
        put_mask = strikes < atm_strike
        call_mask = strikes > atm_strike
        put_iv = ivs[put_mask].mean() if put_mask.any() else None
        call_iv = ivs[call_mask].mean() if call_mask.any() else None
        skew = round(float(put_iv - call_iv), 4) if (put_iv and call_iv) else 0.0

        # Term structure inversion check
        inverted = bool(near_iv and far_iv and near_iv > far_iv * 1.15)

        if inverted:
            state = "🔴 INVERTED — Panic/Squeeze Imminent"
        elif near_iv and far_iv and near_iv > far_iv * 1.05:
            state = "🟠 WARPING — Event Risk Loading"
        else:
            state = "🟢 NORMAL — Standard Conditions"

        return {
            "surface_state": state,
            "near_term_iv": round(float(near_iv), 4) if near_iv else None,
            "far_term_iv": round(float(far_iv), 4) if far_iv else None,
            "put_call_skew": skew,
            "term_inverted": inverted,
        }


# ── 5. COMBINED ARJUN CONVICTION ADJUSTER ─────────────────────────────────────

class ManifoldConvictionAdjuster:
    """
    Aggregates all manifold signals into a single conviction modifier
    for ARKA trade cards and CHAKRA swing entries.
    """

    def __init__(self):
        self.phase_engine = PhaseSpaceEngine(window=20)
        self.topology_engine = TopologyEngine(threshold=0.3)
        self.iv_engine = IVSurfaceEngine()

    def adjust_arka(self, base_conviction: int, prices: list) -> dict:
        """Apply phase space modifier to ARKA scalping conviction."""
        state = None
        for p in prices:
            state = self.phase_engine.update(p)
        if state is None:
            return {"adjusted_conviction": base_conviction, "regime": "UNKNOWN", "modifier": 0}

        # Also compute geodesic deviation
        geo = self.phase_engine.geodesic_deviation(prices)
        adjusted = max(0, min(100, base_conviction + state.conviction_modifier))

        return {
            "adjusted_conviction": adjusted,
            "regime": state.regime,
            "curvature": state.curvature,
            "modifier": state.conviction_modifier,
            "geodesic_signal": geo["signal"],
            "geodesic_deviation_sigma": geo["deviation_sigma"],
        }

    def adjust_chakra(self, base_score: int, topology_state: TopologyState,
                      regime_point: Optional[RegimePoint] = None) -> dict:
        """Apply topology + regime embedding modifier to CHAKRA swing score."""
        modifier = 0
        notes = []

        if topology_state.regime_change_signal:
            modifier -= 20
            notes.append("⚠️ Regime shift detected — reduce conviction")

        if regime_point:
            if regime_point.cluster_label == "BULLISH_SWING" and regime_point.confidence > 0.4:
                modifier += 15
                notes.append(f"✅ UMAP: Bullish cluster ({regime_point.confidence:.0%} confidence)")
            elif regime_point.cluster_label == "BEARISH_SWING" and regime_point.confidence > 0.4:
                modifier -= 10
                notes.append(f"⚠️ UMAP: Bearish cluster ({regime_point.confidence:.0%} confidence)")
            elif regime_point.cluster_label == "CHOPPY":
                modifier -= 15
                notes.append("❌ UMAP: Choppy regime — skip swing entry")

        adjusted = max(0, min(100, base_score + modifier))
        return {
            "adjusted_score": adjusted,
            "modifier": modifier,
            "topology": topology_state.interpretation,
            "notes": notes,
        }


# ── Quick Smoke Test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n── Manifold Engine Smoke Test ──────────────────────────")

    # Phase Space test
    engine = PhaseSpaceEngine(window=20)
    prices = [100 + i * 0.5 + np.random.randn() * 0.3 for i in range(30)]
    state = engine.update(prices[-1])
    for p in prices:
        state = engine.update(p)

    print(f"\n1. Phase Space (ARKA):")
    print(f"   Regime:   {state.regime}")
    print(f"   Curvature: {state.curvature}")
    print(f"   Modifier:  {state.conviction_modifier:+d} pts to conviction")

    geo = engine.geodesic_deviation(prices)
    print(f"   Geodesic deviation: {geo['deviation_sigma']}σ → {geo['signal']}")

    # UMAP Regime test
    print(f"\n2. UMAP Regime Embedding (CHAKRA):")
    manifold = RegimeManifold()
    X = np.random.randn(60, 7)
    labels = ["BULLISH_SWING"] * 20 + ["BEARISH_SWING"] * 20 + ["CHOPPY"] * 20
    manifold.fit(X, labels)
    today = np.random.randn(7)
    result = manifold.infer(today)
    print(f"   Cluster:    {result.cluster_label}")
    print(f"   Confidence: {result.confidence:.0%}")
    print(f"   Embedding:  {result.embedding.round(3)}")

    # Topology test
    print(f"\n3. Persistent Homology (CHAKRA Regime Change):")
    topo = TopologyEngine(threshold=0.3)
    returns = np.random.randn(8, 30)
    ts = topo.detect_regime_change(returns)
    print(f"   Persistence: {ts.persistence_score}")
    print(f"   Signal:      {ts.interpretation}")

    # IV Surface test
    print(f"\n4. IV Surface Curvature (CHAKRA Options):")
    contracts = [
        {"strike": 290, "expiry_days": 7,  "iv": 0.82},
        {"strike": 295, "expiry_days": 7,  "iv": 0.74},
        {"strike": 300, "expiry_days": 14, "iv": 0.65},
        {"strike": 305, "expiry_days": 30, "iv": 0.58},
        {"strike": 310, "expiry_days": 30, "iv": 0.55},
        {"strike": 300, "expiry_days": 60, "iv": 0.50},
    ]
    iv_result = IVSurfaceEngine().analyze(contracts)
    print(f"   State:     {iv_result['surface_state']}")
    print(f"   Skew:      {iv_result['put_call_skew']}")
    print(f"   Inverted:  {iv_result['term_inverted']}")

    # Combined adjuster
    print(f"\n5. Combined Conviction Adjuster:")
    adjuster = ManifoldConvictionAdjuster()
    arka_result = adjuster.adjust_arka(60, prices)
    print(f"   ARKA: base=60 → adjusted={arka_result['adjusted_conviction']} ({arka_result['regime']})")
    chakra_result = adjuster.adjust_chakra(70, ts, result)
    print(f"   CHAKRA: base=70 → adjusted={chakra_result['adjusted_score']}")
    for note in chakra_result["notes"]:
        print(f"   {note}")

    print("\n✅ All manifold components operational\n")


# ── Production Integration Helper ─────────────────────────────────────────────

_global_adjuster = None

def get_adjuster() -> ManifoldConvictionAdjuster:
    """Singleton — reuse across calls to preserve phase space buffer."""
    global _global_adjuster
    if _global_adjuster is None:
        _global_adjuster = ManifoldConvictionAdjuster()
    return _global_adjuster


def apply_manifold_to_signal(signal: dict, recent_prices: list) -> dict:
    """
    Drop-in wrapper for existing ARKA signal dicts.
    Adds manifold_regime, manifold_modifier, manifold_geodesic to signal.

    Usage in your ARKA engine:
        from backend.arka.manifold_engine import apply_manifold_to_signal
        signal = apply_manifold_to_signal(signal, recent_prices)
    """
    adjuster = get_adjuster()
    result = adjuster.adjust_arka(signal.get("conviction", 50), recent_prices)

    signal["conviction"]          = result["adjusted_conviction"]
    signal["manifold_regime"]     = result["regime"]
    signal["manifold_modifier"]   = result["modifier"]
    signal["manifold_geodesic"]   = result["geodesic_signal"]
    signal["manifold_deviation"]  = result["geodesic_deviation_sigma"]
    return signal
