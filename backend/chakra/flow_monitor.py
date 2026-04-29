"""
CHAKRA — Flow Monitor v2
backend/chakra/flow_monitor.py

Scans dark pool prints + unusual options activity every 5 minutes.
Posts tiered Discord alerts to #alerts with call/put recommendation.

Changes from v1:
- Fixed dark pool detection — uses aggregate TRF volume not per-trade size
- Lowered thresholds to match real Polygon data
- Added SPY/QQQ aggregate dark pool % calculation

Cron (every 5 min during market hours):
  */5 9-16 * * 1-5 cd $HOME/trading-ai && venv/bin/python3 backend/chakra/flow_monitor.py >> logs/chakra/flow_monitor.log 2>&1

Usage:
  python3 backend/chakra/flow_monitor.py         # run once
  python3 backend/chakra/flow_monitor.py --watch # run every 5 min
  python3 backend/chakra/flow_monitor.py --test  # mock Discord alerts
"""

import os, sys, json, time, logging, requests, httpx
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [FLOW] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('flow_monitor')

# ── Config ──────────────────────────────────────────────────────────────
POLYGON_KEY    = os.getenv("POLYGON_API_KEY", "")
DISCORD_ALERTS        = os.getenv("DISCORD_ALERTS",              os.getenv("DISCORD_WEBHOOK_URL", ""))
DISCORD_SPX_HOOK      = os.getenv("DISCORD_SPX_WEBHOOK", "")
DISCORD_HEALTH        = os.getenv("DISCORD_APP_HEALTH",          os.getenv("DISCORD_HEALTH_WEBHOOK", ""))
DISCORD_FLOW_EXTREME  = os.getenv("DISCORD_FLOW_EXTREME",         os.getenv("DISCORD_HIGHSTAKES_WEBHOOK", ""))
DISCORD_FLOW_SIGNALS  = os.getenv("DISCORD_FLOW_SIGNALS",         os.getenv("DISCORD_WEBHOOK_URL", ""))

# ── Feature flags ────────────────────────────────────────────────────────
# Set to False to stop dark pool prints from posting to ARKA Discord channels.
# Dark pool data is still fetched and used for flow scoring — only Discord is silenced.
DARK_POOL_DISCORD_ENABLED = False
# ══════════════════════════════════════════════════════════════════════
# SIGNAL QUALITY GATE
# ══════════════════════════════════════════════════════════════════════
# In-memory cooldown — resets on process restart (every 5 min cron is fine)
_post_cooldown: dict = {}        # ticker → last_post_epoch_seconds
_institutional_sent: dict = {}   # key → timestamp — 60-min institutional cooldown

QUALITY_GATE = {
    "min_confidence":  72,    # raised from 65 — fewer but higher-quality alerts
    "extreme_min":     60,    # extreme signals need at least 60%
    "cooldown_secs":  1200,   # same ticker silent for 20 min per direction
    "allowed_dirs":  {"BULLISH", "BEARISH"},  # drop NEUTRAL/WATCH
}

def _is_market_hours() -> bool:
    """True only during regular US market hours Mon-Fri 9:30-16:00 ET."""
    from zoneinfo import ZoneInfo as _ZI
    _et = datetime.now(_ZI("America/New_York"))
    return (
        _et.weekday() < 5 and
        ((_et.hour == 9 and _et.minute >= 30) or _et.hour > 9) and
        _et.hour < 16
    )


def _is_premarket_hours() -> bool:
    """True during premarket window Mon-Fri 4:00am-9:29am ET."""
    from zoneinfo import ZoneInfo as _ZI
    _et = datetime.now(_ZI("America/New_York"))
    _hm = _et.hour * 60 + _et.minute
    return _et.weekday() < 5 and 240 <= _hm < 570  # 4:00am–9:29am


# Premarket daily alert counter — reset each new day
_premarket_alert_state: dict = {"date": "", "count": 0}
_PREMARKET_MAX_ALERTS    = 3
_PREMARKET_MIN_CONFIDENCE = 80
_PREMARKET_MIN_VOL_MULT   = 2.5
_PREMARKET_TICKERS        = {"SPY", "QQQ", "SPX", "IWM"}

_ALWAYS_ON_TICKERS = {"SPY", "QQQ", "SPX"}


def _passes_quality_gate(ticker: str, rec: dict, is_extreme: bool = False,
                          cooldown_key: str = "") -> bool:
    direction  = rec.get("direction", "NEUTRAL")
    confidence = int(rec.get("confidence", 0))
    now        = time.time()
    _ticker_up = ticker.upper()

    # 0. Session gate — determine allowed window
    _market_open  = _is_market_hours()
    _premarket    = _is_premarket_hours()

    if not _market_open and not _premarket:
        # Dead hours — only always-on tickers pass
        if _ticker_up not in _ALWAYS_ON_TICKERS:
            log.info(f"  🔇 {ticker} DROPPED — outside market hours (not always-on)")
            return False

    if not _market_open and _premarket:
        # Premarket window: only allowed tickers, stricter gates
        if _ticker_up not in _PREMARKET_TICKERS:
            log.info(f"  🔇 {ticker} DROPPED — premarket only allows {_PREMARKET_TICKERS}")
            return False
        if confidence < _PREMARKET_MIN_CONFIDENCE:
            log.info(f"  🔇 {ticker} DROPPED — premarket confidence {confidence}% < {_PREMARKET_MIN_CONFIDENCE}%")
            return False
        vol_mult = float(rec.get("volume_mult", rec.get("vol_mult", 0)))
        if vol_mult and vol_mult < _PREMARKET_MIN_VOL_MULT:
            log.info(f"  🔇 {ticker} DROPPED — premarket vol_mult {vol_mult:.1f}x < {_PREMARKET_MIN_VOL_MULT}x")
            return False
        # Daily premarket alert cap
        from datetime import date as _date
        _today = _date.today().isoformat()
        if _premarket_alert_state["date"] != _today:
            _premarket_alert_state["date"]  = _today
            _premarket_alert_state["count"] = 0
        if _premarket_alert_state["count"] >= _PREMARKET_MAX_ALERTS:
            log.info(f"  🔇 {ticker} DROPPED — premarket cap reached ({_PREMARKET_MAX_ALERTS}/day)")
            return False
        # Inject premarket prefix into message if present
        if "message" in rec:
            rec["message"] = "🌅 PRE-MARKET — " + rec["message"]
        _premarket_alert_state["count"] += 1
        log.info(f"  🌅 PREMARKET ALERT #{_premarket_alert_state['count']} — {ticker}")

    # 1. Drop NEUTRAL / WATCH
    if direction not in QUALITY_GATE["allowed_dirs"]:
        log.info(f"  🔇 {ticker} DROPPED — direction {direction} not actionable")
        return False

    # 2. Confidence gate
    min_conf = QUALITY_GATE["extreme_min"] if is_extreme else QUALITY_GATE["min_confidence"]
    if confidence < min_conf:
        log.info(f"  🔇 {ticker} DROPPED — confidence {confidence}% < {min_conf}%")
        return False

    # 3. Cooldown — use specific key (ticker+strike+expiry+direction) to avoid duplicate alerts
    _cd_key = cooldown_key if cooldown_key else ticker
    last = _post_cooldown.get(_cd_key, 0)
    if now - last < QUALITY_GATE["cooldown_secs"]:
        remaining = int(QUALITY_GATE["cooldown_secs"] - (now - last))
        log.info(f"  🔇 {ticker} DROPPED — cooldown {remaining}s remaining (key={_cd_key})")
        return False

    _post_cooldown[_cd_key] = now
    return True


def institutional_cooldown_ok(ticker: str, strike: float, direction: str) -> bool:
    """
    60-minute per-ticker+strike+direction cooldown for institutional alerts.
    Separate from the regular quality-gate cooldown.
    Returns True (and registers timestamp) when the alert may fire.
    """
    key     = f"{ticker.upper()}_{strike}_{direction.upper()}"
    elapsed = time.time() - _institutional_sent.get(key, 0)
    if elapsed < 3600:
        log.debug(f"  🏛️ {ticker} inst cooldown — {int(3600-elapsed)}s remaining")
        return False
    _institutional_sent[key] = time.time()
    return True


