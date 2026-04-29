"""
CHAKRA Swings Engine v2.0 — Multi-day swing trades
backend/chakra/chakra_swings_engine.py

Entry gates:
  ✅ ARJUN signal = BUY
  ✅ Conviction >= 60
  ✅ Bull score > Bear score
  ✅ GEX != NEGATIVE_GAMMA
  ✅ Risk manager != BLOCK
  ✅ Risk/reward >= 1.5
  ✅ No existing position in ticker

Hold rules:
  📏 EMA 20 daily trailing stop (recalculated each morning)
  ⏱  Max hold = 5 trading days
  🔄 Stop trails up as price rises, never moves down

Exit triggers:
  🔴 Price hits stop loss
  ✅ Price hits target
  🐻 Bear score > 55 (ARJUN turns bearish)
  ⚡ GEX flips to NEGATIVE_GAMMA
  📅 Max hold days reached (5)
  🛑 Manual: python3 chakra_swings_engine.py --close TICKER

Usage:
  python3 backend/chakra/chakra_swings_engine.py            # run entry scan
  python3 backend/chakra/chakra_swings_engine.py --monitor  # check open positions
  python3 backend/chakra/chakra_swings_engine.py --status   # print open trades
  python3 backend/chakra/chakra_swings_engine.py --close SPY # force close

Crons:
  # Entry scan — after ARJUN runs at 8am
  15 8 * * 1-5 cd $HOME/trading-ai && venv/bin/python3 backend/chakra/chakra_swings_engine.py >> logs/swings/swings.log 2>&1

  # Position monitor — every 30 min during market hours
  */30 9-16 * * 1-5 cd $HOME/trading-ai && venv/bin/python3 backend/chakra/chakra_swings_engine.py --monitor >> logs/swings/swings.log 2>&1
"""

import os, sys, json, glob, sqlite3, asyncio, logging, httpx, requests
from datetime import datetime, date, timedelta
from pathlib import Path
from dotenv import load_dotenv

# ── Setup ──────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [SWINGS] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('chakra_swings')

# ── Config ─────────────────────────────────────────────────────────────
ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET   = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE     = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
POLYGON_KEY     = os.getenv("POLYGON_API_KEY", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_TRADES_WEBHOOK", "") or os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_HEALTH  = os.getenv("DISCORD_HEALTH_WEBHOOK", "")

ALPACA_HEADERS  = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

# ── Trading params ─────────────────────────────────────────────────────
RISK_PCT         = 0.02      # 2% account risk per swing trade
MAX_HOLD_DAYS    = 5         # max trading days to hold
CONVICTION_MIN   = 52        # minimum ARJUN conviction
BEAR_EXIT_SCORE  = 65        # exit if bear score exceeds this
RR_MIN           = 1.0       # minimum risk/reward ratio
MAX_POSITIONS    = 3         # max concurrent swing positions
TRAIL_BUFFER_PCT = 0.005     # 0.5% buffer below EMA20 for trailing stop

DB_PATH = BASE / "logs" / "swings" / "swings.db"
LOG_DIR = BASE / "logs" / "swings"


# ══════════════════════════════════════════════════════════════════════
# 1. DATABASE
# ══════════════════════════════════════════════════════════════════════

def init_db():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker       TEXT NOT NULL,
            entry_date   TEXT NOT NULL,
            entry_price  REAL NOT NULL,
            target       REAL,
            stop         REAL,
            trail_stop   REAL,
            shares       INTEGER,
            confidence   REAL,
            bull_score   REAL,
            bear_score   REAL,
            gex_regime   TEXT,
            rr_ratio     REAL,
            status       TEXT DEFAULT 'OPEN',
            hold_days    INTEGER DEFAULT 0,
            exit_date    TEXT,
            exit_price   REAL,
            exit_reason  TEXT,
            pnl          REAL,
            pnl_pct      REAL,
            outcome      TEXT,
            signal_file  TEXT,
            created_at   TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"DB ready: {DB_PATH}")


