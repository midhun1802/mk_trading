#!/usr/bin/env python3
"""
ARKA Swings Engine
==================
Swing trading module under the ARKA brand.
Replaces CHAKRA/TARAKA swing logic.

Strategies:
  ARKA-SWING  — multi-day stock swings (2–10 days)
  ARKA-LONG   — longer-term position trades (2–4 weeks)

Usage:
  python3 -m backend.arka.arka_swings                  # entry scan
  python3 -m backend.arka.arka_swings --monitor        # monitor positions
  python3 -m backend.arka.arka_swings --premarket      # pre-market watchlist
  python3 -m backend.arka.arka_swings --postmarket     # post-market scan
  python3 -m backend.arka.arka_swings --status         # show open positions
"""

import os, sys, json, logging, sqlite3, argparse
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import httpx
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
load_dotenv(BASE / ".env", override=True)

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.Swings")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARKA-SWINGS] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY     = os.getenv("POLYGON_API_KEY", "")
ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET   = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE     = "https://paper-api.alpaca.markets"

DB_PATH         = BASE / "logs/swings/swings_v3.db"
LOG_DIR         = BASE / "logs/swings"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Strategy thresholds ───────────────────────────────────────────────────────
MIN_SCORE           = 70    # minimum score to enter (screener) — raised from 60
MIN_SCORE_DISCORD   = 75    # minimum score to POST to Discord (higher bar)
MIN_PRICE           = 5.0   # min stock price
MAX_PRICE           = 500.0 # max stock price
MIN_VOLUME          = 500_000  # min avg daily volume
MAX_HOLD_DAYS       = 28    # max hold period (4 weeks hard cap)
MAX_DTE             = 28    # max DTE for swing options (4 weeks hard cap)
CONTRACTS_PER_TRADE = 1     # always 1 contract — fixed size
STOP_LOSS_PCT       = 0.25  # 25% stop loss on premium
TP1_PCT             = 0.35  # 35% first target → R/R = 1:1.4
TP2_PCT             = 0.60  # 60% runner target if we ever do 2+ contracts
MAX_POSITIONS       = 3     # max concurrent swing positions
MAX_DISCORD_ALERTS  = 5     # max swing Discord alerts per day
POSITION_SIZE_PCT   = 0.10  # 10% of buying power per swing trade

# ── TESTING MODE — override sizing ───────────────────────────────────────────
SWING_BUDGET        = 2000.0  # max $ per swing trade
MAX_CONTRACTS       = 1       # never more than 1 contract during testing

# ── Screener universe ─────────────────────────────────────────────────────────
# Base watchlist — flow monitor and ARJUN signals supplement this
BASE_UNIVERSE = [
    # Index ETFs — swing plays on major indexes and commodities
    "SPY","QQQ","IWM","DIA","GLD","SLV",
    # Large cap momentum
    "AAPL","NVDA","TSLA","MSFT","AMZN","META","GOOGL","AMD","NFLX","CRM",
    # ETFs for sector plays
    "XLK","XLF","XLE","XLV","XLI","ARKK","SOXX","IBB",
    # High momentum / volatility
    "MSTR","COIN","HOOD","RBLX","PLTR","IONQ","SMCI",
    # Bearish swings: use PUT options on underlying — no inverse ETFs
    # (SQQQ/SH/UVXY/SPXS removed — CHAKRA trades options only, not equity)
]

# ── Webhook — all swing alerts go to #arjun-alerts ───────────────────────────
WH_ARJUN_ALERTS   = os.getenv("DISCORD_ARJUN_ALERTS", os.getenv("DISCORD_ALERTS", ""))
# Keep these for any legacy references — but we don't post to them directly
WH_SWINGS_SIGNALS = WH_ARJUN_ALERTS
WH_SWINGS_EXTREME = WH_ARJUN_ALERTS
WH_ALERTS         = WH_ARJUN_ALERTS


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS arka_swings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT    NOT NULL,
            strategy    TEXT    DEFAULT 'ARKA-SWING',
            direction   TEXT    DEFAULT 'LONG',
            entry_price REAL,
            stop_loss   REAL,
            tp1         REAL,
            tp2         REAL,
            qty         INTEGER DEFAULT 0,
            score       INTEGER DEFAULT 0,
            entry_date  TEXT,
            exit_date   TEXT,
            exit_price  REAL,
            pnl         REAL,
            pnl_pct     REAL,
            tp1_hit     INTEGER DEFAULT 0,
            hold_days   INTEGER DEFAULT 0,
            status      TEXT    DEFAULT 'OPEN',
            catalyst    TEXT,
            notes       TEXT,
            order_id    TEXT
        )
    """)
    db.commit()
    return db


def get_open_positions():
    db  = get_db()
    rows = db.execute("SELECT * FROM arka_swings WHERE status='OPEN'").fetchall()
    db.close()
    return [dict(r) for r in rows]


def save_position(pos: dict):
    db = get_db()
    db.execute("""
        INSERT INTO arka_swings
        (ticker, strategy, direction, entry_price, stop_loss, tp1, tp2,
         qty, score, entry_date, status, catalyst, notes, order_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        pos["ticker"], pos.get("strategy","ARKA-SWING"), pos.get("direction","LONG"),
        pos["entry_price"], pos["stop_loss"], pos["tp1"], pos["tp2"],
        pos.get("qty",0), pos.get("score",0),
        date.today().isoformat(), "OPEN",
        pos.get("catalyst",""), pos.get("notes",""), pos.get("order_id","")
    ))
    db.commit()
    db.close()