def is_institutional_flow(signal: dict) -> tuple:
    """
    Detect if a signal qualifies as institutional-grade flow.
    Returns (is_institutional: bool, tier: str, confidence_boost: int)

    Institutional criteria:
    - Premium >= $100K (large block)
    - Dark pool % >= 70% (off-exchange = institutional)
    - Volume ratio >= 5x average
    - Execution type = SWEEP (multiple exchanges)
    - Score >= 75
    """
    premium   = float(signal.get("premium",   0) or 0)
    dark_pool = float(signal.get("dark_pool_pct", 0) or 0)
    vol_ratio = float(signal.get("vol_ratio", signal.get("volume_mult", 0)) or 0)
    score     = float(signal.get("score",     signal.get("confidence", 0)) or 0)
    execution = str(signal.get("execution",   signal.get("sweep", "")) or "")

    if premium >= 500_000 and dark_pool >= 80 and vol_ratio >= 8:
        return True, "MEGA_INSTITUTIONAL", 45
    elif premium >= 200_000 and dark_pool >= 75 and vol_ratio >= 5:
        return True, "INSTITUTIONAL", 35
    elif premium >= 100_000 and dark_pool >= 70 and score >= 75:
        return True, "LARGE_BLOCK", 25
    elif "SWEEP" in execution.upper() and premium >= 50_000:
        return True, "SWEEP", 20

    return False, "", 0


# ══════════════════════════════════════════════════════════════════════
# GLOBAL MARKET SESSION SYSTEM
# ══════════════════════════════════════════════════════════════════════

_GLOBAL_SESSION_ALERTS = {'session': '', 'count': 0}
_GLOBAL_MACRO_TICKERS  = {"SPY", "QQQ", "SPX", "IWM", "GLD", "SLV", "TLT"}


def get_market_session() -> str:
    """
    Returns the current trading session based on ET clock.
    Priority: US_MARKET > US_PREMARKET > LONDON > ASIA > CLOSED
    """
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    wd  = now.weekday()        # 0=Mon … 6=Sun
    hm  = now.hour * 60 + now.minute  # minutes since midnight ET

    # US_MARKET: Mon-Fri 9:30am-4pm (570-960)
    if wd < 5 and 570 <= hm < 960:
        return 'US_MARKET'

    # US_PREMARKET: Mon-Fri 4am-9:30am (240-570)
    if wd < 5 and 240 <= hm < 570:
        return 'US_PREMARKET'

    # LONDON: Mon-Fri 3am-4am (180-240) — early London before US pre-market
    if wd < 5 and 180 <= hm < 240:
        return 'LONDON'

    # ASIA evening (7pm-midnight): Sun through Thu
    if hm >= 1140 and wd in (0, 1, 2, 3, 6):
        return 'ASIA'
    # ASIA overnight (midnight-2am): Mon through Fri (carrying over from prior evening)
    if hm < 120 and wd in (0, 1, 2, 3, 4):
        return 'ASIA'

    return 'CLOSED'


def global_session_gate(ticker: str, confidence: float, vol_ratio: float,
                        dark_pool_pct: float, tier: str) -> dict:
    """
    Strict quality gate for non-US-market sessions.
    Returns {allow: bool, reason: str}.
    Applies higher bars than US market hours.
    """
    global _GLOBAL_SESSION_ALERTS

    session = get_market_session()

    # Only macro ETFs allowed during global hours
    if ticker.upper() not in _GLOBAL_MACRO_TICKERS:
        return {"allow": False,
                "reason": f"global session: only macro ETFs allowed ({ticker} blocked)"}

    # EXTREME tier only (MEGA maps to EXTREME for UOA)
    if tier not in ("EXTREME", "MEGA"):
        return {"allow": False,
                "reason": f"global session: EXTREME tier required (got {tier})"}

    # Confidence >= 85%
    if confidence < 85:
        return {"allow": False,
                "reason": f"global session: confidence {confidence}% < 85% required"}

    # Volume ratio >= 3.0x (skip if not applicable, e.g. dark pool signals)
    if vol_ratio > 0 and vol_ratio < 3.0:
        return {"allow": False,
                "reason": f"global session: vol ratio {vol_ratio:.1f}x < 3.0x required"}

    # Dark pool % >= 80% (skip if not provided)
    if dark_pool_pct > 0 and dark_pool_pct < 80:
        return {"allow": False,
                "reason": f"global session: dark pool {dark_pool_pct:.0f}% < 80% required"}

    # 60-minute cooldown (stricter than 30-min US hours)
    now  = time.time()
    last = _post_cooldown.get(ticker, 0)
    if now - last < 3600:
        remaining = int(3600 - (now - last))
        return {"allow": False,
                "reason": f"global cooldown: {remaining // 60}min remaining for {ticker}"}

    # Max 2 alerts per session — reset counter when session changes
    if _GLOBAL_SESSION_ALERTS['session'] != session:
        _GLOBAL_SESSION_ALERTS = {'session': session, 'count': 0}

    if _GLOBAL_SESSION_ALERTS['count'] >= 2:
        return {"allow": False,
                "reason": f"global session: max 2 alerts reached for {session}"}

    _GLOBAL_SESSION_ALERTS['count'] += 1
    _post_cooldown[ticker] = now   # register cooldown immediately
    return {"allow": True,
            "reason": f"{session} gate passed ({_GLOBAL_SESSION_ALERTS['count']}/2 used)"}


_SESSION_PREFIX = {
    'US_PREMARKET': "🌅 PRE-MARKET — ",
    'LONDON':       "🇬🇧 LONDON SESSION — ",
    'ASIA':         "🌏 ASIA SESSION — ",
}
_SESSION_FOOTER = {
    'US_PREMARKET': "CHAKRA Global Monitor • Pre-Market • Only A+ signals fire",
    'LONDON':       "CHAKRA Global Monitor • London Session • Only A+ signals fire",
    'ASIA':         "CHAKRA Global Monitor • Asia Session • Only A+ signals fire",
}

# ── Always-on index tickers (never removed) ──────────────────────────────────
INDEX_TICKERS = {"SPY", "QQQ", "IWM", "DIA", "SPX"}

# ── Top options-active stocks (smart static list — high liquidity only) ───────
# These have deep options markets and reliable flow signals
TOP_OPTIONS_STOCKS = [
    # Mega cap
    "AAPL","NVDA","TSLA","MSFT","AMZN","META","GOOGL","AMD","NFLX","AVGO",
    # High beta / momentum
    "CRM","COIN","MSTR","PLTR","HOOD","RBLX","IONQ","SMCI","ARM","SNOW",
    "UBER","SHOP","SQ","ROKU","DKNG","RIVN","LCID","SOFI","UPST","AFRM",
    # Sector leaders with active options
    "GS","JPM","BAC","XOM","CVX","UNH","LLY","PFE","MRNA","BNTX",
    # Volatility / inverse ETFs
    "UVXY","VXX","SQQQ","SPXS","TQQQ","SOXS","LABD",
]

def get_dynamic_universe() -> list:
    """
    Build dynamic scan universe every cycle:
    - Always includes index ETFs + top options stocks
    - Always includes swing screener watchlist tickers
    - During market hours: adds today's movers (price>5, vol>500K)
    Returns deduplicated list of tickers to scan.
    """
    universe = list(INDEX_TICKERS) + TOP_OPTIONS_STOCKS

    # Always include swing watchlist tickers
    universe.extend(_get_swing_watchlist_tickers())

    # Add market movers during trading hours (9:30am - 4pm ET)
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if 9 <= now.hour < 16:
        try:
            for endpoint in ["gainers", "losers"]:
                r = httpx.get(
                    f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{endpoint}",
                    params={"apiKey": POLYGON_KEY, "include_otc": "false"},
                    timeout=8
                )
                for t in r.json().get("tickers", []):
                    price  = t.get("day", {}).get("c", 0) or 0
                    volume = t.get("day", {}).get("v", 0) or 0
                    sym    = t.get("ticker", "")
                    # Filter: real stocks only (price >5, volume >500K, no warrants/SPACs)
                    if (price >= 5 and volume >= 500_000 and
                        not any(c in sym for c in [".","W","R","U"]) and
                        len(sym) <= 5):
                        universe.append(sym)
        except Exception as _e:
            log.debug(f"Dynamic universe fetch failed: {_e}")

    # Deduplicate preserving order
    seen = set()
    result = []
    for t in universe:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result

