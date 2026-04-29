"""
ARJUN Temporal Transformer — Module 7 (Mastermind Session 4)
30-day sequence model across 8 market features.
Captures temporal patterns XGBoost treats as independent days.
Trained weekly alongside XGBoost. At signal time, weighted 30% 
alongside XGBoost 70% until accuracy proven.
"""
import json, logging
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

log          = logging.getLogger(__name__)
MODEL_PATH   = Path("logs/arjun/transformer.pth")
FEATURE_COLS = ["price_change", "vix", "dex", "hurst",
                "vrp", "entropy", "neural_pulse", "volume_ratio"]
SEQ_LEN      = 30
N_CLASSES    = 3   # 0=SELL 1=HOLD 2=BUY
LABEL_MAP    = {0: "SELL", 1: "HOLD", 2: "BUY"}
REVERSE_MAP  = {"SELL": 0, "HOLD": 1, "BUY": 2}


class MarketTransformer(nn.Module):
    def __init__(self, n_features=8, seq_len=30, n_heads=4, n_layers=2):
        super().__init__()
        self.embedding   = nn.Linear(n_features, 64)
        self.pos_enc     = nn.Embedding(seq_len, 64)
        encoder_layer    = nn.TransformerEncoderLayer(
            d_model=64, nhead=n_heads, batch_first=True, dropout=0.1
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
        self.classifier  = nn.Linear(64, N_CLASSES)

    def forward(self, x):
        # x: (batch, seq_len, n_features)
        pos = torch.arange(x.size(1)).unsqueeze(0).to(x.device)
        x   = self.embedding(x) + self.pos_enc(pos)
        x   = self.transformer(x)
        return self.classifier(x[:, -1, :])  # last timestep


def build_sequences(df, seq_len=SEQ_LEN):
    """Convert flat DataFrame to (X_seq, y) for training."""
    import pandas as pd
    feats = df[FEATURE_COLS].fillna(0).values.astype(np.float32)
    # Normalize each feature to 0-1
    col_min  = feats.min(axis=0)
    col_max  = feats.max(axis=0)
    col_rng  = np.where((col_max - col_min) > 1e-9, col_max - col_min, 1.0)
    feats    = (feats - col_min) / col_rng

    X, y = [], []
    for i in range(seq_len, len(feats)):
        X.append(feats[i - seq_len:i])
        # Label: next-day return direction
        ret = df["next_day_return"].iloc[i] if "next_day_return" in df.columns else 0
        y.append(2 if ret > 0.003 else 0 if ret < -0.003 else 1)

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int64)


def train(df, epochs=60, lr=0.001) -> MarketTransformer:
    X, y = build_sequences(df)
    if len(X) < 10:
        raise ValueError(f"Need 40+ rows to train, got {len(df)}")

    model     = MarketTransformer()
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.CrossEntropyLoss()
    X_t       = torch.FloatTensor(X)
    y_t       = torch.LongTensor(y)

    model.train()
    for epoch in range(epochs):
        optimizer.zero_grad()
        out  = model(X_t)
        loss = criterion(out, y_t)
        loss.backward()
        optimizer.step()
        if (epoch + 1) % 20 == 0:
            log.debug(f"[Transformer] Epoch {epoch+1}/{epochs}  loss={loss.item():.4f}")

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), MODEL_PATH)
    log.info(f"[Transformer] Model saved → {MODEL_PATH}")
    return model


def predict(sequence: np.ndarray) -> dict:
    """
    sequence: (30, 8) numpy array of recent market features.
    Returns signal + probabilities.
    """
    model = MarketTransformer()
    if not MODEL_PATH.exists():
        return {"signal": "HOLD", "confidence": 50.0,
                "probs": {}, "note": "Model not trained yet"}
    model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
    model.eval()

    with torch.no_grad():
        x      = torch.FloatTensor(sequence).unsqueeze(0)  # (1,30,8)
        logits = model(x)
        probs  = torch.softmax(logits, dim=-1)[0].numpy()

    pred   = int(np.argmax(probs))
    conf   = float(probs[pred]) * 100
    return {
        "signal":     LABEL_MAP[pred],
        "confidence": round(conf, 1),
        "probs": {
            "SELL": round(float(probs[0]), 4),
            "HOLD": round(float(probs[1]), 4),
            "BUY":  round(float(probs[2]), 4),
        },
    }


def _make_synthetic_df(n=200):
    """Synthetic training data for unit test."""
    import pandas as pd
    np.random.seed(42)
    returns = np.random.randn(n) * 0.008
    return pd.DataFrame({
        "price_change":   returns,
        "vix":            np.random.uniform(12, 35, n),
        "dex":            np.random.uniform(30, 80, n),
        "hurst":          np.random.uniform(0.3, 0.7, n),
        "vrp":            np.random.uniform(0.7, 1.5, n),
        "entropy":        np.random.uniform(0.5, 2.5, n),
        "neural_pulse":   np.random.uniform(20, 80, n),
        "volume_ratio":   np.random.uniform(0.5, 2.5, n),
        "next_day_return": np.concatenate([returns[1:], [0]]),
    })


if __name__ == "__main__":
    import pandas as pd
    print("Training Temporal Transformer on synthetic data...")
    df    = _make_synthetic_df(200)
    model = train(df, epochs=60)
    print(f"✅  Model trained + saved → {MODEL_PATH}")

    # Test prediction with random 30-day sequence
    seq    = np.random.randn(SEQ_LEN, len(FEATURE_COLS)).astype(np.float32)
    result = predict(seq)
    print(f"\nTest prediction:")
    print(f"  Signal:     {result['signal']}")
    print(f"  Confidence: {result['confidence']}%")
    print(f"  Probs:      SELL={result['probs']['SELL']:.3f}  "
          f"HOLD={result['probs']['HOLD']:.3f}  "
          f"BUY={result['probs']['BUY']:.3f}")
    print(f"  Saved:      {MODEL_PATH}")
