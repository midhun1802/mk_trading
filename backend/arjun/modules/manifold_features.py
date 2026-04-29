from sklearn.manifold import Isomap
import numpy as np

def extract_manifold_features(indicator_matrix: np.ndarray, n_components: int = 5) -> np.ndarray:
    """
    Reduce 20 technical indicators to n_components manifold coordinates.
    Use Isomap (preserves geodesic distances — better than PCA for market data).

    Usage in training:
        X_manifold = extract_manifold_features(X_raw, n_components=5)
        xgb_model.fit(X_manifold, y_labels)
    """
    isomap = Isomap(n_neighbors=10, n_components=n_components)
    return isomap.fit_transform(indicator_matrix)


# ── MANIFOLD FIX — Ricci Curvature + Regime Classifier (Mastermind S1) ──────
import numpy as np
from scipy.spatial.distance import pdist, squareform


def compute_ricci_curvature(coords):
    """
    Ollivier-Ricci curvature approximation via W1 transport.
    Compares local neighbor distribution vs global expected distance.
    High positive = curved/clustered (regime shift zone).
    Near zero     = flat/trending (stable manifold).
    Negative      = saddle/dispersing (expansion phase).
    coords: np.ndarray shape (n, 3)
    """
    n = len(coords)
    ricci = np.zeros(n)
    if n < 4:
        return ricci
    dists = squareform(pdist(coords, "euclidean"))
    global_mean = np.mean(dists[dists > 0])   # baseline: avg distance across all pairs
    for i in range(n):
        k         = min(8, n - 1)
        neighbors = np.argsort(dists[i])[1:k + 1]
        local_avg = np.mean(dists[i, neighbors])
        # Ricci > 0: locally tighter than global average (curved/attractor)
        # Ricci < 0: locally more spread than global average (saddle/dispersing)
        ricci[i]  = (global_mean - local_avg) / (global_mean + 1e-6)
    return ricci


def classify_manifold_regime(ricci_curvature):
    """
    Classifies market state from manifold geometry.
    Returns ARJUN score modifier + regime label.

    INFLECTION   spike > 2.5      reversal brewing      +15 pts
    FLAT         mean < 0.3       random walk no edge   -20 pts
    FOLDED       std  > 0.8       regime transition     -10 pts
    SMOOTH_TREND otherwise        clean structure        +8 pts
    """
    mean_curv = float(np.mean(np.abs(ricci_curvature)))
    max_spike  = float(np.max(np.abs(ricci_curvature)))
    std_curv   = float(np.std(ricci_curvature))

    if max_spike > 2.5:
        regime, arjun_mod = "INFLECTION",    +15
    elif mean_curv < 0.3 and std_curv < 0.1:
        regime, arjun_mod = "FLAT",          -20
    elif std_curv > 0.8:
        regime, arjun_mod = "FOLDED",        -10
    else:
        regime, arjun_mod = "SMOOTH_TREND",  +8

    return {
        "regime":               regime,
        "arjun_score_modifier": arjun_mod,
        "mean_curvature":       round(mean_curv, 3),
        "max_spike":            round(max_spike, 3),
        "std_curvature":        round(std_curv,  3),
    }