# For backward compat — TICKERS is now dynamic
TICKERS = list(INDEX_TICKERS) + TOP_OPTIONS_STOCKS
SEEN_FILE      = BASE / "logs" / "chakra" / "flow_seen.json"

# ── Dark pool thresholds (per-trade) ────────────────────────────────────
DP_LARGE   = 100_000    # $100K per trade
DP_WHALE   = 500_000    # $500K per trade
DP_MEGA    = 2_000_000  # $2M per trade

# ── Dark pool aggregate thresholds (total TRF volume ratio) ─────────────
DP_AGG_ELEVATED  = 0.70   # 70%+ → notable (was 55% — too noisy)
DP_AGG_HIGH      = 0.78   # 78%+ → elevated institutional activity (was 65%)
DP_AGG_EXTREME   = 0.88   # 88%+ → extreme dark pool dominance (was 80%)

# ── UOA thresholds ──────────────────────────────────────────────────────
UOA_LARGE  = 20    # 20x normal (was 10x — too noisy)
UOA_WHALE  = 35    # 35x normal (was 25x)
UOA_MEGA   = 50    # 50x normal (unchanged — extreme)

# Per-category minimums
UOA_STOCK_MIN  = 25   # stocks need 25x+ to alert (was 15x)
UOA_INDEX_MIN  = 20   # indexes alert at 20x+ (was 10x)
UOA_MIN_VOL = 500  # minimum volume to count (was 200)


# ══════════════════════════════════════════════════════════════════════
# 1. DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════

def fetch_dark_pool(ticker: str) -> dict:
    """
    Fetch recent trades and classify dark pool (TRF) vs lit volume.
    Returns aggregate stats + individual large blocks.
    """
    try:
        r = httpx.get(
            f"https://api.polygon.io/v3/trades/{ticker}",
            params={"apiKey": POLYGON_KEY, "limit": 500, "order": "desc"},
            timeout=12
        )
        trades = r.json().get("results", [])
        trades = [t for t in (trades or []) if isinstance(t, dict)]
        if not trades:
            return {"error": "no trades", "ticker": ticker}

        total_vol  = 0
        dark_vol   = 0
        dark_value = 0
        lit_vol    = 0
        blocks     = []  # individual large prints

        for t in trades:
            size     = t.get("size", 0)
            price    = t.get("price", 0)
            exchange = t.get("exchange", 0)
            value    = size * price
            total_vol += size

            if exchange == 4:  # TRF = dark pool
                dark_vol   += size
                dark_value += value
                if value >= DP_LARGE:
                    raw_cond   = t.get("conditions", [])
                    conditions = raw_cond if isinstance(raw_cond, list) else ([raw_cond] if raw_cond else [])
                    side = "buy" if 14 not in conditions else "sell"
                    blocks.append({
                        "size":      size,
                        "price":     price,
                        "value":     value,
                        "side":      side,
                        "exchange":  exchange,
                        "timestamp": str(t.get("participant_timestamp", ""))[:16],
                    })
            else:
                lit_vol += size

        dark_pct = dark_vol / total_vol if total_vol > 0 else 0

        # Classify aggregate dark pool activity
        if dark_pct >= DP_AGG_EXTREME:
            agg_tier, agg_emoji = "EXTREME", "🔥"
        elif dark_pct >= DP_AGG_HIGH:
            agg_tier, agg_emoji = "HIGH",    "🐋"
        elif dark_pct >= DP_AGG_ELEVATED:
            agg_tier, agg_emoji = "ELEVATED","💰"
        else:
            agg_tier, agg_emoji = "NORMAL",  "📊"

        return {
            "ticker":     ticker,
            "total_vol":  total_vol,
            "dark_vol":   dark_vol,
            "dark_value": dark_value,
            "dark_pct":   round(dark_pct * 100, 1),
            "agg_tier":   agg_tier,
            "agg_emoji":  agg_emoji,
            "blocks":     sorted(blocks, key=lambda x: -x["value"])[:5],
            "trade_count": len(trades),
        }
    except Exception as e:
        log.warning(f"Dark pool fetch error {ticker}: {e}")
        return {"ticker": ticker, "error": str(e)}


