"""
ARKA — Live Scanning Engine
Runs every 60 seconds during market hours.
Combines rule-based conviction score + XGBoost fakeout filter
to generate and execute intraday trades via Alpaca paper trading.

Run from ~/trading-ai:
    python3 backend/arka/arka_engine.py

To run in background:
    nohup python3 backend/arka/arka_engine.py > logs/arka/arka_engine.log 2>&1 &

CHANGES vs v1:
    - Conviction threshold: 60 → 55 (normal), 50 → 45 (power hour)
    - Fakeout block threshold: 0.45 → 0.55 (less aggressive filtering)
    - Added detailed per-scan diagnostic logging
    - Added Polygon data failure logging (was silent before)
    - Added scan summary line showing exactly why each ticker passed/blocked
    - Added daily conviction score tracker to dashboard summary
"""

import asyncio
import httpx
import pandas as pd
import numpy as np
import pickle
import os

# ── UOA Detector (wired Day 10) ───────────────────────────────────────
try:
    from backend.flow.uoa_detector import detect_unusual_options as _detect_uoa_fn
    _UOA_AVAILABLE = True
except ImportError:
    _detect_uoa_fn = None
    _UOA_AVAILABLE = False


def _check_uoa(ticker: str) -> bool:
    """Returns True if unusual options activity detected for ticker."""
    if not _UOA_AVAILABLE or _detect_uoa_fn is None:
        return False
    try:
        result = _detect_uoa_fn(ticker)
        return result.get('count', 0) > 0
    except Exception:
        return False

import json
import logging
import time
from pathlib import Path
from backend.chakra.regime_gates import get_regime_gates
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from backend.arka.gex_state import load_gex_state, get_gex_by_expiry, check_zero_gamma_shift
from backend.arka.gex_gate  import gex_gate

# ── Session 1 Power Intelligence Modules ─────────────────────────────
try:
    from backend.chakra.modules.dex_calculator import get_dex_conviction_boost, compute_and_cache_dex
    from backend.chakra.modules.hurst_engine   import get_hurst_conviction_boost, get_market_hurst
    from backend.chakra.modules.entropy_engine  import get_entropy_arka_params, get_market_entropy
    from backend.chakra.modules.hmm_regime     import get_hmm_arka_params
    from backend.chakra.modules.iceberg_detector import get_iceberg_conviction_boost
    _POWER_MODULES = True
except ImportError:
    _POWER_MODULES = False
# ── WebSocket stream (optional) ──────────────────────────────────────────────
try:
    from backend.arka.polygon_stream import PolygonStream
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False


load_dotenv(override=True)

# ── Discord notifier ──────────────────────────────────────────────────────────
try:
    import sys
    sys.path.insert(0, '/Users/midhunkrothapalli/trading-ai')
    from backend.arka.discord_notifier import post_arka_entry, post_arka_exit, post_arka_self_correct, post_system_alert, post_arka_daily_summary, post_arka_eod_summary, post_position_update
    DISCORD_ENABLED = True
except Exception as e:
    DISCORD_ENABLED = False
    import logging as _l
    _l.getLogger("ARKA").warning(f"Discord disabled: {e}")

# ── config loader ─────────────────────────────────────────────────────────────
_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "arka_config.json")

def load_arka_config() -> dict:
    """Load thresholds from arka_config.json — called on startup and after self-correction."""
    if os.path.exists(_CONFIG_FILE):
        with open(_CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg
    # Fallback defaults if config file missing
    return {
        "thresholds": {
            "conviction_normal":     55,
            "conviction_power_hour": 45,
            "fakeout_block":         0.78,
        },
        "self_correct": {"enabled": True}
    }

def reload_thresholds():
    """Hot-reload thresholds from config file without restarting engine."""
    global CONVICTION_THRESHOLD_NORMAL, CONVICTION_THRESHOLD_POWER_HOUR, FAKEOUT_BLOCK_THRESHOLD, CONVICTION_THRESHOLD_QQQ
    cfg = load_arka_config()
    thr = cfg.get("thresholds", {})
    CONVICTION_THRESHOLD_NORMAL     = thr.get("conviction_normal",     55)
    CONVICTION_THRESHOLD_POWER_HOUR = thr.get("conviction_power_hour", 45)
    FAKEOUT_BLOCK_THRESHOLD         = thr.get("fakeout_block",         0.65)
    CONVICTION_THRESHOLD_QQQ        = thr.get("conviction_qqq",        72)
    log.info(f"  🔄 Thresholds reloaded: conviction={CONVICTION_THRESHOLD_NORMAL} | QQQ={CONVICTION_THRESHOLD_QQQ} | fakeout={FAKEOUT_BLOCK_THRESHOLD}")


async def _post_loss_recalibration_report(
    loss_count: int, total_loss: float, loss_tickers: list,
    diagnoses: list, old_conv: int, new_conv: int,
    old_fakeout: float, new_fakeout: float, pause_minutes: int,
    stopped_for_day: bool = False,
):
    """Post a detailed loss-streak recalibration report to Discord."""
    try:
        from backend.arka.discord_notifier import post_embed
        _et_now = datetime.now(ET).strftime("%H:%M ET")
        _ticker_str = ", ".join(dict.fromkeys(loss_tickers))  # dedup, preserve order

        _diag_text = "\n".join(f"• {d}" for d in diagnoses) if diagnoses else "• Mixed signals, unfavorable conditions"
        _change_text = (
            f"🔼 Conviction: `{old_conv}` → `{new_conv}` (harder to enter)\n"
            f"🔽 Fakeout block: `{old_fakeout:.3f}` → `{new_fakeout:.3f}` (stricter filter)"
        )

        if stopped_for_day:
            _status_name  = "🛑 Engine Stopped for the Day"
            _status_value = (
                f"**{loss_count} consecutive losses** is the hard limit.\n"
                f"ARKA will not take any more trades today.\n"
                f"Thresholds recalibrated — will apply from tomorrow's session."
            )
            _color  = 0xcc0000
            _author = f"🧠 ARKA — {loss_count} Losses in a Row — Trading Halted"
        else:
            _status_name  = "⏸  Engine Paused"
            _status_value = f"Cooling off for **{pause_minutes} minutes** — will resume around {datetime.now(ET).strftime('%H:%M ET')}"
            _color  = 0xff2244
            _author = f"🧠 ARKA Self-Recalibration — {loss_count} Consecutive Losses"

        embed = {
            "color": _color,
            "author": {"name": _author},
            "fields": [
                {
                    "name": f"📉 {loss_count} Losses in a Row  |  Total: -${abs(total_loss):.2f}",
                    "value": f"Tickers: `{_ticker_str}`",
                    "inline": False,
                },
                {
                    "name": "🔍 Why We Lost",
                    "value": _diag_text,
                    "inline": False,
                },
                {
                    "name": "⚙️ What Changed",
                    "value": _change_text,
                    "inline": False,
                },
                {
                    "name": _status_name,
                    "value": _status_value,
                    "inline": False,
                },
            ],
            "footer": {"text": f"ARKA Recalibration • {_et_now}"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        await post_embed(embed, username="ARKA (Self-Correct)")
        log.info("  📣 Loss recalibration report posted to Discord")
    except Exception as _pe:
        log.error(f"  [loss-report discord] {_pe}")

# ── paths ─────────────────────────────────────────────────────────────────────
MODEL_DIR  = "models/arka"
LOG_DIR    = "logs/arka"
os.makedirs(LOG_DIR, exist_ok=True)

# ── logging ───────────────────────────────────────────────────────────────────
from logging.handlers import TimedRotatingFileHandler as _TRFH
_file_handler = _TRFH(
    f"{LOG_DIR}/arka.log",
    when="midnight",
    interval=1,
    backupCount=30,
)
_file_handler.suffix = "%Y-%m-%d"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        _file_handler,
    ]
)
log = logging.getLogger("ARKA")

# ── config ────────────────────────────────────────────────────────────────────
# Index ETFs — always scan, lower conviction threshold (55)
# Includes equity indexes (SPY/QQQ/IWM/DIA), SPX cash index, and commodity ETFs (GLD/SLV)
INDEX_TICKERS  = ["SPY", "QQQ", "SPX", "IWM", "DIA", "GLD", "SLV"]

# Top 10 S&P 500 stocks by market cap with liquid options — higher threshold (70)
TOP10_TICKERS  = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "NFLX", "AMD"]

TICKERS        = INDEX_TICKERS + TOP10_TICKERS
# SPX: scored + logged, Alpaca order skipped (paper unsupported)

# ── Index-only whitelist — ARKA scalps indexes exclusively ───────────────────
# Stocks are excluded: wider spreads, lower liquidity options, need larger moves.
# SPXW = weekly SPX options (same underlying as SPX, separate OCC symbol).
ALLOWED_TICKERS = {"SPY", "QQQ", "IWM", "SPX", "SPXW"}

# Options direction map — SHORT signal = buy PUT options on same underlying
# No inverse ETFs — we buy puts directly
INVERSE_MAP = {}  # deprecated — ARKA now uses options contracts only
SCAN_INTERVAL  = 60          # seconds between scans
ET             = ZoneInfo("America/New_York")

MARKET_OPEN    = (9, 30)
MARKET_CLOSE   = (16, 0)
AUTO_CLOSE_AT  = (15, 58)    # close all positions at 3:58pm

# Risk management
MAX_DAILY_LOSS_PCT   = 0.02   # -2% of portfolio = stop for the day
MAX_CONCURRENT       = 5      # max open positions total (was 8 — too many)
MAX_CONCURRENT_INDEX = 2      # reserved slots for indexes (was 4)
MAX_TRADES_PER_DAY   = 12     # hard cap — 12 trades/day max (prevents overtrading on restarts)
LOSING_STREAK_LIMIT  = 3      # pause after 3 consecutive losses
LOSING_STREAK_PAUSE  = 1800   # 30 minute pause (seconds)
LOSING_STREAK_STOP   = 5      # stop trading for the day after 5 consecutive losses
STOP_COOLDOWN_INDEX  = 60     # minutes before re-entering same index after a stop (was 45)
STOP_COOLDOWN_STOCK  = 120    # minutes before re-entering same stock after a stop (was 90)
MAX_ENTRIES_PER_TICKER = 2    # max times we enter the same ticker in one session
# ── Smart re-entry after losses (don't hard-block — raise the bar instead) ──
LARGE_LOSS_THRESHOLD  = 100   # $ — single trade loss that triggers elevated re-entry rules
REVERSAL_THRESHOLD_ADJ = 10   # conviction boost REQUIRED for reversal re-entry (opposite dir)
SAME_DIR_THRESHOLD_ADJ = 25   # conviction boost REQUIRED for same-direction re-entry (failed thesis)
REVERSAL_MAX_CONTRACTS = 1    # force 1 contract on reversal re-entry (reduced size)
TICKER_DAILY_LOSS_CAP = 150   # $ — HARD block if ticker total loss >= $150 today

# Position sizing
NORMAL_POSITION_PCT  = 0.25   # 25% of buying power per trade
POWER_HOUR_PCT       = 0.375  # 37.5% in power hour
ATR_STOP_MULT        = 2.50   # stop = entry - ATR * this (widened to prevent noise stops)
ATR_TARGET_MULT      = 5.00   # target = entry + ATR * this (2:1 R/R minimum)

# ── THRESHOLDS — loaded from arka_config.json (self-correcting) ──────────────
_cfg = load_arka_config()
_thr = _cfg.get("thresholds", {})
CONVICTION_THRESHOLD_NORMAL     = _thr.get("conviction_normal",     55)
CONVICTION_THRESHOLD_POWER_HOUR = _thr.get("conviction_power_hour", 45)
FAKEOUT_BLOCK_THRESHOLD         = _thr.get("fakeout_block",         0.78)

# Individual stocks need a higher bar — more noise, wider spreads, faster moves
STOCK_TICKERS              = set(TOP10_TICKERS) | {"COIN", "MSFT", "GOOG"}
CONVICTION_THRESHOLD_STOCK = 80   # stocks need stronger conviction than indexes

# QQQ-specific threshold — historical data shows QQQ at 33% WR vs SPY 67% WR
# on the same threshold. QQQ needs significantly higher bar.
# Data: 63 QQQ trades, 33% WR, -$2,279 total vs SPY 42 trades, 67% WR
CONVICTION_THRESHOLD_QQQ = _thr.get("conviction_qqq", 72)    # 17 pts above normal

def is_stock(ticker: str) -> bool:
    """Any ticker that is NOT a pure index ETF is treated as a stock.
    This catches dynamic universe additions (MSTR, CRM, PLTR, etc.)
    that were getting index-level thresholds by mistake."""
    return ticker.upper() not in INDEX_TICKERS


def get_conviction_threshold(session: str, hour: int, minute: int) -> int:
    """
    Dynamic conviction threshold based on time of day.
    Open = lower bar (institutional flow strongest).
    Midday = higher bar (choppy, low volume).
    Power hour = handled by lotto engine separately.
    """
    # ── Open print (9:30–10:15): most fakeouts, widest spreads, gaps filling ──
    # Lower bar is counterintuitive here — RAISE it. First 45 min = trap zone.
    if hour == 9 and minute >= 30:
        return 65   # was 48 — too permissive, open prints are often reversed
    if hour == 10 and minute < 15:
        return 65   # still open print window
    # ── Best scalp window (10:15–11:30): trends are established, flow is clean ──
    if hour == 10 and minute >= 15:
        return 55
    if hour == 11 and minute < 30:
        return 55
    # ── Midday doldrums (11:30–14:00): chop, algo noise — raise bar hard ──
    if (hour == 11 and minute >= 30) or hour in (12, 13):
        return 65
    if hour == 14 and minute < 0:
        return 65
    # ── Afternoon pickup (14:00–14:30) ──
    if hour == 14 and minute < 30:
        return 60
    # ── Pre-power-hour (14:30–15:00) ──
    if hour == 14 and minute >= 30:
        return 55
    # ── Power hour (15:00–15:58): momentum trades only ──
    if hour == 15:
        return 45
    return 55  # fallback


def get_index_correlation_gate(signal_direction: str, spy_change_pct: float, ticker: str) -> dict:
    """
    Block trades that fight the index trend.
    SPY is the master bias filter for correlated tickers.
    Non-correlated tickers (GLD, TLT, SLV) are exempt.
    """
    EXEMPT = {"GLD", "TLT", "SLV", "VXX", "UVXY"}
    STRONG_TREND_PCT = 0.8

    if ticker.upper() in EXEMPT:
        return {"allow": True, "reason": "exempt from correlation gate"}

    if spy_change_pct <= -STRONG_TREND_PCT and signal_direction == "CALL":
        return {
            "allow":  False,
            "reason": f"SPY down {spy_change_pct:.2f}% — blocking CALL on correlated ticker",
        }
    if spy_change_pct >= STRONG_TREND_PCT and signal_direction == "PUT":
        return {
            "allow":  False,
            "reason": f"SPY up {spy_change_pct:.2f}% — blocking PUT on correlated ticker",
        }
    if abs(spy_change_pct) >= 0.4:
        return {"allow": True, "reason": f"SPY {spy_change_pct:+.2f}% — minor headwind, proceeding"}
    return {"allow": True, "reason": "no correlation concern"}


# Polygon
POLYGON_KEY  = os.getenv("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"

# Alpaca
ALPACA_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE   = "https://paper-api.alpaca.markets"
ALPACA_DATA   = "https://data.alpaca.markets"

# ── helpers ───────────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(ET)

def is_market_open() -> bool:
    now = now_et()
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE

def session_name(ts: datetime) -> str:
    h, m = ts.hour, ts.minute
    mins_since_open = (h - 9) * 60 + m - 30
    if mins_since_open < 0:           return "PRE"
    if h >= 16:                       return "CLOSED"
    if h == 15 and m >= 56:           return "CLOSE"
    if (h == 14 and m >= 30) or (h == 15 and m < 56): return "POWER_HOUR"
    if h == 12 and 0 <= m < 30: return "LUNCH"  # Patched: narrowed from 11:30-13:30 to 12:00-12:30
    if mins_since_open <= 30:         return "OPEN"
    return "NORMAL"

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(alpha=1/n, adjust=False).mean()
    al    = loss.ewm(alpha=1/n, adjust=False).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> float:
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/n, adjust=False).mean().iloc[-1])

def macd_hist(close: pd.Series) -> float:
    ml = ema(close, 12) - ema(close, 26)
    return float((ml - ema(ml, 9)).iloc[-1])

# ── Alpaca client ─────────────────────────────────────────────────────────────

class AlpacaClient:
    def __init__(self):
        self.headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
            "Content-Type":        "application/json",
        }

    async def get(self, path: str, params: dict = None) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ALPACA_BASE}{path}", headers=self.headers, params=params)
            return r.json()

    async def post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{ALPACA_BASE}{path}", headers=self.headers, json=body)
            if r.status_code not in (200, 201):
                log.warning(f"  Alpaca POST {path} → {r.status_code}: {r.text[:200]}")
            return r.json()

    async def get_account(self) -> dict:
        return await self.get("/v2/account")

    async def get_positions(self) -> list:
        data = await self.get("/v2/positions")
        return data if isinstance(data, list) else []

    async def get_position(self, ticker: str) -> dict | None:
        positions = await self.get_positions()
        for p in positions:
            if p.get("symbol") == ticker:
                return p
        return None

    async def place_order(self, ticker: str, qty: int, side: str, note: str = "") -> dict:
        # ── Options-only guard ────────────────────────────────────────────────
        from backend.arka.order_guard import validate_options_order
        _valid, _reason = validate_options_order(ticker, qty, side)
        if not _valid:
            log.error(f"  🛡️  ORDER GUARD BLOCKED: {_reason}")
            return {"error": _reason, "blocked": True}
        log.info(f"  🛡️  {_reason}")
        body = {
            "symbol":        ticker,
            "qty":           str(qty),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        }
        result = await self.post("/v2/orders", body)
        log.info(f"  ORDER  {side.upper()} {qty} {ticker}  {note}  → {result.get('id','?')[:8]}")
        return result

    async def close_position(self, ticker: str, reason: str = "", qty: int = 0) -> dict:
        """
        Close an options position.
        For options: uses a market sell order (DELETE /positions sometimes fails for options).
        Falls back to DELETE if sell order fails.
        """
        # Determine qty to close
        _qty = qty
        if not _qty:
            try:
                positions = await self.get_positions()
                for p in positions:
                    if p.get("symbol") == ticker:
                        _qty = abs(int(float(p.get("qty", 1))))
                        break
            except Exception:
                pass
        if not _qty:
            _qty = 1

        # For options: place a sell order (more reliable than DELETE for paper)
        _is_option = len(ticker) > 10 and any(c in ticker for c in ('C', 'P'))
        if _is_option:
            body = {
                "symbol":        ticker,
                "qty":           str(_qty),
                "side":          "sell",
                "type":          "market",
                "time_in_force": "day",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"{ALPACA_BASE}/v2/orders", headers=self.headers, json=body)
            log.info(f"  CLOSE  {ticker}  reason={reason}  sell_order={r.status_code}  qty={_qty}")
            if r.status_code in (200, 201):
                return r.json()
            if r.status_code == 403 and "day trades" in r.text.lower():
                log.error(f"  ⛔ PDT BLOCK CLOSE {ticker}: {r.text[:200]}")
                if DISCORD_ENABLED:
                    try:
                        await post_system_alert(
                            "⛔ PDT BLOCK — Manual Close Required",
                            f"**{ticker}** could not be closed — Alpaca rejected with PDT rule (account equity < $25K).\n\n"
                            f"Reason: `{reason}`\n\n"
                            f"**Action needed:** Manually close `{ticker}` in the Alpaca paper dashboard.",
                            level="error",
                        )
                    except Exception as _pdt_e:
                        log.debug(f"  PDT discord alert failed: {_pdt_e}")
                return {}
            log.warning(f"  CLOSE {ticker}: sell order {r.status_code} — {r.text[:150]} — trying DELETE fallback")

        # DELETE fallback (equity positions or if sell order failed)
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.delete(
                    f"{ALPACA_BASE}/v2/positions/{ticker}",
                    headers=self.headers
                )
            log.info(f"  CLOSE  {ticker}  reason={reason}  status={r.status_code}  attempt={attempt+1}")
            if r.status_code in (200, 204, 207):
                return r.json() if r.content else {}
            if r.status_code == 404:
                log.warning(f"  CLOSE {ticker}: 404 — position already closed or not found")
                return {}
            if r.status_code in (422,):
                log.warning(f"  CLOSE {ticker}: 422 — {r.text[:120]}")
                return {}
            if r.status_code == 403 and "day trades" in r.text.lower():
                log.error(f"  ⛔ PDT BLOCK (DELETE) {ticker}: {r.text[:200]}")
                if DISCORD_ENABLED:
                    try:
                        await post_system_alert(
                            "⛔ PDT BLOCK — Manual Close Required",
                            f"**{ticker}** could not be closed — Alpaca rejected with PDT rule (account equity < $25K).\n\n"
                            f"Reason: `{reason}`\n\n"
                            f"**Action needed:** Manually close `{ticker}` in the Alpaca paper dashboard.",
                            level="error",
                        )
                    except Exception as _pdt_e:
                        log.debug(f"  PDT discord alert failed: {_pdt_e}")
                return {}
            log.warning(f"  CLOSE {ticker}: status={r.status_code} — {r.text[:150]} — {'retrying' if attempt == 0 else 'giving up'}")
            if attempt == 0:
                await asyncio.sleep(2)
        return {}

    async def close_all_positions(self, reason: str = "auto-close") -> None:
        """Close ALL open Alpaca positions using individual sell orders (options-safe)."""
        log.info(f"  🔔 EOD CLOSE — closing all positions  reason={reason}")
        positions = await self.get_positions()
        if not positions:
            log.info("  EOD CLOSE: no open positions")
            return
        for p in positions:
            sym = p.get("symbol")
            qty = abs(int(float(p.get("qty", 1))))
            if sym:
                await self.close_position(sym, reason, qty=qty)

# ── Alpaca data fetcher (replaces Polygon — free with paper account) ──────────

async def fetch_bars(ticker: str, minutes: int = 120) -> pd.DataFrame | None:
    """Fetch real-time 1-minute bars from Polygon Stocks Advanced."""
    try:
        end   = datetime.now(ET)
        start = end - timedelta(minutes=minutes + 60)
        start_str = start.strftime("%Y-%m-%d")
        end_str   = end.strftime("%Y-%m-%d")

        url = (
            f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/minute"
            f"/{start_str}/{end_str}"
            f"?adjusted=true&sort=asc&limit=500&apiKey={POLYGON_KEY}"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            r    = await client.get(url)
            data = r.json()

        status = data.get("status", "")
        if status == "NOT_AUTHORIZED":
            log.error(f"  ❌ Polygon NOT_AUTHORIZED for {ticker} — check Stocks Advanced plan")
            return None
        if status == "ERROR":
            log.error(f"  ❌ Polygon ERROR for {ticker}: {data.get('error','')}")
            return None

        results = data.get("results", [])
        if not results:
            log.warning(f"  ⚠️  {ticker}: Polygon returned 0 bars — market may be closed")
            return None

        df = pd.DataFrame(results)
        df = df.rename(columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low",       "c": "close","v": "volume", "vw": "vwap"
        })

        # Convert millisecond epoch → ET timezone
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["timestamp"] = df["timestamp"].dt.tz_convert("America/New_York")

        # Filter to market hours only (9:30am–4:00pm ET)
        t  = df["timestamp"]
        df = df[((t.dt.hour > 9) | ((t.dt.hour == 9) & (t.dt.minute >= 30))) &
                (t.dt.hour < 16)]
        df = df.reset_index(drop=True)

        if len(df) < 30:
            log.warning(f"  ⚠️  {ticker}: only {len(df)} market-hours bars — too early in session?")
            return None

        log.debug(f"  Polygon {ticker}: {len(df)} bars | latest close ${df['close'].iloc[-1]:.2f}")
        return df

    except Exception as e:
        log.error(f"  ❌  fetch_bars {ticker}: {e}")
        return None

# ── Feature builder ───────────────────────────────────────────────────────────