def update_position(ticker: str, **kwargs):
    db = get_db()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    db.execute(f"UPDATE arka_swings SET {sets} WHERE ticker=? AND status='OPEN'",
               [*kwargs.values(), ticker])
    db.commit()
    db.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_daily_bars(ticker: str, days: int = 60) -> list:
    """Fetch daily bars from Polygon."""
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=days + 30)).isoformat()
    url   = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
             f"/{start}/{end}?adjusted=true&sort=asc&limit={days+30}&apiKey={POLYGON_KEY}")
    try:
        r = httpx.get(url, timeout=10)
        return r.json().get("results", [])
    except Exception as e:
        log.warning(f"  {ticker}: bars fetch failed — {e}")
        return []


def get_current_price(ticker: str) -> float:
    """Get current price from Polygon snapshot."""
    try:
        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}?apiKey={POLYGON_KEY}"
        r   = httpx.get(url, timeout=5)
        snap = r.json().get("ticker", {})
        return float(snap.get("day", {}).get("c", 0) or snap.get("prevDay", {}).get("c", 0))
    except Exception:
        return 0.0


def get_alpaca_buying_power() -> float:
    try:
        r = httpx.get(f"{ALPACA_BASE}/v2/account",
                      headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
                      timeout=5)
        return float(r.json().get("buying_power", 50000))
    except Exception:
        return 50000.0