def fetch_uoa(ticker: str) -> list[dict]:
    """Fetch unusual options activity from Polygon snapshot.

    Scans 0-7 DTE to capture both:
      - 0DTE same-day put hedging (short-term protection)
      - 1-7 DTE institutional call sweeps (directional bets go out 1 week)
    Previously only scanned today's expiry, which structurally missed calls.
    """
    from datetime import timedelta
    try:
        _poly_sym  = f"I:{ticker}" if ticker.upper() == "SPX" else ticker
        _today     = date.today()
        _exp_from  = _today.isoformat()
        _exp_to    = (_today + timedelta(days=7)).isoformat()

        def _fetch(sym: str) -> list:
            resp = httpx.get(
                f"https://api.polygon.io/v3/snapshot/options/{sym}",
                params={
                    "apiKey":                POLYGON_KEY,
                    "limit":                 250,
                    "expiration_date.gte":   _exp_from,
                    "expiration_date.lte":   _exp_to,
                },
                timeout=12,
            )
            return resp.json().get("results", []) if resp.status_code == 200 else []

        contracts = _fetch(_poly_sym)
        if not contracts and ticker.upper() == "SPX":
            contracts = _fetch(ticker)
        unusual   = []

        for c in contracts:
            vol    = c.get("day", {}).get("volume", 0)
            oi     = c.get("open_interest", 0) or 1
            ratio  = vol / oi
            detail  = c.get("details", {})
            expiry  = detail.get("expiration_date", "")
            try:
                from datetime import date as _d2
                _dte = (_d2.fromisoformat(expiry) - _today).days if expiry else 0
            except Exception:
                _dte = 0

            # DTE-adjusted minimum ratio:
            # 0DTE naturally has very high vol/OI (OI starts at 0), so keep bar high.
            # 1-7 DTE contracts have accumulated OI from prior days → lower bar needed
            # to catch institutional sweeps that happen early in the week.
            _is_idx = ticker in {"SPY","QQQ","IWM","DIA","SPX"}
            if _dte == 0:
                _min_ratio = UOA_INDEX_MIN if _is_idx else UOA_STOCK_MIN
            elif _dte <= 3:
                _min_ratio = (UOA_INDEX_MIN * 0.6) if _is_idx else (UOA_STOCK_MIN * 0.6)
            else:
                _min_ratio = (UOA_INDEX_MIN * 0.4) if _is_idx else (UOA_STOCK_MIN * 0.4)

            if vol < UOA_MIN_VOL or ratio < _min_ratio:
                continue
            mark   = c.get("day", {}).get("close", 0) or c.get("day", {}).get("vwap", 0)
            prem   = vol * mark * 100

            # Minimum premium: weekly sweeps need $50K+, 0DTE lower bar
            _min_prem = 50_000 if _dte > 0 else 15_000
            if prem < _min_prem:
                continue

            unusual.append({
                "contract": detail.get("ticker", ""),
                "type":     detail.get("contract_type", ""),
                "strike":   detail.get("strike_price", 0),
                "expiry":   expiry,
                "dte":      _dte,
                "volume":   vol,
                "oi":       oi,
                "ratio":    round(ratio, 1),
                "mark":     round(mark, 2),
                "premium":  round(prem, 0),
                "iv":       round(c.get("implied_volatility", 0) * 100, 1),
            })

        return sorted(unusual, key=lambda x: -x["ratio"])[:15]  # top 15 for wider DTE
    except Exception as e:
        log.warning(f"UOA fetch error {ticker}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════
# 2. RECOMMENDATION ENGINE
# ══════════════════════════════════════════════════════════════════════

def get_recommendation(ticker: str, dp: dict, uoa_items: list) -> dict:
    """Analyze all signals and recommend CALL, PUT, or WATCH."""
    bull = 0
    bear = 0
    reasons = []

    # ── Dark pool aggregate bias ────────────────────────────────────
    dp_pct  = dp.get("dark_pct", 0)
    blocks  = dp.get("blocks", [])
    buy_val  = sum(b["value"] for b in blocks if b.get("side") == "buy")
    sell_val = sum(b["value"] for b in blocks if b.get("side") == "sell")

    if dp_pct >= DP_AGG_ELEVATED * 100:
        if buy_val > sell_val * 1.3:
            bull += 30
            reasons.append(f"Dark pool {dp_pct:.0f}% of volume — buy side dominant (${buy_val/1e3:.0f}K)")
        elif sell_val > buy_val * 1.3:
            bear += 30
            reasons.append(f"Dark pool {dp_pct:.0f}% of volume — sell side dominant (${sell_val/1e3:.0f}K)")
        else:
            reasons.append(f"Dark pool {dp_pct:.0f}% of volume — mixed direction")

    # ── Individual large blocks ─────────────────────────────────────
    for b in blocks:
        if b["value"] >= DP_WHALE:
            if b.get("side") == "buy":
                bull += 20
                reasons.append(f"Whale block BUY ${b['value']/1e3:.0f}K at ${b['price']}")
            elif b.get("side") == "sell":
                bear += 20
                reasons.append(f"Whale block SELL ${b['value']/1e3:.0f}K at ${b['price']}")

    # ── UOA call/put balance ────────────────────────────────────────
    call_flow = sum(u["ratio"] * u["volume"] for u in uoa_items if u["type"] == "call")
    put_flow  = sum(u["ratio"] * u["volume"] for u in uoa_items if u["type"] == "put")
    total_flow = call_flow + put_flow

    if total_flow > 0:
        call_pct = call_flow / total_flow
        if call_pct > 0.60:
            bull += 35
            reasons.append(f"Options flow {call_pct*100:.0f}% calls — bullish sweep")
        elif call_pct < 0.40:
            bear += 35
            reasons.append(f"Options flow {(1-call_pct)*100:.0f}% puts — bearish sweep")

    # ── Whale UOA ───────────────────────────────────────────────────
    whale_calls = [u for u in uoa_items if u["type"] == "call" and u["ratio"] >= UOA_WHALE]
    whale_puts  = [u for u in uoa_items if u["type"] == "put"  and u["ratio"] >= UOA_WHALE]
    if whale_calls:
        bull += 15
        top = whale_calls[0]
        reasons.append(f"Whale call: {top['contract']} {top['ratio']}x OI at ${top['strike']}")
    if whale_puts:
        bear += 15
        top = whale_puts[0]
        reasons.append(f"Whale put: {top['contract']} {top['ratio']}x OI at ${top['strike']}")

    total = bull + bear
    if total == 0 or abs(bull - bear) < 10:
        return {"direction": "NEUTRAL", "confidence": 0, "action": "👀 WATCH", "color": 0x6C757D, "reasons": reasons}

    if bull > bear:
        conf = min(100, int(bull / total * 100))
        return {
            "direction":  "BULLISH",
            "confidence": conf,
            "action":     f"📈 **BUY CALL** on {ticker}",
            "color":      0x00FF9D,
            "reasons":    reasons[:3],
        }
    else:
        conf = min(100, int(bear / total * 100))
        return {
            "direction":  "BEARISH",
            "confidence": conf,
            "action":     f"📉 **BUY PUT** on {ticker}",
            "color":      0xFF2D55,
            "reasons":    reasons[:3],
        }


# ══════════════════════════════════════════════════════════════════════
# 3. DISCORD ALERTS
# ══════════════════════════════════════════════════════════════════════

def post_dark_pool_alert(ticker: str, dp: dict, rec: dict):
    """Post dark pool aggregate alert to Discord."""
    # ── Institutional check — fires regardless of DARK_POOL_DISCORD_ENABLED ──
    _inst_premium = sum(b.get("value", 0) for b in dp.get("blocks", []))
    _inst_sig = {
        "ticker":        ticker,
        "direction":     rec.get("direction", "NEUTRAL"),
        "strike":        0,
        "dte":           0,
        "premium":       _inst_premium,
        "dark_pool_pct": dp.get("dark_pct", 0),
        "vol_ratio":     0,
        "score":         rec.get("confidence", 0),
        "execution":     "DARK POOL",
    }
    _is_inst, _inst_tier, _inst_boost = is_institutional_flow(_inst_sig)
    if _is_inst and institutional_cooldown_ok(ticker, 0, rec.get("direction", "?")):
        try:
            from backend.arka.arka_discord_notifier import post_institutional_flow as _pif
            _pif(_inst_sig)
            log.info(f"  🏛️ INSTITUTIONAL DP: {ticker} tier={_inst_tier} boost={_inst_boost}")
        except Exception as _ie:
            log.debug(f"  [InstFlow DP] {_ie}")

    if not DARK_POOL_DISCORD_ENABLED:
        return False
    if not DISCORD_ALERTS:
        return False

    # ── Market hours gate — blocks non-always-on tickers outside 9:30-4pm ET ──
    if not _is_market_hours() and ticker.upper() not in _ALWAYS_ON_TICKERS:
        log.info(f"  🔇 {ticker} DP DROPPED — outside market hours")
        return False

    _session   = get_market_session()
    _is_global       = _session in ('US_PREMARKET', 'LONDON', 'ASIA')
    _title_prefix    = _SESSION_PREFIX.get(_session, "")
    _footer_text     = _SESSION_FOOTER.get(_session, "CHAKRA Flow Monitor • Dark Pool Scanner")

    if _is_global:
        _gate = global_session_gate(
            ticker,
            confidence    = int(rec.get("confidence", 0)),
            vol_ratio     = 0.0,   # not applicable for dark pool signals
            dark_pool_pct = float(dp.get("dark_pct", 0)),
            tier          = dp.get("agg_tier", "ELEVATED"),
        )
        if not _gate["allow"]:
            log.info(f"  🔇 {ticker} DP DROPPED — {_gate['reason']}")
            return False
        # Direction check (global_session_gate doesn't check direction)
        if rec.get("direction", "NEUTRAL") not in QUALITY_GATE["allowed_dirs"]:
            log.info(f"  🔇 {ticker} DROPPED — direction {rec.get('direction')} not actionable")
            return False

    tier   = dp["agg_tier"]
    emoji  = dp["agg_emoji"]
    pct    = dp["dark_pct"]
    dark_v = dp["dark_vol"]
    blocks = dp["blocks"]
    now    = datetime.now().strftime("%H:%M ET")
    color  = rec.get("color", 0x00D4FF)

    # Title with tier callout (session prefix prepended for global sessions)
    tier_title = {
        "EXTREME": f"🔥🔥 {_title_prefix}EXTREME Dark Pool Activity — {ticker}",
        "HIGH":    f"🐋 {_title_prefix}High Dark Pool Activity — {ticker}",
        "ELEVATED":f"💰 {_title_prefix}Elevated Dark Pool Activity — {ticker}",
    }.get(tier, f"📊 {_title_prefix}Dark Pool Activity — {ticker}")

    # Block breakdown
    block_lines = []
    for b in blocks[:3]:
        side_emoji = "🟢" if b.get("side") == "buy" else "🔴" if b.get("side") == "sell" else "⚪"
        block_lines.append(f"{side_emoji} ${b['value']/1e3:.0f}K — {b['size']:,} shares @ ${b['price']:.2f}")

    reason_text = "\n".join(f"• {r}" for r in rec.get("reasons", []))

    fields = [
        {"name": "🕳️ Dark Pool %",  "value": f"**{pct}%** of volume",             "inline": True},
        {"name": "📊 Dark Vol",      "value": f"{dark_v:,} shares",                "inline": True},
        {"name": "📈 Total Scanned", "value": f"{dp['trade_count']} recent trades", "inline": True},
    ]

    if block_lines:
        fields.append({
            "name":   "🧱 Large Prints",
            "value":  "\n".join(block_lines),
            "inline": False,
        })

    fields.append({
        "name":   "🎯 CHAKRA Recommendation",
        "value":  f"{rec['action']}\nConfidence: **{rec['confidence']}%**\n\n{reason_text}",
        "inline": False,
    })

    payload = {"embeds": [{
        "title":       tier_title,
        "color":       color,
        "description": f"**{rec['direction']}** dark pool signal on **{ticker}** at {now}",
        "fields":      fields,
        "footer":      {"text": _footer_text},
        "timestamp":   datetime.utcnow().isoformat() + "Z",
    }]}

    # Extra ping for EXTREME tier
    if tier == "EXTREME":
        payload["content"] = f"🔥🔥 **EXTREME** dark pool activity on **{ticker}** — {rec['action']}"

    try:
        is_extreme = (tier == "EXTREME")
        # ── Quality gate — skip for global sessions (already checked above) ──
        if not _is_global and not _passes_quality_gate(ticker, rec, is_extreme=is_extreme):
            return False

        # ── Smart channel routing ─────────────────────────────────────────────
        from zoneinfo import ZoneInfo as _ZI
        _now_et   = datetime.now(_ZI("America/New_York"))
        _is_lotto = (_now_et.hour == 15 and _now_et.minute >= 30) and not _is_global
        _is_idx   = ticker.upper() in {"SPY", "QQQ", "IWM", "DIA", "SPX"}

        _CH_SCALP_EXT  = os.getenv("DISCORD_ARKA_SCALP_EXTREME","")
        _CH_SCALP_SIG  = os.getenv("DISCORD_ARKA_SCALP_SIGNALS","")
        _CH_SWING_EXT  = os.getenv("DISCORD_ARKA_SWINGS_EXTREME","")
        _CH_SWING_SIG  = os.getenv("DISCORD_ARKA_SWINGS_SIGNALS","")
        _CH_LOTTO      = os.getenv("DISCORD_ARKA_LOTTO","")

        if _is_lotto:
            _url = _CH_LOTTO or DISCORD_ALERTS
        elif ticker.upper() == "SPX" and DISCORD_SPX_HOOK:
            _url = DISCORD_SPX_HOOK
        elif _is_idx and is_extreme:
            _url = _CH_SCALP_EXT or DISCORD_FLOW_EXTREME or DISCORD_ALERTS
        elif _is_idx:
            _url = _CH_SCALP_SIG or DISCORD_FLOW_SIGNALS or DISCORD_ALERTS
        elif is_extreme:
            _url = _CH_SWING_EXT or DISCORD_FLOW_EXTREME or DISCORD_ALERTS
        else:
            _url = _CH_SWING_SIG or DISCORD_FLOW_SIGNALS or DISCORD_ALERTS
        if not _url: _url = DISCORD_ALERTS
        r = requests.post(_url, json=payload, timeout=8)
        # Also mirror SPX alerts to SPX-only channel
        if ticker.upper() == "SPX" and DISCORD_SPX_HOOK and _url != DISCORD_SPX_HOOK:
            requests.post(DISCORD_SPX_HOOK, json=payload, timeout=8)
        # Cache dark pool signal
        try:
            _bias   = rec.get("direction", "NEUTRAL")
            _conf   = int(rec.get("confidence", 65))
            _dp_pct = float(dp.get("dark_pct", 0)) / 100.0
            _write_flow_signal_cache(ticker, _bias, _conf, 0.0, False, _dp_pct)
        except Exception:
            pass
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord post error: {e}")
        return False


def post_uoa_alert(ticker: str, uoa: dict, tier: str, emoji: str, label: str, rec: dict):
    """Post unusual options alert to Discord."""
    if not DISCORD_ALERTS:
        return False

    # ── Market hours gate — blocks non-always-on tickers outside 9:30-4pm ET ──
    if not _is_market_hours() and ticker.upper() not in _ALWAYS_ON_TICKERS:
        log.info(f"  🔇 {ticker} UOA DROPPED — outside market hours")
        return False

    # ── Institutional flow check — own 60-min cooldown, bypasses quality gate ──
    try:
        from datetime import date as _date_inst
        _dte_inst = 0
        try:
            _exp_inst = uoa.get("expiry", "")
            _dte_inst = (datetime.strptime(_exp_inst, "%Y-%m-%d").date() - _date_inst.today()).days if _exp_inst else 0
        except Exception:
            pass
        _inst_sig_uoa = {
            "ticker":        ticker,
            "direction":     rec.get("direction", "NEUTRAL"),
            "strike":        uoa.get("strike", 0),
            "dte":           _dte_inst,
            "premium":       uoa.get("premium", 0),
            "dark_pool_pct": uoa.get("dark_pool_pct", 0),
            "vol_ratio":     uoa.get("ratio", 0),
            "score":         rec.get("confidence", 0),
            "execution":     "SWEEP" if tier in ("MEGA", "WHALE") else "BLOCK",
        }
        _is_inst_uoa, _inst_tier_uoa, _inst_boost_uoa = is_institutional_flow(_inst_sig_uoa)
        if _is_inst_uoa and institutional_cooldown_ok(
                ticker, uoa.get("strike", 0), rec.get("direction", "?")):
            from backend.arka.arka_discord_notifier import post_institutional_flow as _pif_uoa
            _pif_uoa(_inst_sig_uoa)
            log.info(f"  🏛️ INSTITUTIONAL UOA: {ticker} tier={_inst_tier_uoa} boost={_inst_boost_uoa}")
    except Exception as _ie_uoa:
        log.debug(f"  [InstFlow UOA] {_ie_uoa}")

    _session   = get_market_session()
    _is_global       = _session in ('US_PREMARKET', 'LONDON', 'ASIA')
    _title_prefix    = _SESSION_PREFIX.get(_session, "")
    _footer_text     = _SESSION_FOOTER.get(_session, "CHAKRA Flow Monitor • UOA Scanner")

    if _is_global:
        _gate = global_session_gate(
            ticker,
            confidence    = int(rec.get("confidence", 0)),
            vol_ratio     = float(uoa.get("ratio", 0)),
            dark_pool_pct = float(uoa.get("dark_pool_pct", 0)),
            tier          = tier,   # "MEGA"/"WHALE"/"LARGE" — MEGA maps to EXTREME in gate
        )
        if not _gate["allow"]:
            log.info(f"  🔇 {ticker} UOA DROPPED — {_gate['reason']}")
            return False
        if rec.get("direction", "NEUTRAL") not in QUALITY_GATE["allowed_dirs"]:
            log.info(f"  🔇 {ticker} DROPPED — direction {rec.get('direction')} not actionable")
            return False

    ct     = uoa["type"].upper()
    dir_color = 0x00FF9D if ct == "CALL" else 0xFF2D55
    now    = datetime.now().strftime("%H:%M ET")
    reason_text = "\n".join(f"• {r}" for r in rec.get("reasons", []))

    tier_title = {
        "MEGA":  f"🔥⚡ {_title_prefix}EXTREME Options Flow — {ticker} {ct}",
        "WHALE": f"⚡ {_title_prefix}Whale Options Sweep — {ticker} {ct}",
        "LARGE": f"📊 {_title_prefix}Unusual Options Activity — {ticker} {ct}",
    }.get(tier, f"{emoji} {_title_prefix}Options Flow — {ticker}")

    agg_contracts = uoa.get("agg_contracts", 1)
    agg_pct       = uoa.get("agg_pct", 100)

    # Calculate DTE and trade details
    from datetime import date as _date
    try:
        _exp = uoa.get("expiry", "")
        _dte = (datetime.strptime(_exp, "%Y-%m-%d").date() - _date.today()).days if _exp else 0
    except Exception:
        _dte = 0

    # Spot price estimate from strike proximity
    _spot  = uoa.get("spot", uoa["strike"])
    _mark  = float(uoa.get("mark", 0))
    _is_call = ct == "CALL"

    # Suggested trade levels based on options flow
    _entry_note  = f"Enter ${ticker} {'above' if _is_call else 'below'} ${uoa['strike']:.0f}"
    _target_note = f"Target: {'call wall' if _is_call else 'put wall'} — watch GEX levels"
    _stop_note   = f"Stop: below {'${:.0f}'.format(uoa['strike'] * 0.97) if _is_call else '${:.0f}'.format(uoa['strike'] * 1.03)}"
    _dte_label   = f"{_dte}DTE" if _dte > 0 else "0DTE"
    _swing_note  = "0DTE scalp play" if _dte == 0 else f"Swing trade — {_dte} days to expiry {_exp}"

    fields = [
        {"name": "📋 Top Contract",   "value": f"**{ct} ${uoa['strike']:.0f} exp {uoa['expiry']}** ({_dte_label})", "inline": False},
        {"name": "🔥 Vol/OI Ratio",   "value": f"**{uoa['ratio']}x** normal volume",              "inline": True},
        {"name": "📊 Volume",         "value": f"{uoa['volume']:,} contracts",                    "inline": True},
        {"name": "📋 OI",             "value": f"{uoa['oi']:,}",                                  "inline": True},
        {"name": "💰 Premium",        "value": f"~${uoa['premium']/1e3:.0f}K" if uoa['premium'] > 0 else "N/A", "inline": True},
        {"name": "📉 IV",             "value": f"{uoa['iv']}%",                                   "inline": True},
        {"name": "💲 Mark",           "value": f"${uoa['mark']}",                                 "inline": True},
        {"name": "⚖️ Flow Dominance", "value": f"**{agg_pct:.0f}%** of flow is **{ct.upper()}s** ({agg_contracts} contracts)", "inline": False},
        {"name": "📅 Trade Type",     "value": _swing_note,                                       "inline": False},
        {"name": "🏦 Institutional Vol", "value": f"**{uoa.get('dark_pool_pct', 0):.1f}%** dark pool" if uoa.get('dark_pool_pct') else "N/A", "inline": True},
        {"name": "🧱 Block Trades",   "value": f"{uoa.get('block_count', 0)} large prints" if uoa.get('block_count') else "N/A", "inline": True},
        {"name": "📐 Delta",          "value": f"{uoa.get('delta', 0):.2f}" if uoa.get('delta') else "N/A", "inline": True},
        {"name": "📍 Entry",          "value": _entry_note,                                       "inline": True},
        {"name": "🎯 Target",         "value": _target_note,                                      "inline": True},
        {"name": "🛑 Stop",           "value": _stop_note,                                        "inline": True},
        {"name": "🎯 ARKA Recommendation",
         "value": f"{rec['action']}\nConfidence: **{rec['confidence']}%**\n\n{reason_text}",
         "inline": False},
    ]

    payload = {"embeds": [{
        "title":       tier_title,
        "color":       dir_color,
        "description": f"**{uoa['ratio']}x** unusual activity on **{ticker}** {ct} at {now}",
        "fields":      fields,
        "footer":      {"text": _footer_text},
        "timestamp":   datetime.utcnow().isoformat() + "Z",
    }]}

    if tier == "MEGA":
        payload["content"] = f"🔥⚡ **EXTREME** options flow on **{ticker}** {ct} — {uoa['ratio']}x normal — {rec['action']}"

    try:
        _uoa_ratio = float(uoa.get("ratio", 0))
        is_extreme = (tier == "MEGA") or (_uoa_ratio >= UOA_MEGA)
        # ── Quality gate — cooldown key = ticker+direction+date (no strike) ──
        # Strike-based keys let SPY bypass cooldown every $1 move while trapping other tickers
        _cd_key = f"{ticker}_{ct}_{str(date.today())}"
        if not _is_global and not _passes_quality_gate(ticker, rec, is_extreme=is_extreme,
                                                        cooldown_key=_cd_key):
            return False

        # ── Smart channel routing ─────────────────────────────────────────────
        from zoneinfo import ZoneInfo as _ZI
        _now_et   = datetime.now(_ZI("America/New_York"))
        _is_lotto = (_now_et.hour == 15 and _now_et.minute >= 30) and not _is_global
        _is_idx   = ticker.upper() in {"SPY", "QQQ", "IWM", "DIA", "SPX"}

        _CH_SCALP_EXT  = os.getenv("DISCORD_ARKA_SCALP_EXTREME","")
        _CH_SCALP_SIG  = os.getenv("DISCORD_ARKA_SCALP_SIGNALS","")
        _CH_SWING_EXT  = os.getenv("DISCORD_ARKA_SWINGS_EXTREME","")
        _CH_SWING_SIG  = os.getenv("DISCORD_ARKA_SWINGS_SIGNALS","")
        _CH_LOTTO      = os.getenv("DISCORD_ARKA_LOTTO","")

        if _is_lotto:
            _url = _CH_LOTTO or DISCORD_ALERTS
        elif ticker.upper() == "SPX" and DISCORD_SPX_HOOK:
            _url = DISCORD_SPX_HOOK
        elif _is_idx and is_extreme:
            _url = _CH_SCALP_EXT or DISCORD_FLOW_EXTREME or DISCORD_ALERTS
        elif _is_idx:
            _url = _CH_SCALP_SIG or DISCORD_FLOW_SIGNALS or DISCORD_ALERTS
        elif is_extreme:
            _url = _CH_SWING_EXT or DISCORD_FLOW_EXTREME or DISCORD_ALERTS
        else:
            _url = _CH_SWING_SIG or DISCORD_FLOW_SIGNALS or DISCORD_ALERTS
        if not _url: _url = DISCORD_ALERTS
        r = requests.post(_url, json=payload, timeout=8)
        # Also mirror SPX alerts to SPX-only channel
        if ticker.upper() == "SPX" and DISCORD_SPX_HOOK and _url != DISCORD_SPX_HOOK:
            requests.post(DISCORD_SPX_HOOK, json=payload, timeout=8)
        # Cache flow signal for ARKA engine (enriched with direction/premium/tier)
        try:
            _bias   = rec.get("direction", "NEUTRAL")
            _conf   = int(rec.get("confidence", 65))
            _ratio  = float(uoa.get("ratio", 0))
            _xtreme = _ratio >= 50 or tier == "MEGA"
            _prem   = float(uoa.get("premium", 0))
            _strike = float(uoa.get("strike", 0))
            _dte_   = int(uoa.get("dte", 0) if "dte" in uoa else 0)
            _dir    = uoa.get("type", "call").upper()  # "CALL" or "PUT"
            _write_flow_signal_cache(ticker, _bias, _conf, _ratio, _xtreme, 0.0,
                                     direction=_dir, premium=_prem,
                                     tier=tier, strike=_strike, dte=_dte_)
        except Exception:
            pass
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord post error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# 4. DEDUP
# ══════════════════════════════════════════════════════════════════════

def load_seen() -> dict:
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
        if data.get("date") != date.today().isoformat():
            return {"date": date.today().isoformat(), "seen": []}
        return data
    except Exception:
        return {"date": date.today().isoformat(), "seen": []}


def save_seen(seen: dict):
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SEEN_FILE, "w") as f:
            json.dump(seen, f)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 5. MAIN SCAN