def get_open_positions() -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM swings WHERE status='OPEN'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_position(ticker: str) -> dict | None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM swings WHERE ticker=? AND status='OPEN'", (ticker,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def open_position(ticker, entry_price, target, stop, shares, conf,
                   bull, bear, gex, rr, signal_file):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO swings
        (ticker,entry_date,entry_price,target,stop,trail_stop,shares,
         confidence,bull_score,bear_score,gex_regime,rr_ratio,
         signal_file,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ticker, date.today().isoformat(), entry_price,
        target, stop, stop,  # trail_stop starts = stop
        shares, conf, bull, bear, gex, rr,
        signal_file, datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()


def close_position(ticker: str, exit_price: float, reason: str):
    pos = get_position(ticker)
    if not pos:
        return
    entry   = pos["entry_price"]
    shares  = pos["shares"]
    pnl     = round((exit_price - entry) * shares, 2)
    pnl_pct = round((exit_price - entry) / entry * 100, 2)
    outcome = "WIN" if pnl > 0 else "LOSS"

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        UPDATE swings SET
            status='CLOSED', exit_date=?, exit_price=?,
            exit_reason=?, pnl=?, pnl_pct=?, outcome=?
        WHERE ticker=? AND status='OPEN'
    """, (
        date.today().isoformat(), exit_price,
        reason, pnl, pnl_pct, outcome, ticker
    ))
    conn.commit()
    conn.close()
    log.info(f"Position closed: {ticker} {outcome} P&L=${pnl:.2f} ({pnl_pct:.1f}%)")
    return pnl, pnl_pct, outcome


def update_trail_stop(ticker: str, new_stop: float):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE swings SET trail_stop=? WHERE ticker=? AND status='OPEN'",
        (new_stop, ticker)
    )
    conn.commit()
    conn.close()


def increment_hold_days():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE swings SET hold_days=hold_days+1 WHERE status='OPEN'")
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════
# 2. SIGNAL LOADER
# ══════════════════════════════════════════════════════════════════════

def load_latest_signals() -> dict:
    """Load most recent ARJUN signals from logs/signals/."""
    signals     = {}
    signal_dir  = BASE / "logs" / "signals"
    files       = sorted(signal_dir.glob("*.json"), reverse=True)

    # Load signals from last 2 files (today's runs)
    for f in files[:4]:
        try:
            data = json.loads(f.read_text())
            if not isinstance(data, list):
                data = [data]
            for s in data:
                sym = s.get("ticker", "")
                if sym and sym not in signals:
                    signals[sym] = {**s, "_file": str(f)}
        except Exception as e:
            log.warning(f"Could not load {f}: {e}")

    log.info(f"Loaded {len(signals)} signals from {len(files[:2])} files")
    return signals


# ══════════════════════════════════════════════════════════════════════
# 3. PRICE + EMA FETCHER
# ══════════════════════════════════════════════════════════════════════

def get_current_price(ticker: str) -> float:
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY}, timeout=8
        )
        t = r.json().get("ticker", {})
        price = (t.get("lastTrade", {}).get("p") or
                 t.get("day", {}).get("c") or
                 t.get("prevDay", {}).get("c") or 0)
        return float(price)
    except Exception as e:
        log.warning(f"Price fetch error {ticker}: {e}")
        return 0.0


def get_ema20_daily(ticker: str) -> float:
    """Calculate EMA20 from last 30 daily bars."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=45)).isoformat()
        r     = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 50},
            timeout=10
        )
        bars   = r.json().get("results", [])
        closes = [b["c"] for b in bars]
        if len(closes) < 20:
            return 0.0

        # Calculate EMA20
        k   = 2 / (20 + 1)
        ema = sum(closes[:20]) / 20  # SMA seed
        for c in closes[20:]:
            ema = c * k + ema * (1 - k)
        return round(ema, 2)
    except Exception as e:
        log.warning(f"EMA20 fetch error {ticker}: {e}")
        return 0.0


def get_account_equity() -> float:
    try:
        r = httpx.get(
            f"{ALPACA_BASE}/v2/account",
            headers=ALPACA_HEADERS, timeout=8
        )
        return float(r.json().get("equity", 100_000))
    except Exception:
        return 100_000.0


# ══════════════════════════════════════════════════════════════════════
# 4. ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════