def build_live_features(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    rsi14    = rsi(c, 14)
    rsi3     = rsi(c, 3)
    e9       = ema(c, 9)
    e20      = ema(c, 20)
    macd_l   = ema(c, 12) - ema(c, 26)
    macd_s   = ema(macd_l, 9)
    macd_h   = macd_l - macd_s
    atr14    = atr(h, l, c, 14)
    vol_ma   = v.rolling(20).mean()

    bb_mid   = c.rolling(20).mean()
    bb_std   = c.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    pct_b    = (c - bb_lower) / (bb_upper - bb_lower + 1e-9)
    bb_width = (bb_upper - bb_lower) / (bb_mid + 1e-9)

    today_bars = df[df["timestamp"].dt.date == df["timestamp"].dt.date.iloc[-1]]
    open_bars  = today_bars[
        (today_bars["timestamp"].dt.hour == 9) & (today_bars["timestamp"].dt.minute >= 30) |
        (today_bars["timestamp"].dt.hour == 9) & (today_bars["timestamp"].dt.minute < 45)
    ]
    if len(open_bars) >= 2:
        orb_high = float(open_bars["high"].max())
        orb_low  = float(open_bars["low"].min())
    else:
        orb_high = float(today_bars["high"].max())
        orb_low  = float(today_bars["low"].min())

    last        = df.iloc[-1]
    close_now   = float(last["close"])
    vwap_now    = float(last.get("vwap", close_now))
    vol_now     = float(last["volume"])
    vol_avg     = float(vol_ma.iloc[-1]) if not np.isnan(vol_ma.iloc[-1]) else vol_now

    atr_val     = float(atr14)
    rsi14_val   = float(rsi14.iloc[-1])
    rsi3_val    = float(rsi3.iloc[-1])
    rsi3_slope  = float(rsi3.diff(3).iloc[-1])
    macd_h_val  = float(macd_h.iloc[-1])
    macd_h_prev = float(macd_h.iloc[-2]) if len(macd_h) > 1 else 0
    e9_val      = float(e9.iloc[-1])
    e20_val     = float(e20.iloc[-1])
    vol_ratio   = vol_now / (vol_avg + 1e-9)

    ts          = last["timestamp"]
    h_val, m_val = ts.hour, ts.minute
    mins_open   = (h_val - 9) * 60 + m_val - 30

    open_p      = float(last["open"])
    high_p      = float(last["high"])
    low_p       = float(last["low"])
    body        = abs(close_now - open_p)
    upper_wick  = high_p - max(close_now, open_p)
    lower_wick  = min(close_now, open_p) - low_p

    above_orb   = int(close_now > orb_high)
    below_orb   = int(close_now < orb_low)
    inside_orb  = int(not above_orb and not below_orb)

    feat = {
        "rsi14":           rsi14_val,
        "rsi3":            rsi3_val,
        "rsi3_slope":      rsi3_slope,
        "rsi_bullish":     int(rsi14_val > 50),
        "rsi_bearish":     int(rsi14_val < 50),
        "rsi_overbought":  int(rsi14_val > 70),
        "rsi_oversold":    int(rsi14_val < 30),
        "rsi3_bullish":    int(rsi3_val > 50),
        "macd_hist":       macd_h_val,
        "macd_line":       float(macd_l.iloc[-1]),
        "macd_sig":        float(macd_s.iloc[-1]),
        "macd_bullish":    int(macd_h_val > 0),
        "macd_cross_up":   int(macd_h_val > 0 and macd_h_prev <= 0),
        "macd_cross_dn":   int(macd_h_val < 0 and macd_h_prev >= 0),
        "vwap":            vwap_now,
        "above_vwap":      int(close_now > vwap_now),
        "vwap_dist_pct":   (close_now - vwap_now) / (vwap_now + 1e-9) * 100,
        "vwap_reclaim":    0,
        "vwap_lose":       0,
        "vwap_extended":   int(abs((close_now - vwap_now) / (vwap_now + 1e-9) * 100) > 0.5),
        "above_ema9":      int(close_now > e9_val),
        "above_ema20":     int(close_now > e20_val),
        "ema_bullish_stack": int(e9_val > e20_val and close_now > e9_val),
        "ema_bearish_stack": int(e9_val < e20_val and close_now < e9_val),
        "pct_b":           float(pct_b.iloc[-1]),
        "bb_width":        float(bb_width.iloc[-1]),
        "bb_upper_touch":  int(float(pct_b.iloc[-1]) > 0.95),
        "bb_lower_touch":  int(float(pct_b.iloc[-1]) < 0.05),
        "bb_squeeze":      int(float(bb_width.iloc[-1]) < float(bb_width.rolling(20).mean().iloc[-1]) * 0.8),
        "above_orb_high":  above_orb,
        "below_orb_low":   below_orb,
        "inside_orb":      inside_orb,
        "orb_high":        orb_high,
        "orb_low":         orb_low,
        "dist_orb_high":   (close_now - orb_high) / (atr_val + 1e-9),
        "dist_orb_low":    (close_now - orb_low)  / (atr_val + 1e-9),
        "failed_breakout": 0,
        "failed_breakdown":0,
        "vol_ratio":       vol_ratio,
        "vol_surge":       int(vol_ratio > 1.5),
        "vol_dry":         int(vol_ratio < 0.7),
        "low_vol_breakout": int(above_orb and vol_ratio < 0.85),
        "low_vol_breakdown":int(below_orb and vol_ratio < 0.85),
        "wick_ratio_upper": upper_wick / (body + 1e-9),
        "wick_ratio_lower": lower_wick / (body + 1e-9),
        "wick_ratio_total": (upper_wick + lower_wick) / (body + 1e-9),
        "price_mom5":      float(c.pct_change(5).iloc[-1]),
        "price_mom15":     float(c.pct_change(15).iloc[-1]),
        "price_mom30":     float(c.pct_change(30).iloc[-1]),
        "momentum_divergence": 0,
        "is_open_30min":   int(mins_open <= 30),
        "is_lunch":        int((h_val == 11 and m_val >= 30) or h_val == 12 or (h_val == 13 and m_val < 30)),
        "is_power_hour":   int((h_val == 14 and m_val >= 30) or h_val == 15),
        "is_close_30min":  int(h_val == 15 and m_val >= 30),
        "minutes_to_close": max(0, (15 * 60 + 58) - mins_open),
        "day_of_week":     ts.dayofweek,
        "atr14":           atr_val,
        "opening_trap":    int(mins_open <= 30 and (upper_wick + lower_wick) / (body + 1e-9) > 2),
        "lunch_trap":      int((h_val == 11 and m_val >= 30 or h_val == 12 or h_val == 13 and m_val < 30) and vol_ratio < 0.6),
        "timestamp":       str(last["timestamp"]),
    }

    if len(df) >= 2:
        prev_close = float(df.iloc[-2]["close"])
        prev_vwap  = float(df.iloc[-2].get("vwap", prev_close))
        feat["vwap_reclaim"] = int(close_now > vwap_now and prev_close <= prev_vwap)
        feat["vwap_lose"]    = int(close_now < vwap_now and prev_close >= prev_vwap)

    return pd.Series(feat)

# ── Rule-based conviction scorer ──────────────────────────────────────────────


# ── Arjun bias reader ─────────────────────────────────────────────────────────

def load_arjun_signals() -> dict:
    """Load today's Arjun ML signals. Returns dict keyed by ticker."""
    import glob
    from datetime import date
    sig_dir = "logs/signals"
    today   = date.today().strftime("%Y%m%d")
    matches = sorted(glob.glob(f"{sig_dir}/signals_{today}*.json"), reverse=True)
    if not matches:
        matches = sorted(glob.glob(f"{sig_dir}/*.json"), reverse=True)
    if not matches:
        return {}
    try:
        with open(matches[0]) as f:
            signals = json.load(f)
        return {s["ticker"]: s for s in signals}
    except Exception:
        return {}

# ── Discord Flow Signal Cache ────────────────────────────────────────────────
# Reads latest flow monitor signals — updated every 5 min by flow_monitor.py
_flow_cache: dict = {}
_flow_cache_time: float = 0.0

def get_flow_signal(ticker: str, strict: bool = False) -> dict:
    """
    Read latest Discord flow signal for ticker.
    Flow monitor writes to logs/chakra/flow_signals_latest.json every 5 min.
    Returns dict with: bias, confidence, vol_oi_ratio, is_extreme, dark_pool_pct

    strict=True  — entry conviction only:
                   indexes (SPY/QQQ/IWM/SPX): 10-min TTL (fast-moving, signal decays quickly)
                   stocks: 60-min TTL (swing signals valid longer, flow monitor re-writes less often)
    strict=False — monitoring/fakeout: 120-min TTL, still requires today
    """
    global _flow_cache, _flow_cache_time
    import time as _t
    if _t.time() - _flow_cache_time > 300:  # 5 min refresh
        try:
            import json as _j
            _p = Path("logs/chakra/flow_signals_latest.json")
            if _p.exists():
                _flow_cache      = _j.loads(_p.read_text())
                _flow_cache_time = _t.time()
                log.info(f"  [FLOW] cache loaded {len(_flow_cache)} signals "
                         f"(SPY {_flow_cache.get('SPY',{}).get('bias','?')} "
                         f"{_flow_cache.get('SPY',{}).get('confidence','?')}%)")
        except Exception as _e:
            log.warning(f"  [FLOW] cache load failed: {_e}")

    sig = _flow_cache.get(ticker, {})

    # Two-tier staleness filter — always require today, then check age by tier
    if sig:
        try:
            from datetime import date as _d2, datetime as _dt2
            sig_ts = sig.get("timestamp", "")
            if sig_ts:
                if str(_d2.today()) not in sig_ts[:10]:
                    sig = {}  # previous day — always reject
                else:
                    age_min = (_dt2.now() - _dt2.fromisoformat(sig_ts).replace(tzinfo=None)).total_seconds() / 60
                    _idx_tickers = {"SPY", "QQQ", "IWM", "SPX", "DIA"}
                    if strict:
                        max_age = 10 if ticker.upper() in _idx_tickers else 60
                    else:
                        max_age = 120
                    if age_min > max_age:
                        log.debug(f"  [FLOW] {ticker} signal {age_min:.1f}min old > {max_age}min limit (strict={strict}) — skip")
                        sig = {}
        except Exception:
            pass

    if not sig:
        # Check today's flow seen log for any recent signals
        # Infer direction from the dedup key: uoa_{ticker}_call_* → BULLISH, _put_* → BEARISH
        try:
            import json as _j
            from datetime import date as _d
            _p2 = Path("logs/chakra/flow_seen.json")
            if _p2.exists():
                seen = _j.loads(_p2.read_text())
                today = _d.today().isoformat()
                for key, val in seen.items():
                    if ticker.upper() not in key.upper():
                        continue
                    if today not in str(val):
                        continue
                    # Infer direction from key format: uoa_{TICKER}_call_* or uoa_{TICKER}_put_*
                    _key_lower = key.lower()
                    if f"_{ticker.lower()}_call" in _key_lower or f"_{ticker.lower()}_c_" in _key_lower:
                        _seen_bias = "BULLISH"
                    elif f"_{ticker.lower()}_put" in _key_lower or f"_{ticker.lower()}_p_" in _key_lower:
                        _seen_bias = "BEARISH"
                    else:
                        _seen_bias = "NEUTRAL"   # can't determine — skip
                    if _seen_bias == "NEUTRAL":
                        continue
                    sig = {"bias": _seen_bias, "confidence": 65,
                           "vol_oi_ratio": 10, "is_extreme": False,
                           "dark_pool_pct": 0, "source": "flow_seen"}
                    break
        except Exception:
            pass

    return sig


def get_market_regime_bias() -> dict:
    """
    George-style market regime: check SPY 5-day trend + GEX.
    Returns: bias (BULL/BEAR/NEUTRAL), discomfort (0-100), block_long, block_short
    """
    try:
        import httpx as _hx
        from datetime import date as _d, timedelta as _td
        key = os.getenv("POLYGON_API_KEY","")
        end = _d.today().isoformat()
        start = (_d.today() - _td(days=10)).isoformat()
        r = _hx.get(
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/{start}/{end}",
            params={"apiKey": key, "adjusted": "true", "sort": "asc", "limit": 10},
            timeout=5
        )
        bars = r.json().get("results", [])
        if len(bars) >= 5:
            closes   = [b["c"] for b in bars[-5:]]
            trend5   = (closes[-1] - closes[0]) / closes[0] * 100
            vol_ratio = bars[-1]["v"] / (sum(b["v"] for b in bars[-5:]) / 5)

            # Discomfort index: how stressed is the market (0=calm, 100=panic)
            discomfort = min(100, max(0, int(50 - trend5 * 5 + (vol_ratio - 1) * 20)))

            if trend5 <= -1.5:
                return {"bias":"BEAR","trend5":round(trend5,2),"discomfort":discomfort,
                        "block_long":True,"block_short":False}
            elif trend5 >= 1.5:
                return {"bias":"BULL","trend5":round(trend5,2),"discomfort":discomfort,
                        "block_long":False,"block_short":True}
            else:
                return {"bias":"NEUTRAL","trend5":round(trend5,2),"discomfort":discomfort,
                        "block_long":False,"block_short":False}
    except Exception:
        pass
    return {"bias":"NEUTRAL","trend5":0,"discomfort":50,"block_long":False,"block_short":False}


# ── Cache Arjun signals — reload every 30 minutes to avoid disk I/O every scan
_arjun_cache: dict = {}
_arjun_cache_time: float = 0.0

def get_arjun_bias(ticker: str) -> dict:
    """
    Returns Arjun's swing signal for ticker with conviction boost info.
    Uses 30-min cache to avoid reloading every scan.
    """
    global _arjun_cache, _arjun_cache_time
    import time
    if time.time() - _arjun_cache_time > 1800:  # 30 min
        _arjun_cache      = load_arjun_signals()
        _arjun_cache_time = time.time()
        if _arjun_cache:
            log.info(f"  🧠 Arjun signals loaded: {list(_arjun_cache.keys())}")

    sig = _arjun_cache.get(ticker, {})
    if not sig:
        return {"signal": "HOLD", "confidence": 0, "boost": 0, "reason": "no signal", "raw": {}}

    signal     = sig.get("signal", "HOLD")
    confidence = float(sig.get("confidence", 0))

    # If risk manager blocked the trade, synthesize the effective direction.
    # HOLD + BLOCK → check risk_manager.reason/blocks for bearish/bullish language
    _agents    = sig.get("agents", {})
    _risk_mgr  = _agents.get("risk_manager", {})
    _risk_dec  = _risk_mgr.get("decision", "")
    if signal == "HOLD" and _risk_dec == "BLOCK":
        _rm_blocks   = " ".join(_risk_mgr.get("blocks", []) + [_risk_mgr.get("reason", "")])
        _rm_lower    = _rm_blocks.lower()
        _bearish_kw  = any(k in _rm_lower for k in (
            "too risky for long", "bear score", "bearish", "negative gamma", "put wall",
            "short the pops", "overbought", "selling pressure"
        ))
        _bullish_kw  = any(k in _rm_lower for k in (
            "too risky for short", "bull score", "bullish", "positive gamma",
            "buy the dips", "oversold", "buying pressure"
        ))
        if _bearish_kw and not _bullish_kw:
            signal     = "SELL"
            confidence = max(confidence, 62.0)  # enough to trigger conflict gate
        elif _bullish_kw and not _bearish_kw:
            signal     = "BUY"
            confidence = max(confidence, 62.0)

    # Step 4: +15 boost when aligned, -20 penalty when opposing
    if signal == "BUY" and confidence >= 60:
        boost  = +15
        reason = f"ARJUN ✅ BUY {confidence:.0f}% — ML aligned (+15)"
    elif signal == "BUY" and confidence >= 45:
        boost  = +7
        reason = f"ARJUN 🟡 BUY {confidence:.0f}% — weak ML signal (+7)"
    elif signal == "SELL" and confidence >= 60:
        boost  = -20
        reason = f"ARJUN ❌ SELL {confidence:.0f}% — ML opposing (-20)"
    elif signal == "SELL" and confidence >= 45:
        boost  = -10
        reason = f"ARJUN 🟡 SELL {confidence:.0f}% — weak ML opposition (-10)"
    else:
        boost  = 0
        reason = f"ARJUN ⚪ HOLD {confidence:.0f}%"

    return {"signal": signal, "confidence": confidence, "boost": boost, "reason": reason, "raw": sig}

def conviction_score(row: pd.Series, ticker: str) -> dict:
    ts  = pd.to_datetime(row["timestamp"])
    ses = session_name(ts)

    # Individual stocks require a higher conviction bar than indexes
    _is_stock = is_stock(ticker)
    _is_qqq   = ticker.upper() == "QQQ"
    _stock_thr = CONVICTION_THRESHOLD_STOCK
    # QQQ historical data: 33% WR on 63 trades — needs its own higher threshold.
    # Power hour is the worst session for ALL tickers (data: -$2,570 on 14 trades).
    # Block QQQ in power hour entirely — lotto covers that window for SPY only.
    _base_thr = CONVICTION_THRESHOLD_QQQ if _is_qqq else (
                _stock_thr if _is_stock else CONVICTION_THRESHOLD_NORMAL)
    # Dynamic time-of-day threshold for non-QQQ, non-stock tickers
    if not _is_qqq and not _is_stock:
        _dyn_thr = get_conviction_threshold(ses, ts.hour, ts.minute)
        _base_thr = _dyn_thr
    thresholds = {
        "OPEN":       _base_thr,
        "NORMAL":     _base_thr,
        "LUNCH":      999,
        "POWER_HOUR": 999 if _is_qqq else ((_stock_thr - 5) if _is_stock else CONVICTION_THRESHOLD_POWER_HOUR),
        "CLOSE":      999,
        "PRE":        999,
        "CLOSED":     999,
    }
    thr = thresholds.get(ses, 999)

    # ── DISCORD FLOW SIGNAL (80% weight) ──────────────────────────────────────
    # Flow monitor runs every 5 min — if it has a signal, it dominates
    flow_sig    = get_flow_signal(ticker, strict=True)
    flow_conf   = int(flow_sig.get("confidence", 0))
    flow_bias   = flow_sig.get("bias", "NEUTRAL")
    flow_ratio  = float(flow_sig.get("vol_oi_ratio", 0))
    flow_extreme = bool(flow_sig.get("is_extreme", False))
    flow_dp_pct  = float(flow_sig.get("dark_pool_pct", 0))

    # Flow score: 0–80 points (80% of total conviction)
    flow_score = 0
    if flow_conf >= 80 and flow_bias in ("BULLISH","BEARISH"):
        flow_score = 80  # very high confidence flow → instant signal
    elif flow_conf >= 65 and flow_bias in ("BULLISH","BEARISH"):
        flow_score = int(flow_conf * 0.8)  # scale to 0-80
    elif flow_conf >= 50:
        flow_score = int(flow_conf * 0.5)  # partial credit

    # Extreme flow boost
    if flow_extreme:
        flow_score = min(80, flow_score + 15)
    if flow_dp_pct >= 0.3:
        flow_score = min(80, flow_score + 8)

    # ── ARJUN ML SIGNAL (10% weight + direct override) ───────────────────────
    arjun       = get_arjun_bias(ticker)
    arjun_conf  = float(arjun.get("confidence", 0))
    arjun_signal = arjun.get("signal", "HOLD")
    arjun_score = 0
    if arjun_signal == "BUY"  and arjun_conf >= 60: arjun_score = +10
    elif arjun_signal == "SELL" and arjun_conf >= 60: arjun_score = -10
    elif arjun_signal == "BUY"  and arjun_conf >= 45: arjun_score = +5
    elif arjun_signal == "SELL" and arjun_conf >= 45: arjun_score = -5

    # Arjun direct override: if Arjun would_trade=True with high confidence,
    # boost flow_score to ensure ARKA acts on Arjun's pick
    _arjun_raw = _arjun_cache.get(ticker, {})
    _arjun_would_trade = _arjun_raw.get("would_trade", False)
    if _arjun_would_trade and arjun_conf >= 70 and arjun_signal in ("BUY","SELL"):
        old_flow = flow_score
        flow_score = max(flow_score, 65)  # ensure we meet threshold
        if flow_score > old_flow:
            reasons.append(f"ARJUN OVERRIDE ⚡ {arjun_signal} {arjun_conf:.0f}% — elevated to trade")
            log.info(f"  ⚡ ARJUN OVERRIDE: {ticker} flow_score {old_flow}→{flow_score}")

    # ── MARKET REGIME GATE (George-style) ─────────────────────────────────────
    regime       = get_market_regime_bias()
    discomfort   = regime.get("discomfort", 50)
    regime_bias  = regime.get("bias", "NEUTRAL")
    block_long   = regime.get("block_long", False)
    block_short  = regime.get("block_short", False)

    # Technical score (10% weight) — computed below in comp{}
    comp = {}
    reasons = []

    # VWAP (20)
    vp = 0
    if row.get("above_vwap", 0):   vp += 12; reasons.append("above VWAP")
    if row.get("vwap_reclaim", 0): vp += 8;  reasons.append("VWAP reclaim")
    elif row.get("vwap_lose", 0):  vp -= 8;  reasons.append("lost VWAP")
    if abs(row.get("vwap_dist_pct", 0)) > 0.8: vp -= 6; reasons.append("VWAP extended")
    comp["vwap"] = np.clip(vp, -20, 20)

    # ORB (20)
    op = 0
    if row.get("above_orb_high", 0):   op += 16; reasons.append("above ORB")
    elif row.get("below_orb_low", 0):  op -= 16; reasons.append("below ORB")
    else:
        if row.get("dist_orb_high", 0) < -0.3: op += 6
        elif row.get("dist_orb_high", 0) > 0.3: op -= 6
    if row.get("failed_breakout", 0): op -= 10; reasons.append("failed breakout")
    comp["orb"] = np.clip(op, -20, 20)

    # MACD (15)
    mp = 0
    if row.get("macd_bullish", 0):   mp += 9
    if row.get("macd_cross_up", 0):  mp += 6; reasons.append("MACD cross up")
    elif row.get("macd_cross_dn", 0):mp -= 6; reasons.append("MACD cross dn")
    hist = row.get("macd_hist", 0)
    mp += np.clip(hist * 500, -4, 4)
    comp["macd"] = np.clip(mp, -15, 15)

    # RSI (15)
    rp = 0
    r14 = row.get("rsi14", 50)
    if r14 > 55: rp += 10
    elif r14 > 50: rp += 5
    elif r14 < 45: rp -= 5
    elif r14 < 40: rp -= 10
    slope = row.get("rsi3_slope", 0)
    if slope > 2:  rp += 5; reasons.append("RSI rising")
    elif slope < -2: rp -= 5
    if row.get("rsi_overbought", 0) and row.get("above_orb_high", 0): rp -= 4
    comp["rsi"] = np.clip(rp, -15, 15)

    # Volume (15)
    vr = row.get("vol_ratio", 1.0)
    if vr >= 1.5:   vp2 = 12; reasons.append(f"vol {vr:.1f}x")
    elif vr >= 1.0: vp2 = 6
    elif vr < 0.6:  vp2 = -8; reasons.append("low vol")
    elif vr < 0.8:  vp2 = -4
    else:           vp2 = 0
    comp["volume"] = np.clip(vp2, -15, 15)

    # EMA (15)
    ep = 0
    if row.get("ema_bullish_stack", 0):   ep += 12; reasons.append("EMA stack ✅")
    elif row.get("ema_bearish_stack", 0): ep -= 12
    elif row.get("above_ema9", 0):        ep += 6
    else:                                 ep -= 6
    if row.get("bb_squeeze", 0) and row.get("above_orb_high", 0):
        ep += 3; reasons.append("BB squeeze break")
    comp["ema"] = np.clip(ep, -15, 15)

    # ── Technical score (10% of total) ──────────────────────────────────
    raw        = sum(comp.values())
    tech_score = (raw + 100) / 2  # normalize to 0-100
    if ses == "POWER_HOUR":
        tech_score = min(100, tech_score * 1.08)
    tech_contribution = int(tech_score * 0.10)  # 10% weight

    # ── Arjun ML signal (10% of total) ────────────────────────────────
    arjun        = get_arjun_bias(ticker)
    arjun_boost  = arjun["boost"]   # already ±10
    arjun_reason = arjun["reason"]
    if arjun_boost != 0:
        reasons.append(arjun_reason)
    comp["arjun"] = arjun_boost

    # ── Discord Flow Signal (80% of total) ───────────────────────────
    flow_sig     = get_flow_signal(ticker, strict=True)
    flow_conf    = int(flow_sig.get("confidence", 0))
    flow_bias    = flow_sig.get("bias", "NEUTRAL")
    flow_ratio   = float(flow_sig.get("vol_oi_ratio", 0))
    flow_extreme = bool(flow_sig.get("is_extreme", False))
    flow_dp_pct  = float(flow_sig.get("dark_pool_pct", 0))

    # Scale flow to ±80 points — BULLISH adds (→ CALL), BEARISH subtracts (→ PUT)
    _flow_mag = 0
    if flow_conf >= 80 and flow_bias in ("BULLISH", "BEARISH"):
        _flow_mag = 80
    elif flow_conf >= 65 and flow_bias in ("BULLISH", "BEARISH"):
        _flow_mag = int(flow_conf * 0.80)
    elif flow_conf >= 50:
        _flow_mag = int(flow_conf * 0.50)

    if flow_extreme:  _flow_mag = min(80, _flow_mag + 15)
    if flow_dp_pct >= 0.30: _flow_mag = min(80, _flow_mag + 8)

    # Direction: BULLISH flow → positive (push toward CALL), BEARISH → negative (push toward PUT)
    if flow_bias == "BEARISH":
        flow_contribution = -_flow_mag
    else:
        flow_contribution = _flow_mag

    if flow_contribution != 0:
        reasons.append(f"Flow {flow_bias} {flow_conf}% conf {'🔥EXTREME' if flow_extreme else ''} ({flow_contribution:+d})")
    comp["flow_discord"] = flow_contribution
    # ── Market regime gate (George Discomfort Index) ──────────────────
    regime      = get_market_regime_bias()
    discomfort  = regime.get("discomfort", 50)
    block_long  = regime.get("block_long", False)
    block_short = regime.get("block_short", False)
    if discomfort >= 70:
        reasons.append(f"Discomfort {discomfort}% — size reduced")

    # ── Final combined score ──────────────────────────────────────────
    score = tech_contribution + arjun_boost + flow_contribution

    # Regime penalty — don't go LONG into bear market without strong bullish flow
    if block_long and flow_bias != "BULLISH" and flow_contribution < 60:
        score = max(0, score - 30)
        reasons.append(f"BEAR regime blocks LONG (SPY {regime.get('trend5',0):+.1f}%)")
    # Don't go SHORT into bull market without strong bearish flow (flow_contribution < -60)
    if block_short and flow_bias != "BEARISH" and flow_contribution > -60:
        score = min(100, score + 30)

    # Discomfort reduction
    if discomfort >= 70:
        score = int(score * 0.70)

    score = max(0, min(100, score))

    # ── Session 1: DEX + Hurst boost ────────────────────────────────
    dex_data   = {}
    hurst_data = {}
    if _POWER_MODULES:
        try:
            trade_dir = "LONG" if score >= 50 else "SHORT"
            dex_result   = get_dex_conviction_boost(ticker, trade_dir)
            hurst_result = get_hurst_conviction_boost(ticker, trade_dir)

            dex_boost   = dex_result.get("boost", 0)
            hurst_boost = hurst_result.get("boost", 0)

            if dex_boost != 0:
                score = max(0, min(100, score + dex_boost))
                reasons.append(dex_result["reason"])
                comp["dex"] = dex_boost

            if hurst_boost != 0:
                score = max(0, min(100, score + hurst_boost))
                reasons.append(hurst_result["reason"])
                comp["hurst"] = hurst_boost

            # Hurst threshold adjustment (RANDOM regime raises threshold)
            market_hurst = get_market_hurst()
            thr_adj = market_hurst.get("threshold_adj", 0)
            if thr_adj > 0:
                thr = min(999, thr + thr_adj)
                reasons.append(f"Hurst {market_hurst['regime']} → threshold +{thr_adj}")

            # ── Entropy adjustment ───────────────────────────────
            try:
                entropy_params = get_entropy_arka_params(ticker)
                e_thr_adj = entropy_params.get("threshold_adj", 0)
                e_mode    = entropy_params.get("mode", "NORMAL")
                if e_thr_adj != 0:
                    thr = min(999, max(0, thr + e_thr_adj))
                    reasons.append(f"Entropy {e_mode} → threshold {e_thr_adj:+d}")
                    comp["entropy"] = e_thr_adj
            except Exception:
                pass  # never let entropy break ARKA

            # ── Manifold Engine: Phase Space + Topology boost ─────────────
            try:
                from backend.arka.manifold_engine import ManifoldEngine
                _mf_engine = ManifoldEngine()
                _phase     = _mf_engine.phase_engine.get_state()
                _topology  = _mf_engine.topology_engine.detect_regime_change(
                    np.array([[row.get('close', 0)] * 5])  # minimal stub
                )
                _mf_result = _mf_engine.adjust_arka(score, _phase, _topology)
                _mf_mod    = _mf_result.get('modifier', 0)
                if _mf_mod != 0:
                    score = max(0, min(100, score + _mf_mod))
                    reasons.append(f"Manifold {_mf_result.get('regime','?')} → {_mf_mod:+.1f}")
                    comp['manifold'] = round(_mf_mod, 1)
            except Exception:
                pass  # never let manifold break ARKA



            dex_data   = dex_result.get("dex", {})
            hurst_data = hurst_result.get("hurst", {})
        except Exception as _e:
            pass   # never let power modules break ARKA

    # ── Regime Gates: Gamma Flip + Breadth + VIX ──────────────────────────
    if thr < 999:
        try:
            _spy_price = row.get("close", None)
            _gates     = get_regime_gates(spy_price=_spy_price)

            if _gates["suppress_longs"] and score >= 50:
                if flow_contribution >= 60:
                    # Strong directional flow overrides suppress_longs inversion.
                    # Don't flip to SHORT — just reduce conviction slightly.
                    score = max(50, score - 15)
                    reasons.append(f"[GATE] Longs suppressed — flow override, score capped ({_gates['bias']})")
                else:
                    # No strong flow — invert score to SHORT
                    score = max(0, 100 - score)
                    reasons.append(f"[GATE] Longs suppressed → SHORT {_gates['bias']} regime (score inverted)")

            _gate_adj = _gates["long_threshold_adj"] if score >= 50                         else _gates["short_threshold_adj"]
            if _gate_adj != 0:
                thr = min(999, max(40, thr + _gate_adj))
                reasons.append(
                    f"[GATE] thr {_gate_adj:+d} → {thr} | "
                    f"{_gates['reasons'][0] if _gates['reasons'] else _gates['bias']}"
                )
                comp["regime_gate"] = _gate_adj
        except Exception as _ge:
            log.warning(f"[GATES] error: {_ge}")
    # ─────────────────────────────────────────────────────────────────────────
    # S2/S3/S4 MODULE BOOSTS — injected by patchsession2/3/4
    try:
        import json as _j, pathlib as _pl

        # Entropy (S2)
        _ent_f = _pl.Path("logs/chakra/entropy_latest.json")
        if _ent_f.exists():
            _ent = _j.loads(_ent_f.read_text())
            _esig = _ent.get("signal", "NORMAL")
            _eval = float(_ent.get("entropy_score", 1.5))
            if _esig == "TRENDING" and _eval >= 2.0:
                score = min(100, score + 8); reasons.append(f"Entropy TRENDING({_eval:.2f}) +8")
            elif _esig == "CHOPPY":
                score = max(0, score - 8);  reasons.append(f"Entropy CHOPPY({_eval:.2f}) -8")

        # HMM Regime (S3)
        _hmm_f = _pl.Path("logs/chakra/hmm_latest.json")
        if _hmm_f.exists():
            _hmm = _j.loads(_hmm_f.read_text())
            _reg = _hmm.get("regime", "LOWVOL_TREND")
            if _reg == "CRISIS":
                thr = thr + 20; reasons.append("HMM CRISIS → thr +20")
            elif _reg == "CHOPPY_RANGE":
                thr = thr + 15; reasons.append("HMM CHOPPY → thr +15")
            elif _reg == "HIGHVOL_TREND":
                thr = thr + 5;  reasons.append("HMM HIGHVOL → thr +5")

        # Iceberg (S3)
        _ice_f = _pl.Path("logs/chakra/iceberg_latest.json")
        if _ice_f.exists():
            _ice = _j.loads(_ice_f.read_text())
            _idir = _ice.get("direction", "NEUTRAL")
            _iconf = float(_ice.get("confidence", 0))
            if _iconf > 0.6:
                if _idir == "BULLISH":
                    score = min(100, score + 12); reasons.append(f"Iceberg BULLISH +12")
                elif _idir == "BEARISH":
                    score = max(0, score - 6);   reasons.append(f"Iceberg BEARISH -6")

        # Kyle Lambda (S4)
        _lam_f = _pl.Path("logs/chakra/lambda_latest.json")
        if _lam_f.exists():
            _lam = _j.loads(_lam_f.read_text())
            _lsig = _lam.get("signal", "NORMAL")
            if _lsig == "EXTREME":
                score = 0; reasons.append("Lambda EXTREME — gated out")
            elif _lsig == "HIGH":
                score = max(0, score - 10); reasons.append("Lambda HIGH illiquidity -10")

        # ProbDist (S4)
        _pd_f = _pl.Path("logs/chakra/probdist_latest.json")
        if _pd_f.exists():
            _pd = _j.loads(_pd_f.read_text())
            _tail = float(_pd.get("tail_risk_pct", 0))
            if _tail > 0.15:
                reasons.append(f"ProbDist tail_risk={_tail:.1%} → size x0.5")
            elif _tail < 0.05:
                reasons.append(f"ProbDist tail_risk={_tail:.1%} → size x1.2")

    except Exception as _me:
        log.debug(f"[S2-S4 modules] {_me}")

    # ── Sector Rotation modifier ──────────────────────────────────────────────
    _signal_dir = "BULLISH" if score >= 50 else "BEARISH"
    try:
        from backend.chakra.sector_rotation import get_sector_conviction_modifier as _sec_mod
        _sec_adj = _sec_mod(ticker, _signal_dir)
        if _sec_adj != 0:
            score = max(0, min(100, score + _sec_adj))
            reasons.append(f"Sector rotation {_signal_dir} {_sec_adj:+d}")
            comp["sector"] = _sec_adj
    except Exception:
        pass

    # ── OI Delta boost ────────────────────────────────────────────────────────
    try:
        from backend.chakra.oi_tracker import get_oi_conviction_boost as _oi_boost_fn
        _oi_adj = _oi_boost_fn(ticker, _signal_dir)
        if _oi_adj != 0:
            score = max(0, min(100, score + _oi_adj))
            reasons.append(f"OI buildup {_signal_dir} {_oi_adj:+d}")
            comp["oi_delta"] = _oi_adj
    except Exception:
        pass

    # ── Sweep Detector + MOC Imbalance boost ─────────────────────────────────
    try:
        from backend.chakra.sweep_detector import get_sweep_boost as _get_sweep_boost
        _sweep_boost = _get_sweep_boost(ticker, _signal_dir)
        if _sweep_boost != 0:
            score = max(0, min(100, score + _sweep_boost))
            reasons.append(f"Sweep {'multi' if abs(_sweep_boost) >= 7 else 'single'}-exchange {_signal_dir} {_sweep_boost:+d}")
            comp["sweep"] = _sweep_boost
    except Exception:
        pass  # never let sweep detection break ARKA

    try:
        from backend.chakra.moc_imbalance import get_moc_conviction_modifier as _get_moc_mod
        if ticker in ("SPY", "QQQ", "SPX", "IWM"):
            _moc_mod = _get_moc_mod(ticker, _signal_dir)
            if _moc_mod != 0:
                score = max(0, min(100, score + _moc_mod))
                reasons.append(f"MOC imbalance {_signal_dir} {_moc_mod:+d}")
                comp["moc"] = _moc_mod
    except Exception:
        pass  # never let MOC detection break ARKA

    # ── Pullback / VWAP bounce boost (matches manual entry style) ────────────
    _pb_bars = row.get("_raw_bars", [])
    _pb_vwap = float(row.get("vwap", 0))
    if _pb_bars and _pb_vwap:
        _pb = detect_pullback(_pb_bars, _pb_vwap)
        if _pb["pullback"] and score >= 40:
            score = min(100, score + 15)
            reasons.append(f"📈 Pullback depth={_pb['depth_pct']:.1f}% +15")
            comp["pullback"] = 15
        if _pb["vwap_bounce"] and score >= 40:
            score = min(100, score + 10)
            reasons.append("📈 VWAP bounce confirmed +10")
            comp["vwap_bounce"] = 10

    # ── Retest detection: price bouncing off key level ────────────────────────
    if _pb_bars:
        try:
            _gex_for_retest = load_gex_state(ticker)
            _key_levels = [_pb_vwap] if _pb_vwap else []
            if _gex_for_retest:
                for _lvl_key in ("zero_gamma", "call_wall", "put_wall"):
                    _v = _gex_for_retest.get(_lvl_key)
                    if _v and _v > 0:
                        _key_levels.append(float(_v))
            if _key_levels:
                _rt = detect_retest(_pb_bars, _key_levels)
                if _rt["retest"] and score >= 40:
                    _rt_dir_long  = _rt["direction"] == "up"
                    _rt_dir_short = _rt["direction"] == "down"
                    _is_bullish   = score >= 50   # current bias
                    _aligned      = (_rt_dir_long and _is_bullish) or (_rt_dir_short and not _is_bullish)
                    if _aligned:
                        score = min(100, score + 20)
                        reasons.append(
                            f"🎯 Retest ${_rt['level']} confirmed ({_rt['direction'].upper()}) +20"
                        )
                        comp["retest"] = 20
                    else:
                        score = max(0, score - 10)
                        reasons.append(
                            f"⚠️ Retest ${_rt['level']} AGAINST bias ({_rt['direction'].upper()}) -10"
                        )
                        comp["retest"] = -10
        except Exception:
            pass   # never let retest block conviction

    # ── Adaptive threshold: lower bar for high-quality setups ────────────────
    _hs_confirms  = flow_extreme or flow_dp_pct >= 0.30
    _pb_confirms  = comp.get("pullback", 0) > 0 and comp.get("vwap_bounce", 0) > 0
    if _pb_confirms and _hs_confirms and thr < 999:
        thr = max(45, thr - 10)          # pullback + flow confirmed → thr -10
        reasons.append(f"Adaptive thr → {thr} (pullback+flow confirmed)")
    elif flow_extreme and thr < 999:
        thr = max(50, thr - 5)           # extreme flow alone → thr -5
        reasons.append(f"Adaptive thr → {thr} (extreme flow)")

    # SHORT thresholds — mirror of long thresholds, bearish side
    short_thr        = 100 - thr          # e.g. if long thr=55, short fires at score<=45
    short_strong_thr = 30                 # strong short when very bearish

    # Component consensus filter — require majority agreement before trading
    # Prevents MACD briefly going bullish from overriding 4 bearish components
    _core_comps = ["vwap", "orb", "macd", "rsi", "ema"]
    _bull_count = sum(1 for k in _core_comps if comp.get(k, 0) > 0)
    _bear_count = sum(1 for k in _core_comps if comp.get(k, 0) < 0)
    _consensus_long  = _bull_count >= 3   # at least 3/5 core components bullish
    _consensus_short = _bear_count >= 3   # at least 3/5 core components bearish

    # ── Historical accuracy boost from feedback log ───────────────────
    try:
        from backend.arjun.feedback_writer import get_historical_accuracy_boost as _hist_boost
        _acc_boost = _hist_boost(ticker, "CALL" if score > 50 else "PUT")
        if _acc_boost != 0:
            score = round(score + _acc_boost, 1)
            reasons.append(f"History: {'+' if _acc_boost > 0 else ''}{_acc_boost} accuracy adj")
    except Exception:
        pass

    # ── Weekly conviction adjustments from Sunday review ─────────────
    try:
        import json as _jadj
        from pathlib import Path as _Padj
        _adj_path = _Padj("logs/arjun/conviction_adjustments.json")
        if _adj_path.exists():
            _adj_data = _jadj.loads(_adj_path.read_text())
            _adj = _adj_data.get("adjustments", {}).get(ticker, {})
            if _adj:
                thr = max(45, thr + int(_adj.get("delta", 0)))
                reasons.append(f"Weekly adj: {_adj.get('reason','')}")
    except Exception:
        pass

    # ── RSI Divergence boost ──────────────────────────────────────────────────
    # Uses the same _raw_bars already fetched for pullback/retest detection.
    # Aligned divergence (BULLISH on a CALL signal, BEARISH on a PUT signal) adds
    # 12-20 pts. Opposing divergence subtracts 8 pts.
    try:
        _div_bars = row.get("_raw_bars", [])
        if len(_div_bars) >= 20:
            from backend.chakra.modules.rsi_divergence import detect_rsi_divergence, score_divergence as _score_div

            def _rsi_s(closes, period=14):
                g, l = [], []
                for i in range(1, len(closes)):
                    d = closes[i] - closes[i-1]
                    g.append(max(d, 0)); l.append(max(-d, 0))
                ag = sum(g[:period]) / period
                al = sum(l[:period]) / period
                out = []
                for i in range(period, len(closes)):
                    if i > period:
                        ag = (ag*(period-1) + g[i-1]) / period
                        al = (al*(period-1) + l[i-1]) / period
                    rs = ag / al if al > 0 else 100
                    out.append(100 - 100/(1+rs))
                return out

            _div_closes = [float(b["c"]) for b in _div_bars if "c" in b]
            _div_rsi    = _rsi_s(_div_closes)
            if len(_div_closes) >= 16 and len(_div_rsi) >= 14:
                _div_c_aligned = _div_closes[len(_div_closes) - len(_div_rsi):]
                _div = detect_rsi_divergence(_div_c_aligned, _div_rsi, lookback=14)
                _div_pts, _div_dir = _score_div(_div)
                _current_dir = "CALL" if score >= 50 else "PUT"
                if _div.get("type") and _div_pts > 0:
                    if _div_dir == _current_dir:
                        # Aligned — full boost
                        score = min(100, score + _div_pts)
                        reasons.append(f"RSI {_div['type']} divergence +{_div_pts} ✅")
                        comp["rsi_divergence"] = _div_pts
                    elif _div_dir and _div_dir != _current_dir:
                        # Opposing — mild penalty
                        score = max(0, score - 8)
                        reasons.append(f"RSI {_div['type']} divergence opposes signal -8 ⚠️")
                        comp["rsi_divergence"] = -8
    except Exception:
        pass  # never let divergence detection block ARKA

    # Strong institutional flow overrides technical consensus requirement.
    # When flow is conf ≥ 85%, the flow IS the consensus — don't block on technicals.
    _flow_overrides_consensus = flow_conf >= 85 and flow_bias in ("BULLISH", "BEARISH")
    if _flow_overrides_consensus and flow_bias == "BULLISH":
        _consensus_long  = True
    if _flow_overrides_consensus and flow_bias == "BEARISH":
        _consensus_short = True
        # Also restore bearish score — clamped-to-0 hides PUT conviction
        if score == 0 and flow_contribution <= -60:
            score = max(0, 20 - _bull_count * 3)  # small score in PUT range (≤ short_thr)

    if ses in ("LUNCH", "CLOSE"):   direction = "FLAT"
    elif score >= 70 and _consensus_long:               direction = "STRONG_LONG"
    elif score >= thr and _consensus_long:              direction = "LONG"
    elif score <= short_strong_thr and _consensus_short: direction = "STRONG_SHORT"
    elif score <= short_thr and _consensus_short:       direction = "SHORT"
    elif score <= short_thr and not _consensus_short:
        # Score says short but components conflict — reduce to FLAT
        reasons.append(f"SHORT blocked: only {_bear_count}/5 bearish components")
        direction = "FLAT"
    elif score >= thr and not _consensus_long:
        # Score says long but components conflict — reduce to FLAT
        reasons.append(f"LONG blocked: only {_bull_count}/5 bullish components")
        direction = "FLAT"
    else:                           direction = "FLAT"

    return {
        "score":       round(score, 1),
        "direction":   direction,
        "should_trade":direction in ("LONG", "STRONG_LONG", "SHORT", "STRONG_SHORT"),
        "session":     ses,
        "threshold":   thr,
        "components":  comp,
        "reasons":     reasons,
        "arjun_bias":  arjun,
        "dex":         dex_data,
        "hurst":       hurst_data,
    }

# ── Fakeout model loader ──────────────────────────────────────────────────────

def load_fakeout_models() -> dict:
    models = {}
    for ticker in TICKERS:
        path = os.path.join(MODEL_DIR, f"arka_fakeout_{ticker.lower()}.pkl")
        if os.path.exists(path):
            with open(path, "rb") as f:
                models[ticker] = pickle.load(f)
            log.info(f"  Loaded fakeout model: {ticker}")
        else:
            log.warning(f"  Fakeout model not found: {path} — fakeout filter disabled for {ticker}")
    return models

def fakeout_prob(row: pd.Series, ticker: str, models: dict) -> float:
    if ticker not in models:
        return 0.0
    m     = models[ticker]
    feats = m["features"]
    X     = pd.DataFrame([row[feats].fillna(0)])
    return float(m["model"].predict_proba(X)[0, 1])


# ── ARKA Scalp Model — intraday WIN probability ───────────────────────────────
_ARKA_SCALP_MODEL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "arjun", "models", "arka_scalp_model.json"
)
_GEX_REGIME_MAP  = {"POSITIVE_GAMMA": 1, "NEGATIVE_GAMMA": -1, "LOW_VOL": 0}
_REGIME_CALL_MAP = {"SHORT_THE_POPS": -1, "FOLLOW_MOMENTUM": 1, "BUY_THE_DIPS": 0, "NEUTRAL": 0}
_FLOW_BIAS_MAP   = {"STRONG_BULLISH": 2, "BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1, "STRONG_BEARISH": -2}
_SESSION_MAP     = {"MORNING": 1, "MIDDAY": 0, "POWER_HOUR": 2, "LUNCH": -1}
_DIRECTION_MAP   = {"CALL": 1, "PUT": -1}


def load_arka_scalp_model():
    """Load the ARKA intraday scalp model trained by weekly_retrain.py."""
    try:
        from xgboost import XGBClassifier
        m = XGBClassifier()
        path = os.path.abspath(_ARKA_SCALP_MODEL_PATH)
        if not os.path.exists(path):
            log.warning(f"  ARKA scalp model not found at {path} — model filter disabled")
            return None
        m.load_model(path)
        log.info(f"  ✅ ARKA scalp model loaded: {path}")
        return m
    except Exception as e:
        log.warning(f"  ARKA scalp model load failed ({e}) — model filter disabled")
        return None


def arka_scalp_win_prob(
    conviction: float,
    threshold: float,
    gex_state: dict | None,
    regime_call: str,
    bias_ratio: float,
    flow: dict,
    row,          # pd.Series from build_live_features
    session: str,
    direction: str,           # "CALL" or "PUT"
    was_post_loss: int,
    is_reversal: int,
    prior_loss_pnl: float,
    model,
) -> float | None:
    """
    Compute intraday WIN probability from the ARKA scalp model.
    Returns float 0-1, or None if model unavailable.
    Feature order must match ARKA_FEATURE_NAMES in weekly_retrain.py.
    """
    if model is None:
        return None
    try:
        import numpy as _np
        _gex = gex_state or {}
        _flow_is_extreme = 1 if flow.get("is_extreme") else 0
        _vwap_above      = 1 if float(row.get("vwap_dist_pct", 0)) > 0 else 0
        _ema_aligned     = 1 if row.get("ema_stack_bull", 0) else 0

        vec = _np.array([[
            float(conviction),
            float(threshold),
            float(conviction) - float(threshold),          # conviction_margin
            float(_GEX_REGIME_MAP.get(_gex.get("regime", ""), 0)),
            float(_REGIME_CALL_MAP.get(regime_call, 0)),
            float(bias_ratio),
            float(1 if abs(float(_gex.get("spot", 0)) - float(_gex.get("zero_gamma", 0) or 0)) <= 1.5 else 0),
            float(_FLOW_BIAS_MAP.get(flow.get("bias", "NEUTRAL"), 0)),
            float(flow.get("confidence", 0)),
            float(_flow_is_extreme),
            float(row.get("rsi14", 50)),
            float(_vwap_above),
            float(row.get("volume_ratio", 1.0)),
            float(_ema_aligned),
            float(_SESSION_MAP.get(session, 0)),
            float(_DIRECTION_MAP.get(direction, 1)),
            float(was_post_loss),
            float(is_reversal),
            float(prior_loss_pnl),
            float(is_reversal) * float(_flow_is_extreme),  # reversal_x_flow
            float(is_reversal) * float(conviction),         # reversal_x_conviction
        ]], dtype=float)
        _np.nan_to_num(vec, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        return float(model.predict_proba(vec)[0, 1])
    except Exception as _e:
        log.debug(f"  [arka_scalp_win_prob] {_e}")
        return None


# ── Pullback detector — matches manual trading style ─────────────────────────

def detect_pullback(bars: list, vwap: float) -> dict:
    """
    Detect if price just bounced off a dip — ARKA entry signal.
    Matches the red-dot entry on TradingView charts.
    Returns: {pullback, vwap_bounce, depth_pct, bounce_bars}
    """
    if len(bars) < 5:
        return {"pullback": False, "vwap_bounce": False, "depth_pct": 0.0, "bounce_bars": 0}

    recent = bars[-5:]
    closes = [b.get("c", b.get("close", 0)) for b in recent]
    lows   = [b.get("l", b.get("low", 0))   for b in recent]

    if not all(closes) or not all(lows):
        return {"pullback": False, "vwap_bounce": False, "depth_pct": 0.0, "bounce_bars": 0}

    min_low = min(lows)
    dip_idx = lows.index(min_low)
    current = closes[-1]

    # Bounce: dip happened before last bar, and price recovered above it
    bounced = dip_idx < 4 and current > min_low * 1.001
    depth   = (closes[0] - min_low) / closes[0] * 100 if closes[0] else 0

    # VWAP bounce: price dipped below VWAP and is now back above it
    vwap_bounce = bool(vwap and min_low < vwap and current > vwap)

    return {
        "pullback":    bounced and depth > 0.1,
        "vwap_bounce": vwap_bounce,
        "depth_pct":   round(depth, 2),
        "bounce_bars": 4 - dip_idx,
    }


# ── VWAP Surge Detector — fast momentum entry signal ─────────────────────────

def detect_vwap_surge(bars: list, vwap: float) -> dict:
    """
    Detect sharp velocity moves away from VWAP in the last 3 bars.
    A surge = price moving >0.8% in 3 bars AND already >0.5% from VWAP anchor.
    These are high-momentum entries — price is committed, not drifting.
    """
    if len(bars) < 4 or not vwap:
        return {"surge": False, "surge_up": False, "surge_down": False,
                "deviation_pct": 0.0, "move_3bar_pct": 0.0}

    current   = bars[-1].get("c", bars[-1].get("close", 0))
    prev3     = bars[-4].get("c", bars[-4].get("close", 0))
    if not current or not prev3:
        return {"surge": False, "surge_up": False, "surge_down": False,
                "deviation_pct": 0.0, "move_3bar_pct": 0.0}

    deviation   = (current - vwap)  / vwap  * 100
    move_3bars  = (current - prev3) / prev3 * 100

    surge_up   = move_3bars >  0.8 and deviation >  0.5
    surge_down = move_3bars < -0.8 and deviation < -0.5

    return {
        "surge":          surge_up or surge_down,
        "surge_up":       surge_up,
        "surge_down":     surge_down,
        "deviation_pct":  round(deviation, 2),
        "move_3bar_pct":  round(move_3bars, 2),
    }


# ── Retest detector — key level bounce confirmation ───────────────────────────

def detect_retest(bars: list, key_levels: list) -> dict:
    """
    Detect if price is retesting a key level (VWAP, zero gamma, call/put wall).
    A retest = price touched the level within the last 3 bars and is now bouncing.
    Returns: {retest, level, direction, distance_pct, bars_since_touch}
    direction='up'  → price bounced up off support (CALL signal)
    direction='down'→ price rejected down from resistance (PUT signal)
    """
    empty = {"retest": False, "level": None, "direction": None, "distance_pct": 0.0, "bars_since_touch": 0}
    if len(bars) < 4 or not key_levels:
        return empty

    recent  = bars[-4:]
    closes  = [b.get("c", b.get("close", 0)) for b in recent]
    highs   = [b.get("h", b.get("high",  0)) for b in recent]
    lows    = [b.get("l", b.get("low",   0)) for b in recent]
    current = closes[-1]

    if not current or current <= 0:
        return empty

    tolerance = current * 0.002   # 0.2% touch zone

    for lvl in key_levels:
        if not lvl or lvl <= 0:
            continue
        for i, (lo, hi, cl) in enumerate(zip(lows, highs, closes)):
            bars_ago = len(recent) - 1 - i
            if bars_ago == 0:
                continue   # skip current bar — need prior touch
            # Support retest: low touched level, current close is above and recovering
            if abs(lo - lvl) <= tolerance or lo <= lvl <= hi:
                if current > lvl and current > cl:
                    dist = abs(current - lvl) / lvl * 100
                    return {
                        "retest":          True,
                        "level":           round(lvl, 2),
                        "direction":       "up",
                        "distance_pct":    round(dist, 2),
                        "bars_since_touch": bars_ago,
                    }
            # Resistance retest: high touched level, current close is below and fading
            if abs(hi - lvl) <= tolerance or lo <= lvl <= hi:
                if current < lvl and current < cl:
                    dist = abs(current - lvl) / lvl * 100
                    return {
                        "retest":          True,
                        "level":           round(lvl, 2),
                        "direction":       "down",
                        "distance_pct":    round(dist, 2),
                        "bars_since_touch": bars_ago,
                    }

    return empty


# ── Position sizer ────────────────────────────────────────────────────────────

def calc_position(buying_power: float, price: float, session: str, atr_val: float) -> dict:
    """
    ARKA Scalper: options contracts only.
    Max 3 contracts per trade. Uses options_buying_power cap.
    Stop = -20% from entry, Target = +40% from entry (2:1 R:R).
    """
    # Estimate option premium: ATM 0DTE ≈ 0.3% of underlying (SPY $540 → ~$1.60)
    # This is only used as a fallback if Alpaca fill price isn't returned immediately
    est_premium   = round(price * 0.003, 2)
    contract_cost = est_premium * 100  # 1 contract = 100 shares

    # ── 0DTE Scalp budget: $4,000 per trade ─────────────────────────────────
    SCALP_BUDGET = 4000.0   # max dollars to spend per scalp trade
    qty          = 2        # 2 contracts per trade

    # Fixed % stops/targets based on options premium (not underlying)
    stop = round(est_premium * 0.80, 2)   # -20% stop on premium
    tgt  = round(est_premium * 1.40, 2)   # +40% target on premium (2:1 R:R)
    risk = est_premium * qty * 100
    return {"qty": qty, "stop": stop, "target": tgt,
            "risk_dollars": round(risk, 2), "est_premium": est_premium}

# ── Daily state ───────────────────────────────────────────────────────────────

def _restore_buckets(trade_log: list, state) -> None:
    """Recalculate per-bucket realized P&L from a saved trade_log after a restart.
    Keeps budgets accurate: positive P&L inflates available budget, losses reduce it.
    Falls back to ticker-based bucket detection for old logs without a 'bucket' field.
    """
    odte_pnl  = 0.0
    swing_pnl = 0.0
    for t in trade_log:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        pnl = float(pnl)
        bucket = t.get("bucket", "")
        if not bucket:
            # Infer bucket from ticker if old log entry has no bucket field
            ticker = t.get("ticker", "")
            bucket = "swing" if is_stock(ticker) else "odte"
        if bucket == "swing":
            swing_pnl += pnl
        else:
            odte_pnl  += pnl
    state.odte_realized_pnl  = round(odte_pnl,  2)
    state.swing_realized_pnl = round(swing_pnl, 2)


class DayState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.trades_today       = 0
        self.losses_streak      = 0
        self.paused_until       = None
        self.daily_pnl          = 0.0
        self.odte_realized_pnl  = 0.0   # realized P&L from 0DTE positions today
        self.swing_realized_pnl = 0.0   # realized P&L from swing positions today
        self.starting_equity    = None
        self.stopped            = False
        self.open_positions     = {}
        self.position_peaks     = {}  # track high-water mark for runner exit
        self.trade_log          = []
        self.scan_history       = []   # NEW: track scores over time for dashboard
        self.stop_cooldowns     = {}   # ticker → datetime when cooldown expires
        self.entries_today      = {}   # ticker → count of entries today
        self.ticker_daily_pnl   = {}   # ticker → realized P&L today (for per-ticker loss cap)
        self.large_loss_tickers = {}   # ticker → {"pnl": float, "direction": "CALL"/"PUT"}
                                       # Set when a single trade loses >= LARGE_LOSS_THRESHOLD.
                                       # Re-entry allowed but at elevated conviction threshold.

    def is_paused(self) -> bool:
        if self.paused_until and datetime.now(ET) < self.paused_until:
            remaining = int((self.paused_until - datetime.now(ET)).total_seconds() / 60)
            log.info(f"  ⏸  PAUSED — losing streak, {remaining}min remaining")
            return True
        return False

    def record_trade(self, ticker, side, price, qty, pnl=None, bucket: str = "",
                     gex_override: bool = False):
        entry = {
            "time":         now_et().strftime("%H:%M"),
            "ticker":       ticker,
            "side":         side,
            "price":        price,
            "qty":          qty,
            "pnl":          pnl,
            "bucket":       bucket,       # "odte" or "swing" — for per-bucket P&L tracking
            "gex_override": gex_override, # True = traded against a GEX block (conviction ≥90)
        }
        self.trade_log.append(entry)
        # Count entries (BUY/SHORT) immediately — not just on close
        if side in ("BUY", "SHORT") and pnl is None:
            self.trades_today += 1
        if pnl is not None:
            self.daily_pnl += pnl
            # Per-bucket realized P&L
            if bucket == "odte":
                self.odte_realized_pnl  += pnl
            elif bucket == "swing":
                self.swing_realized_pnl += pnl
            # Per-ticker realized P&L for daily loss cap
            if not hasattr(self, 'ticker_daily_pnl'):
                self.ticker_daily_pnl = {}
            self.ticker_daily_pnl[ticker] = self.ticker_daily_pnl.get(ticker, 0) + pnl
            if pnl < 0:
                self.losses_streak += 1
                if self.losses_streak >= LOSING_STREAK_STOP:
                    # 5+ consecutive losses — stop for the rest of the day
                    self.stopped = True
                    log.warning(
                        f"  🛑 {self.losses_streak} consecutive losses — STOPPING FOR THE DAY"
                    )
                    self._recalibrate_after_losses()  # final recal + Discord report
                elif self.losses_streak >= LOSING_STREAK_LIMIT:
                    self.paused_until = datetime.now(ET) + timedelta(seconds=LOSING_STREAK_PAUSE)
                    log.warning(f"  ⚠️  {self.losses_streak} consecutive losses — pausing 30min")
                    self._recalibrate_after_losses()
            else:
                self.losses_streak = 0
            self.trades_today += 1
            # ── Self-correction: check after every completed trade ─────────
            self._maybe_self_correct()

    def _maybe_self_correct(self):
        """
        Reload thresholds and — every check_interval_trades closed trades —
        run win-rate analysis to nudge thresholds up or down.
        """
        try:
            reload_thresholds()
        except Exception as _sc_err:
            log.debug(f"  [self-correct] {_sc_err}")

        try:
            _cfg  = load_arka_config()
            _sc   = _cfg.get("self_correct", {})
            if not _sc.get("enabled", True):
                return

            _interval = int(_sc.get("check_interval_trades", 5))
            _min_tr   = int(_sc.get("min_trades_to_adjust",  5))

            # Count closed trades (have pnl)
            _closed = [t for t in self.trade_log if t.get("pnl") is not None]
            if len(_closed) < _min_tr or len(_closed) % _interval != 0:
                return

            # Win-rate over last _interval closed trades
            _recent   = _closed[-_interval:]
            _wins     = sum(1 for t in _recent if float(t.get("pnl", 0)) > 0)
            _win_rate = _wins / _interval

            _thr       = _cfg.get("thresholds", {})
            _step      = int(_sc.get("conviction_step", 2))
            _conv_min  = int(_sc.get("conviction_min",  40))
            _conv_max  = int(_sc.get("conviction_max",  70))
            _f_step    = float(_sc.get("fakeout_step",  0.05))
            _f_min     = float(_sc.get("fakeout_min",   0.48))
            _f_max     = float(_sc.get("fakeout_max",   0.75))

            _old_conv = int(_thr.get("conviction_normal", 55))
            _old_f    = float(_thr.get("fakeout_block",   0.55))
            _changed  = False

            _wr_low  = float(_sc.get("win_rate_low_trigger",  0.50))
            _wr_high = float(_sc.get("win_rate_high_trigger", 0.75))

            if _win_rate < _wr_low:
                # Losing more than winning — tighten up
                _new_conv = min(_conv_max, _old_conv + _step)
                _new_f    = max(_f_min,    _old_f    - _f_step)
                _direction = "tightened (low win rate)"
                _changed = True
            elif _win_rate > _wr_high:
                # On a hot streak — can loosen slightly to capture more
                _new_conv = max(_conv_min, _old_conv - _step)
                _new_f    = min(_f_max,    _old_f    + _f_step)
                _direction = "loosened (high win rate)"
                _changed = True
            else:
                return  # Win rate in acceptable range, no change

            if not _changed:
                return

            _thr["conviction_normal"]     = _new_conv
            _thr["conviction_power_hour"] = max(45, _thr.get("conviction_power_hour", 45) + (_step if _win_rate < _wr_low else -_step))
            _thr["fakeout_block"]         = round(_new_f, 3)
            _cfg["thresholds"]  = _thr
            _cfg["updated_at"]  = datetime.now(ET).isoformat()
            _cfg["updated_by"]  = "win_rate_self_correct"
            _cfg.setdefault("history", []).append({
                "ts":        datetime.now(ET).isoformat(),
                "event":     f"win_rate_adjust_{_direction}",
                "win_rate":  round(_win_rate, 3),
                "old_conv":  _old_conv,
                "new_conv":  _new_conv,
                "old_f":     _old_f,
                "new_f":     round(_new_f, 3),
            })
            _cfg["history"] = _cfg["history"][-50:]

            with open(_CONFIG_FILE, "w") as _wf:
                json.dump(_cfg, _wf, indent=2)

            reload_thresholds()
            log.info(
                f"  📊 Win-rate self-correct ({_direction}): "
                f"conv {_old_conv}→{_new_conv} | fakeout {_old_f:.3f}→{_new_f:.3f} "
                f"(win rate {_win_rate:.0%} over last {_interval} trades)"
            )

        except Exception as _sce2:
            log.debug(f"  [win-rate self-correct] {_sce2}")

    def _recalibrate_after_losses(self):
        """
        Called when losing streak >= LOSING_STREAK_LIMIT.
        Analyzes recent losses, tightens thresholds, posts Discord loss report.
        """
        try:
            import asyncio as _asyncio

            # ── 1. Collect the recent losing trades ───────────────────────
            _recent_losses = [
                t for t in self.trade_log
                if t.get("pnl") is not None and float(t.get("pnl", 0)) < 0
            ][-self.losses_streak:]

            if not _recent_losses:
                return

            _total_loss   = sum(float(t["pnl"]) for t in _recent_losses)
            _loss_count   = len(_recent_losses)
            _loss_tickers = [t["ticker"] for t in _recent_losses]
            _loss_buckets = [t.get("bucket", "?") for t in _recent_losses]

            # ── 2. Diagnose patterns ──────────────────────────────────────
            _diagnoses = []

            # Check direction bias (were all losses calls or all puts?)
            _call_losses  = sum(1 for t in _recent_losses if "C" in t.get("ticker", "")[-10:])
            _put_losses   = sum(1 for t in _recent_losses if "P" in t.get("ticker", "")[-10:])
            if _call_losses == _loss_count:
                _diagnoses.append("All losses on CALLS — market may be rejecting upside")
            elif _put_losses == _loss_count:
                _diagnoses.append("All losses on PUTS — market may be rejecting downside")

            # Check if all stock losses (swing bucket)
            _swing_losses = sum(1 for b in _loss_buckets if b == "swing")
            if _swing_losses == _loss_count:
                _diagnoses.append("All losses in SWING bucket — stock options too expensive or trend wrong")

            # Check average loss size
            _avg_loss = abs(_total_loss) / _loss_count
            if _avg_loss > 60:
                _diagnoses.append(f"Large avg loss ${_avg_loss:.0f} — premiums too expensive, stops too wide")

            if not _diagnoses:
                _diagnoses.append("Repeated losses across mixed signals — market conditions unfavorable")

            # ── 3. Load config and apply threshold adjustments ────────────
            _cfg     = load_arka_config()
            _old_cfg = json.loads(json.dumps(_cfg))  # deep copy

            _thr = _cfg.get("thresholds", {})
            _sc  = _cfg.get("self_correct", {})
            _step           = int(_sc.get("conviction_step", 2))
            _conv_max       = int(_sc.get("conviction_max", 70))
            _fakeout_step   = float(_sc.get("fakeout_step", 0.05))
            _fakeout_max    = float(_sc.get("fakeout_max", 0.75))

            _old_conv    = int(_thr.get("conviction_normal", 55))
            _old_fakeout = float(_thr.get("fakeout_block", 0.55))
            _fakeout_min = float(_sc.get("fakeout_min", 0.48))  # use config floor, not hardcoded

            # Paralysis check: if we're at both limits simultaneously, the self-correction
            # has blocked all trading. Reset to safe defaults instead of continuing to tighten.
            _at_conv_max   = _old_conv >= _conv_max - 2
            _at_fakeout_fl = _old_fakeout <= _fakeout_min + 0.02
            if _at_conv_max and _at_fakeout_fl:
                log.warning(
                    f"  ⚠️  SELF-CORRECT PARALYSIS DETECTED — conv={_old_conv}/{_conv_max} "
                    f"fakeout={_old_fakeout}/{_fakeout_min} — resetting to safe defaults"
                )
                _new_conv    = 55
                _new_fakeout = 0.55
            else:
                # Raise conviction by 2x step on a loss streak (more conservative)
                _new_conv    = min(_conv_max, _old_conv + _step * 2)
                # Tighten fakeout filter (lower = more trades blocked), respect config floor
                _new_fakeout = max(_fakeout_min, _old_fakeout - _fakeout_step)

            _thr["conviction_normal"]     = _new_conv
            _thr["conviction_power_hour"] = max(45, _thr.get("conviction_power_hour", 45) + _step)
            _thr["fakeout_block"]         = round(_new_fakeout, 3)

            _cfg["thresholds"] = _thr
            _cfg["updated_at"] = datetime.now(ET).isoformat()
            _cfg["updated_by"] = "auto_recalibrate"

            # Record in history
            _cfg.setdefault("history", []).append({
                "ts":           datetime.now(ET).isoformat(),
                "event":        f"loss_streak_{_loss_count}",
                "total_loss":   round(_total_loss, 2),
                "old_conv":     _old_conv,
                "new_conv":     _new_conv,
                "old_fakeout":  _old_fakeout,
                "new_fakeout":  round(_new_fakeout, 3),
                "diagnoses":    _diagnoses,
            })
            # Keep only last 50 history entries
            _cfg["history"] = _cfg["history"][-50:]

            with open(_CONFIG_FILE, "w") as _f:
                json.dump(_cfg, _f, indent=2)

            # Apply immediately (no restart needed)
            reload_thresholds()

            log.warning(
                f"  🔧 AUTO-RECALIBRATE: conviction {_old_conv}→{_new_conv} | "
                f"fakeout {_old_fakeout:.3f}→{_new_fakeout:.3f}"
            )

            # ── 4. Post Discord loss report ───────────────────────────────
            _stopped_day = self.losses_streak >= LOSING_STREAK_STOP
            _loop = _asyncio.get_event_loop()
            if _loop.is_running():
                _asyncio.ensure_future(
                    _post_loss_recalibration_report(
                        loss_count=_loss_count,
                        total_loss=_total_loss,
                        loss_tickers=_loss_tickers,
                        diagnoses=_diagnoses,
                        old_conv=_old_conv,
                        new_conv=_new_conv,
                        old_fakeout=_old_fakeout,
                        new_fakeout=round(_new_fakeout, 3),
                        pause_minutes=LOSING_STREAK_PAUSE // 60,
                        stopped_for_day=_stopped_day,
                    )
                )

        except Exception as _rc_err:
            log.error(f"  [recalibrate] {_rc_err}")

    def record_scan(self, ticker: str, score: float, fakeout: float, decision: str):
        """NEW: Track conviction scores over time for dashboard charting."""
        self.scan_history.append({
            "time":    now_et().strftime("%H:%M"),
            "ticker":  ticker,
            "score":   score,
            "fakeout": fakeout,
            "decision": decision,
        })
        # keep last 200 scans only
        if len(self.scan_history) > 200:
            self.scan_history = self.scan_history[-200:]

    def save_summary(self):
        def _serial(obj):
            """JSON serializer for datetime/date objects."""
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

        # Sanitize open_positions — strip non-serializable values (datetime → str, drop atr)
        _safe_positions = {}
        for k, v in self.open_positions.items():
            _safe_positions[k] = {
                kk: (vv.isoformat() if hasattr(vv, "isoformat") else vv)
                for kk, vv in v.items() if kk != "atr"
            }

        summary = {
            "date":               date.today().isoformat(),
            "trades":             self.trades_today,
            "daily_pnl":          round(self.daily_pnl, 2),
            "odte_realized_pnl":  round(self.odte_realized_pnl, 2),
            "swing_realized_pnl": round(self.swing_realized_pnl, 2),
            "losing_streak":      self.losses_streak,
            "trade_log":          self.trade_log,
            "scan_history":       self.scan_history,
            "open_positions":     _safe_positions,
            "config": {
                "conviction_threshold_normal":     CONVICTION_THRESHOLD_NORMAL,
                "conviction_threshold_power_hour": CONVICTION_THRESHOLD_POWER_HOUR,
                "fakeout_block_threshold":         FAKEOUT_BLOCK_THRESHOLD,
                "version":                         "v2",
            }
        }
        path = f"{LOG_DIR}/summary_{date.today()}.json"
        with open(path, "w") as f:
            json.dump(summary, f, indent=2, default=_serial)
        log.info(f"  📊 Summary saved → {path}")

# ── Main engine ───────────────────────────────────────────────────────────────


# ── Stale Score Watchdog ──────────────────────────────────────────────────────
_score_history:  dict[str, list] = {}
_stale_alerted:  dict[str, bool] = {}

def _check_stale_scores(ticker: str, score: float, webhook_url: str = "") -> None:
    """Alert Discord if conviction score unchanged for 5+ consecutive scans."""
    hist = _score_history.setdefault(ticker, [])
    hist.append(round(score, 2))
    if len(hist) > 8: hist.pop(0)
    if len(hist) >= 5 and len(set(hist[-5:])) == 1:
        if not _stale_alerted.get(ticker):
            _stale_alerted[ticker] = True
            msg = (f"⚠️ **ARKA STALE SCORE — {ticker}**\n"
                   f"Score **{score:.1f}** frozen for **{len(hist)} consecutive scans**\n"
                   f"Likely: `fetch_bars` returning same bar or Polygon timeout\n"
                   f"**Action:** Restart ARKA engine if market is open.")
            try:
                import requests as _req, datetime as _dt
                _wh = "https://discord.com/api/webhooks/1480607582795071580/iZ3jGtWuTnkR782tRHsbldMBPRz0gpQZnsUZdnfb0CVvWS7-5S4ny8WklI-ZEcDRQiSH"
                _now = _dt.datetime.now().strftime("%H:%M ET")
                _payload = {
                    "content": "@here 🚨 **ARKA ENGINE HEALTH ALERT**",
                    "embeds": [{
                        "title": f"🔴 STALE CONVICTION SCORE — {ticker}",
                        "description": (
                            f"Score **{score:.1f}** has been **frozen for {len(hist)} consecutive scans** (~{len(hist)} min)\n\n"
                            f"**Likely Causes:**\n"
                            f"• `fetch_bars` returning cached/stale Polygon data\n"
                            f"• Polygon API timeout or rate limit\n"
                            f"• Market session detection misfiring\n\n"
                            f"**Impact:** ARKA cannot detect new entries or exits accurately\n\n"
                            f"**Action Required:** Restart ARKA engine via dashboard or `TARAKA restart ARKA`"
                        ),
                        "color": 0xff0000,
                        "fields": [
                            {"name": "Ticker",       "value": ticker,             "inline": True},
                            {"name": "Frozen Score", "value": f"{score:.1f}",  "inline": True},
                            {"name": "Scans Frozen", "value": str(len(hist)),    "inline": True},
                            {"name": "Time",         "value": _now,              "inline": True},
                        ],
                        "footer": {"text": "CHAKRA Health Monitor · ARKA Engine Watchdog"}
                    }]
                }
                _req.post(_wh, json=_payload, timeout=5)
                log.warning(f"  ⚠️ STALE SCORE ALERT sent for {ticker} (score={score:.1f} x{len(hist)})")
            except Exception as _e:
                log.warning(f"  ⚠️ Stale alert failed: {_e}")
    else:
        _stale_alerted[ticker] = False


class ARKAEngine:
    def __init__(self):
        self.alpaca       = AlpacaClient()
        self.state        = DayState()
        self.fakeout      = load_fakeout_models()
        self.scalp_model  = load_arka_scalp_model()
        self.last_scan_date = None
        # In-flight entry guard: set of underlying tickers currently being entered.
        # Prevents race condition where two concurrent scan tasks both pass the
        # Alpaca position check before either order has settled (e.g. 9:35 double-fire).
        self._entering: set = set()
        # VIX spike pause — epoch timestamp until which new entries are blocked
        self.vix_pause_until: float = 0.0
        # SPY prev-close cache for correlation gate
        self._spy_prev_close: float = 0.0
        self._spy_prev_close_date: str = ""

    async def check_daily_reset(self):
        today = date.today()
        if self.last_scan_date != today:
            log.info(f"\n{'='*50}")
            log.info(f"  ARKA v2 — New trading day: {today}")
            log.info(f"  Thresholds: conviction≥{CONVICTION_THRESHOLD_NORMAL} | fakeout<{FAKEOUT_BLOCK_THRESHOLD}")
            log.info(f"{'='*50}")
            self.state.reset()
            self.last_scan_date = today
            acct = await self.alpaca.get_account()
            self.state.starting_equity = float(acct.get("equity", 0))
            log.info(f"  Starting equity: ${self.state.starting_equity:,.2f}")

            # ── Restore open positions from today's summary (survive restarts) ──
            import json as _rj, pathlib as _rp
            _summary_path = _rp.Path(f"logs/arka/summary_{today}.json")
            if _summary_path.exists():
                try:
                    _sd = _rj.loads(_summary_path.read_text())
                    _op = _sd.get("open_positions", {})
                    _tlog = _sd.get("trade_log", [])
                    if _op:
                        self.state.open_positions = _op
                        self.state.trade_log      = _tlog
                        _realized = sum(float(t.get("pnl", 0) or 0) for t in _tlog if t.get("pnl"))
                        self.state.daily_pnl = _realized if _realized != 0 else float(_sd.get("daily_pnl", 0))
                        self.state.trades_today   = int(_sd.get("trades", 0))
                        _restore_buckets(_tlog, self.state)
                        log.info(f"  ♻️  Restored {len(_op)} open position(s): {list(_op.keys())}")
                        log.info(f"  ♻️  Budgets — 0DTE realized: {self.state.odte_realized_pnl:+.2f} | "
                                 f"Swing realized: {self.state.swing_realized_pnl:+.2f}")
                    else:
                        # Restore trade log + PnL even if no open positions
                        self.state.trade_log  = _tlog
                        self.state.daily_pnl  = float(_sd.get("daily_pnl", 0))
                        self.state.trades_today = int(_sd.get("trades", 0))
                        _restore_buckets(_tlog, self.state)

                    # ── CRITICAL: Rebuild entries_today from trade_log so
                    # restarts don't reset per-ticker entry counts mid-session ──
                    for _tr in _tlog:
                        if _tr.get("side") in ("BUY", "SHORT"):
                            _tk = _tr.get("ticker", "")
                            if _tk:
                                self.state.entries_today[_tk] = \
                                    self.state.entries_today.get(_tk, 0) + 1

                    # ── Rebuild large_loss_tickers from trade_log ──
                    # We can't fully reconstruct direction per trade without more data,
                    # but we flag any ticker with a single loss >= LARGE_LOSS_THRESHOLD.
                    _seen_losses: dict = {}
                    for _tr in _tlog:
                        _pnl = _tr.get("pnl")
                        if _pnl is not None and float(_pnl) <= -LARGE_LOSS_THRESHOLD:
                            _tk = _tr.get("ticker", "")
                            if _tk and (_tk not in _seen_losses or float(_pnl) < _seen_losses[_tk].get("pnl", 0)):
                                _seen_losses[_tk] = {
                                    "pnl":       float(_pnl),
                                    "direction": "CALL",  # conservative default (unknown from log)
                                    "time":      _tr.get("time", "?"),
                                }
                    if not hasattr(self.state, 'large_loss_tickers'):
                        self.state.large_loss_tickers = {}
                    self.state.large_loss_tickers.update(_seen_losses)
                    if _seen_losses:
                        log.info(f"  ♻️  Large-loss flags restored: {list(_seen_losses.keys())}")

                    # ── Rebuild per-ticker daily P&L for TICKER_DAILY_LOSS_CAP ──
                    _ticker_pnl: dict = {}
                    for _tr in _tlog:
                        _pnl = _tr.get("pnl")
                        if _pnl is not None:
                            _tk = _tr.get("ticker", "")
                            if _tk:
                                _ticker_pnl[_tk] = _ticker_pnl.get(_tk, 0) + float(_pnl)
                    self.state.ticker_daily_pnl = _ticker_pnl

                    log.info(f"  ♻️  Entries restored: {dict(self.state.entries_today)}")
                    _losers = {k: v for k, v in _ticker_pnl.items() if v < -50}
                    if _losers:
                        log.info(f"  ♻️  Ticker P&L (losers): {_losers}")

                except Exception as _re:
                    log.warning(f"  ⚠️  Could not restore state: {_re}")

            # Check no-trade-days trigger at start of each new day
            self.state._maybe_self_correct()
            # Reconcile with Alpaca — remove positions closed manually
            await self._reconcile_with_alpaca()
            # ── Startup orphan sweep — close any expired 0DTE options from prior sessions ──
            await self._close_orphaned_options()

    async def _close_orphaned_options(self):
        """
        At startup: scan Alpaca for any options positions with YESTERDAY's or earlier
        expiry date (expired 0DTE that EOD close missed). Close them immediately before
        trading begins to prevent them hitting stop-loss or expiring worthless.
        """
        try:
            import re as _re_o
            from datetime import date as _dt_o
            positions = await self.alpaca.get_positions()
            _today = _dt_o.today()
            _closed = []
            for _p in positions:
                _sym = _p.get("symbol", "")
                _m   = _re_o.search(r'(\d{2})(\d{2})(\d{2})[CP]', _sym)
                if not _m:
                    continue
                _exp = _dt_o(2000 + int(_m.group(1)), int(_m.group(2)), int(_m.group(3)))
                if _exp < _today:
                    log.warning(
                        f"  ⚠️  ORPHAN DETECTED: {_sym} expired {_exp} — closing immediately"
                    )
                    await self.alpaca.close_position(_sym, "orphan_close_startup")
                    _closed.append(_sym)
            if _closed:
                log.warning(f"  🧹 Closed {len(_closed)} orphaned expired position(s): {_closed}")
            else:
                log.info("  ✅ Startup orphan sweep: no expired positions found")
        except Exception as _oe:
            log.warning(f"  Orphan sweep error: {_oe}")

    async def _reconcile_with_alpaca(self):
        """Remove any ARKA state positions that no longer exist in Alpaca.
        State keys are underlying tickers (QQQ), Alpaca has full OCC symbols (QQQ260424C00660000).
        Match by checking if any Alpaca symbol starts with the underlying ticker.
        """
        try:
            alpaca_positions = await self.alpaca.get_positions()
            alpaca_syms = {p.get("symbol","") for p in alpaca_positions}
            # For each state ticker, check if any Alpaca position belongs to it
            def _has_alpaca_position(underlying: str) -> bool:
                # Direct match (equity) OR options contract starting with underlying
                if underlying in alpaca_syms:
                    return True
                # Check stored contract_sym in the position dict
                pos_data = self.state.open_positions.get(underlying, {})
                contract = pos_data.get("contract_sym") or pos_data.get("trade_sym", "")
                if contract and contract in alpaca_syms:
                    return True
                # Fallback: any Alpaca symbol that starts with the underlying ticker
                return any(s.startswith(underlying) for s in alpaca_syms)

            stale = [t for t in list(self.state.open_positions.keys())
                     if not _has_alpaca_position(t)]
            for t in stale:
                log.info(f"  🔄 RECONCILE: {t} not in Alpaca — removing from state (manually closed?)")
                self.state.open_positions.pop(t, None)
                self.state.position_peaks.pop(t, None)
            if stale:
                log.info(f"  ✅ Reconciled {len(stale)} stale position(s): {stale}")
        except Exception as e:
            log.warning(f"  Reconcile error: {e}")

    async def check_stops_and_targets(self):
        """
        Monitor ALL open Alpaca options positions — scalp AND swing.
        Exit rules (based on option premium P&L):
          +40% → take profit (full exit, 2:1 R:R)
          -20% → stop loss
          3:58 PM ET → EOD hard close (handled by close_all_positions call in scan loop)
        """
        positions = await self.alpaca.get_positions()

        # Build reverse map: contract_sym → underlying key (for state cleanup)
        contract_map: dict[str, str] = {}
        for tk, ref in self.state.open_positions.items():
            cs = ref.get("contract_sym")
            if cs:
                contract_map[cs] = tk

        options_found = [p for p in positions if p.get("asset_class") == "us_option"]
        if not options_found:
            return

        import re as _re_mon
        for p in options_found:
            contract_sym = p.get("symbol", "")
            qty          = int(float(p.get("qty", 0)))
            opt_entry    = float(p.get("avg_entry_price", 0) or 0)
            opt_price    = float(p.get("current_price", 0) or 0)

            if opt_entry <= 0 or qty <= 0:
                continue

            # Derive underlying ticker from contract symbol (e.g. QQQ260407P00573000 → QQQ)
            m = _re_mon.match(r'^([A-Z]{1,6})\d{6}[CP]\d+$', contract_sym)
            underlying = m.group(1) if m else contract_sym

            # Use raw price calculation — more reliable than Alpaca's unrealized_plpc field
            pnl_pct = (opt_price - opt_entry) / opt_entry * 100 if opt_entry > 0 else 0

            # Update peak (high-water mark on option premium)
            peak = self.state.position_peaks.get(contract_sym, opt_entry)
            if opt_price > peak:
                peak = opt_price
                self.state.position_peaks[contract_sym] = peak
            peak_pct = (peak - opt_entry) / opt_entry * 100 if opt_entry > 0 else 0

            # Bucket = ticker type: stocks → swing budget, indexes → 0DTE budget
            # (Matches the enter_trade budget gate — ticker drives the pool, not expiry)
            _bucket = "swing" if is_stock(underlying) else "odte"

            # ── Exit rules — Epoch 3 (2:1 R:R baseline) ─────────────────
            # Math: at 2:1 R:R you only need 33% win rate to be profitable.
            # Previous -30%/-10% needed 75% win rate — impossible.
            # Previous -20%/+25% needed 44% win rate — hard.
            # New -20%/+40% needs 33% win rate — achievable with quality signals.
            #
            # Tier 1 exit: +40% full close   (hard profit)
            # Tier 2 (trail): activate at +50%, trail at 60% of peak — lets winners run
            # Stop: -20% firm for both buckets
            _sl_pct = -20.0
            _tp_pct =  40.0  # 2:1 R:R vs -20% stop

            log.info(
                f"  📊 {contract_sym}: entry=${opt_entry:.4f} "
                f"now=${opt_price:.4f} pnl={pnl_pct:+.1f}% peak={peak_pct:+.1f}% | "
                f"TP=+{_tp_pct:.0f}% SL={_sl_pct:.0f}%"
            )

            # Helper: close + record + notify + clean up state
            async def _exit_position(reason: str, pnl_p: float = pnl_pct,
                                     o_price: float = opt_price,
                                     c_sym: str = contract_sym,
                                     ulay: str = underlying,
                                     q: int = qty,
                                     o_entry: float = opt_entry,
                                     bkt: str = _bucket):
                await self.alpaca.close_position(c_sym, reason)
                pnl_d = (o_price - o_entry) * q * 100
                self.state.record_trade(ulay, reason.upper()[:4], o_price, q, pnl_d, bucket=bkt)
                _skey = contract_map.get(c_sym, ulay)
                _pos_ref = self.state.open_positions.get(_skey, {})
                _entry_ts = _pos_ref.get("entry_time")
                if isinstance(_entry_ts, str):
                    try:
                        _entry_ts = datetime.fromisoformat(_entry_ts)
                        if _entry_ts.tzinfo is None:
                            _entry_ts = _entry_ts.replace(tzinfo=ET)
                    except Exception:
                        _entry_ts = None
                _hold_min = int((datetime.now(ET) - (_entry_ts or datetime.now(ET))).total_seconds() / 60) if _entry_ts else 0
                self.state.open_positions.pop(_skey, None)
                self.state.position_peaks.pop(c_sym, None)
                if DISCORD_ENABLED:
                    await post_arka_exit(ulay, o_entry, o_price, q, reason)
                # ── Update ARKA learning table with exit outcome ──────────
                try:
                    _db_row = _pos_ref.get("arka_db_row")
                    if _db_row:
                        from backend.arjun.agents.performance_db import log_arka_trade_exit as _log_exit
                        _log_exit(
                            row_id      = _db_row,
                            exit_reason = reason,
                            pnl_dollars = pnl_d,
                            pnl_pct     = pnl_p,
                            hold_minutes= _hold_min,
                        )
                except Exception as _dbe:
                    pass
                try:
                    from backend.arjun.memory.signal_memory import update_outcome as _mem_upd
                    _mem_upd(ulay, str(date.today()), pnl_d, pnl_p, _hold_min)
                except Exception:
                    pass
                # ── Feedback loop — log outcome for weekly review ─────
                try:
                    from backend.arjun.feedback_writer import record_outcome as _rec_outcome
                    _is_call_cont = "C" in c_sym[len(ulay):]
                    _rec_outcome(
                        ticker       = ulay,
                        direction    = "CALL" if _is_call_cont else "PUT",
                        entry        = o_entry,
                        exit_price   = o_price,
                        pnl_pct      = pnl_p,
                        reason       = reason,
                        conviction   = _pos_ref.get("conviction", 0),
                        signals_used = _pos_ref.get("signals", []),
                    )
                except Exception:
                    pass

            # ── STOP LOSS: -20% stocks / -30% indexes ────────────────
            if pnl_pct <= _sl_pct:
                log.info(
                    f"  🛑 STOP HIT  {contract_sym}  "
                    f"entry=${opt_entry:.4f} now=${opt_price:.4f} pnl={pnl_pct:.1f}% "
                    f"(sl={_sl_pct:.0f}%)"
                )
                if hasattr(self.state, 'stop_cooldowns'):
                    _cd_mins = STOP_COOLDOWN_STOCK if is_stock(underlying) else STOP_COOLDOWN_INDEX
                    self.state.stop_cooldowns[underlying] = datetime.now(ET) + timedelta(minutes=_cd_mins)
                    log.info(f"  🕐 Stop cooldown set: {underlying} blocked for {_cd_mins}min")
                await _exit_position("stop_loss")
                # After a large stop loss, record direction so re-entry rules can be applied smartly.
                # We do NOT hard-block — if the ticker reverses with strong flow, that's the recovery trade.
                _stop_pnl_d = (opt_price - opt_entry) * qty * 100
                if abs(_stop_pnl_d) >= LARGE_LOSS_THRESHOLD:
                    # Derive direction from contract type (C = CALL, P = PUT)
                    _is_call_stop = "C" in contract_sym[len(underlying):]
                    _lost_dir = "CALL" if _is_call_stop else "PUT"
                    if not hasattr(self.state, 'large_loss_tickers'):
                        self.state.large_loss_tickers = {}
                    self.state.large_loss_tickers[underlying] = {
                        "pnl":       _stop_pnl_d,
                        "direction": _lost_dir,
                        "time":      datetime.now(ET).strftime("%H:%M"),
                    }
                    log.warning(
                        f"  ⚡ LARGE LOSS FLAG: {underlying} lost ${abs(_stop_pnl_d):.0f} on {_lost_dir} — "
                        f"re-entry allowed but threshold raised "
                        f"(reversal +{REVERSAL_THRESHOLD_ADJ} / same-dir +{SAME_DIR_THRESHOLD_ADJ})"
                    )

            # ── TAKE PROFIT: +15% stocks / +10% indexes ──────────────
            elif pnl_pct >= _tp_pct:
                # Check if we should use trailing stop instead of hard exit
                TRAIL_ACTIVATION_MULT = 1.25  # activate trail at +50% (1.25 × 40% target)
                if pnl_pct >= _tp_pct * TRAIL_ACTIVATION_MULT:
                    # Lock in with trailing stop instead of full exit
                    pos_ref = self.state.open_positions.get(
                        contract_map.get(contract_sym, underlying), {}
                    )
                    pos_ref["trailing_active"] = True
                    if pnl_pct > pos_ref.get("peak_pnl_pct", 0):
                        pos_ref["peak_pnl_pct"] = pnl_pct
                    log.info(
                        f"  🔒 TRAIL ACTIVATED  {contract_sym}  "
                        f"pnl={pnl_pct:.1f}% (>{_tp_pct*TRAIL_ACTIVATION_MULT:.0f}%) — "
                        f"trailing instead of hard exit"
                    )
                else:
                    log.info(
                        f"  🎯 TAKE PROFIT  {contract_sym}  "
                        f"entry=${opt_entry:.4f} now=${opt_price:.4f} "
                        f"pnl={pnl_pct:.1f}%  qty={qty}  (tp=+{_tp_pct:.0f}%)"
                    )
                    await _exit_position("take_profit")
                    log.info(f"  ✅ EXIT: Full close at +{pnl_pct:.1f}%")

            # ── TRAILING STOP AFTER PROFIT ────────────────────────────
            # If peak was >= TP threshold and price has since dropped 5% from peak,
            # re-check conviction and close if direction is no longer supported.
            elif peak_pct >= _tp_pct and (peak_pct - pnl_pct) >= 5.0:
                # Quick conviction check using cached flow/ARJUN signals (no bar fetch)
                flow_sig   = get_flow_signal(underlying)
                flow_bias  = flow_sig.get("bias", "NEUTRAL").upper()
                flow_conf  = int(flow_sig.get("confidence", 0))
                arjun_data = get_arjun_bias(underlying)
                arjun_sig  = arjun_data.get("signal", "HOLD").upper()

                # Determine trade direction from contract type
                is_call    = "C" in contract_sym[len(underlying):]
                trade_dir  = "BULLISH" if is_call else "BEARISH"

                flow_aligned  = (trade_dir == "BULLISH" and flow_bias in ("BULLISH", "STRONG_BULLISH")) or \
                                (trade_dir == "BEARISH" and flow_bias in ("BEARISH", "STRONG_BEARISH"))
                arjun_aligned = (trade_dir == "BULLISH" and arjun_sig == "BUY") or \
                                (trade_dir == "BEARISH" and arjun_sig == "SELL")
                still_strong  = flow_conf >= 60 and flow_aligned

                log.info(
                    f"  ⚠️  TRAIL CHECK {contract_sym}: "
                    f"peak={peak_pct:.1f}% now={pnl_pct:.1f}% "
                    f"flow={flow_bias}({flow_conf}) arjun={arjun_sig} "
                    f"aligned={flow_aligned} still_strong={still_strong}"
                )

                if still_strong and arjun_aligned:
                    log.info(f"  🔄 HOLDING {contract_sym}: conviction still strong, letting it run")
                else:
                    log.info(
                        f"  🎯 TRAIL EXIT {contract_sym}: "
                        f"peaked at {peak_pct:.1f}%, now {pnl_pct:.1f}%, "
                        f"conviction faded — exiting"
                    )
                    await _exit_position("trail_stop_after_profit")

            # ── TRAILING STOP LOCK (hard floor once activated) ────────
            _pos_state = self.state.open_positions.get(
                contract_map.get(contract_sym, underlying), {}
            )
            if _pos_state.get("trailing_active") and _pos_state.get("peak_pnl_pct", 0) > 0:
                TRAIL_LOCK_FRACTION = 0.60  # lock in 60% of peak gain (was 50%)
                _trail_floor = _pos_state["peak_pnl_pct"] * TRAIL_LOCK_FRACTION
                _trail_floor = max(_trail_floor, _tp_pct)
                if pnl_pct > _pos_state.get("peak_pnl_pct", 0):
                    _pos_state["peak_pnl_pct"] = pnl_pct
                if pnl_pct <= _trail_floor:
                    log.info(
                        f"  🔒 TRAILING STOP  {contract_sym}  "
                        f"peak={_pos_state['peak_pnl_pct']:.1f}% "
                        f"current={pnl_pct:.1f}% "
                        f"floor={_trail_floor:.1f}%"
                    )
                    await _exit_position("trailing_stop")


    async def position_monitor_loop(self):
        """
        Dedicated position monitor — runs every 15 seconds INDEPENDENTLY of the
        main scan loop so that quick TP/SL spikes are never missed.
        The main scan loop's check_stops_and_targets() call is kept as a backup.
        """
        MONITOR_INTERVAL = 15   # seconds between checks
        log.info("  👁  Position monitor loop started (every 15s)")
        while True:
            try:
                await asyncio.sleep(MONITOR_INTERVAL)
                if is_market_open():
                    await self.check_stops_and_targets()
            except asyncio.CancelledError:
                log.info("  👁  Position monitor loop cancelled")
                break
            except Exception as e:
                log.warning(f"  👁  Monitor loop error: {e}")

    async def _gex_refresh_loop(self):
        """
        Background loop: refresh GEX state files for all tickers in scan universe every 15 min.
        Keeps gex_latest_{ticker}.json fresh so regime change detection works for stocks.
        Without this, stock GEX files go stale after the first scan and regime flips go undetected.
        """
        _GEX_REFRESH_INTERVAL = 900  # 15 minutes
        await asyncio.sleep(60)  # stagger from startup
        while True:
            try:
                if not is_market_open():
                    await asyncio.sleep(_GEX_REFRESH_INTERVAL)
                    continue

                # Build current universe
                try:
                    from backend.arka.dynamic_universe import get_universe as _get_univ
                    _univ = _get_univ()
                except Exception:
                    _univ = list(TICKERS)

                from backend.arjun.agents.gex_calculator import get_gex_for_ticker, write_gex_state
                import pathlib as _pl

                refreshed, skipped = 0, 0
                for _tk in _univ:
                    try:
                        # Only refresh if file is older than 10 min (TTL threshold)
                        _gex_path = _pl.Path(f"logs/gex/gex_latest_{_tk.upper()}.json")
                        if _gex_path.exists():
                            _age = time.time() - _gex_path.stat().st_mtime
                            if _age < 600:   # fresh enough
                                skipped += 1
                                continue

                        # Fetch live spot price via Polygon
                        async with httpx.AsyncClient(timeout=6) as _hc:
                            _sr = await _hc.get(
                                f"{POLYGON_BASE}/v2/snapshot/locale/us/markets/stocks/tickers/{_tk}",
                                params={"apiKey": POLYGON_KEY}
                            )
                        _spot = float(_sr.json().get("ticker", {}).get("day", {}).get("c", 0) or 0)
                        if _spot <= 0:
                            continue

                        # Run GEX in thread executor (sync call)
                        loop = asyncio.get_event_loop()
                        _gex_result = await loop.run_in_executor(
                            None, get_gex_for_ticker, _tk, _spot
                        )
                        if _gex_result and _gex_result.get("regime") not in (None, "UNKNOWN"):
                            write_gex_state(_tk, _gex_result)
                            refreshed += 1
                        await asyncio.sleep(0.5)   # rate limit
                    except Exception as _te:
                        log.debug(f"  [GEX refresh] {_tk}: {_te}")

                if refreshed:
                    log.info(f"  📐 GEX refresh: {refreshed} updated, {skipped} fresh, {len(_univ)-refreshed-skipped} skipped")

            except asyncio.CancelledError:
                break
            except Exception as _e:
                log.debug(f"  [GEX refresh loop] {_e}")

            await asyncio.sleep(_GEX_REFRESH_INTERVAL)

    def _check_vix_spike(self) -> bool:
        """Returns True if VIX has spiked >15% intraday (PANIC regime)."""
        from pathlib import Path as _P
        path = _P("logs/internals/internals_latest.json")
        if not path.exists():
            return False
        try:
            d = json.loads(path.read_text())
            return d.get("vix_regime", "") == "PANIC"
        except Exception:
            return False

    async def _fetch_spy_change_pct(self) -> float:
        """Fetch SPY day-change % for correlation gate. Cached per calendar day."""
        today = date.today().isoformat()
        # Step 1 — fetch prev close once per day using /prev endpoint
        if self._spy_prev_close_date != today or self._spy_prev_close <= 0:
            try:
                url = f"{POLYGON_BASE}/v2/aggs/ticker/SPY/prev"
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(url, params={"adjusted": "true", "apiKey": POLYGON_KEY})
                    bars = r.json().get("results", [])
                    if bars:
                        self._spy_prev_close = float(bars[0]["c"])
                        self._spy_prev_close_date = today
                        log.debug(f"  📊 SPY prev close cached: ${self._spy_prev_close:.2f}")
            except Exception as _e:
                log.debug(f"  SPY prev-close fetch failed: {_e}")
        # Step 2 — fetch latest 1-min bar for current price
        if self._spy_prev_close > 0:
            try:
                url = f"{POLYGON_BASE}/v2/aggs/ticker/SPY/range/1/minute/{today}/{today}"
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(url, params={"adjusted": "true", "sort": "desc", "limit": 1, "apiKey": POLYGON_KEY})
                    bars = r.json().get("results", [])
                    if bars:
                        current = float(bars[0]["c"])
                        return (current - self._spy_prev_close) / self._spy_prev_close * 100
            except Exception as _e:
                log.debug(f"  SPY current-price fetch failed: {_e}")
        return 0.0

    def _get_heatseeker_boost(self, ticker: str, direction: str) -> int:
        """
        Return conviction boost from Heat Seeker bridge (no HTTP round-trip).
        Uses in-memory + file cache populated by auto_refresh_loop.
        Returns 0–25 conviction points.
        """
        try:
            from backend.arka.heat_seeker_bridge import get_hs_conviction_boost as _hs_boost
            hs = _hs_boost(ticker, direction)
            if hs["boost"] > 0:
                sweep_tag = " ⚡SWEEP" if hs["is_sweep"] else ""
                log.info(f"  🔥 {hs['reason']}{sweep_tag} → +{hs['boost']} conviction")
            return hs["boost"]
        except Exception:
            return 0

    async def scan_ticker(self, ticker: str) -> dict | None:
        df = await fetch_bars(ticker, minutes=120)
        if df is None or len(df) < 50:
            return None

        row  = build_live_features(df)
        row["timestamp"] = str(now_et())
        # Attach last 30 raw bars for pullback/retest/divergence detection
        _raw = df.tail(30)[["open","high","low","close","volume"]].rename(
            columns={"open":"o","high":"h","low":"l","close":"c","volume":"v"}
        ).to_dict("records")
        row["_raw_bars"] = _raw
        cv   = conviction_score(row, ticker)
        fk   = fakeout_prob(row, ticker, self.fakeout)
        # ── Stale score health check ──
        try:
            _check_stale_scores(ticker, cv["score"])
        except Exception:
            pass

        price = float(df.iloc[-1]["close"])
        atr_v = float(row["atr14"])

        # ── ARJUN Trade Request (from HS→ARJUN pipeline) ─────────────────────
        # ARJUN has already deliberated using Bull + Risk Manager agents.
        # If it says EXECUTE on this ticker, boost conviction significantly
        # and align direction. This runs before GEX so both adjustments stack.
        _arjun_req = self._check_arjun_trade_request()
        if _arjun_req and _arjun_req.get("ticker", "").upper() == ticker.upper():
            _arjun_boost = min(30, int(_arjun_req.get("confidence", 50) - 50))
            if _arjun_boost > 0:
                cv["score"] = min(100, cv["score"] + _arjun_boost)
                cv["reasons"].append(
                    f"[ARJUN-REQ +{_arjun_boost} conf={_arjun_req['confidence']:.0f}%]"
                )
                log.info(
                    f"  🤖 ARJUN TRADE REQUEST: {ticker} {_arjun_req['direction']} "
                    f"conf={_arjun_req['confidence']:.0f}% → +{_arjun_boost} conviction"
                )
                log.info(f"     Reason: {_arjun_req.get('key_reason','')}")
            # Override direction if ARJUN disagrees
            _req_dir = _arjun_req.get("direction", "")
            _arjun_arka_dir = "LONG" if _req_dir == "BULLISH" else "SHORT" if _req_dir == "BEARISH" else None
            if _arjun_arka_dir and cv["direction"] not in (_arjun_arka_dir, f"STRONG_{_arjun_arka_dir}"):
                log.info(
                    f"  🤖 ARJUN overriding direction: {cv['direction']} → {_arjun_arka_dir}"
                )
                cv["direction"]    = _arjun_arka_dir
                cv["should_trade"] = True
            # Respect ARJUN max_contracts cap
            if _arjun_req.get("max_contracts", 3) < 3:
                cv["_arjun_max_contracts"] = _arjun_req["max_contracts"]
                log.info(f"  🤖 ARJUN capping contracts at {_arjun_req['max_contracts']}")

        # ── GEX Gate — block/adjust based on gamma structure ─────────────────
        _gex_state   = None
        _regime_call = "NEUTRAL"
        _bias_ratio  = 1.0
        try:
            is_short      = cv["direction"] in ("SHORT", "STRONG_SHORT")
            gex_direction = "PUT" if is_short else "CALL"
            _gex_state    = load_gex_state(ticker)
            if _gex_state:
                _gex_state["live_spot"] = price   # override stale spot with live price
            gate_result   = gex_gate(gex_direction, cv["score"], _gex_state)
            _regime_call  = gate_result.get("regime_call", "NEUTRAL")
            _bias_ratio   = gate_result.get("bias_ratio", 1.0)

            if not gate_result["allow"]:
                if cv["score"] >= 90:
                    # High-conviction override: GEX says no, but signal is too strong to ignore.
                    # Trade with 1 contract + force 1DTE to reduce risk from the adverse wall/bias.
                    log.info(
                        f"  ⚡ GEX BLOCK OVERRIDE {ticker} {gex_direction} "
                        f"conviction={cv['score']:.1f}≥90 — 1 contract, force 1DTE "
                        f"| {gate_result['reason']}"
                    )
                    cv["gex_blocked"]          = True   # still flagged for Discord/logging
                    cv["gex_override"]         = True   # override mode active
                    cv["gex_override_max_qty"] = 1      # cap at 1 contract downstream
                    cv["force_1dte"]           = True   # bypass regime DTE logic downstream
                    cv["reasons"].append(f"[GEX OVERRIDE ≥90] {gate_result['reason']}")
                else:
                    log.info(f"  🚫 GEX GATE BLOCKED {ticker} {gex_direction}: {gate_result['reason']}")
                    cv["direction"]    = "FLAT"
                    cv["should_trade"] = False
                    cv["gex_blocked"]  = True
                    cv["reasons"].append(f"[GEX BLOCK] {gate_result['reason']}")
            else:
                old_score = cv["score"]
                cv["score"] = gate_result["conviction"]
                if cv["score"] != old_score:
                    log.info(
                        f"  📊 GEX adj {ticker}: {old_score:.1f}→{cv['score']:.1f} | "
                        f"{gate_result['reason']}"
                    )
                    cv["reasons"].append(f"[GEX] {gate_result['reason']}")
                    _is_short_dir = cv["direction"] in ("SHORT", "STRONG_SHORT")
                    _short_thr    = 100 - cv["threshold"]
                    _gex_below    = (cv["score"] > _short_thr if _is_short_dir else cv["score"] < cv["threshold"])
                    _gex_above    = (cv["score"] <= _short_thr if _is_short_dir else cv["score"] >= cv["threshold"])
                    if _gex_below and cv["should_trade"]:
                        # GEX penalty dragged score outside threshold — disable
                        cv["direction"]    = "FLAT"
                        cv["should_trade"] = False
                    elif _gex_above and not cv["should_trade"]:
                        # GEX boost pushed a near-threshold FLAT signal over the line
                        # Re-enable only if technicals had a clear directional lean
                        # (gex_direction is already CALL/PUT based on pre-GEX score direction)
                        cv["direction"]    = "LONG"  if gex_direction == "CALL" else "SHORT"
                        cv["should_trade"] = True
                        log.info(
                            f"  ✅ GEX RESCUE {ticker}: score {old_score:.1f}→{cv['score']:.1f} "
                            f"crossed threshold {cv['threshold']} — re-enabling signal"
                        )

            # ── 1SD Strike Filter — never buy far-OTM strikes ──────────────
            if _gex_state:
                if gex_direction == "CALL" and _gex_state.get("upper_1sd"):
                    cv["max_strike"] = _gex_state["upper_1sd"]
                    log.info(f"  📐 1SD call cap: ${_gex_state['upper_1sd']:.2f}")
                elif gex_direction == "PUT" and _gex_state.get("lower_1sd"):
                    cv["min_strike"] = _gex_state["lower_1sd"]
                    log.info(f"  📐 1SD put floor: ${_gex_state['lower_1sd']:.2f}")

            # ── Zero-gamma shift boost — regime in flux = opportunity ──────
            if _gex_state and cv["should_trade"]:
                _zg = check_zero_gamma_shift(ticker, _gex_state.get("zero_gamma", 0))
                if _zg["shifted"]:
                    if _zg["direction"] == "UP" and gex_direction == "CALL":
                        cv["score"] = min(100, cv["score"] + 8)
                        cv["reasons"].append(
                            f"🔄 Zero-γ shift UP {_zg['shift_pct']:.1f}% +8"
                        )
                        log.info(
                            f"  🔄 Zero gamma shifted UP {_zg['shift_pct']:.1f}% "
                            f"(${_zg['prev_zero']}→${_zg['current_zero']}) +8"
                        )
                    elif _zg["direction"] == "DOWN" and gex_direction == "PUT":
                        cv["score"] = min(100, cv["score"] + 8)
                        cv["reasons"].append(
                            f"🔄 Zero-γ shift DOWN {_zg['shift_pct']:.1f}% +8"
                        )
                        log.info(
                            f"  🔄 Zero gamma shifted DOWN {_zg['shift_pct']:.1f}% "
                            f"(${_zg['prev_zero']}→${_zg['current_zero']}) +8"
                        )

            # ── Regime change boost — explosive flip = high-momentum setup ──
            if _gex_state:
                try:
                    from backend.arka.gex_state import check_regime_change as _crc2
                    _flip = _crc2(ticker, _gex_state.get("regime", ""), _gex_state)
                    if _flip and _flip.get("changed"):
                        _new_r = _flip.get("new_regime", "")
                        _sev   = _flip.get("severity", "MILD")
                        _boost_map = {"STRONG": 20, "MODERATE": 12, "MILD": 6}
                        _flip_boost = _boost_map.get(_sev, 6)
                        # Only boost if signal direction aligns with regime
                        # NEGATIVE regime = momentum → boost aligned direction
                        # POSITIVE regime = mean-revert → only boost counter-trend
                        _aligned = (
                            (_new_r == "NEGATIVE_GAMMA") or  # explosive: both directions valid
                            (_new_r == "POSITIVE_GAMMA" and not cv["should_trade"])  # pin: contrarian
                        )
                        if _aligned and cv["should_trade"]:
                            cv["score"] = min(100, cv["score"] + _flip_boost)
                            cv["reasons"].append(
                                f"🔄 REGIME FLIP → {_flip['new_label']} +{_flip_boost}"
                            )
                            log.info(
                                f"  🔄 REGIME FLIP {ticker}: {_flip['old_label']} → "
                                f"{_flip['new_label']} ({_sev}) +{_flip_boost}"
                            )
                        # For POSITIVE→NEGATIVE (most explosive), force 0DTE preference
                        if (_flip.get("old_regime") == "POSITIVE_GAMMA" and
                                _new_r == "NEGATIVE_GAMMA" and cv["should_trade"]):
                            cv["prefer_0dte"] = True
                            log.info(f"  🔄 Regime flip → forcing 0DTE preference for {ticker}")
                        # ── Fire Discord alert for ALL regime changes (regardless of trade) ──
                        if DISCORD_ENABLED:
                            try:
                                asyncio.ensure_future(
                                    post_system_alert(
                                        f"GEX Regime Flip — {ticker}",
                                        f"**{_flip.get('old_label','?')} → {_flip.get('new_label','?')}** ({_sev})\n\n"
                                        f"{_flip.get('description','')}\n\n"
                                        f"**Regime call:** {_flip.get('regime_call','?')} | "
                                        f"**Dealer bias:** {_flip.get('dealer_bias','?')} | "
                                        f"**Net GEX:** ${_flip.get('net_gex_m',0):.0f}M\n"
                                        f"Call wall: ${_flip.get('call_wall',0):.0f} | "
                                        f"Put wall: ${_flip.get('put_wall',0):.0f} | "
                                        f"Spot: ${_flip.get('spot',0):.2f}",
                                        level="warning" if _sev == "STRONG" else "info",
                                    )
                                )
                            except Exception as _nd:
                                log.debug(f"  [Regime flip notify] {_nd}")
                except Exception as _re:
                    log.debug(f"  [Regime flip check] {_re}")

        except Exception as _ge:
            log.debug(f"  [GEX gate] {_ge}")

        # ── Heat Seeker boost (bridge cache — no HTTP call) ────────────────
        try:
            _hs_boost = self._get_heatseeker_boost(ticker, gex_direction)
            if _hs_boost > 0 and cv["should_trade"]:
                cv["score"] = min(100, cv["score"] + _hs_boost)
                cv["reasons"].append(f"[HS +{_hs_boost}]")
                # Confirmed sweep lowers entry threshold
                from backend.arka.heat_seeker_bridge import get_hs_conviction_boost as _hsb
                if _hsb(ticker, gex_direction).get("is_sweep"):
                    cv["threshold"] = max(40, cv["threshold"] - 5)
                    cv["reasons"].append("[HS SWEEP -5 thresh]")
        except Exception as _hse:
            log.debug(f"  [HS boost] {_hse}")

        # ── VWAP Surge boost — fast momentum confirmation ──────────────────
        try:
            _surge_bars = row.get("_raw_bars", [])
            _surge_vwap = float(row.get("vwap", 0))
            if _surge_bars and _surge_vwap and cv["should_trade"]:
                _surge = detect_vwap_surge(_surge_bars, _surge_vwap)
                if _surge["surge_up"] and cv["direction"] in ("LONG", "STRONG_LONG"):
                    cv["score"] = min(100, cv["score"] + 12)
                    cv["reasons"].append(
                        f"⚡ VWAP surge UP {_surge['move_3bar_pct']:.1f}% "
                        f"dev={_surge['deviation_pct']:.1f}% +12"
                    )
                    log.info(
                        f"  ⚡ VWAP surge UP {_surge['move_3bar_pct']:.1f}% "
                        f"in 3 bars, dev={_surge['deviation_pct']:.1f}% +12"
                    )
                elif _surge["surge_down"] and cv["direction"] in ("SHORT", "STRONG_SHORT"):
                    cv["score"] = min(100, cv["score"] + 12)
                    cv["reasons"].append(
                        f"⚡ VWAP surge DOWN {_surge['move_3bar_pct']:.1f}% "
                        f"dev={_surge['deviation_pct']:.1f}% +12"
                    )
                    log.info(
                        f"  ⚡ VWAP surge DOWN {_surge['move_3bar_pct']:.1f}% "
                        f"in 3 bars, dev={_surge['deviation_pct']:.1f}% +12"
                    )
        except Exception as _vse:
            log.debug(f"  [VWAP surge] {_vse}")

        # ── ARKA Scalp Model conviction filter ────────────────────────────
        # Uses the XGBoost model trained on historical arka_trades outcomes.
        # Penalises setups the model considers low-probability wins.
        # Applied AFTER all boosts so it's working on the finalised score.
        _scalp_win_prob = None
        _scalp_adj      = 0
        if cv["should_trade"] and self.scalp_model is not None:
            try:
                _flow_for_model = get_flow_signal(ticker)
                _t_ll           = getattr(self.state, "large_loss_tickers", {}).get(ticker, {})
                _was_post_loss  = 1 if _t_ll else 0
                _lost_dir       = _t_ll.get("direction", "") if _t_ll else ""
                _sig_call_put   = "PUT" if cv["direction"] in ("SHORT", "STRONG_SHORT") else "CALL"
                _is_reversal    = int(
                    _was_post_loss and _lost_dir and _lost_dir != _sig_call_put
                )
                _prior_pnl      = float(_t_ll.get("pnl", 0)) if _t_ll else 0.0

                _scalp_win_prob = arka_scalp_win_prob(
                    conviction    = cv["score"],
                    threshold     = cv["threshold"],
                    gex_state     = _gex_state,
                    regime_call   = _regime_call,
                    bias_ratio    = _bias_ratio,
                    flow          = _flow_for_model,
                    row           = row,
                    session       = cv["session"],
                    direction     = _sig_call_put,
                    was_post_loss = _was_post_loss,
                    is_reversal   = _is_reversal,
                    prior_loss_pnl= _prior_pnl,
                    model         = self.scalp_model,
                )
                if _scalp_win_prob is not None:
                    # Hard block: ML win prob < 40% → no trade, regardless of direction or score
                    # The score=0 (STRONG_SHORT) clamp prevents the soft penalty from ever firing
                    # on extreme bearish signals, so this hard gate is necessary.
                    if _scalp_win_prob < 0.40:
                        cv["should_trade"] = False
                        cv["direction"]    = "FLAT"
                        log.info(
                            f"  🚫 ML HARD BLOCK: {ticker} WIN={_scalp_win_prob:.0%} < 40% — FLAT regardless of conviction"
                        )
                    # For flow-driven signals (flo≥60), halve the ML penalty —
                    # institutional flow supersedes a model trained on limited data
                    _flow_driven = abs(cv.get("components", {}).get("flow_discord", 0)) >= 60
                    _ml_penalty_scale = 0.5 if _flow_driven else 1.0
                    if _scalp_win_prob < 0.35:
                        _scalp_adj = int(-15 * _ml_penalty_scale)
                        cv["reasons"].append(f"[ARKA-ML WARN {_scalp_win_prob:.0%} WIN] {_scalp_adj}")
                        log.info(
                            f"  🔴 ARKA scalp model: {ticker} WIN={_scalp_win_prob:.0%} "
                            f"— low confidence, conviction {_scalp_adj}"
                        )
                    elif _scalp_win_prob < 0.45:
                        _scalp_adj = int(-10 * _ml_penalty_scale)
                        cv["reasons"].append(f"[ARKA-ML {_scalp_win_prob:.0%} WIN] {_scalp_adj}")
                        log.info(
                            f"  🟡 ARKA scalp model: {ticker} WIN={_scalp_win_prob:.0%} "
                            f"— below average, conviction -10"
                        )
                    elif _scalp_win_prob >= 0.65:
                        _scalp_adj = +5
                        cv["reasons"].append(f"[ARKA-ML {_scalp_win_prob:.0%} WIN] +5")
                        log.info(
                            f"  🟢 ARKA scalp model: {ticker} WIN={_scalp_win_prob:.0%} "
                            f"— high confidence +5"
                        )
                    else:
                        log.info(
                            f"  ⚪ ARKA scalp model: {ticker} WIN={_scalp_win_prob:.0%} "
                            f"— neutral, no adjustment"
                        )
                    if _scalp_adj != 0:
                        cv["score"] = max(0, min(100, cv["score"] + _scalp_adj))
                        # Re-check threshold after adjustment (direction-aware)
                        _is_short_cv  = cv["direction"] in ("SHORT", "STRONG_SHORT")
                        _short_thr_cv = 100 - cv["threshold"]
                        _ml_below_thr = (cv["score"] > _short_thr_cv if _is_short_cv else cv["score"] < cv["threshold"])
                        if _ml_below_thr and cv["should_trade"]:
                            cv["direction"]    = "FLAT"
                            cv["should_trade"] = False
                            log.info(
                                f"  🚫 ARKA-ML penalty dropped {ticker} below threshold "
                                f"({'score ' + str(cv['score']) + ' > short_thr ' + str(_short_thr_cv) if _is_short_cv else 'score ' + str(cv['score']) + ' < thr ' + str(cv['threshold'])}) — FLAT"
                            )
            except Exception as _sme:
                log.debug(f"  [arka scalp model] {_sme}")

        # Read Neural Pulse + GEX for Discord enrichment
        _internals = {}
        try:
            import json as _j, os as _o
            _ip = "logs/internals/internals_latest.json"
            if _o.path.exists(_ip):
                with open(_ip) as _f: _internals = _j.load(_f)
        except: pass

        signal = {
            "ticker":          ticker,
            "price":           round(price, 2),
            "conviction":      cv["score"],
            "direction":       cv["direction"],
            "should_trade":    cv["should_trade"],
            "session":         cv["session"],
            "threshold":       cv["threshold"],
            "fakeout_prob":    round(fk, 3),
            "fakeout_blocked": fk >= FAKEOUT_BLOCK_THRESHOLD,
            "gex_blocked":          cv.get("gex_blocked", False),
            "gex_override":         cv.get("gex_override", False),
            "gex_override_max_qty": cv.get("gex_override_max_qty", None),
            "force_1dte":           cv.get("force_1dte", False),
            "prefer_0dte":          cv.get("prefer_0dte", False),
            "reasons":         cv["reasons"],
            "components":      cv["components"],
            "atr":             round(atr_v, 4),
            # ── GEX enrichment fields ──
            "regime_call":     _regime_call,
            "gex_bias_ratio":  _bias_ratio,
            # ── ARKA ML model fields ──
            "scalp_win_prob":  round(_scalp_win_prob, 3) if _scalp_win_prob is not None else None,
            "scalp_adj":       _scalp_adj,
            # ── Discord enrichment fields ──
            "neural_pulse":    _internals.get("neural_pulse", {}).get("score", 50),
            "gex_regime":      _internals.get("gex_regime", "UNKNOWN"),
            "uoa_detected":    _check_uoa(ticker),
            "sector_modifier": cv.get("components", {}).get("sector", 0),
            "rsi":             round(float(row.get("rsi14", 50)), 1),
            "macd":            "BULLISH" if row.get("macd_bullish", 0) else "BEARISH",
            "vwap_bias":       "ABOVE" if row.get("vwap_dist_pct", 0) > 0 else "BELOW",
            "orb_bias":        "LONG" if row.get("orb_high", 0) > 0 else "SHORT" if row.get("orb_low", 0) > 0 else "NEUTRAL",
            "arjun_bias":      cv.get("arjun_bias", {}),
        }
        return signal

    async def evaluate_entry(self, signal: dict) -> tuple[bool, str]:
        """Returns (should_enter, reason_if_blocked)"""
        t = signal["ticker"]

        # In-flight guard: if another coroutine is already mid-entry for this ticker,
        # block immediately before hitting Alpaca (race condition at market open / 60s boundary)
        if t in self._entering:
            return False, f"{t} entry already in-flight — skip duplicate"

        # Belt-and-suspenders: always re-verify conviction vs threshold regardless of
        # should_trade flag — catches any state inconsistency from GEX/HS boost sequences
        _conv     = signal["conviction"]
        _thr      = signal["threshold"]
        _is_short = signal.get("direction", "") in ("SHORT", "STRONG_SHORT")
        _short_thr = 100 - _thr
        _below_thr = (_conv > _short_thr if _is_short else _conv < _thr)
        if _below_thr:
            return False, f"conviction {_conv:.1f} {'> short_thr ' + str(_short_thr) if _is_short else '< threshold ' + str(_thr)}"

        if not signal["should_trade"]:
            return False, f"direction={signal['direction']} — no tradeable signal"

        if signal["fakeout_blocked"]:
            # Index tickers get a more lenient fakeout threshold; stocks use strict default
            _eff_fakeout_thr = min(0.65, FAKEOUT_BLOCK_THRESHOLD + 0.10) \
                if signal["ticker"] in set(INDEX_TICKERS) else FAKEOUT_BLOCK_THRESHOLD
            if signal["fakeout_prob"] < _eff_fakeout_thr:
                log.info(f"  ✅ INDEX FAKEOUT PASS — {signal['ticker']} {signal['fakeout_prob']:.2f} < {_eff_fakeout_thr:.2f} (index leniency)")
            else:
                # Override: if extreme options flow aligns with direction, bypass fakeout
                flow = get_flow_signal(signal["ticker"])
                flow_extreme = flow.get("is_extreme", False)
                flow_bias    = flow.get("bias", "NEUTRAL")
                sig_dir      = signal.get("direction", "")
                flow_aligns  = (flow_bias == "BULLISH" and "LONG" in sig_dir) or \
                               (flow_bias == "BEARISH" and "SHORT" in sig_dir)
                if flow_extreme and flow_aligns:
                    log.info(f"  ⚡ FAKEOUT OVERRIDE — extreme flow {flow_bias} aligns with {sig_dir}")
                else:
                    return False, f"fakeout prob {signal['fakeout_prob']:.2f} ≥ {_eff_fakeout_thr:.2f}"

        if self.state.stopped:
            return False, "daily loss limit hit"

        if self.state.is_paused():
            return False, "losing streak pause"

        # Hard daily trade cap
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return False, f"daily trade cap reached ({self.state.trades_today}/{MAX_TRADES_PER_DAY})"

        _open = self.state.open_positions
        _total = len(_open)
        _index_set = set(INDEX_TICKERS)
        _is_index = t in _index_set
        if _is_index:
            # Indexes always get their own reserved slots
            _open_indexes = sum(1 for sym in _open if sym in _index_set)
            if _open_indexes >= MAX_CONCURRENT_INDEX:
                return False, f"index slot full ({_open_indexes}/{MAX_CONCURRENT_INDEX})"
        else:
            # Stock positions limited to total cap minus reserved index slots
            _stock_cap = MAX_CONCURRENT - MAX_CONCURRENT_INDEX
            _open_stocks = _total - sum(1 for sym in _open if sym in _index_set)
            if _open_stocks >= _stock_cap:
                return False, f"stock positions full ({_open_stocks}/{_stock_cap})"
        if _total >= MAX_CONCURRENT:
            return False, f"max concurrent positions ({MAX_CONCURRENT})"

        if t in self.state.open_positions:
            return False, f"already in {t}"

        # Live Alpaca position check — catches contracts opened in previous sessions
        # that survived a restart (state dict may be stale).
        # Also enforces no opposing direction on same ticker (no simultaneous call+put).
        try:
            _live_positions = await self.alpaca.get_positions()
            import re as _re_ep
            _sig_is_call = signal.get("direction", "") in ("LONG", "STRONG_LONG")
            for _lp in _live_positions:
                _sym = _lp.get("symbol", "")
                _m   = _re_ep.match(r'^([A-Z]{1,6})\d{6}([CP])\d+$', _sym)
                if not _m:
                    continue
                _und, _opt_type = _m.group(1), _m.group(2)
                if _und != t:
                    continue
                # Same underlying already open in Alpaca
                _existing_is_call = (_opt_type == "C")
                if _existing_is_call != _sig_is_call:
                    return False, f"opposing contract {_sym} already open in Alpaca"
                else:
                    return False, f"contract {_sym} already open in Alpaca"
        except Exception as _lpe:
            log.debug(f"  [live position check] {_lpe}")

        if hasattr(self.state, 'stop_cooldowns'):
            cd = self.state.stop_cooldowns.get(t)
            if cd and datetime.now(ET) < cd:
                mins = int((cd - datetime.now(ET)).total_seconds()/60)
                return False, f"stop cooldown {mins}min on {t}"

        # Max entries per ticker per session
        _entries_today = self.state.entries_today.get(t, 0)
        if _entries_today >= MAX_ENTRIES_PER_TICKER:
            return False, f"max {MAX_ENTRIES_PER_TICKER} entries/day on {t} (already {_entries_today})"

        # ── Per-ticker cumulative loss cap — HARD block only at $300 total ──────
        _tkr_pnl = getattr(self.state, 'ticker_daily_pnl', {}).get(t, 0)
        if _tkr_pnl <= -TICKER_DAILY_LOSS_CAP:
            return False, f"{t} daily loss cap: ${abs(_tkr_pnl):.0f} lost today (hard cap=${TICKER_DAILY_LOSS_CAP})"

        # ── Smart re-entry after a large single loss on this ticker ─────────────
        # Don't block entirely — a reversal trade might be the recovery opportunity.
        # Instead, demand HIGHER conviction: +10 for reversal, +25 for same direction.
        _large_loss = getattr(self.state, 'large_loss_tickers', {}).get(t)
        if _large_loss:
            _lost_dir    = _large_loss.get("direction", "")        # "CALL" or "PUT"
            _sig_dir     = signal.get("direction", "")             # "LONG"/"STRONG_LONG" etc.
            _is_reversal = (
                (_lost_dir == "CALL" and "SHORT" in _sig_dir) or
                (_lost_dir == "PUT"  and "LONG"  in _sig_dir)
            )
            _adj = REVERSAL_THRESHOLD_ADJ if _is_reversal else SAME_DIR_THRESHOLD_ADJ
            _effective_thr = signal["threshold"] + _adj
            if signal["conviction"] < _effective_thr:
                _label = "reversal" if _is_reversal else "same-dir"
                return False, (
                    f"{t} post-loss {_label} gate: conv={signal['conviction']:.1f} < "
                    f"{_effective_thr} (base {signal['threshold']} + {_adj} adj)"
                )
            # Allow through — but log the elevated threshold passage
            _label = "REVERSAL" if _is_reversal else "SAME-DIR"
            log.info(
                f"  🔁 POST-LOSS {_label} RE-ENTRY {t}: "
                f"conv={signal['conviction']:.1f} cleared elevated threshold {_effective_thr} "
                f"(lost ${abs(_large_loss.get('pnl', 0)):.0f} on {_lost_dir} at {_large_loss.get('time','')})"
            )
            # Force 1 contract on recovery trades — protect against double-down disasters
            signal["_post_loss_size_cap"] = REVERSAL_MAX_CONTRACTS

        acct   = await self.alpaca.get_account()
        equity = float(acct.get("equity", 0))
        if self.state.starting_equity:
            drawdown = (self.state.starting_equity - equity) / self.state.starting_equity
            if drawdown >= MAX_DAILY_LOSS_PCT:
                log.warning(f"  🛑 DAILY LOSS LIMIT  drawdown={drawdown*100:.1f}%  — stopping ARKA")
                self.state.stopped = True
                return False, f"daily drawdown {drawdown*100:.1f}%"

        return True, "all checks passed"

    def _check_arjun_trade_request(self) -> dict:
        """
        Read ARJUN's trade_request written by the HS→ARJUN pipeline.
        Returns the request if fresh and EXECUTE, else {}.
        Requests expire after 10 minutes.
        """
        from pathlib import Path as _Path
        _path = _Path("logs/arjun/trade_request.json")
        if not _path.exists():
            return {}
        try:
            req = json.loads(_path.read_text())
        except Exception:
            return {}
        if time.time() > req.get("expires_at", 0):
            try:
                _path.unlink(missing_ok=True)
            except Exception:
                pass
            return {}
        if req.get("decision") != "EXECUTE":
            return {}
        if req.get("confidence", 0) < 65:
            return {}
        return req

    async def enter_trade(self, signal: dict):
        t    = signal["ticker"]
        # Mark this ticker as in-flight so concurrent scan cycles don't double-enter
        self._entering.add(t)
        try:
            await self._enter_trade_inner(signal)
        finally:
            self._entering.discard(t)

    async def _enter_trade_inner(self, signal: dict):
        t    = signal["ticker"]

        # ── Hard conviction=0 block — must fire before ANY Alpaca API call ──────
        _conv = signal.get("conviction")
        if _conv is None or float(_conv) <= 0:
            log.warning(f"  ⛔ HARD BLOCK: {t} conviction={_conv} — refusing to place order")
            return

        acct = await self.alpaca.get_account()
        bp   = float(acct.get("buying_power", 0))
        pos  = calc_position(bp, signal["price"], signal["session"], signal["atr"])

        if pos["qty"] < 1:
            log.info(f"  ⏸  {t}: position size too small")
            return

        # ── Budget gate: $4K 0DTE / $4K swing, reduced by realized losses ──────
        ODTE_BUDGET_CAP  = 2000.0
        SWING_BUDGET_CAP = 2000.0
        pos["qty"] = 2  # default; overridden below based on actual contract price

        # Determine bucket based on underlying ticker
        _is_swing_ticker = is_stock(t)
        _budget_cap    = SWING_BUDGET_CAP if _is_swing_ticker else ODTE_BUDGET_CAP
        _realized_pnl  = (self.state.swing_realized_pnl if _is_swing_ticker
                          else self.state.odte_realized_pnl)

        # Deduct cost basis of currently open positions in the same bucket
        # so we don't overcommit capital already deployed
        _open_committed = sum(
            float(p.get("entry", 0)) * int(p.get("qty", 1)) * 100
            for sym, p in self.state.open_positions.items()
            if (sym in STOCK_TICKERS) == _is_swing_ticker
        )
        _avail_budget  = _budget_cap + _realized_pnl - _open_committed

        # Rough early estimate of option premium (actual check happens after contract selection)
        # Stocks: ~3% of underlying for near-ATM swing options. Indexes: ~0.8% for 0DTE scalps.
        _est_pct   = 0.030 if _is_swing_ticker else 0.008
        est_cost   = round(signal["price"] * _est_pct * 100, 2)
        if _avail_budget <= 0:
            log.info(f"  ⛔ {t}: {'swing' if _is_swing_ticker else '0DTE'} budget exhausted "
                     f"(cap=${_budget_cap:.0f}, realized={_realized_pnl:+.0f}, "
                     f"committed=${_open_committed:.0f}) — skip")
            return
        if est_cost > _avail_budget:
            log.info(f"  ⛔ {t}: est cost ${est_cost:.0f} > available ${_avail_budget:.0f} "
                     f"({'swing' if _is_swing_ticker else '0DTE'} budget, "
                     f"committed=${_open_committed:.0f}) — skip")
            return
        SCALP_BUDGET = _avail_budget  # keep for downstream premium gate check

        # ── ARKA Scalper: fixed % stops (overrides ATR-based calc) ─────
        # LONG:  stop = -20% from entry | target = +40% (2:1 R:R)
        # SHORT: stop = +1.5% underlying | target = -3% underlying
        scalper_entry = signal["price"]
        is_short = signal["direction"] in ("SHORT", "STRONG_SHORT")
        if not is_short:
            pos["stop"]   = round(scalper_entry * 0.80, 2)   # -20% stop
            pos["target"] = round(scalper_entry * 1.40, 2)   # +40% target (2:1 R:R)

        log.info(f"\n  ✅ ENTRY  {t}  price={signal['price']}  "
                 f"qty={pos['qty']}  stop={pos['stop']}  target={pos['target']}")
        log.info(f"     conviction={signal['conviction']}  fakeout={signal['fakeout_prob']:.2f}  "
                 f"session={signal['session']}")
        log.info(f"     reasons: {', '.join(signal['reasons'])}")
        # Options: always trade on the underlying — direction handled by call/put
        trade_sym = t  # SPY, QQQ, SPX — puts for SHORT, calls for LONG

        # SPX: paper trading unsupported — log signal, skip Alpaca order
        if t == "SPX":
            log.info(f"  📊 SPX signal tracked (paper unsupported) — conviction={signal['conviction']}")
            self.state.open_positions[t] = {
                "entry":     signal["price"], "stop": pos["stop"],
                "target":    pos["target"],   "qty":  pos["qty"],
                "atr":       signal["atr"],   "direction": signal["direction"],
                "trade_sym": "SPX", "paper_only": True,
            }
            self.state.record_trade(t, "BUY", signal["price"], pos["qty"],
                                    bucket="odte")  # SPX always index/0DTE pool
            return

        # ── Find ATM options contract (0DTE or nearest expiry) ─────────
        contract_sym = None
        try:
            import httpx as _hx
            from datetime import date as _date, timedelta as _td
            _direction = "call" if not is_short else "put"
            _und_price = signal["price"]
            # Smart DTE: NEGATIVE_GAMMA → 0DTE (fast mover); POSITIVE_GAMMA → 1DTE (avoid pinning)
            try:
                _gex_st  = load_gex_state(t)
                _gex_reg = (_gex_st or {}).get("regime", "UNKNOWN")
            except Exception:
                _gex_reg = "UNKNOWN"
            # DTE by regime:
            #   NEGATIVE_GAMMA → 0DTE  (dealers amplify moves, fast momentum play)
            #   POSITIVE_GAMMA → 1DTE  (pinning regime, 0DTE decays against mean reversion)
            #   UNKNOWN        → 1DTE  (safer fallback — avoid theta burn when regime unclear)
            if _gex_reg == "NEGATIVE_GAMMA":
                _dte_days = 1   # 0DTE
                log.info(f"  📋 DTE: NEGATIVE_GAMMA → 0DTE (dealers amplify, fast move expected)")
            elif _gex_reg == "POSITIVE_GAMMA":
                _dte_days = 2   # 1DTE
                log.info(f"  📋 DTE: POSITIVE_GAMMA → 1DTE (pinning regime, avoid 0DTE decay)")
            else:
                _dte_days = 2   # 1DTE — UNKNOWN or LOW_VOL
                log.info(f"  📋 DTE: regime={_gex_reg} → 1DTE (unknown regime, safer fallback)")

            # ── Regime flip: POSITIVE→NEGATIVE = explosive, prefer 0DTE for fast move ──
            if signal.get("prefer_0dte"):
                _dte_days = 1   # 0DTE (1 = today's expiry via exp_max offset)
                log.info(f"  🔄 Regime flip preference: using 0DTE (explosive regime)")

            # ── GEX override: high-conviction trade against a block → always 1DTE ──
            # 1DTE gives an extra day for the move to play out away from the wall/bias.
            if signal.get("force_1dte"):
                _dte_days = 2
                log.info(f"  ⚡ GEX override: forcing 1DTE (high-conviction block override)")

            # ── 0DTE theta cutoff: after 2:30 PM ET, 0DTE loses ~50% remaining value to theta
            # Force 1DTE after 2:30 PM unless conviction is extreme (≥ 72)
            _now_et   = datetime.now(ET)
            _past_230 = (_now_et.hour == 14 and _now_et.minute >= 30) or _now_et.hour >= 15
            if _past_230 and _dte_days == 1:
                _conv_score = signal.get("conviction", 0)
                if _conv_score < 72:
                    _dte_days = 2  # bump to 1DTE — preserve value, less theta burn
                    log.info(f"  ⏰ 0DTE theta cutoff: after 2:30 PM → using 1DTE (conviction={_conv_score})")
                else:
                    log.info(f"  ⚡ 0DTE allowed post-2:30: conviction={_conv_score} ≥ 72")

            # Stocks always use 1DTE minimum:
            #   - 0DTE stock options have thin per-strike OI (vs SPY/QQQ with thousands)
            #   - Gives the trade a full day to develop
            #   - Avoids PDT same-day buy+sell on paper account
            if is_stock(trade_sym) and _dte_days < 2:
                _dte_days = 2
                log.info(f"  📋 Stock swing: forcing 1DTE (stocks need next-day expiry for liquidity)")

            # _dte_days=1 → 0DTE (today), _dte_days=2 → 1DTE (tomorrow)
            # Set min expiry to match intent so the "soonest first" sort
            # doesn't accidentally grab today's contracts when we want 1DTE.
            _exp_min = (_date.today() + _td(days=_dte_days - 1)).isoformat()
            _exp_max = (_date.today() + _td(days=_dte_days)).isoformat()
            log.info(f"  📋 DTE selection: regime={_gex_reg} → {'1DTE' if _dte_days==2 else '0DTE'} (exp {_exp_min}→{_exp_max})")

            # ── OTM strike selection: target 0.5–2.5% OTM for cheap premium + leverage ──
            # For CALLs: want strike ABOVE spot (OTM call)
            # For PUTs:  want strike BELOW spot (OTM put)
            # Sweet spot: ~0.35% OTM — near-money, still OTM, good leverage
            if _direction == "call":
                _otm_target  = _und_price * 1.0035  # 0.35% above spot
                _strike_lo   = str(round(_und_price * 1.000, 0))   # no ITM calls
                _strike_hi   = str(round(_und_price * 1.030, 0))   # max 3% OTM
            else:  # put
                _otm_target  = _und_price * 0.9965  # 0.35% below spot
                _strike_lo   = str(round(_und_price * 0.970, 0))   # max 3% OTM
                _strike_hi   = str(round(_und_price * 1.000, 0))   # no ITM puts

            _r = _hx.get(
                f"{ALPACA_BASE}/v2/options/contracts",
                headers=self.alpaca.headers,
                params={
                    "underlying_symbols":  trade_sym,
                    "type":                _direction,
                    "expiration_date_gte": _exp_min,
                    "expiration_date_lte": _exp_max,
                    "strike_price_gte":    _strike_lo,
                    "strike_price_lte":    _strike_hi,
                    "limit":               10,
                },
                timeout=8
            )
            _contracts = _r.json().get("option_contracts", [])
            if _contracts:
                # Sort by soonest expiry first, then closest to 1% OTM target
                _contracts.sort(key=lambda c: (
                    c.get("expiration_date",""),
                    abs(float(c.get("strike_price", 0)) - _otm_target)
                ))
                _best        = _contracts[0]
                _best_strike = float(_best.get("strike_price", 0))

                # Hard ITM block — never buy ITM options (too expensive, bad leverage)
                _itm_pct = (_und_price - _best_strike) / _und_price if _direction == "call" \
                           else (_best_strike - _und_price) / _und_price
                if _itm_pct > 0.001:   # >0.1% ITM = block
                    log.warning(f"  ⛔ ITM block: strike ${_best_strike:.2f} is {_itm_pct:.2%} ITM "
                                f"(spot ${_und_price:.2f}) — skipping expensive ITM contract")
                    return

                # OTM sanity check — don't go more than 3% OTM (too cheap = near-zero delta)
                _otm_pct = (_best_strike - _und_price) / _und_price if _direction == "call" \
                           else (_und_price - _best_strike) / _und_price
                if _otm_pct > 0.030:
                    log.warning(f"  ⛔ Far-OTM block: strike ${_best_strike:.2f} is {_otm_pct:.2%} OTM "
                                f"— too far out, near-zero delta")
                    return

                # ITM block — don't buy ITM options (overpaying for intrinsic)
                if _otm_pct < -0.001:
                    log.warning(f"  ⛔ ITM block: {_best.get('symbol','')} "
                                f"strike ${_best_strike:.2f} is {_otm_pct:.2%} ITM — skipping")
                    return

                contract_sym = _best.get("symbol","")
                log.info(f"  📋 OTM contract: {contract_sym} strike=${_best_strike:.2f} "
                         f"otm={_otm_pct:.2%} (target ${_otm_target:.2f})")

                # ── Greeks gate: validate delta 0.40–0.65, log IV ────────────
                try:
                    _pg_key = os.getenv("POLYGON_API_KEY", "")
                    _pg_r = _hx.get(
                        f"https://api.polygon.io/v3/snapshot/options/{trade_sym}/O:{contract_sym}",
                        params={"apiKey": _pg_key},
                        timeout=6,
                    )
                    _pg_data  = _pg_r.json().get("results", {})
                    _greeks   = _pg_data.get("greeks", {})
                    _raw_delta = _greeks.get("delta")
                    _iv        = float(_pg_data.get("implied_volatility", 0) or 0)

                    if _raw_delta is not None:
                        _delta = abs(float(_raw_delta))
                        # OTM 0DTE target delta: 0.20–0.45
                        # Below 0.20 = too far OTM (lottery, near-zero chance)
                        # Above 0.50 = drifted ITM (too expensive)
                        if _delta < 0.20 or _delta > 0.50:
                            log.warning(
                                f"  ⚠️ Greeks gate: delta={_delta:.2f} outside OTM range 0.20-0.50 — "
                                f"trying nearest in-range contract"
                            )
                            _in_range = [
                                c for c in _contracts
                                if 0.20 <= abs(float(
                                    _hx.get(
                                        f"https://api.polygon.io/v3/snapshot/options/{trade_sym}/O:{c.get('symbol','')}",
                                        params={"apiKey": _pg_key}, timeout=4
                                    ).json().get("results", {}).get("greeks", {}).get("delta", 0.35) or 0.35
                                )) <= 0.50
                            ]
                            if _in_range:
                                _in_range.sort(key=lambda c: abs(float(c.get("strike_price", 0)) - _otm_target))
                                contract_sym = _in_range[0].get("symbol", contract_sym)
                                log.info(f"  📋 Greeks-corrected OTM contract: {contract_sym}")
                        else:
                            log.info(f"  📊 Greeks: delta={_delta:.2f} iv={_iv:.1%} ✅ (OTM)")

                        # IV check: warn if IV > 80% annualized (expensive options)
                        if _iv > 0.80:
                            log.warning(
                                f"  ⚠️ High IV={_iv:.1%} — options expensive, reducing size signal"
                            )

                    # ── Liquidity gate: OI, live bid, max spread ──────────────
                    # OI threshold is ticker-aware:
                    #   Index ETFs (SPY/QQQ/IWM): OI ≥ 100 — these have dense chains
                    #   Stocks (AMZN/NVDA/AMD etc.): OI ≥ 25 — per-strike OI is naturally lower
                    # bid == 0: market maker not active on this contract
                    # spread > 30%: giving up >15% immediately on entry + exit
                    _lq_oi     = int(_pg_data.get("open_interest", 0) or 0)
                    _lq_bid    = float((_pg_data.get("last_quote") or {}).get("bid", 0) or 0)
                    _lq_ask    = float((_pg_data.get("last_quote") or {}).get("ask", 0) or 0)
                    _lq_spread = (_lq_ask - _lq_bid) / _lq_ask if _lq_ask > 0 else 1.0
                    _oi_min    = 25 if is_stock(trade_sym) else 100
                    if _lq_oi < _oi_min:
                        log.warning(
                            f"  ⛔ Liquidity gate: {contract_sym} OI={_lq_oi} < {_oi_min} — insufficient open interest, skip"
                        )
                        return
                    if _lq_bid <= 0:
                        log.warning(
                            f"  ⛔ Liquidity gate: {contract_sym} zero bid — market maker inactive, skip"
                        )
                        return
                    if _lq_spread > 0.30:
                        log.warning(
                            f"  ⛔ Liquidity gate: {contract_sym} spread={_lq_spread:.1%} > 30% "
                            f"(bid={_lq_bid:.2f} ask={_lq_ask:.2f}) — too wide, skip"
                        )
                        return
                    log.info(
                        f"  💧 Liquidity OK: OI={_lq_oi} bid={_lq_bid:.2f} "
                        f"ask={_lq_ask:.2f} spread={_lq_spread:.1%}"
                    )
                except Exception as _gre:
                    log.debug(f"  [Greeks gate] {_gre}")

            else:
                log.warning(f"  ⚠️ No options contract found for {trade_sym} — skipping trade")
                return

            # ── Real premium cost gate (after contract selected) ──────────
            # Fetch actual last price from Polygon to check real cost before ordering
            try:
                _pg_key2  = os.getenv("POLYGON_API_KEY", "")
                _snap2    = _hx.get(
                    f"https://api.polygon.io/v3/snapshot/options/{trade_sym}/O:{contract_sym}",
                    params={"apiKey": _pg_key2}, timeout=5
                ).json().get("results", {})
                _last_px  = float(_snap2.get("last_quote", {}).get("ask", 0)
                                  or _snap2.get("day", {}).get("c", 0)
                                  or _snap2.get("details", {}).get("last_price", 0) or 0)
                if _last_px <= 0:
                    # Cannot verify cost — abort. Never trade with unknown premium.
                    log.warning(f"  ⛔ Premium gate: Polygon returned $0 for {contract_sym} — aborting (unknown cost)")
                    return

                # ── Spread gate — skip if bid/ask spread > 15% ───────────────
                # Wide spread means you're already -7% the moment you fill.
                _bid_px = float(_snap2.get("last_quote", {}).get("bid", 0) or 0)
                if _bid_px > 0:
                    _spread_pct = (_last_px - _bid_px) / _last_px
                    if _spread_pct > 0.15:
                        log.warning(
                            f"  ⛔ Spread gate: {contract_sym} bid={_bid_px:.2f} ask={_last_px:.2f} "
                            f"spread={_spread_pct*100:.0f}% > 15% — skip"
                        )
                        return
                    log.info(f"  ✅ Spread OK: {_spread_pct*100:.0f}% (bid={_bid_px:.2f} ask={_last_px:.2f})")

                # ── Epoch 3 cost guardrails (2K budget per bucket) ────────────
                # MIN per-contract premium — filters near-zero lottery tickets
                # Index scalps: $0.25/share ($25/contract) — 0DTE OTM calls trade in this range
                # Stock swings: $0.50/share ($50/contract) — stocks need more premium room
                _MIN_PX = 0.25 if not is_stock(trade_sym) else 0.50
                if _last_px < _MIN_PX:
                    log.warning(
                        f"  ⛔ Min premium: {contract_sym} ${_last_px:.2f}/share "
                        f"< ${_MIN_PX:.2f} minimum — skip"
                    )
                    return

                # MAX per-contract premium — skip if contract is too expensive relative to stock price
                # Index 0DTE scalps: max $2.50/share (fixed — SPY/QQQ OTM premiums are predictable)
                # Stock swings:      max 4% of stock price — scales with the underlying
                #   AMZN $261 → max $10.44  |  NVDA $120 → max $4.80  |  AMD $100 → max $4.00
                _is_stock_entry  = is_stock(trade_sym)
                _MAX_PX_INDEX    = 2.50
                _MAX_PX_STOCK    = round(signal.get("price", 100) * 0.04, 2)  # 4% of stock price
                _max_px          = _MAX_PX_STOCK if _is_stock_entry else _MAX_PX_INDEX
                if _last_px > _max_px:
                    log.warning(
                        f"  ⛔ Cost gate: {contract_sym} ${_last_px:.2f}/share "
                        f"> max ${_max_px:.2f} (4% of ${signal.get('price',0):.2f}) — too expensive, skip"
                    )
                    return

                # MAX total trade spend:
                #   Index scalps: 25% of SCALP_BUDGET = $500 (cheap 0DTE OTM options)
                #   Stock swings: 50% of SWING_BUDGET_CAP = $1000 (higher-priced stock options)
                _real_cost = round(_last_px * 100, 2)
                if _is_stock_entry:
                    _MAX_TRADE_SPEND = round(SWING_BUDGET_CAP * 0.50, 0)  # $1000 for stocks
                else:
                    _MAX_TRADE_SPEND = round(SCALP_BUDGET * 0.25, 0)      # $500 for indexes

                # Hard budget check with actual premium (not estimate)
                _budget_pool = SWING_BUDGET_CAP if _is_stock_entry else SCALP_BUDGET
                if _real_cost > _budget_pool:
                    log.warning(f"  ⛔ Premium gate: actual cost ${_real_cost:.0f} "
                                f"(${_last_px:.2f}/share) > ${_budget_pool:.0f} available budget — skip")
                    return

                # ── Open print size cap: 1 contract only during first 45 min ───
                # First 45 min = widest spreads, most fakeouts — don't overcommit
                # Stock swings: always 1 contract (per CLAUDE.md hard rule)
                _now_et2    = datetime.now(ET)
                _open_print = (_now_et2.hour == 9 and _now_et2.minute >= 30) or \
                              (_now_et2.hour == 10 and _now_et2.minute < 15)
                _qty_cap    = 1 if (_is_stock_entry or _open_print) else 2

                # Qty sizing: start at cap contracts; step down if needed to stay inside trade cap
                if _real_cost * _qty_cap <= _MAX_TRADE_SPEND:
                    pos["qty"] = _qty_cap
                    if _open_print and _qty_cap == 1:
                        log.info(f"  🔒 Open print cap: 1 contract only (9:30–10:15 window)")
                elif _real_cost <= _MAX_TRADE_SPEND:
                    pos["qty"] = 1
                    log.info(f"  📉 2 contracts ${_real_cost*2:.0f} > trade cap ${_MAX_TRADE_SPEND:.0f} — 1 contract only")
                else:
                    log.warning(
                        f"  ⛔ Cost gate: 1 contract ${_real_cost:.0f} already > trade cap "
                        f"${_MAX_TRADE_SPEND:.0f} — skip"
                    )
                    return

                log.info(f"  💰 Premium check: ${_last_px:.2f}/share = ${_real_cost:.0f} → qty={pos['qty']} ✅")
            except Exception as _ce:
                log.debug(f"  [Premium gate] {_ce}")

        except Exception as _oe:
            log.error(f"  ❌ Options lookup failed: {_oe} — skipping trade")
            return

        # ── Post-loss size cap — recovery trades capped at 1 contract ────────
        if signal.get("_post_loss_size_cap"):
            _cap = int(signal["_post_loss_size_cap"])
            if pos["qty"] > _cap:
                log.info(
                    f"  📉 Post-loss size cap: reducing {pos['qty']}→{_cap} contract(s) "
                    f"(recovery trade, protecting downside)"
                )
                pos["qty"] = _cap

        # ── GEX override size cap — high-conviction block override: 1 contract only ──
        if signal.get("gex_override_max_qty") is not None:
            _gex_cap = int(signal["gex_override_max_qty"])
            if pos["qty"] > _gex_cap:
                log.info(
                    f"  ⚡ GEX override size cap: reducing {pos['qty']}→{_gex_cap} contract "
                    f"(trading against GEX block at conviction≥90)"
                )
                pos["qty"] = _gex_cap

        # ── Final options-only guard before order ─────────────────────────
        from backend.arka.order_guard import validate_options_order as _voo
        _ok, _why = _voo(contract_sym, pos["qty"], "buy")
        if not _ok:
            log.error(f"  🛡️  ENTER TRADE BLOCKED: {_why}")
            return

        result = await self.alpaca.place_order(
            contract_sym, pos["qty"], "buy",
            note=f"conviction={signal['conviction']} dir={signal['direction']} contracts={pos['qty']}"
        )
        # PDT guard: if entry was rejected due to day trading rules, alert and skip
        if not result.get("id") and "day trades" in str(result.get("message", "")).lower():
            log.error(f"  ⛔ PDT BLOCK ENTRY {contract_sym}: {result.get('message','')}")
            if DISCORD_ENABLED:
                try:
                    await post_system_alert(
                        "⛔ PDT BLOCK — Entry Rejected",
                        f"**{contract_sym}** entry was rejected — PDT rule (account equity < $25K).\n\n"
                        f"Signal: {signal.get('direction','')} {t} conviction={signal.get('conviction','')}\n\n"
                        f"No new day trades allowed until equity exceeds $25K or next trading day.",
                        level="error",
                    )
                except Exception as _pdte:
                    log.debug(f"  PDT entry discord alert failed: {_pdte}")
            return
        if result.get("id"):
            # Store actual filled premium from Alpaca; fall back to rough estimate only if fill not available
            _opt_entry = float(result.get("filled_avg_price") or result.get("limit_price") or 0) \
                         or pos.get("est_premium") or signal["price"]
            self.state.open_positions[t] = {
                "entry":         _opt_entry,       # options premium paid
                "underlying":    signal["price"],  # equity price at entry time
                "stop":          pos["stop"],
                "target":        pos["target"],
                "qty":           pos["qty"],
                "atr":           signal["atr"],
                "direction":     signal["direction"],
                "trade_sym":     trade_sym,
                "contract_sym":  contract_sym,
                "est_premium":   pos.get("est_premium", 0),
                "entry_time":    datetime.now(ET),
            }
            _entry_bucket = "swing" if is_stock(t) else "odte"
            self.state.record_trade(t, "BUY" if not is_short else "SHORT",
                                    signal["price"], pos["qty"], bucket=_entry_bucket,
                                    gex_override=signal.get("gex_override", False))
            # Track per-ticker entry count for daily cap
            self.state.entries_today[t] = self.state.entries_today.get(t, 0) + 1

            # ── Log to ARKA learning table for ARJUN retraining ────────────
            try:
                from backend.arjun.agents.performance_db import log_arka_trade_entry as _log_entry
                _gex_st    = _gex_state or {}
                _flow_sig  = get_flow_signal(t)
                _ind_cache = signal.get("_indicators", {})
                _vwap_v    = float(signal.get("vwap", 0) or 0)
                _price_v   = float(signal.get("price", 0) or 0)
                _large_loss_info = getattr(self.state, 'large_loss_tickers', {}).get(t, {})
                _arka_row_id = _log_entry(
                    ticker    = t,
                    direction = "PUT" if is_short else "CALL",
                    conviction= float(signal.get("conviction", 0)),
                    threshold = float(signal.get("threshold", 55)),
                    session   = signal.get("session", ""),
                    gex_state = _gex_st,
                    flow      = _flow_sig,
                    indicators= {
                        "rsi":          signal.get("components", {}).get("rsi", 50),
                        "vwap_above":   _price_v > _vwap_v if _vwap_v else None,
                        "volume_ratio": signal.get("components", {}).get("vol", 0),
                        "ema_aligned":  signal.get("components", {}).get("ema", 0) > 0,
                    },
                    large_loss_info = _large_loss_info,
                    gex_override    = signal.get("gex_override", False),
                )
                # Stash row_id on open position so exit can update it
                self.state.open_positions[t]["arka_db_row"] = _arka_row_id
            except Exception as _dbe:
                log.debug(f"  [ARKA DB entry] {_dbe}")
            # ── Discord alert ─────────────────────────────────────────────
            if DISCORD_ENABLED:
                await post_arka_entry(signal, pos)
                # ARJUN conviction alert when ARJUN confidence >= 70% and direction aligns
                try:
                    _arjun_raw = signal.get("arjun_bias", {}).get("raw", {}) or _arjun_cache.get(t, {})
                    _arjun_conf = float(_arjun_raw.get("confidence", 0))
                    _arjun_sig  = _arjun_raw.get("signal", "HOLD")
                    _trade_dir  = "CALL" if not is_short else "PUT"
                    _arjun_aligns = (_arjun_sig == "BUY" and not is_short) or \
                                    (_arjun_sig == "SELL" and is_short)
                    if _arjun_conf >= 70 and _arjun_aligns:
                        from backend.arjun.arjun_discord import post_trade_conviction as _post_arjun_conv
                        _post_arjun_conv(
                            signal=_arjun_raw,
                            arka_entry={
                                "direction":  _trade_dir,
                                "conviction": signal.get("conviction", 50),
                                "strike":     contract_sym,
                                "qty":        pos["qty"],
                            }
                        )
                        log.info(f"  🧠 ARJUN-ARKA alignment posted to Discord: {t} {_trade_dir} {_arjun_conf:.0f}%")
                except Exception as _ae:
                    log.debug(f"  [ARJUN Discord] {_ae}")
                asyncio.ensure_future(post_position_update(signal["ticker"], "ENTRY", {
                    "entry":      signal["price"],
                    "price":      signal["price"],
                    "stop":       pos.get("stop", 0),
                    "target":     pos.get("target", 0),
                    "qty":        pos["qty"],
                    "conviction": signal.get("conviction", 0),
                    "side":       "LONG",
                    "session":    signal.get("session", "NORMAL"),
                }))


    async def on_new_bar(self, bar: dict):
        """
        WebSocket callback — fires on every completed 1-minute bar.
        Wired into ARKAEngine so all existing logic (stops, targets,
        daily reset, GEX refresh) runs exactly as in REST mode.
        """
        if not is_market_open():
            return
        try:
            await self.check_daily_reset()
            await self.run_scan()
        except Exception as e:
            log.error(f"WebSocket bar handler error: {e}", exc_info=True)

    async def run_realtime(self):
        """
        Real-time entry point — uses Polygon WebSocket 1-second bars.
        Enabled when websocket_arka: true in backend/arjun/config.yaml.
        Falls back to REST mode automatically on disconnect.
        """
        log.info("⚡ ARKA starting in WebSocket mode (1-second bars)...")
        stream = PolygonStream(
            tickers=["SPY", "QQQ"],
            on_bar_callback=self.on_new_bar,
            bar_minutes=1,
        )
        try:
            await stream.run()
        except Exception as e:
            log.error(f"WebSocket stream failed: {e} — falling back to REST")
            await self.run()

    async def run_scan(self):
        now = now_et()
        log.info(f"\n─── Scan {now.strftime('%H:%M:%S')} ET ─────────────────────────────")

        # auto-close at 3:58pm — scalp/0DTE positions only, NEVER swing positions
        # Guard: only fire once per calendar day regardless of how many scans hit 3:58-3:59
        if now.hour == AUTO_CLOSE_AT[0] and now.minute >= AUTO_CLOSE_AT[1]:
            _eod_today = date.today().isoformat()
            if getattr(self, '_eod_close_ran_today', None) == _eod_today:
                return   # already ran — skip duplicate
            setattr(self, '_eod_close_ran_today', _eod_today)
            log.info("  ⏰ 3:58pm — closing scalp positions (preserving swings)")
            try:
                # Load swing tickers so we can skip them
                from backend.arka.arka_swings import get_open_positions as _get_swings
                _swing_tickers = {p["ticker"].upper() for p in _get_swings()}
                if _swing_tickers:
                    log.info(f"  🌀 Preserving {len(_swing_tickers)} swing position(s): {_swing_tickers}")
            except Exception as _se:
                _swing_tickers = set()
                log.warning(f"  ⚠️  Could not load swing positions: {_se}")

            try:
                _alpaca_positions = await self.alpaca.get_positions()
                _to_close = []
                _skipped  = []
                import re as _re
                from datetime import date as _dt_date
                for _ap in (_alpaca_positions or []):
                    _sym = _ap.get("symbol","")
                    _is_option = bool(_re.search(r'\d{6}[CP]\d', _sym))
                    _underlying = _re.match(r'^([A-Z]+)\d', _sym)
                    _under_sym  = _underlying.group(1) if _underlying else _sym

                    if _is_option:
                        # Only close 0DTE options (expiring today); preserve multi-day swings
                        _exp_m = _re.search(r'(\d{2})(\d{2})(\d{2})[CP]', _sym)
                        if _exp_m:
                            _exp_date = _dt_date(2000 + int(_exp_m.group(1)), int(_exp_m.group(2)), int(_exp_m.group(3)))
                            _is_0dte  = (_exp_date <= _dt_date.today())
                        else:
                            _is_0dte = True  # can't parse expiry → assume 0DTE, close to be safe
                        if _is_0dte:
                            _to_close.append(_sym)
                        else:
                            _skipped.append(_sym)
                    else:
                        # Equity: skip if it's a known swing
                        if _under_sym in _swing_tickers:
                            _skipped.append(_sym)
                        else:
                            _to_close.append(_sym)

                log.info(f"  📋 {len(_to_close)} to close, {len(_skipped)} swings preserved: {_skipped}")
                for _sym in _to_close:
                    await self.alpaca.close_position(_sym, "eod")
            except Exception as _ape:
                log.warning(f"  ⚠️  EOD position check failed: {_ape}")
                # Fallback: only close 0DTE options (never swing options or equity swings)
                try:
                    _all = await self.alpaca.get_positions()
                    import re as _re2
                    from datetime import date as _dt2
                    for _ap in (_all or []):
                        _sym = _ap.get("symbol","")
                        if _re2.search(r'\d{6}[CP]\d', _sym):
                            _em = _re2.search(r'(\d{2})(\d{2})(\d{2})[CP]', _sym)
                            if _em:
                                _ed = _dt2(2000+int(_em.group(1)), int(_em.group(2)), int(_em.group(3)))
                                if _ed > _dt2.today():
                                    continue  # swing options — preserve
                            await self.alpaca.close_position(_sym, "eod")
                except Exception:
                    pass
            self.state.open_positions.clear()
            self.state.save_summary()
            # ── Post daily summary to Discord ─────────────────
            if DISCORD_ENABLED:
                # Post deep EOD summary to CHAKRA Trades channel
                import json
                summary_path = f"logs/arka/summary_{date.today()}.json"
                try:
                    with open(summary_path) as f_s:
                        full_summary = json.load(f_s)
                except:
                    full_summary = {
                        "trades":    self.state.trades_today,
                        "daily_pnl": self.state.daily_pnl,
                        "trade_log": self.state.trade_log,
                        "scan_history": self.state.scan_history if hasattr(self.state, "scan_history") else [],
                        "config": {}
                    }
                await post_arka_eod_summary(full_summary)
                # Also post to main channel
                await post_arka_daily_summary({
                    "trades":    self.state.trades_today,
                    "daily_pnl": self.state.daily_pnl,
                    "trade_log": self.state.trade_log,
                })
                # Post trading journal to #arjun-alerts
                try:
                    from backend.arka.arka_discord_notifier import _post, _wh
                    import json as _jj
                    _tlog = self.state.trade_log or []
                    _wins  = [t for t in _tlog if (t.get("pnl") or 0) > 0]
                    _loss  = [t for t in _tlog if (t.get("pnl") or 0) < 0]
                    _total = sum(t.get("pnl", 0) for t in _tlog if t.get("pnl") is not None)
                    _wr    = round(len(_wins)/len(_tlog)*100) if _tlog else 0
                    _lines = []
                    for _t in _tlog:
                        _p = _t.get("pnl")
                        _icon = "✅" if (_p or 0) > 0 else ("❌" if (_p or 0) < 0 else "⏳")
                        _pstr = f"{'+' if (_p or 0) >= 0 else ''}${abs(_p or 0):.2f}" if _p is not None else "open"
                        _lines.append(f"{_icon} **{_t.get('ticker','?')}** {_t.get('side','?')} @${_t.get('price',0):.2f} → `{_pstr}`")
                    _color = 0x00E676 if _total >= 0 else 0xFF4444
                    _embed = {
                        "title": f"{'📈' if _total >= 0 else '📉'}  ARKA TRADING JOURNAL — {date.today().strftime('%B %d, %Y')}",
                        "color": _color,
                        "fields": [
                            {"name": "Trades", "value": str(len(_tlog)), "inline": True},
                            {"name": "Win Rate", "value": f"{_wr}%", "inline": True},
                            {"name": "Day P&L", "value": f"{'+'if _total>=0 else ''}${_total:,.2f}", "inline": True},
                            {"name": "Trade Log", "value": "\n".join(_lines) if _lines else "*No trades*", "inline": False},
                        ],
                        "footer": {"text": f"ARKA Engine · EOD {date.today().strftime('%b %d')}"},
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                    _post({"embeds": [_embed]})
                    log.info("  📓 Trading journal posted → #arjun-alerts")
                except Exception as _je:
                    log.warning(f"  Trading journal Discord failed: {_je}")
            return

        await self.check_stops_and_targets()

        # ── VIX Spike Abort ─────────────────────────────────────────────────────
        _now_ts = time.time()
        if _now_ts < self.vix_pause_until:
            _remaining = int((self.vix_pause_until - _now_ts) / 60)
            log.info(f"  ⏸ VIX PAUSE active — {_remaining}min remaining, skipping scan")
            return
        if self._check_vix_spike():
            self.vix_pause_until = _now_ts + (15 * 60)
            log.warning("  🚨 VIX SPIKE DETECTED — pausing all entries for 15 min")
            return

        # ── SPY change % for correlation gate ───────────────────────────────────
        _spy_chg_pct = await self._fetch_spy_change_pct()
        if abs(_spy_chg_pct) >= 0.4:
            log.info(f"  📊 SPY day change: {_spy_chg_pct:+.2f}% — correlation gate active")

        # ── Lotto Engine: runs during 3:00-3:57 PM power hour window ──
        # Runs BEFORE the flat-market gate — lotto relies on GEX wall rejection,
        # not intraday momentum, so it should fire even on flat days.
        _now_et_lotto = now_et()
        _lotto_h, _lotto_m = _now_et_lotto.hour, _now_et_lotto.minute
        if _lotto_h == 15 and 0 <= _lotto_m < 57:
            try:
                from backend.arka.lotto_engine import check_lotto_trigger
                import asyncio as _asyncio
                lotto_result = await _asyncio.get_event_loop().run_in_executor(
                    None, check_lotto_trigger
                )
                action = lotto_result.get("action", "WAIT")
                log.info(f"  🎰 Lotto: {action} — {lotto_result.get('reason', '')}")
            except Exception as _le:
                log.warning(f"  🎰 Lotto engine error: {_le}")

        # ── Flat-market gate: block 0DTE entries when SPY hasn't moved enough ──
        # 0DTE options require ~0.3%+ intraday move to overcome theta + bid-ask spread.
        # Truly flat days (<0.15% SPY move after 10am) are systematic losers for 0DTE.
        # Exception: bypass if flow signals show high-conviction institutional activity (≥80% conf).
        _now_et_flat = now_et()
        _past_open   = _now_et_flat.hour >= 10  # give market 30 min to find direction
        if _past_open and abs(_spy_chg_pct) < 0.15:
            # Check if flow signals override flat gate
            _flow_override = False
            try:
                _fs = get_flow_signal("SPY") or get_flow_signal("QQQ")
                if _fs and int(_fs.get("confidence", 0)) >= 80:
                    _flow_override = True
                    log.info(f"  ⚡ FLAT GATE BYPASSED: flow signal conf={_fs.get('confidence')}% overrides flat market")
            except Exception:
                pass
            if not _flow_override:
                log.info(
                    f"  🚫 FLAT MARKET GATE: SPY only {_spy_chg_pct:+.2f}% intraday — "
                    f"truly flat (<0.15%), no strong flow signal. Skipping scan."
                )
                return

        # ── Dynamic universe — refreshes every 5 min from ARJUN/flow/swings/movers ──
        try:
            from backend.arka.dynamic_universe import get_universe
            _scan_tickers = get_universe()
        except Exception as _ue:
            log.debug(f"Dynamic universe failed, using static: {_ue}")
            _scan_tickers = TICKERS

        # Load swing watchlist tickers — these bypass the index whitelist
        _swing_watchlist: set = set()
        try:
            import json as _wjson, pathlib as _wp
            _wf = _wp.Path("logs/chakra/watchlist_latest.json")
            if _wf.exists():
                _wd = _wjson.loads(_wf.read_text())
                _wcands = _wd if isinstance(_wd, list) else _wd.get("candidates", _wd.get("watchlist", []))
                _swing_watchlist = {c.get("ticker","").upper() for c in _wcands if c.get("ticker")}
        except Exception:
            pass

        for ticker in _scan_tickers:
            _t = ticker.upper()
            if _t not in ALLOWED_TICKERS and _t not in _swing_watchlist:
                log.debug(f"  ⏭ SKIP {ticker} — not in index whitelist or swing watchlist")
                continue

            signal = await self.scan_ticker(ticker)
            if signal is None:
                log.warning(f"  ⚠️  {ticker}: scan returned no signal — skipping")
                continue

            # ── ENHANCED diagnostic log line ──────────────────────────────────
            can_enter, block_reason = await self.evaluate_entry(signal)

            if can_enter:
                tag = "🟢 TRADE"
                decision = "TRADE"
            elif signal["session"] in ("LUNCH", "CLOSE", "PRE", "CLOSED"):
                tag = "🚫 SESSION"
                decision = f"BLOCKED:{signal['session']}"
            elif signal["fakeout_blocked"]:
                tag = "🚫 FAKEOUT"
                decision = "BLOCKED:FAKEOUT"
            elif signal.get("gex_blocked"):
                tag = "🚫 GEX"
                decision = "GEX_BLOCK"
            elif not signal["should_trade"]:
                tag = "⏸  FLAT"
                decision = "FLAT:LOW_CONV"
            else:
                tag = "⏸  BLOCKED"
                decision = f"BLOCKED:{block_reason}"

            # Compact score breakdown
            comp = signal.get("components", {})
            comp_str = " ".join([f"{k[:3]}={v:+.0f}" for k, v in comp.items()])

            _ml_str = (
                f"  ml={signal['scalp_win_prob']:.0%}({signal['scalp_adj']:+d})"
                if signal.get("scalp_win_prob") is not None else ""
            )
            log.info(
                f"  {tag}  {ticker:>4}  "
                f"${signal['price']:>8.2f}  "
                f"conv={signal['conviction']:>5.1f}/{signal['threshold']}  "
                f"fakeout={signal['fakeout_prob']:.2f}{_ml_str}  "
                f"sess={signal['session']:<10}"
            )
            log.info(
                f"         components: {comp_str}"
            )
            if signal["reasons"]:
                log.info(f"         signals: {', '.join(signal['reasons'])}")
            if not can_enter and decision not in ("FLAT:LOW_CONV",):
                log.info(f"         blocked: {block_reason}")

            # Record for dashboard
            self.state.record_scan(ticker, signal["conviction"], signal["fakeout_prob"], decision)

            if can_enter:
                # ── ARJUN conflict gate — never fight high-confidence ARJUN signal ──
                _sig_dir     = signal.get("direction", "")
                _is_long     = _sig_dir in ("LONG", "STRONG_LONG")
                _is_short    = _sig_dir in ("SHORT", "STRONG_SHORT")
                _arjun_entry = get_arjun_bias(ticker)
                _arj_sig     = _arjun_entry.get("signal", "HOLD")
                _arj_conf    = float(_arjun_entry.get("confidence", 0))
                _arjun_blocked = False
                if _arj_sig == "SELL" and _arj_conf >= 55 and _is_long:
                    _arjun_blocked = True
                    _arj_reason = f"ARJUN SELL {_arj_conf:.0f}% confidence conflicts with LONG entry"
                elif _arj_sig == "BUY" and _arj_conf >= 55 and _is_short:
                    _arjun_blocked = True
                    _arj_reason = f"ARJUN BUY {_arj_conf:.0f}% confidence conflicts with SHORT entry"

                if _arjun_blocked:
                    log.info(f"  🧠 ARJUN CONFLICT GATE: {ticker} {_sig_dir} blocked — {_arj_reason}")
                    self.state.record_scan(ticker, signal["conviction"], signal["fakeout_prob"], "BLOCKED:ARJUN_CONFLICT")
                    continue

                # ── Correlation gate — block trades fighting SPY trend ──────────
                _corr = get_index_correlation_gate(_sig_dir, _spy_chg_pct, ticker)
                if not _corr["allow"]:
                    log.info(f"  🚫 CORRELATION GATE: {ticker} {_sig_dir} — {_corr['reason']}")
                    self.state.record_scan(ticker, signal["conviction"], signal["fakeout_prob"], "BLOCKED:CORR_GATE")
                else:
                    await self.enter_trade(signal)

            await asyncio.sleep(1)

        # Save summary every scan so dashboard always has fresh data
        self.state.save_summary()

    async def _hs_open_scan(self):
        """Trigger a Heat Seeker scan right at market open (9:31 AM ET)."""
        try:
            async with httpx.AsyncClient(timeout=45) as _hsc:
                r = await _hsc.get("http://localhost:5001/api/heatseeker/scan?mode=scalp")
                if r.status_code == 200:
                    d     = r.json()
                    stats = d.get("scan_stats", {})
                    log.info(
                        f"  🔥 Open HS scan: {stats.get('total_signals', 0)} signals | "
                        f"{stats.get('sweeps_detected', 0)} sweeps | "
                        f"Top: {stats.get('top_ticker', '?')} score={stats.get('top_score', 0)}"
                    )
        except Exception as e:
            log.debug(f"  HS open scan failed: {e}")

    async def run(self):
        log.info("\n" + "="*50)
        log.info("  ARKA ENGINE v3 STARTING (self-correcting)")
        log.info(f"  Conviction threshold: {CONVICTION_THRESHOLD_NORMAL} (normal) / {CONVICTION_THRESHOLD_POWER_HOUR} (power hour)")
        log.info(f"  Fakeout block threshold: {FAKEOUT_BLOCK_THRESHOLD}")
        log.info("  SPY + QQQ  |  Alpaca Paper Trading")
        log.info("="*50)

        await self.check_daily_reset()

        # Start Heat Seeker background cache refresh
        try:
            from backend.arka.heat_seeker_bridge import auto_refresh_loop as _hs_loop
            asyncio.create_task(_hs_loop())
            log.info("  🔥 Heat Seeker bridge started")
        except Exception as _hse:
            log.warning(f"  HS bridge start failed: {_hse}")

        # Start dedicated position monitor (every 15s) — independent of scan loop
        asyncio.create_task(self.position_monitor_loop())

        # Start GEX refresh loop — keeps gex_latest_{ticker}.json fresh for all scanned tickers
        asyncio.create_task(self._gex_refresh_loop())
        log.info("  📐 GEX refresh loop started (every 15 min)")

        # Announce engine start to Discord
        if DISCORD_ENABLED:
            await post_system_alert(
                "ARKA Engine Started",
                f"⚡ ARKA is live — scanning SPY + QQQ\nConviction threshold: {CONVICTION_THRESHOLD_NORMAL} | Fakeout block: {FAKEOUT_BLOCK_THRESHOLD}\nPaper trading via Alpaca",
                level="success"
            )

        _hs_open_scanned     = False
        _hs_open_scanned_day = None

        while True:
            try:
                now = now_et()

                # Reset open-scan flag each new trading day
                if _hs_open_scanned_day != now.date():
                    _hs_open_scanned     = False
                    _hs_open_scanned_day = now.date()

                if not is_market_open():
                    next_open = "09:30 ET tomorrow" if now.hour >= 16 else "09:30 ET today"
                    log.info(f"  Market closed — next open: {next_open}")
                    # ── Safety net: if we just passed 4pm and still have 0DTE positions, close them ──
                    if now.hour >= 16 and not getattr(self, '_eod_safety_ran_today', None):
                        _today = now.date()
                        setattr(self, '_eod_safety_ran_today', _today)
                        log.info("  🛡️  EOD safety net — checking for unclosed 0DTE positions")
                        try:
                            import re as _re_eod
                            from datetime import date as _dt_eod
                            _positions = await self.alpaca.get_positions()
                            _expired = []
                            for _ap in (_positions or []):
                                _sym = _ap.get("symbol", "")
                                _m = _re_eod.search(r'(\d{2})(\d{2})(\d{2})[CP]', _sym)
                                if _m:
                                    _exp = _dt_eod(2000+int(_m.group(1)), int(_m.group(2)), int(_m.group(3)))
                                    if _exp <= _dt_eod.today():
                                        _expired.append(_sym)
                            if _expired:
                                log.warning(f"  ⚠️  EOD safety net found {len(_expired)} unclosed 0DTE: {_expired}")
                                for _sym in _expired:
                                    try:
                                        await self.alpaca.close_position(_sym, "eod_safety")
                                        log.info(f"  ✅ Safety closed: {_sym}")
                                    except Exception as _ce:
                                        log.error(f"  ❌ Safety close failed for {_sym}: {_ce}")
                                # Post Discord alert about missed close
                                if DISCORD_ENABLED:
                                    try:
                                        from backend.arka.arka_discord_notifier import _post
                                        _post({"content": f"⚠️ **EOD Safety Net** closed {len(_expired)} expired 0DTE position(s) that were missed at 3:58pm: `{'`, `'.join(_expired)}`"})
                                    except Exception:
                                        pass
                            else:
                                log.info("  ✅ EOD safety net: no unclosed 0DTE positions")
                        except Exception as _se:
                            log.error(f"  EOD safety net error: {_se}")
                    await asyncio.sleep(300)
                    continue

                # Auto Heat Seeker scan at 9:31 AM
                if now.hour == 9 and now.minute >= 31 and not _hs_open_scanned:
                    _hs_open_scanned = True
                    asyncio.create_task(self._hs_open_scan())

                await self.check_daily_reset()
                await self.run_scan()

            except Exception as e:
                log.error(f"  ❌ Scan error: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import yaml as _yaml
    _use_ws = False
    try:
        _raw = open("backend/arjun/config.yaml").read()
        for _k, _v in __import__("os").environ.items():
            _raw = _raw.replace(f"${{{_k}}}", _v)
        _cfg    = _yaml.safe_load(_raw)
        _use_ws = _cfg.get("features", {}).get("websocket_arka", False)
    except Exception:
        pass

    _run_fn = ARKAEngine().run_realtime if (_use_ws and WEBSOCKET_AVAILABLE) else ARKAEngine().run
    if _use_ws and WEBSOCKET_AVAILABLE:
        log.info("Starting ARKA in WebSocket mode (1-second bars)")
    else:
        log.info("Starting ARKA in REST polling mode (60-second scans)")

    while True:
        try:
            asyncio.run(_run_fn())
        except (KeyboardInterrupt, SystemExit):
            log.info("ARKA engine stopped.")
            break
        except Exception as _top_e:
            log.error(f"  ❌ Top-level crash: {_top_e} — restarting in 15s", exc_info=True)
            import time as _t; _t.sleep(15)
            _run_fn = ARKAEngine().run  # fresh engine on restart