# ══════════════════════════════════════════════════════════════════════

def _write_flow_signal_cache(ticker: str, bias: str, confidence: int,
                               vol_oi_ratio: float, is_extreme: bool,
                               dark_pool_pct: float = 0,
                               direction: str = "",
                               premium: float = 0,
                               tier: str = "",
                               strike: float = 0,
                               dte: int = 0):
    """Write latest flow signal to JSON cache for ARKA to read."""
    import json as _j
    from pathlib import Path as _P
    cache_path = _P("logs/chakra/flow_signals_latest.json")
    try:
        existing = {}
        if cache_path.exists():
            try:
                existing = _j.loads(cache_path.read_text())
            except Exception:
                existing = {}

        entry = {
            "bias":          bias,
            "confidence":    confidence,
            "vol_oi_ratio":  vol_oi_ratio,
            "is_extreme":    is_extreme,
            "dark_pool_pct": dark_pool_pct,
            "timestamp":     datetime.now().isoformat(),
            "source":        "flow_monitor",
        }
        # Enrich with extra fields when available
        if direction: entry["direction"] = direction
        if premium:   entry["premium"]   = round(premium, 0)
        if tier:      entry["tier"]       = tier
        if strike:    entry["strike"]     = strike
        if dte:       entry["dte"]        = dte

        existing[ticker] = entry
        cache_path.write_text(_j.dumps(existing, indent=2))
    except Exception as _e:
        log.warning(f"Could not write flow signal cache: {_e}")