# ══════════════════════════════════════════════════════════════════════════════
#  SCORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def score_ticker(ticker: str) -> dict | None:
    """
    Score a ticker for swing entry. Returns dict with score and analysis.
    Uses: RSI, EMA trend, volume surge, ATR momentum, ARJUN signal overlay.
    """
    bars = get_daily_bars(ticker, days=60)
    if len(bars) < 30:
        return None

    closes  = np.array([b["c"] for b in bars])
    volumes = np.array([b["v"] for b in bars])
    highs   = np.array([b["h"] for b in bars])
    lows    = np.array([b["l"] for b in bars])

    price   = closes[-1]
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return None

    avg_vol = float(np.mean(volumes[-20:]))
    if avg_vol < MIN_VOLUME:
        return None

    # ── RSI (14) ──────────────────────────────────────────────────────────────
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_g  = np.mean(gains[-14:])
    avg_l  = np.mean(losses[-14:])
    rsi    = 100 - 100 / (1 + avg_g / (avg_l + 1e-9))

    # ── EMA trend (20/50) ──────────────────────────────────────────────────────
    def ema_calc(arr, n):
        k = 2 / (n + 1)
        e = arr[0]
        for v in arr[1:]:
            e = v * k + e * (1 - k)
        return e

    ema20 = ema_calc(closes[-25:], 20)
    ema50 = ema_calc(closes[-55:], 50) if len(closes) >= 55 else ema_calc(closes, len(closes))
    above_ema20 = price > ema20
    above_ema50 = price > ema50
    ema_trending = ema20 > ema50

    # ── Volume surge ───────────────────────────────────────────────────────────
    vol_ratio = volumes[-1] / (avg_vol + 1e-9)

    # ── ATR momentum ───────────────────────────────────────────────────────────
    tr_arr = np.maximum(highs[-15:] - lows[-15:],
             np.maximum(np.abs(highs[-15:] - np.roll(closes[-15:], 1)),
                        np.abs(lows[-15:] - np.roll(closes[-15:], 1))))
    atr14  = float(np.mean(tr_arr[1:]))
    atr_pct = atr14 / price * 100

    # ── Price momentum (5-day) ─────────────────────────────────────────────────
    mom5  = (closes[-1] - closes[-6]) / closes[-6] * 100
    mom20 = (closes[-1] - closes[-21]) / closes[-21] * 100

    # ── Scoring — start at 35, score up from there ────────────────────────────
    # Base lowered from 50 → 35 to spread the passing range across 70–100
    # instead of compressing it into 72–85. Factor weights widened accordingly.
    # Typical passing setup: ~75–82. High-conviction + ARJUN: 88–100.
    score  = 35
    reasons = []

    # RSI (max +28 / min -15)
    if 45 <= rsi <= 65:
        score += 12; reasons.append(f"RSI healthy ({rsi:.0f})")
    elif 35 <= rsi < 45:
        score += 22; reasons.append(f"RSI oversold bounce ({rsi:.0f})")
    elif rsi < 35:
        score += 28; reasons.append(f"RSI deeply oversold ({rsi:.0f})")
    elif rsi > 75:
        score -= 15; reasons.append(f"RSI overbought ({rsi:.0f})")
    elif rsi > 65:
        score -= 5

    # EMA trend (max +25 / min -20)
    if above_ema20 and above_ema50 and ema_trending:
        score += 25; reasons.append("Above EMA20+EMA50, uptrend")
    elif above_ema20 and ema_trending:
        score += 14; reasons.append("Above EMA20, uptrend intact")
    elif above_ema20:
        score += 6
    elif not above_ema20 and not above_ema50 and not ema_trending:
        score -= 20; reasons.append("Below EMA20+EMA50 — downtrend")
    elif not above_ema50:
        score -= 12; reasons.append("Below EMA50 — weak structure")

    # Volume (max +12 / min -8) — unchanged
    if vol_ratio >= 2.5:
        score += 12; reasons.append(f"Volume surge {vol_ratio:.1f}x avg")
    elif vol_ratio >= 1.5:
        score += 7;  reasons.append(f"Volume elevated {vol_ratio:.1f}x")
    elif vol_ratio >= 1.0:
        score += 2
    elif vol_ratio < 0.6:
        score -= 8;  reasons.append("Low volume — weak conviction")

    # Momentum 5-day (max +12 / min -12) — unchanged
    if 3 <= mom5 <= 15:
        score += 12; reasons.append(f"5-day momentum +{mom5:.1f}%")
    elif mom5 > 15:
        score += 6;  reasons.append(f"Strong 5d move +{mom5:.1f}% (extended)")
    elif 1 <= mom5 < 3:
        score += 4
    elif -3 <= mom5 < -1:
        score -= 4
    elif mom5 < -5:
        score -= 12; reasons.append(f"5-day decline {mom5:.1f}%")

    # Momentum 20-day (max +10 / min -10) — unchanged
    if mom20 >= 8:
        score += 10; reasons.append(f"20-day uptrend +{mom20:.1f}%")
    elif mom20 >= 3:
        score += 5
    elif mom20 < -10:
        score -= 10; reasons.append(f"20-day decline {mom20:.1f}%")
    elif mom20 < -5:
        score -= 5

    # ATR (max +5 / min -5) — unchanged
    if 2.0 <= atr_pct <= 6.0:
        score += 5; reasons.append(f"ATR {atr_pct:.1f}% — good range")
    elif atr_pct > 10.0:
        score -= 5; reasons.append(f"ATR {atr_pct:.1f}% — too volatile")

    # ── ARJUN signal overlay — primary differentiator for 85+ scores ──────────
    try:
        sig_file = BASE / "logs/signals" / f"signals_{date.today()}.json"
        if sig_file.exists():
            sigs = json.loads(sig_file.read_text())
            for s in sigs:
                if s.get("ticker") == ticker:
                    arjun_conf = float(s.get("confidence", 0))
                    if arjun_conf >= 70:
                        score += 20; reasons.append(f"ARJUN signal {arjun_conf:.0f}%")
                    elif arjun_conf >= 55:
                        score += 10; reasons.append(f"ARJUN signal {arjun_conf:.0f}%")
                    break
    except Exception:
        pass

    # Dual scoring — score both LONG and SHORT setups
    bull_score = score  # already computed above (bullish bias)

    # Bear score: invert EMA, RSI, momentum signals
    bear_score = 0
    if not above_ema20 and not above_ema50 and not ema_trending:
        bear_score += 20
    elif not above_ema50:
        bear_score += 12
    if rsi < 40:
        bear_score += 15
    elif rsi > 60:
        bear_score -= 10
    if vol_ratio >= 2.0:
        bear_score += 15
    elif vol_ratio >= 1.5:
        bear_score += 8
    if mom5 < -2:
        bear_score += 15
    elif mom5 < -5:
        bear_score += 5
    if mom20 < -5:
        bear_score += 10

    # ── GEX Gate — apply per-ticker GEX conviction adjustment ────────────────
    try:
        from backend.arka.gex_state import load_gex_state as _lgs
        from backend.arka.gex_gate  import gex_gate as _gg
        _gex_st = _lgs(ticker)
        if _gex_st:
            # Apply to bull direction
            _gr_bull = _gg("CALL", bull_score, _gex_st)
            if not _gr_bull["allow"]:
                bull_score = 0  # GEX hard-blocked calls
                reasons.append(f"[GEX BLOCK CALL] {_gr_bull['reason']}")
            elif _gr_bull["conviction"] != bull_score:
                reasons.append(f"[GEX] {_gr_bull['reason']}")
                bull_score = _gr_bull["conviction"]
            # Apply to bear direction
            _gr_bear = _gg("PUT", bear_score, _gex_st)
            if not _gr_bear["allow"]:
                bear_score = 0  # GEX hard-blocked puts
                reasons.append(f"[GEX BLOCK PUT] {_gr_bear['reason']}")
            elif _gr_bear["conviction"] != bear_score:
                bear_score = _gr_bear["conviction"]
            score = bull_score  # re-sync (may have changed)
    except Exception:
        pass

    # Determine direction — pick whichever has higher conviction
    if bull_score >= MIN_SCORE and bull_score >= bear_score:
        direction = "LONG"
    elif bear_score >= MIN_SCORE and bear_score > bull_score:
        direction = "SHORT"
        score = bear_score  # use bear score for ranking
    else:
        direction = "NEUTRAL"

    # Stop / targets — based on direction
    if direction == "SHORT":
        # PUT setup: profit when price drops
        stop = round(price * (1 + STOP_LOSS_PCT), 2)   # stop if price rises 30%
        tp1  = round(price * (1 - TP1_PCT), 2)          # TP1 at -20%
        tp2  = round(price * (1 - TP2_PCT), 2)          # TP2 at -50%
    else:
        # CALL setup: profit when price rises
        stop = round(price * (1 - STOP_LOSS_PCT), 2)   # stop at -30%
        tp1  = round(price * (1 + TP1_PCT), 2)          # TP1 at +20%
        tp2  = round(price * (1 + TP2_PCT), 2)          # TP2 at +50%
    rr = abs(tp1 - price) / abs(price - stop) if price != stop else 0

    return {
        "ticker":    ticker,
        "price":     round(price, 2),
        "score":     max(0, min(100, score)),
        "direction": direction,
        "rsi":       round(rsi, 1),
        "vol_ratio": round(vol_ratio, 2),
        "mom5":      round(mom5, 2),
        "atr_pct":   round(atr_pct, 2),
        "stop":      stop,
        "tp1":       tp1,
        "tp2":       tp2,
        "rr":        round(rr, 2),
        "reasons":   reasons,
        "strategy":  "ARKA-SWING",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  SCREENER
# ══════════════════════════════════════════════════════════════════════════════

def screen_universe() -> list:
    """Screen the universe and return scored candidates."""
    # Build universe: base + flow monitor candidates + ARJUN signals
    universe = set(BASE_UNIVERSE)

    # Add from watchlist_latest.json
    wl_path = BASE / "logs/chakra/watchlist_latest.json"
    if wl_path.exists():
        try:
            wl = json.loads(wl_path.read_text())
            for c in wl.get("candidates", []):
                universe.add(c["ticker"])
        except Exception:
            pass

    # Add from today's ARJUN signals
    sig_file = BASE / "logs/signals" / f"signals_{date.today()}.json"
    if sig_file.exists():
        try:
            sigs = json.loads(sig_file.read_text())
            for s in sigs:
                universe.add(s["ticker"])
        except Exception:
            pass

    log.info(f"  Screening {len(universe)} tickers...")
    candidates = []
    for ticker in sorted(universe):
        try:
            result = score_ticker(ticker)
            if result and result["score"] >= MIN_SCORE and result["direction"] in ("LONG", "SHORT"):
                candidates.append(result)
                badge = "📈 CALL" if result["direction"] in ("LONG", "SHORT") else "📉 PUT"
                _dir = result["direction"]
                badge = "📈 CALL" if _dir == "LONG" else "📉 PUT"
                log.info(f"  ✅ {badge} {ticker}: score={result['score']} rsi={result['rsi']} "
                         f"vol={result['vol_ratio']:.1f}x entry=${result['price']:.2f} "
                         f"stop=${result['stop']:.2f} tp1=${result['tp1']:.2f} R/R=1:{result['rr']:.2f}")
        except Exception as e:
            log.debug(f"  {ticker}: {e}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"  Screener complete — {len(candidates)} candidates")
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def find_options_contract(ticker: str, price: float, direction: str, max_dte: int = 21) -> dict | None:
    """Find ATM options contract expiring within max_dte days. Retries 3x on network errors.
    Uses AlpacaCircuitBreaker (arka_self_correct) to detect repeated failures,
    run live diagnostics, and pause lookups until Alpaca recovers.
    """
    import time as _time
    try:
        from backend.arka.arka_self_correct import alpaca_circuit as _cb
    except Exception:
        _cb = None

    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error(f"  {ticker}: ALPACA_KEY/ALPACA_SECRET not set — skipping options lookup")
        return None

    # ── Circuit breaker gate — skip if Alpaca is known-down ─────────────────
    if _cb and _cb.is_open():
        return None

    exp_max   = (date.today() + timedelta(days=max_dte)).isoformat()
    exp_min   = (date.today() + timedelta(days=7)).isoformat()  # min 7 DTE for swings
    strike_lo = round(price * 0.95, 0)
    strike_hi = round(price * 1.05, 0)
    params = {
        "underlying_symbols":  ticker,
        "type":                "call" if direction == "LONG" else "put",
        "expiration_date_gte": exp_min,
        "expiration_date_lte": exp_max,
        "strike_price_gte":    str(strike_lo),
        "strike_price_lte":    str(strike_hi),
        "limit":               10,
    }
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

    last_err      = None
    last_err_type = "UnknownError"
    for attempt in range(1, 4):
        try:
            r = httpx.get(
                f"{ALPACA_BASE}/v2/options/contracts",
                headers=headers,
                params=params,
                timeout=12,
            )
            if r.status_code == 401:
                log.error(f"  {ticker}: Alpaca 401 Unauthorized — check ALPACA_KEY/SECRET")
                if _cb:
                    _cb.record_failure("HTTP_401", "Unauthorized — bad credentials")
                return None
            if r.status_code == 403:
                log.error(f"  {ticker}: Alpaca 403 Forbidden — options not enabled on this account")
                if _cb:
                    _cb.record_failure("HTTP_403", "Forbidden — options not enabled")
                return None
            if r.status_code not in (200, 201):
                last_err      = f"HTTP {r.status_code}: {r.text[:80]}"
                last_err_type = f"HTTP_{r.status_code}"
                log.warning(f"  {ticker}: Alpaca HTTP {r.status_code} on attempt {attempt}")
                _time.sleep(2 * attempt)
                continue

            contracts = r.json().get("option_contracts", [])
            if not contracts:
                log.warning(f"  {ticker}: no options contracts found within {max_dte} DTE")
                if _cb:
                    _cb.record_success(ticker)  # API itself worked fine
                return None

            # Pick nearest expiry + closest ATM strike
            contracts.sort(key=lambda c: (
                c.get("expiration_date", ""),
                abs(float(c.get("strike_price", 0)) - price)
            ))
            c   = contracts[0]
            dte = (date.fromisoformat(c["expiration_date"]) - date.today()).days
            log.info(f"  {ticker}: contract {c['symbol']} strike=${c['strike_price']} exp={c['expiration_date']} ({dte}DTE)")
            if _cb:
                _cb.record_success(ticker)
            return c

        except (httpx.ConnectError, OSError) as e:
            last_err      = str(e)
            last_err_type = "ConnectError"
            log.warning(f"  {ticker}: options lookup attempt {attempt}/3 failed — {e}")
            if attempt < 3:
                _time.sleep(2 * attempt)
        except httpx.TimeoutException as e:
            last_err      = str(e)
            last_err_type = "TimeoutError"
            log.warning(f"  {ticker}: options lookup timeout attempt {attempt}/3")
            if attempt < 3:
                _time.sleep(2 * attempt)
        except Exception as e:
            log.warning(f"  {ticker}: options lookup failed — {e}")
            return None

    # All 3 attempts failed — tell the circuit breaker
    log.error(f"  {ticker}: options lookup failed after 3 attempts — {last_err}")
    if _cb:
        _cb.record_failure(last_err_type, last_err or "unknown")
    return None


def is_valid_options_symbol(sym: str) -> bool:
    """Return True if sym matches the options contract format e.g. SPY260401C00640000."""
    import re
    return bool(re.match(r'^[A-Z]{1,6}\d{6}[CP]\d{5,8}$', (sym or "").upper().strip()))


def place_order(ticker: str, qty: int, side: str = "buy",
                contract_symbol: str = "") -> dict:
    """Place options order (1 contract) via Alpaca paper trading. OPTIONS ONLY."""
    # Always use contract symbol — never fall back to bare equity ticker
    order_sym = contract_symbol if contract_symbol else ticker

    # ── Options-only guard ────────────────────────────────────────────────
    from backend.arka.order_guard import validate_options_order
    _valid, _reason = validate_options_order(order_sym, qty, side)
    if not _valid:
        log.error(f"  🛡️  SWINGS ORDER BLOCKED: {_reason}")
        return {"success": False, "error": _reason, "blocked": True}
    log.info(f"  🛡️  {_reason}")

    try:
        r = httpx.post(
            f"{ALPACA_BASE}/v2/orders",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            json={"symbol": order_sym, "qty": str(qty), "side": side,
                  "type": "market", "time_in_force": "day",
                  "asset_class": "us_option"},
            timeout=10,
        )
        if r.status_code in (200, 201):
            result = r.json()
            log.info(f"  ✅ OPTIONS ORDER {side.upper()} {qty}x {order_sym} → {result.get('id','?')[:8]}")
            return {"success": True, "order_id": result.get("id",""), "qty": qty,
                    "contract": order_sym}
        else:
            log.error(f"  ❌ Order failed: {r.status_code} {r.text[:100]}")
            return {"success": False, "error": r.text[:100]}
    except Exception as e:
        log.error(f"  ❌ Order exception: {e}")
        return {"success": False, "error": str(e)}


def post_discord(webhook: str, embed: dict, username: str = "ARKA Swings",
                 force: bool = False) -> bool:
    """Post embed to #arjun-alerts. Blocked after hours unless force=True."""
    url = WH_ARJUN_ALERTS or webhook
    if not url:
        return False
    # After-hours gate
    if not force:
        _et = datetime.now(ET)
        _market_open = (_et.weekday() < 5 and
                        ((_et.hour == 9 and _et.minute >= 30) or _et.hour > 9) and
                        _et.hour < 16)
        if not _market_open:
            log.info("  🔇 Swing Discord DROPPED — market closed")
            return False
    try:
        r = httpx.post(url, json={"username": username, "embeds": [embed]}, timeout=8)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  RISK GATES
# ══════════════════════════════════════════════════════════════════════════════

DAILY_LOSS_LIMIT   = -300.0  # pause new entries if daily P&L < this
BEARISH_SPY_THRESH = -0.40   # SPY day change % below which LONG entries need score ≥ HIGH_SCORE_THRESHOLD
HIGH_SCORE_THRESHOLD = 80    # required score for LONG entries when SPY is bearish
COOLDOWN_DAYS      = 1       # days to wait before re-entering after a stop


def _get_spy_day_change() -> float:
    """Return SPY intraday % change. Returns 0.0 on failure."""
    try:
        url  = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY?apiKey={POLYGON_KEY}"
        r    = httpx.get(url, timeout=5)
        snap = r.json().get("ticker", {})
        day  = snap.get("day", {})
        prev = snap.get("prevDay", {})
        c    = float(day.get("c") or 0)
        pc   = float(prev.get("c") or 0)
        if pc > 0 and c > 0:
            return (c - pc) / pc * 100
    except Exception:
        pass
    return 0.0


def _get_daily_pnl() -> float:
    """Read today's realized P&L from the swings DB (closed positions today)."""
    try:
        db  = get_db()
        row = db.execute(
            "SELECT COALESCE(SUM(pnl), 0) as daily FROM arka_swings "
            "WHERE status='CLOSED' AND exit_date=?",
            (date.today().isoformat(),)
        ).fetchone()
        db.close()
        return float(row["daily"]) if row else 0.0
    except Exception:
        return 0.0


def _ticker_on_cooldown(ticker: str) -> bool:
    """Return True if ticker was stopped out within the last COOLDOWN_DAYS days."""
    try:
        cutoff = (date.today() - timedelta(days=COOLDOWN_DAYS)).isoformat()
        db     = get_db()
        row    = db.execute(
            "SELECT COUNT(*) as n FROM arka_swings "
            "WHERE ticker=? AND status='CLOSED' AND exit_date >= ? "
            "AND pnl < 0",
            (ticker, cutoff)
        ).fetchone()
        db.close()
        return int(row["n"]) > 0 if row else False
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY SCAN
# ══════════════════════════════════════════════════════════════════════════════

def run_entry_scan():
    """Main entry scan — screen, score, place orders."""
    now = datetime.now(ET)
    log.info(f"\n{'='*55}")
    log.info(f"  ARKA-SWING Entry Scan — {now.strftime('%Y-%m-%d %H:%M ET')}")
    log.info(f"{'='*55}")

    # Check market hours
    if not (9 <= now.hour < 16):
        log.info("  Market closed — saving watchlist only")
        _save_watchlist(screen_universe(), "entry_scan_closed")
        return

    # ── Daily loss circuit breaker ──────────────────────────────────────────
    daily_pnl = _get_daily_pnl()
    if daily_pnl < DAILY_LOSS_LIMIT:
        log.warning(f"  🛑 CIRCUIT BREAKER: daily P&L ${daily_pnl:.0f} < ${DAILY_LOSS_LIMIT:.0f} "
                    f"— pausing new swing entries for today")
        _save_watchlist(screen_universe(), "entry_scan_paused")
        return
    if daily_pnl < 0:
        log.info(f"  ⚠️  Daily P&L ${daily_pnl:.0f} (above limit, proceeding)")

    # ── Market breadth gate ─────────────────────────────────────────────────
    spy_chg = _get_spy_day_change()
    log.info(f"  📊 SPY day change: {spy_chg:+.2f}%")
    _long_blocked = spy_chg < BEARISH_SPY_THRESH
    if _long_blocked:
        log.warning(f"  ⚠️  Bearish market ({spy_chg:+.2f}%) — LONG entries require score ≥ {HIGH_SCORE_THRESHOLD}")

    open_positions = get_open_positions()
    if len(open_positions) >= MAX_POSITIONS:
        log.info(f"  Max positions reached ({MAX_POSITIONS}) — skipping entry scan")
        return

    slots_available = MAX_POSITIONS - len(open_positions)
    candidates      = screen_universe()
    bp              = get_alpaca_buying_power()

    # Skip tickers already in open positions
    open_tickers = {p["ticker"] for p in open_positions}
    new_candidates = [c for c in candidates if c["ticker"] not in open_tickers]

    entered = 0
    for candidate in new_candidates[:slots_available]:
        ticker    = candidate["ticker"]
        price     = candidate["price"]
        score     = candidate["score"]
        direction = candidate.get("direction", "LONG")

        # ── Same-ticker cooldown ────────────────────────────────────────────
        if _ticker_on_cooldown(ticker):
            log.info(f"  ⏸  {ticker}: on cooldown (stopped out within {COOLDOWN_DAYS}d) — skip")
            continue

        # ── Bearish market filter: require higher score for LONG entries ────
        if direction == "LONG" and _long_blocked and score < HIGH_SCORE_THRESHOLD:
            log.info(f"  ⛔ {ticker}: LONG blocked — bearish market, score={score} < {HIGH_SCORE_THRESHOLD}")
            continue

        # Position sizing — TESTING MODE: 1 contract, $1000 max budget
        qty        = MAX_CONTRACTS  # always 1 during testing
        est_cost   = round(price * 0.04 * 100)  # ~4% ATM premium estimate for swings
        if est_cost > SWING_BUDGET:
            log.info(f"  ⛔ {ticker}: contract cost ${est_cost:.0f} > $2000 swing budget — skip")
            continue

        log.info(f"\n  → Entering {ticker} @ ${price:.2f} score={score} qty={qty}")

        # Find options contract (≤21 DTE, ATM)
        contract  = find_options_contract(ticker, price, direction, max_dte=MAX_DTE)
        if not contract:
            log.warning(f"  {ticker}: no options contract found — skipping")
            continue
        contract_sym = contract.get("symbol", "")

        # Always 1 contract
        qty = CONTRACTS_PER_TRADE
        dte = (date.fromisoformat(contract["expiration_date"]) - date.today()).days

        # ── Epoch 3 cost guardrail — check ACTUAL contract premium before ordering ──
        # The estimate above (price * 0.04) is often wrong for volatile names like COIN/MSTR.
        # Fetch the real mid-price from Polygon snapshot and reject if too expensive.
        _MAX_CONTRACT_PX = 5.00   # $500/contract hard ceiling for swing entries
        _actual_px       = None
        try:
            _snap_url = (f"https://api.polygon.io/v3/snapshot/options/{ticker}"
                         f"?apiKey={POLYGON_KEY}&ticker={contract_sym}&limit=1")
            _snap_r   = httpx.get(_snap_url, timeout=8)
            if _snap_r.status_code == 200:
                _snaps = _snap_r.json().get("results", [])
                if _snaps:
                    _q = _snaps[0].get("last_quote", {})
                    _bid = float(_q.get("bid", 0) or 0)
                    _ask = float(_q.get("ask", 0) or 0)
                    if _bid > 0 and _ask > 0:
                        _actual_px = round((_bid + _ask) / 2, 2)
                    elif _ask > 0:
                        _actual_px = _ask
        except Exception as _e:
            log.warning(f"  {ticker}: could not fetch contract price — {_e}")

        if _actual_px is not None:
            if _actual_px > _MAX_CONTRACT_PX:
                log.warning(
                    f"  ⛔ Cost gate: {contract_sym} actual premium ${_actual_px:.2f}/share "
                    f"= ${_actual_px*100:.0f}/contract > ${_MAX_CONTRACT_PX*100:.0f} ceiling — SKIP"
                )
                continue
            log.info(f"  ✅ Cost OK: {contract_sym} ${_actual_px:.2f}/share (${_actual_px*100:.0f}/contract)")
        else:
            log.warning(f"  ⚠️  Cost gate: could not verify {contract_sym} price — proceeding with caution")

        # Place options order
        result = place_order(ticker, qty, "buy", contract_symbol=contract_sym)
        if not result["success"]:
            log.error(f"  ❌ {ticker}: order failed — {result.get('error','?')}")
            continue

        # Save to DB
        pos = {**candidate, "qty": qty, "entry_price": price,
               "stop_loss": candidate["stop"], "order_id": result.get("order_id",""),
               "catalyst": " | ".join(candidate["reasons"][:3]),
               "notes": f"{contract_sym} {dte}DTE {'CALL' if direction=='LONG' else 'PUT'}"}
        save_position(pos)

        # Estimate premium for Discord (actual fill will differ)
        est_premium = round(price * 0.04, 2)

        # Always notify when we actually place a trade — no quality gate skip
        try:
            from backend.arka.arka_discord_notifier import post_swing_entry
            post_swing_entry(
                candidate    = candidate,
                contract_sym = contract_sym,
                qty          = qty,
                premium      = est_premium,
                dte          = dte,
            )
            log.info(f"  ✅ {ticker}: entered and notified → #arjun-alerts")
        except Exception as _de:
            log.warning(f"  {ticker}: Discord notify failed — {_de}")
        entered += 1

    log.info(f"\n  Entry scan complete — {entered} new position(s)")
    _save_watchlist(candidates, "entry_scan")


def _save_watchlist(candidates: list, mode: str):
    """Save watchlist for dashboard. Sends watchlist Discord update for premarket/postmarket."""
    import json as _j
    wl = {
        "candidates": candidates[:20],
        "count": len(candidates),
        "scan_time": datetime.now().isoformat(),
        "mode": mode,
        "top5": candidates[:5],
        "engine": "ARKA-SWINGS",
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    (BASE / f"logs/chakra/watchlist_{ts}.json").write_text(_j.dumps(wl, indent=2))
    (BASE / "logs/chakra/watchlist_latest.json").write_text(_j.dumps(wl, indent=2))
    log.info(f"  Watchlist saved: {len(candidates)} candidates")
    # Send watchlist to #arjun-alerts for market-closed scans (premarket handled separately)
    if mode == "entry_scan_closed" and candidates:
        try:
            from backend.arka.arka_discord_notifier import post_swing_watchlist
            post_swing_watchlist(candidates, mode=mode)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION MONITOR
# ══════════════════════════════════════════════════════════════════════════════

def run_monitor():
    """Monitor open positions — check stops, targets, hold days."""
    now       = datetime.now(ET)
    positions = get_open_positions()

    if not positions:
        log.info("  No open swing positions to monitor")
        return

    log.info(f"\n  Monitoring {len(positions)} position(s)...")

    for pos in positions:
        ticker     = pos["ticker"]
        entry      = float(pos["entry_price"])
        stop       = float(pos["stop_loss"])
        tp1        = float(pos["tp1"])
        tp2        = float(pos["tp2"])
        qty        = int(pos["qty"])
        tp1_hit    = bool(pos["tp1_hit"])
        entry_date = date.fromisoformat(pos["entry_date"])
        hold_days  = (date.today() - entry_date).days

        # Extract options contract symbol stored in notes (e.g. "SPY260401C00640000 2DTE CALL")
        _notes = pos.get("notes", "")
        _contract_sym = ""
        if _notes:
            import re as _re_sw
            _m = _re_sw.match(r'^([A-Z]{1,6}\d{6}[CP]\d{5,8})', _notes.strip())
            if _m:
                _contract_sym = _m.group(1)
        if not _contract_sym:
            log.warning(f"  {ticker}: no options contract symbol in notes='{_notes}' — skipping exit")
            continue

        price = get_current_price(ticker)
        if not price:
            log.warning(f"  {ticker}: could not get price")
            continue

        pnl     = (price - entry) * qty
        pnl_pct = (price - entry) / entry * 100
        update_position(ticker, hold_days=hold_days)

        log.info(f"  {ticker} [{_contract_sym}]: ${price:.2f} | P&L {pnl_pct:+.1f}% (${pnl:+.2f}) | "
                 f"day {hold_days}/{MAX_HOLD_DAYS}")

        action = None

        def _notify_swing_exit(reason_str: str):
            try:
                from backend.arka.arka_discord_notifier import post_swing_exit as _pse
                _pse(
                    ticker      = ticker,
                    contract_sym= _contract_sym,
                    entry       = entry,
                    exit_px     = price,
                    qty         = qty,
                    hold_days   = hold_days,
                    reason      = reason_str,
                    pnl         = round(pnl, 2),
                    pnl_pct     = round(pnl_pct, 1),
                )
            except Exception as _de:
                log.warning(f"  {ticker}: Discord exit notify failed — {_de}")

        # ── Stop loss hit ─────────────────────────────────────────────────────
        if price <= stop:
            action = "STOP"
            log.info(f"  🛑 {ticker}: STOP HIT @ ${price:.2f}")
            result = place_order(ticker, qty, "sell", contract_symbol=_contract_sym)
            if result["success"]:
                update_position(ticker, status="CLOSED", exit_price=price,
                                exit_date=date.today().isoformat(),
                                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2))
                _notify_swing_exit("Stop loss hit")

        # ── TP1 hit ───────────────────────────────────────────────────────────
        elif price >= tp1 and not tp1_hit:
            action = "TP1"
            sell_qty = max(1, qty // 2)
            log.info(f"  🎯 {ticker}: TP1 HIT @ ${price:.2f} — selling {sell_qty}")
            result = place_order(ticker, sell_qty, "sell", contract_symbol=_contract_sym)
            if result["success"]:
                update_position(ticker, tp1_hit=1, qty=qty - sell_qty)
                _notify_swing_exit(
                    f"TP1 hit — sold {sell_qty} contract, "
                    f"{qty - sell_qty} runner remains targeting ${tp2:.2f}"
                )

        # ── TP2 hit ───────────────────────────────────────────────────────────
        elif price >= tp2 and tp1_hit:
            action = "TP2"
            log.info(f"  🏆 {ticker}: TP2 HIT @ ${price:.2f} — closing position")
            result = place_order(ticker, qty, "sell", contract_symbol=_contract_sym)
            if result["success"]:
                update_position(ticker, status="CLOSED", exit_price=price,
                                exit_date=date.today().isoformat(),
                                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2))
                _notify_swing_exit("TP2 full target hit")

        # ── Max hold days ──────────────────────────────────────────────────────
        elif hold_days >= MAX_HOLD_DAYS:
            action = "TIMEOUT"
            log.info(f"  ⏰ {ticker}: max hold ({MAX_HOLD_DAYS}d) — closing")
            result = place_order(ticker, qty, "sell", contract_symbol=_contract_sym)
            if result["success"]:
                update_position(ticker, status="CLOSED", exit_price=price,
                                exit_date=date.today().isoformat(),
                                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2))
                _notify_swing_exit(f"Max hold period reached ({MAX_HOLD_DAYS} days)")

        if action:
            log.info(f"  ✅ {ticker}: {action} processed")


# ══════════════════════════════════════════════════════════════════════════════
#  PRE/POST MARKET
# ══════════════════════════════════════════════════════════════════════════════

def run_premarket():
    """Pre-market watchlist scan — no orders."""
    log.info("  📋 ARKA-SWING pre-market scan")
    candidates = screen_universe()
    _save_watchlist(candidates, "premarket")

    if candidates:
        try:
            from backend.arka.arka_discord_notifier import post_swing_watchlist
            post_swing_watchlist(candidates, mode="premarket")
            log.info(f"  📣 Pre-market watchlist posted → #arjun-alerts")
        except Exception as _de:
            log.warning(f"  Pre-market Discord failed — {_de}")


def run_postmarket():
    """Post-market scan — build tomorrow's watchlist."""
    log.info("  🌙 ARKA-SWING post-market scan")
    candidates = screen_universe()
    _save_watchlist(candidates, "postmarket")

    # Summarize today's closed positions
    db   = get_db()
    wins = db.execute("SELECT COUNT(*) FROM arka_swings WHERE status='CLOSED' AND exit_date=? AND pnl>0",
                      (date.today().isoformat(),)).fetchone()[0]
    loss = db.execute("SELECT COUNT(*) FROM arka_swings WHERE status='CLOSED' AND exit_date=? AND pnl<=0",
                      (date.today().isoformat(),)).fetchone()[0]
    total_pnl = db.execute("SELECT SUM(pnl) FROM arka_swings WHERE status='CLOSED' AND exit_date=?",
                           (date.today().isoformat(),)).fetchone()[0] or 0
    db.close()

    if (wins + loss) > 0 or candidates:
        try:
            from backend.arka.arka_discord_notifier import post_eod_summary, post_swing_watchlist
            post_eod_summary(wins, loss, total_pnl, candidates)
            if candidates:
                post_swing_watchlist(candidates, mode="postmarket")
            log.info("  📣 EOD summary posted → #arjun-alerts")
        except Exception as _de:
            log.warning(f"  EOD Discord failed — {_de}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARKA Swings Engine")
    parser.add_argument("--entry",      action="store_true", help="Run entry scan (default)")
    parser.add_argument("--monitor",    action="store_true", help="Monitor open positions")
    parser.add_argument("--premarket",  action="store_true", help="Pre-market watchlist scan")
    parser.add_argument("--postmarket", action="store_true", help="Post-market scan + EOD summary")
    parser.add_argument("--status",     action="store_true", help="Show open positions")
    parser.add_argument("--screen",     action="store_true", help="Screen only, no orders")
    args = parser.parse_args()

    if args.premarket:
        run_premarket()
    elif args.postmarket:
        run_postmarket()
    elif args.monitor:
        run_monitor()
    elif args.status:
        positions = get_open_positions()
        print(f"\n{len(positions)} open ARKA swing position(s):")
        for p in positions:
            price   = get_current_price(p["ticker"])
            pnl_pct = (price - p["entry_price"]) / p["entry_price"] * 100 if price else 0
            print(f"  {p['ticker']:6s}: entry=${p['entry_price']:.2f} "
                  f"current=${price:.2f} P&L={pnl_pct:+.1f}% "
                  f"day={p['hold_days']}/{MAX_HOLD_DAYS} "
                  f"TP1={'✅' if p['tp1_hit'] else '⏳'}")
    elif args.screen:
        candidates = screen_universe()
        print(f"\nTop candidates:")
        for c in candidates[:10]:
            print(f"  {c['ticker']:6s} ${c['price']:7.2f}  score={c['score']:3d}  "
                  f"rsi={c['rsi']:.0f}  vol={c['vol_ratio']:.1f}x  mom5={c['mom5']:+.1f}%")
    else:
        run_entry_scan()
