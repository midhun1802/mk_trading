"""
CHAKRA — Intraday Divergence Scanner
backend/chakra/divergence_scanner.py

Runs every 5 min (called from flow_monitor's scan loop).
Scans swing watchlist tickers + indexes for RSI divergence on 5-min bars.
Posts Discord alert when a NEW divergence forms (60-min per-ticker cooldown).
"""

import os
import json
import logging
import time
import requests
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("CHAKRA.Divergence")

BASE_DIR    = Path(__file__).resolve().parents[2]
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
DISCORD_URL = os.getenv("DISCORD_FLOW_SIGNALS", os.getenv("DISCORD_ALERTS", os.getenv("DISCORD_WEBHOOK_URL", "")))

ET = ZoneInfo("America/New_York")

# Cooldown: don't re-alert same ticker+type within 60 min
_COOLDOWN: dict[str, float] = {}  # key: "TICKER_TYPE" -> last_alert_ts
COOLDOWN_SECS = 3600

INDEX_TICKERS = ["SPY", "QQQ", "IWM"]

# ── Polygon 5-min bar fetch ───────────────────────────────────────────────────

def _fetch_bars(ticker: str, n_bars: int = 40) -> list:
    """Fetch last n 5-min bars from Polygon. Returns list of closes."""
    try:
        import httpx
        today = date.today().isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute/{today}/{today}",
            params={"adjusted": "true", "sort": "asc", "limit": n_bars, "apiKey": POLYGON_KEY},
            timeout=6,
        )
        results = r.json().get("results", [])
        return [float(b["c"]) for b in results if "c" in b]
    except Exception as e:
        log.debug(f"  [DivScan] bar fetch failed {ticker}: {e}")
        return []


# ── RSI series computation ────────────────────────────────────────────────────

def _rsi_series(closes: list, period: int = 14) -> list:
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_vals = []
    for i in range(period, len(closes)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi_vals.append(100 - (100 / (1 + rs)))
    return rsi_vals


# ── Discord post ──────────────────────────────────────────────────────────────

def _post_discord(ticker: str, div_type: str, strength: str, price: float, description: str):
    if not DISCORD_URL:
        return
    emoji = {"BULLISH": "📈", "BEARISH": "📉", "HIDDEN_BULL": "🔼", "HIDDEN_BEAR": "🔽"}.get(div_type, "📐")
    color = 0x00ff88 if "BULL" in div_type else 0xff4466
    label = div_type.replace("_", " ").title()
    now_et = datetime.now(ET).strftime("%H:%M ET")
    payload = {
        "embeds": [{
            "title":       f"{emoji} RSI Divergence — {ticker}",
            "description": f"**{label}** ({strength}) @ ${price:.2f}\n{description}",
            "color":       color,
            "footer":      {"text": f"CHAKRA Divergence Scanner • {now_et}"},
            "fields": [
                {"name": "Ticker",     "value": ticker,          "inline": True},
                {"name": "Type",       "value": label,           "inline": True},
                {"name": "Strength",   "value": strength or "—", "inline": True},
            ],
        }]
    }
    try:
        requests.post(DISCORD_URL, json=payload, timeout=5)
        log.info(f"  📐 Discord alert sent: {ticker} {label} {strength}")
    except Exception as e:
        log.debug(f"  [DivScan] Discord post failed: {e}")


# ── Single-ticker scan ────────────────────────────────────────────────────────

def scan_ticker_divergence(ticker: str) -> dict | None:
    """
    Scan one ticker for RSI divergence on 5-min bars.
    Returns divergence dict or None.
    """
    from backend.chakra.modules.rsi_divergence import detect_rsi_divergence

    closes = _fetch_bars(ticker, n_bars=40)
    if len(closes) < 16:
        return None

    rsi_vals = _rsi_series(closes)
    if len(rsi_vals) < 14:
        return None

    # Align: rsi_vals starts at index `period`, so align with closes[period:]
    aligned_closes = closes[len(closes) - len(rsi_vals):]
    div = detect_rsi_divergence(aligned_closes, rsi_vals, lookback=14)

    if not div.get("type"):
        return None

    return {
        "ticker":      ticker,
        "type":        div["type"],
        "strength":    div.get("strength"),
        "description": div.get("description", ""),
        "price":       closes[-1],
        "price_diff":  div.get("price_diff", 0),
        "rsi_diff":    div.get("rsi_diff", 0),
        "ts":          time.time(),
    }


# ── Main scan (called from flow_monitor) ─────────────────────────────────────

def run_divergence_scan() -> list:
    """
    Scan all watchlist + index tickers for RSI divergence.
    Posts Discord for new signals. Returns list of detected divergences.
    """
    now_et = datetime.now(ET)
    # Market hours only: 9:30 AM – 4:00 PM ET
    if not (now_et.weekday() < 5 and
            ((now_et.hour == 9 and now_et.minute >= 30) or now_et.hour > 9) and
            now_et.hour < 16):
        return []

    # Build ticker universe: indexes + swing watchlist
    tickers = list(INDEX_TICKERS)
    try:
        wf = BASE_DIR / "logs/chakra/watchlist_latest.json"
        if wf.exists():
            wd = json.loads(wf.read_text())
            cands = wd if isinstance(wd, list) else wd.get("candidates", wd.get("watchlist", []))
            for c in cands[:12]:  # top 12 from watchlist
                tk = c.get("ticker", "").upper()
                if tk and tk not in tickers:
                    tickers.append(tk)
    except Exception:
        pass

    found = []
    for ticker in tickers:
        try:
            result = scan_ticker_divergence(ticker)
            if not result:
                continue

            div_type = result["type"]
            strength = result.get("strength") or "MODERATE"
            cooldown_key = f"{ticker}_{div_type}"

            # Skip if in cooldown
            last = _COOLDOWN.get(cooldown_key, 0)
            if time.time() - last < COOLDOWN_SECS:
                log.debug(f"  [DivScan] {ticker} {div_type} in cooldown ({(time.time()-last)/60:.0f}m ago)")
                continue

            _COOLDOWN[cooldown_key] = time.time()
            found.append(result)
            log.info(f"  📐 DIVERGENCE: {ticker} {div_type} {strength} @ ${result['price']:.2f} — {result['description']}")
            _post_discord(ticker, div_type, strength, result["price"], result["description"])

        except Exception as e:
            log.debug(f"  [DivScan] {ticker} error: {e}")

    return found


# ── Standalone run ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    results = run_divergence_scan()
    print(json.dumps(results, indent=2, default=str))