def _fetch_live_spot(ticker: str) -> float:
    """Fetch live spot price from Polygon snapshot. Returns 0.0 on failure."""
    try:
        _r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=6,
        )
        if _r.status_code == 200:
            _d = _r.json()
            return float(_d.get("ticker", {}).get("day", {}).get("c", 0) or
                         _d.get("ticker", {}).get("prevDay", {}).get("c", 0) or 0)
    except Exception:
        pass
    return 0.0


def _get_swing_watchlist_tickers() -> list:
    """Return tickers from the swing screener watchlist_latest.json."""
    import json as _j
    from pathlib import Path as _P
    try:
        _wl = _P("logs/chakra/watchlist_latest.json")
        if _wl.exists():
            _d = _j.loads(_wl.read_text())
            _cands = _d.get("candidates", [])
            return [c.get("ticker", c) if isinstance(c, dict) else str(c)
                    for c in _cands if c]
    except Exception:
        pass
    return []


def _check_gamma_flip():
    """
    Check if index/swing tickers crossed zero_gamma vs last scan.
    - Index (SPY/QQQ/SPX): verify spot via live Polygon, post on any crossing
    - Swing tickers: post when within $2 of zero_gamma (proximity alert)
    - SPX fallback: uses SPY * 10 when gex_latest_SPX.json unavailable
    - GEX TTL: 600s (10 min)
    - Posts to DISCORD_GAMMA_FLIP_WEBHOOK with 60-min per-ticker cooldown.
    State stored in logs/gex/gamma_flip_state.json.
    """
    import json as _j
    from pathlib import Path as _P

    if not _is_market_hours():
        return

    _state_path = _P("logs/gex/gamma_flip_state.json")
    try:
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        _prev = _j.loads(_state_path.read_text()) if _state_path.exists() else {}
    except Exception:
        _prev = {}

    _flip_url  = os.getenv("DISCORD_ARKA_SCALP_EXTREME", os.getenv("DISCORD_ALERTS", ""))
    _gf_hook   = os.getenv("DISCORD_GAMMA_FLIP_WEBHOOK", _flip_url)
    _now_ts    = time.time()
    _new_state = dict(_prev)

    # ── Build ticker list: indices + swing watchlist ──────────────────
    _index_tickers = ["SPY", "QQQ", "SPX"]
    _swing_tickers = _get_swing_watchlist_tickers()
    _all_flip_tickers = list(dict.fromkeys(_index_tickers + _swing_tickers))

    for _fticker in _all_flip_tickers:
        _gex_file = _P(f"logs/gex/gex_latest_{_fticker}.json")
        _is_index = _fticker in _index_tickers

        try:
            # ── Load GEX state ────────────────────────────────────────
            if _gex_file.exists():
                _gd = _j.loads(_gex_file.read_text())
                _zero_gam = float(_gd.get("zero_gamma", 0))
                _gex_ts   = float(_gd.get("ts", 0))
                _gex_spot = float(_gd.get("spot", 0))
            elif _fticker == "SPX":
                # SPX fallback: derive from SPY GEX * 10
                _spy_file = _P("logs/gex/gex_latest_SPY.json")
                if not _spy_file.exists():
                    continue
                _spy_gd   = _j.loads(_spy_file.read_text())
                _zero_gam = float(_spy_gd.get("zero_gamma", 0)) * 10
                _gex_ts   = float(_spy_gd.get("ts", 0))
                _gex_spot = float(_spy_gd.get("spot", 0)) * 10
            else:
                continue  # no GEX data for this ticker

            if not _zero_gam:
                continue

            # TTL check — GEX data must be fresh (< 10 min)
            if _now_ts - _gex_ts > 600:
                log.debug(f"  Gamma flip {_fticker}: GEX stale ({int(_now_ts-_gex_ts)}s old), skipping")
                continue

            # ── Get live spot (verify, don't trust stale GEX spot) ───
            if _fticker == "SPX":
                _live_spy = _fetch_live_spot("SPY")
                _spot = (_live_spy * 10) if _live_spy > 0 else _gex_spot
            else:
                _live = _fetch_live_spot(_fticker)
                _spot = _live if _live > 0 else _gex_spot

            if not _spot:
                continue

            _above      = _spot > _zero_gam
            _prev_above = _prev.get(f"{_fticker}_above")
            _last_flip  = float(_prev.get(f"{_fticker}_last_flip", 0))
            _new_state[f"{_fticker}_above"] = _above

            # ── Cooldown check ────────────────────────────────────────
            if _now_ts - _last_flip < 3600:
                log.debug(f"  ⚡ GAMMA FLIP {_fticker} — cooldown ({int(3600-(_now_ts-_last_flip))}s left)")
                continue

            if _is_index:
                # Index: alert on any zero_gamma crossing — BOTH directions
                if _prev_above is None:
                    continue  # first reading — save state, no alert
                if _above == _prev_above:
                    continue  # no flip
                _dist = abs(_spot - _zero_gam)
                if _above:
                    # Was BELOW, now ABOVE = BULLISH FLIP
                    _flip_dir   = "BULLISH"
                    _flip_emoji = "📈⚡"
                    _flip_color = 0x00FF88
                    _action     = "BUY CALLS — dealers now amplifying UPSIDE moves"
                    _msg = (f"📈⚡ **BULLISH GAMMA FLIP** — **{_fticker}** crossed "
                            f"ABOVE ${_zero_gam:.2f} zero gamma (spot ${_spot:.2f})\n"
                            f"→ **BUY CALLS** — dealers amplify UP moves")
                else:
                    # Was ABOVE, now BELOW = BEARISH FLIP
                    _flip_dir   = "BEARISH"
                    _flip_emoji = "📉⚡"
                    _flip_color = 0xFF4444
                    _action     = "BUY PUTS — dealers now amplifying DOWNSIDE moves"
                    _msg = (f"📉⚡ **BEARISH GAMMA FLIP** — **{_fticker}** crossed "
                            f"BELOW ${_zero_gam:.2f} zero gamma (spot ${_spot:.2f})\n"
                            f"→ **BUY PUTS** — dealers amplify DOWN moves")
                _payload = {
                    "content": _msg,
                    "embeds": [{
                        "title":  f"{_flip_emoji} GAMMA FLIP — {_fticker}",
                        "color":  _flip_color,
                        "fields": [
                            {"name": "Direction",  "value": _flip_dir,           "inline": True},
                            {"name": "Zero Gamma", "value": f"${_zero_gam:.2f}", "inline": True},
                            {"name": "Spot",       "value": f"${_spot:.2f}",     "inline": True},
                            {"name": "Signal",     "value": _action,             "inline": False},
                        ],
                        "footer": {"text": f"CHAKRA Gamma Flip • {_fticker} • "
                                           f"{datetime.now().strftime('%I:%M %p ET')}"},
                    }],
                }
                log.info(f"  ⚡ GAMMA FLIP {_flip_dir}: {_fticker} "
                         f"zero_gamma=${_zero_gam:.2f} spot=${_spot:.2f}")
            else:
                # Swing stock: alert when approaching zero_gamma within $2
                _dist = abs(_spot - _zero_gam)
                if _dist > 2.0:
                    continue
                _direction = "ABOVE" if _above else "BELOW"
                _action    = "📈 **BUY CALLS** — above zero gamma" if _above \
                             else "📉 **BUY PUTS** — below zero gamma"
                _msg = (f"🎯 **GAMMA ZONE** — **{_fticker}** within ${_dist:.2f} of zero gamma "
                        f"${_zero_gam:.2f} (**{_direction}** | spot ${_spot:.2f})\n"
                        f"{_action}")
                _payload = {"content": _msg}
                log.info(f"  🎯 GAMMA ZONE: {_fticker} within ${_dist:.2f} of zero_gamma=${_zero_gam:.2f}")

            if _gf_hook:
                try:
                    requests.post(_gf_hook, json=_payload, timeout=8)
                    _new_state[f"{_fticker}_last_flip"] = _now_ts
                except Exception as _fe:
                    log.error(f"  Gamma flip post failed {_fticker}: {_fe}")

        except Exception as _ge:
            log.debug(f"  Gamma flip check {_fticker}: {_ge}")

    try:
        _state_path.write_text(_j.dumps(_new_state, indent=2))
    except Exception:
        pass