def place_order(ticker: str, shares: int, side: str = "buy") -> dict:
    # HARD FREEZE: chakra_swings_engine is the OLD equity engine — all orders permanently blocked.
    # Use backend/arka/arka_swings.py for options-only swing trading.
    log.error(f"  🛡️  HARD BLOCK: chakra_swings_engine.place_order called for {ticker} x{shares} — "
              f"equity trading is permanently disabled. CHAKRA trades OPTIONS ONLY.")
    return {"success": False, "blocked": True,
            "error": "HARD BLOCK: equity orders disabled — use arka_swings.py options path"}
    # --- DEAD CODE BELOW — kept for reference only ---
    try:
        r = httpx.post(
            f"{ALPACA_BASE}/v2/orders",
            headers=ALPACA_HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares),
                "side":          side,
                "type":          "market",
                "time_in_force": "day",
            },
            timeout=10
        )
        if r.status_code in (200, 201):
            order = r.json()
            log.info(f"Order placed: {side.upper()} {shares} {ticker} → {order.get('id')}")
            return {"success": True, "order": order}
        else:
            log.error(f"Order failed: {r.status_code} {r.text}")
            return {"success": False, "error": r.text}
    except Exception as e:
        log.error(f"Order exception: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════
# 5. DISCORD NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════

def post_entry_alert(ticker: str, price: float, target: float, stop: float,
                      shares: int, conf: float, bull: float, bear: float,
                      rr: float, gex: str, signal: dict):
    if not DISCORD_WEBHOOK:
        return
    pct_to_target = round((target - price) / price * 100, 1)
    pct_stop      = round((price - stop) / price * 100, 1)
    cost          = round(price * shares, 2)

    embed = {
        "title":       f"🏹 CHAKRA SWING ENTRY — {ticker}",
        "color":       0x00D084,
        "description": (f"Multi-day swing opened on **{ticker}**\n"
                        f"ARJUN conviction **{conf:.0f}%** | GEX: {gex}"),
        "fields": [
            {"name": "💵 Entry",     "value": f"**${price:.2f}**",           "inline": True},
            {"name": "🎯 Target",    "value": f"**${target:.2f}** (+{pct_to_target}%)", "inline": True},
            {"name": "🛑 Stop",      "value": f"**${stop:.2f}** (-{pct_stop}%)",        "inline": True},
            {"name": "📊 Shares",    "value": f"{shares:,}",                  "inline": True},
            {"name": "💰 Cost",      "value": f"${cost:,.2f}",                "inline": True},
            {"name": "⚖️ R/R",       "value": f"1:{rr:.1f}",                  "inline": True},
            {"name": "🐂 Bull Score","value": f"{bull:.0f}/100",              "inline": True},
            {"name": "🐻 Bear Score","value": f"{bear:.0f}/100",              "inline": True},
            {"name": "⏱️ Max Hold",  "value": f"{MAX_HOLD_DAYS} days",        "inline": True},
            {"name": "📋 Strategy",
             "value": "EMA20 trailing stop · Exit on bear reversal or GEX flip",
             "inline": False},
            {"name": "📅 Entry Date",  "value": datetime.now().strftime("%b %d, %Y"),  "inline": True},
            {"name": "⏳ Expected Hold", "value": f"3–{MAX_HOLD_DAYS} trading days",    "inline": True},
            {"name": "📊 Signal",       "value": signal.get("signal", "HOLD") + " @ " + str(round(signal.get("confidence", conf), 1)) + "%", "inline": True},
        ],
        "footer":    {"text": f"CHAKRA Swings Engine • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=8)
        log.info(f"Entry alert posted: {ticker}")
    except Exception as e:
        log.warning(f"Discord error: {e}")


def post_exit_alert(ticker: str, entry: float, exit_price: float,
                     shares: int, pnl: float, pnl_pct: float,
                     reason: str, outcome: str, hold_days: int):
    if not DISCORD_WEBHOOK:
        return

    color    = 0x00FF9D if outcome == "WIN" else 0xFF2D55
    emoji    = "✅" if outcome == "WIN" else "❌"
    pnl_sign = "+" if pnl >= 0 else ""

    embed = {
        "title":       f"{emoji} CHAKRA SWING EXIT — {ticker} {outcome}",
        "color":       color,
        "description": f"**{outcome}** | {reason}",
        "fields": [
            {"name": "📥 Entry",      "value": f"${entry:.2f}",              "inline": True},
            {"name": "📤 Exit",       "value": f"${exit_price:.2f}",         "inline": True},
            {"name": "💵 P&L",        "value": f"**{pnl_sign}${pnl:.2f}** ({pnl_sign}{pnl_pct:.1f}%)", "inline": True},
            {"name": "📊 Shares",     "value": f"{shares:,}",                "inline": True},
            {"name": "📅 Hold Days",  "value": f"{hold_days} days",          "inline": True},
            {"name": "🚪 Exit Reason","value": reason,                       "inline": True},
        ],
        "footer":    {"text": f"CHAKRA Swings Engine • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=8)
        log.info(f"Exit alert posted: {ticker} {outcome} P&L=${pnl:.2f}")
    except Exception as e:
        log.warning(f"Discord error: {e}")


def post_trail_update(ticker: str, old_stop: float, new_stop: float, price: float):
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content":
            f"📏 **{ticker}** trailing stop raised: "
            f"${old_stop:.2f} → **${new_stop:.2f}** | Price: ${price:.2f}"
        }, timeout=8)
    except Exception:
        pass


def post_status_summary(positions: list[dict]):
    """Post open positions summary to Discord."""
    if not DISCORD_WEBHOOK or not positions:
        return

    lines = []
    total_pnl = 0
    for p in positions:
        price   = get_current_price(p["ticker"])
        unreal  = round((price - p["entry_price"]) * p["shares"], 2)
        total_pnl += unreal
        sign    = "+" if unreal >= 0 else ""
        lines.append(
            f"**{p['ticker']}** | Entry ${p['entry_price']:.2f} → ${price:.2f} | "
            f"P&L: {sign}${unreal:.2f} | Stop: ${p['trail_stop']:.2f} | "
            f"Day {p['hold_days']}/{MAX_HOLD_DAYS}"
        )

    sign = "+" if total_pnl >= 0 else ""
    embed = {
        "title":       f"📊 CHAKRA Swings — {len(positions)} Open Position(s)",
        "color":       0x00D4FF,
        "description": "\n".join(lines),
        "footer":      {"text": f"Total Unrealized: {sign}${total_pnl:.2f} • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp":   datetime.utcnow().isoformat() + "Z",
    }
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=8)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# 6. ENTRY SCAN
# ══════════════════════════════════════════════════════════════════════

def run_entry_scan():
    """Scan ARJUN signals for swing entry opportunities."""
    init_db()
    signals   = load_latest_signals()
    open_pos  = get_open_positions()
    open_tickers = {p["ticker"] for p in open_pos}
    equity    = get_account_equity()

    log.info(f"Entry scan — equity=${equity:,.0f} | open={len(open_pos)}/{MAX_POSITIONS}")

    if len(open_pos) >= MAX_POSITIONS:
        log.info(f"Max positions reached ({MAX_POSITIONS}) — skipping entry scan")
        return

    entries = 0
    for ticker, sig in signals.items():

        # ── Gate 1: Already in position ──────────────────────────────
        if ticker in open_tickers:
            log.info(f"  {ticker}: already in position — skip")
            continue

        # ── Gate 2: Signal must be BUY ────────────────────────────────
        signal_type = sig.get("signal", "").upper()
        if signal_type != "BUY":
            log.info(f"  {ticker}: signal={signal_type} — skip")
            continue

        # ── Gate 3: Conviction threshold ─────────────────────────────
        conf = float(str(sig.get('confidence', 0)).replace('%',''))
        if conf < CONVICTION_MIN:
            log.info(f"  {ticker}: conviction={conf:.0f} < {CONVICTION_MIN} — skip")
            continue

        # ── Gate 4: Agent scores ──────────────────────────────────────
        agents     = sig.get("agents", {})
        bull_score = float(agents.get("bull", {}).get("score", 0))
        bear_score = float(agents.get("bear", {}).get("score", 100))

        if bear_score > bull_score:
            log.info(f"  {ticker}: bear({bear_score:.0f}) > bull({bull_score:.0f}) — skip")
            continue

        # ── Gate 5: Risk manager must not BLOCK ──────────────────────
        risk_decision = (agents.get('risk_manager', {}) or agents.get('risk', {})).get('decision', '')
        if "BLOCK" in str(risk_decision).upper():
            log.info(f"  {ticker}: risk manager BLOCK — skip")
            continue

        # ── Gate 6: GEX regime ────────────────────────────────────────
        gex_regime = sig.get("gex", {}).get("regime", "UNKNOWN")
        if gex_regime == "NEGATIVE_GAMMA":
            log.info(f"  {ticker}: GEX={gex_regime} — skip")
            continue

        # ── Gate 7: Risk/reward ───────────────────────────────────────
        rr = float(sig.get("risk_reward", 0))
        if rr < RR_MIN:
            log.info(f"  {ticker}: R/R={rr:.1f} < {RR_MIN} — skip")
            continue

        # ── All gates passed — calculate position size ─────────────────
        price  = get_current_price(ticker)
        if not price:
            log.warning(f"  {ticker}: could not get price — skip")
            continue

        stop   = float(sig.get("stop_loss", price * 0.97))
        target = float(sig.get("target_price", sig.get("target", price * 1.06)))
        risk   = price - stop
        if risk <= 0:
            continue

        dollar_risk = equity * RISK_PCT
        shares      = max(1, int(dollar_risk / risk))

        # Cap at 10% of equity per position
        max_shares = int(equity * 0.10 / price)
        shares     = min(shares, max_shares)

        log.info(
            f"  ✅ {ticker}: BUY {shares}sh @ ${price:.2f} "
            f"target=${target:.2f} stop=${stop:.2f} "
            f"conf={conf:.0f}% bull={bull_score:.0f} bear={bear_score:.0f} "
            f"R/R=1:{rr:.1f} GEX={gex_regime}"
        )

        # ── Place order ───────────────────────────────────────────────
        result = place_order(ticker, shares, "buy")
        if result["success"]:
            open_position(
                ticker, price, target, stop, shares,
                conf, bull_score, bear_score, gex_regime, rr,
                sig.get("_file", "")
            )
            post_entry_alert(
                ticker, price, target, stop, shares,
                conf, bull_score, bear_score, rr, gex_regime, sig
            )
            open_tickers.add(ticker)
            entries += 1

            if len(open_tickers) >= MAX_POSITIONS:
                log.info("Max positions reached — stopping entry scan")
                break
        else:
            log.error(f"  {ticker}: order failed — {result.get('error','')[:100]}")

    log.info(f"Entry scan complete — {entries} new position(s) opened")
    return entries


# ══════════════════════════════════════════════════════════════════════
# 7. POSITION MONITOR
# ══════════════════════════════════════════════════════════════════════

def run_monitor():
    """
    Check all open swing positions.
    Updates trailing stops, checks exit conditions.
    Run every 30 min during market hours.
    """
    init_db()
    positions = get_open_positions()

    if not positions:
        log.info("No open swing positions to monitor")
        return

    log.info(f"Monitoring {len(positions)} open position(s)...")
    increment_hold_days()

    # Load latest ARJUN signals for exit checks
    signals = load_latest_signals()

    for pos in positions:
        ticker    = pos["ticker"]
        entry     = pos["entry_price"]
        target    = pos["target"]
        stop      = pos["stop"]
        trail     = pos["trail_stop"]
        shares    = pos["shares"]
        hold_days = pos["hold_days"]

        price = get_current_price(ticker)
        if not price:
            log.warning(f"  {ticker}: could not get price — skipping")
            continue

        pnl = round((price - entry) * shares, 2)
        log.info(
            f"  {ticker}: price=${price:.2f} entry=${entry:.2f} "
            f"trail=${trail:.2f} target=${target:.2f} "
            f"P&L=${pnl:.2f} day={hold_days}/{MAX_HOLD_DAYS}"
        )

        exit_reason = None

        # ── Check 1: Stop loss hit ────────────────────────────────────
        if price <= trail:
            exit_reason = f"Stop loss hit (${price:.2f} ≤ ${trail:.2f})"

        # ── Check 2: Target hit ───────────────────────────────────────
        elif price >= target:
            exit_reason = f"Target reached (${price:.2f} ≥ ${target:.2f})"

        # ── Check 3: Max hold days ────────────────────────────────────
        elif hold_days >= MAX_HOLD_DAYS:
            exit_reason = f"Max hold days reached ({hold_days} days)"

        # ── Check 4: ARJUN turned bearish ────────────────────────────
        elif ticker in signals:
            sig        = signals[ticker]
            agents     = sig.get("agents", {})
            bear_now   = float(agents.get("bear", {}).get("score", 0))
            gex_now    = sig.get("gex", {}).get("regime", "")

            if bear_now > BEAR_EXIT_SCORE:
                exit_reason = f"ARJUN bear score {bear_now:.0f} > {BEAR_EXIT_SCORE} threshold"
            elif gex_now == "NEGATIVE_GAMMA":
                exit_reason = f"GEX flipped to NEGATIVE_GAMMA"

        # ── Exit if triggered ─────────────────────────────────────────
        if exit_reason:
            log.info(f"  {ticker}: EXIT — {exit_reason}")
            result = place_order(ticker, shares, "sell")
            if result["success"]:
                pnl_result = close_position(ticker, price, exit_reason)
                if pnl_result:
                    pnl_final, pnl_pct, outcome = pnl_result
                    post_exit_alert(
                        ticker, entry, price, shares,
                        pnl_final, pnl_pct, exit_reason, outcome, hold_days
                    )
            else:
                log.error(f"  {ticker}: sell order failed — {result.get('error','')[:100]}")
            continue

        # ── Update trailing stop ──────────────────────────────────────
        ema20 = get_ema20_daily(ticker)
        if ema20 > 0:
            new_trail = round(ema20 * (1 - TRAIL_BUFFER_PCT), 2)
            if new_trail > trail:  # trail only moves UP
                update_trail_stop(ticker, new_trail)
                post_trail_update(ticker, trail, new_trail, price)
                log.info(f"  {ticker}: trailing stop raised ${trail:.2f} → ${new_trail:.2f} (EMA20={ema20:.2f})")

    log.info("Monitor complete")


# ══════════════════════════════════════════════════════════════════════
# 8. STATUS REPORT
# ══════════════════════════════════════════════════════════════════════

def print_status():
    init_db()
    positions = get_open_positions()

    if not positions:
        print("\n✅ No open swing positions\n")
        return

    print(f"\n{'═'*70}")
    print(f"  CHAKRA SWINGS — {len(positions)} OPEN POSITION(S)")
    print(f"{'═'*70}")

    for p in positions:
        price    = get_current_price(p["ticker"])
        unreal   = round((price - p["entry_price"]) * p["shares"], 2) if price else 0
        sign     = "+" if unreal >= 0 else ""
        print(f"\n  {p['ticker']}")
        print(f"    Entry:      ${p['entry_price']:.2f} ({p['entry_date']})")
        print(f"    Current:    ${price:.2f}")
        print(f"    Trail Stop: ${p['trail_stop']:.2f}")
        print(f"    Target:     ${p['target']:.2f}")
        print(f"    Shares:     {p['shares']:,}")
        print(f"    Unrealized: {sign}${unreal:.2f}")
        print(f"    Hold Days:  {p['hold_days']}/{MAX_HOLD_DAYS}")

    print(f"\n{'═'*70}\n")
    post_status_summary(positions)


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CHAKRA Swings Engine v2.0")
    parser.add_argument("--monitor", action="store_true", help="Check open positions + trailing stops")
    parser.add_argument("--status",  action="store_true", help="Print open positions")
    parser.add_argument("--close",   type=str,            help="Force close a position: --close SPY")
    args = parser.parse_args()

    if args.status:
        print_status()

    elif args.monitor:
        run_monitor()

    elif args.close:
        ticker = args.close.upper()
        pos    = get_position(ticker)
        if not pos:
            print(f"No open position found for {ticker}")
            sys.exit(1)
        price = get_current_price(ticker)
        result = place_order(ticker, pos["shares"], "sell")
        if result["success"]:
            pnl_result = close_position(ticker, price, "Manual close")
            if pnl_result:
                pnl, pnl_pct, outcome = pnl_result
                post_exit_alert(
                    ticker, pos["entry_price"], price, pos["shares"],
                    pnl, pnl_pct, "Manual close", outcome, pos["hold_days"]
                )
            print(f"✅ {ticker} closed @ ${price:.2f}")
        else:
            print(f"❌ Close failed: {result.get('error')}")

    else:
        # Default: run entry scan
        run_entry_scan()