def run_flow_scan():
    # Skip scan entirely outside market hours — saves API calls and prevents
    # edge-case Discord leaks. Always-on tickers (SPY/QQQ/SPX) still fire
    # through their own gates inside post_dark_pool_alert / post_uoa_alert.
    if not _is_market_hours():
        log.info("Flow scan skipped — outside market hours (9:30–4:00 ET)")
        return

    seen   = load_seen()
    alerts = 0
    now_str = datetime.now().strftime("%H:%M")
    log.info(f"Flow scan — {now_str}")
    _check_gamma_flip()

    for ticker in get_dynamic_universe():
        # ── Dark Pool ─────────────────────────────────────────────────
        dp = fetch_dark_pool(ticker)
        if "error" not in dp:
            tier = dp.get("agg_tier", "NORMAL")
            pct  = dp.get("dark_pct", 0)

            log.info(f"  {ticker} dark pool: {pct}% [{tier}] "
                     f"{len(dp.get('blocks',[]))} large prints")

            # Only alert on HIGH or EXTREME (ELEVATED is too noisy)
            if tier in ("HIGH", "EXTREME"):
                key = f"dp_{ticker}_{tier}_{now_str[:4]}0"  # dedupe per 10min window
                if key not in seen.get("seen", []):
                    uoa_items = fetch_uoa(ticker)
                    rec = get_recommendation(ticker, dp, uoa_items)
                    posted = post_dark_pool_alert(ticker, dp, rec)
                    if posted:
                        seen.setdefault("seen", []).append(key)
                        alerts += 1
                        log.info(f"  ✅ Dark pool alert posted: {ticker} {tier} — {rec['direction']}")
                    time.sleep(1)

        # ── UOA ───────────────────────────────────────────────────────
        uoa_items = fetch_uoa(ticker)
        log.info(f"  {ticker} UOA: {len(uoa_items)} unusual contracts")

        if not uoa_items:
            continue

        # ── Step 1: Compute aggregate call/put flow balance ───────────
        call_flow = sum(u["ratio"] * u["volume"] for u in uoa_items if u["type"] == "call")
        put_flow  = sum(u["ratio"] * u["volume"] for u in uoa_items if u["type"] == "put")
        total_flow = call_flow + put_flow

        if total_flow == 0:
            continue

        call_pct = call_flow / total_flow
        put_pct  = put_flow  / total_flow

        # ── Step 2: Only alert if one side is clearly dominant ─────────
        # Indexes: 65% dominance required, Stocks: 75% (tighter filter)
        _is_idx2 = ticker in {"SPY","QQQ","IWM","DIA","SPX"}
        DOMINANCE_THRESHOLD = 0.65 if _is_idx2 else 0.75
        if call_pct >= DOMINANCE_THRESHOLD:
            dominant_side = "call"
            dominant_pct  = call_pct
        elif put_pct >= DOMINANCE_THRESHOLD:
            dominant_side = "put"
            dominant_pct  = put_pct
        else:
            # Mixed flow — log only, no Discord post (no directional edge)
            log.info(f"  {ticker} UOA mixed flow — calls {call_pct*100:.0f}% / puts {put_pct*100:.0f}% — skipped")
            continue

        # ── Step 3: Filter to dominant side only ──────────────────────
        dominant_contracts = [u for u in uoa_items if u["type"] == dominant_side]
        dominant_contracts.sort(key=lambda x: -x["ratio"])

        log.info(f"  {ticker} UOA dominant={dominant_side.upper()} "
                 f"({dominant_pct*100:.0f}%) — {len(dominant_contracts)} contracts")

        # ── Step 4: One alert per ticker per scan (use top contract) ──
        ticker_key = f"uoa_{ticker}_{dominant_side}_{now_str[:4]}0"
        if ticker_key in seen.get("seen", []):
            log.info(f"  {ticker} UOA already alerted this window — skip")
            continue

        # Use top contract for the alert, but pass ALL dominant contracts for recommendation
        top_uoa = dominant_contracts[0]
        ratio   = top_uoa["ratio"]

        # Classify tier by top contract ratio
        if ratio >= UOA_MEGA:
            tier, emoji, label = "MEGA",  "🔥⚡", "EXTREME Flow"
        elif ratio >= UOA_WHALE:
            tier, emoji, label = "WHALE", "⚡",   "Whale Sweep"
        else:
            tier, emoji, label = "LARGE", "📊",   "Unusual Activity"

        # Recommendation uses full dominant-side picture
        dp_ctx = dp if "error" not in dp else {"ticker": ticker, "dark_pct": 0, "blocks": [], "agg_tier": "NORMAL"}
        rec    = get_recommendation(ticker, dp_ctx, dominant_contracts)

        # Enhance top_uoa with aggregate context
        top_uoa["agg_contracts"] = len(dominant_contracts)
        top_uoa["agg_pct"]       = round(dominant_pct * 100, 1)
        top_uoa["dark_pool_pct"] = dp_ctx.get("dark_pct", 0)
        top_uoa["block_count"]   = len(dp_ctx.get("blocks", []))
        top_uoa["delta"]         = top_uoa.get("delta", top_uoa.get("greeks", {}).get("delta", 0))

        posted = post_uoa_alert(ticker, top_uoa, tier, emoji, label, rec)
        if posted:
            seen.setdefault("seen", []).append(ticker_key)
            alerts += 1
            log.info(f"  ✅ UOA alert: {ticker} {dominant_side.upper()} "
                     f"{dominant_pct*100:.0f}% dominant | top={ratio}x")
        time.sleep(1)

    save_seen(seen)
    log.info(f"Flow scan complete — {alerts} alert(s) posted")
    return alerts


# ══════════════════════════════════════════════════════════════════════
# 6. TEST
# ══════════════════════════════════════════════════════════════════════

def _run_test():
    log.info("TEST MODE — posting mock alerts to Discord...")

    mock_dp = {
        "ticker": "SPY", "total_vol": 45_000_000,
        "dark_vol": 28_000_000, "dark_value": 18_760_000_000,
        "dark_pct": 62.2, "agg_tier": "EXTREME", "agg_emoji": "🔥",
        "trade_count": 500,
        "blocks": [
            {"size": 85000, "price": 671.50, "value": 57_077_500, "side": "buy"},
            {"size": 42000, "price": 671.20, "value": 28_190_400, "side": "buy"},
            {"size": 31000, "price": 671.80, "value": 20_825_800, "side": "sell"},
        ]
    }
    mock_rec = {
        "direction": "BULLISH", "confidence": 78,
        "action": "📈 **BUY CALL** on SPY", "color": 0x00FF9D,
        "reasons": [
            "Dark pool 62% of volume — buy side dominant ($85M)",
            "Whale block BUY $57M at $671.50",
            "Options flow 68% calls — bullish sweep",
        ]
    }
    post_dark_pool_alert("SPY", mock_dp, mock_rec)
    time.sleep(2)

    mock_uoa = {
        "contract": "O:QQQ260309C00520000", "type": "call",
        "strike": 520, "expiry": "2026-03-09",
        "volume": 8500, "oi": 310, "ratio": 27.4,
        "mark": 2.15, "premium": 1_827_500, "iv": 31.2
    }
    mock_rec2 = {
        "direction": "BULLISH", "confidence": 71,
        "action": "📈 **BUY CALL** on QQQ", "color": 0x00FF9D,
        "reasons": ["Whale call sweep 27x normal volume at $520 strike"]
    }
    post_uoa_alert("QQQ", mock_uoa, "WHALE", "⚡", "Whale Sweep", mock_rec2)
    log.info("✅ Test alerts posted — check #alerts in Discord")


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CHAKRA Flow Monitor v2")
    parser.add_argument("--watch", action="store_true", help="Run every 5 min")
    parser.add_argument("--test",  action="store_true", help="Post mock alerts")
    args = parser.parse_args()

    if args.test:
        _run_test()
        sys.exit(0)

    if args.watch:
        log.info("Flow monitor watching — every 5 min...")
        while True:
            try:
                run_flow_scan()
            except Exception as e:
                log.error(f"Scan error: {e}")
            try:
                from backend.chakra.divergence_scanner import run_divergence_scan
                run_divergence_scan()
            except Exception as _de:
                log.debug(f"Divergence scan error: {_de}")
            time.sleep(300)
    else:
        run_flow_scan()
