import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[2]))
try:
    from backend.arka.discord_notifier import notify_chakra_swing_entry_sync
except ImportError:
    def notify_chakra_swing_entry_sync(*args, **kwargs): pass  # graceful fallback
"""
CHAKRA Swings Engine v3.0 — Stock Options, 2-3 Week Holds
==========================================================
Strategy:
  - Scans stocks via Polygon screener (volume + momentum)
  - Buys OPTIONS (calls/puts) on qualified stocks
  - Hold up to 15 trading days (3 weeks)
  - 1st TP at +30%, trail runners
  - Runner exit: sell when P&L drops 50% from peak
  - Max positions: 3

Entry gates:
  ✅ Polygon screener: price $5-$500, volume > 1M, momentum positive
  ✅ Technical score >= 55 (EMA stack + RSI + MACD + volume)
  ✅ GEX != NEGATIVE_GAMMA
  ✅ R/R >= 1.0
  ✅ No existing position in ticker
"""

import os, sys, json, sqlite3, logging, httpx, requests
from datetime import datetime, date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[0]
# When deployed: BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [CHAKRA-SWINGS] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('chakra_swings')

# ── Config ─────────────────────────────────────────────────────────────
ALPACA_KEY     = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET  = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE    = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
POLYGON_KEY    = os.getenv("POLYGON_API_KEY", "")
DISCORD_HOOK   = os.getenv("DISCORD_TRADES_WEBHOOK", "") or os.getenv("DISCORD_WEBHOOK_URL", "")

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

# ── Trading params ─────────────────────────────────────────────────────
CONVICTION_MIN   = 55       # technical score minimum
RR_MIN           = 1.0      # minimum risk/reward
MAX_POSITIONS    = 3        # max concurrent swing positions
MAX_HOLD_DAYS    = 15       # 3 weeks max
RISK_PCT         = 0.02     # 2% account risk per trade
FIRST_TP_PCT     = 0.10     # +10% = first take profit (sell half, leave runner)
RUNNER_STOP_PCT  = 0.05     # close runner if drops 5% from entry after TP1
RUNNER_TARGET_PCT= 1.00     # close runner at +100% from entry
                            # e.g. peak +40% → sell at +20%

# Polygon screener params
SCREENER_MIN_PRICE  = 5.0
SCREENER_MAX_PRICE  = 500.0
SCREENER_MIN_VOL    = 1_000_000
SCREENER_MAX_TICKERS = 20   # top 20 by volume*momentum

DB_PATH = Path("logs/swings/swings_v3.db")
LOG_DIR = Path("logs/swings")

# ══════════════════════════════════════════════════════════════════════
# 1. DATABASE
# ══════════════════════════════════════════════════════════════════════

def init_db():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT NOT NULL,
            entry_date    TEXT NOT NULL,
            entry_price   REAL NOT NULL,
            contract      TEXT,
            option_type   TEXT,
            target        REAL,
            stop          REAL,
            peak_price    REAL,
            shares        INTEGER,
            tp1_hit       INTEGER DEFAULT 0,
            tp1_price     REAL,
            confidence    REAL,
            score         REAL,
            gex_regime    TEXT,
            rr_ratio      REAL,
            status        TEXT DEFAULT 'OPEN',
            hold_days     INTEGER DEFAULT 0,
            exit_date     TEXT,
            exit_price    REAL,
            exit_reason   TEXT,
            pnl           REAL,
            pnl_pct       REAL,
            outcome       TEXT,
            created_at    TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"DB ready: {DB_PATH}")

def get_open_positions():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM swings WHERE status='OPEN'").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_position(ticker):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM swings WHERE ticker=? AND status='OPEN'", (ticker,)).fetchone()
    conn.close()
    return dict(row) if row else None

def open_position(ticker, entry_price, target, stop, shares, conf, score, gex, rr):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO swings
        (ticker,entry_date,entry_price,target,stop,peak_price,shares,
         confidence,score,gex_regime,rr_ratio,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ticker, date.today().isoformat(), entry_price, target, stop,
          entry_price, shares, conf, score, gex, rr, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_peak(ticker, new_peak):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE swings SET peak_price=? WHERE ticker=? AND status='OPEN'", (new_peak, ticker))
    conn.commit()
    conn.close()

def mark_tp1_hit(ticker, tp1_price):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE swings SET tp1_hit=1, tp1_price=? WHERE ticker=? AND status='OPEN'", (tp1_price, ticker))
    conn.commit()
    conn.close()

def close_position(ticker, exit_price, reason):
    pos = get_position(ticker)
    if not pos: return None
    pnl     = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
    pnl_pct = round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)
    outcome = "WIN" if pnl > 0 else "LOSS"
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        UPDATE swings SET status='CLOSED', exit_date=?, exit_price=?,
        exit_reason=?, pnl=?, pnl_pct=?, outcome=?
        WHERE ticker=? AND status='OPEN'
    """, (date.today().isoformat(), exit_price, reason, pnl, pnl_pct, outcome, ticker))
    conn.commit()
    conn.close()
    return pnl, pnl_pct, outcome

def increment_hold_days():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE swings SET hold_days=hold_days+1 WHERE status='OPEN'")
    conn.commit()
    conn.close()

# ══════════════════════════════════════════════════════════════════════
# 2. POLYGON SCREENER
# ══════════════════════════════════════════════════════════════════════

def screen_stocks():
    """
    Polygon snapshot screener: price $5-$500, vol > 1M, sorted by momentum.
    Returns list of tickers sorted by volume * price_change (momentum proxy).
    """
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={
                "apiKey": POLYGON_KEY,
                "include_otc": "false",
            },
            timeout=20
        )
        tickers_data = r.json().get("tickers", [])
        log.info(f"Screener: {len(tickers_data)} tickers from Polygon")

        candidates = []
        for t in tickers_data:
            day    = t.get("day", {})
            prev   = t.get("prevDay", {})
            price  = float(day.get("c") or prev.get("c") or 0)
            volume = float(day.get("v") or 0)
            chg    = float(t.get("todaysChangePerc") or 0)

            # Filter
            if price < SCREENER_MIN_PRICE or price > SCREENER_MAX_PRICE:
                continue
            if volume < SCREENER_MIN_VOL:
                continue
            if abs(chg) < 0.3 or abs(chg) > 25:  # skip flat and extreme news spikes
                continue

            sym = t.get("ticker", "")
            if not sym or len(sym) > 5:  # skip options/complex instruments
                continue

            # Momentum score = volume * % change (bigger moves with volume)
            momentum = volume * chg
            candidates.append({
                "ticker":    sym,
                "price":     price,
                "volume":    volume,
                "chg_pct":   chg,
                "momentum":  momentum,
            })

        # If snapshot returned few candidates, supplement with fixed watchlist
        WATCHLIST = ["SPY","QQQ","IWM","AAPL","MSFT","NVDA","TSLA","META","AMZN","GOOGL",
                     "AMD","PLTR","COIN","HOOD","NFLX","CRM","XLK","XLF","XLE","SOXX"]
        watchlist_syms = {c["ticker"] for c in candidates}
        if len(candidates) < 5:
            for sym in WATCHLIST:
                if sym not in watchlist_syms:
                    try:
                        r2 = httpx.get(
                            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}",
                            params={"apiKey": POLYGON_KEY}, timeout=5)
                        t2 = r2.json().get("ticker", {})
                        day2 = t2.get("day", {}); prev2 = t2.get("prevDay", {})
                        p2 = float(day2.get("c") or prev2.get("c") or 0)
                        v2 = float(day2.get("v") or 0)
                        c2 = float(t2.get("todaysChangePerc") or 0)
                        if p2 > 0 and v2 >= SCREENER_MIN_VOL and abs(c2) >= 0.3:
                            candidates.append({"ticker": sym, "price": p2, "volume": v2,
                                               "chg_pct": c2, "momentum": v2 * abs(c2)})
                    except: pass

        # Sort by absolute momentum (captures both bull and bear moves)
        candidates.sort(key=lambda x: abs(x["momentum"]), reverse=True)
        top = candidates[:SCREENER_MAX_TICKERS]
        log.info(f"Screener: {len(top)} candidates after filtering")
        for c in top[:5]:
            log.info(f"  {c['ticker']}: ${c['price']:.2f} +{c['chg_pct']:.1f}% vol={c['volume']/1e6:.1f}M")
        return top

    except Exception as e:
        log.error(f"Screener error: {e}")
        return []

# ══════════════════════════════════════════════════════════════════════
# 3. TECHNICAL SCORING
# ══════════════════════════════════════════════════════════════════════

def score_ticker(ticker):
    """Fetch daily bars and compute technical score. Returns (score, signal, entry, stop, target, rr)."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=90)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 100, "apiKey": POLYGON_KEY},
            timeout=10
        )
        bars = r.json().get("results", [])
        if len(bars) < 20:
            return None

        closes = [b["c"] for b in bars]
        highs  = [b["h"] for b in bars]
        lows   = [b["l"] for b in bars]
        vols   = [b["v"] for b in bars]

        def ema_calc(arr, n):
            k, e = 2/(n+1), arr[0]
            for v in arr[1:]: e = v*k + e*(1-k)
            return e

        price  = closes[-1]
        e9     = ema_calc(closes, 9)
        e20    = ema_calc(closes, 20)
        e50    = ema_calc(closes[-50:], 50) if len(closes) >= 50 else closes[-1]

        # RSI
        deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas[-14:]]
        losses = [abs(min(d, 0)) for d in deltas[-14:]]
        ag, al = sum(gains)/14, sum(losses)/14
        rsi    = 100 - (100/(1+ag/al)) if al > 0 else 100

        # Volume ratio
        vol_avg   = sum(vols[-20:]) / 20
        vol_ratio = vols[-1] / vol_avg if vol_avg > 0 else 1

        # ATR
        trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
               for i in range(1, len(bars))]
        atr = sum(trs[-14:]) / 14

        # Score BULLISH (call) setup
        bull_score = 50
        if e9 > e20 > e50 and price > e9:  bull_score += 15
        elif e9 > e20 and price > e9:       bull_score += 8
        if 50 < rsi < 70:                   bull_score += 10
        elif rsi >= 70:                     bull_score -= 5
        elif rsi < 40:                      bull_score -= 10
        if vol_ratio >= 1.5:                bull_score += 8
        elif vol_ratio >= 1.0:              bull_score += 3

        # Score BEARISH (put) setup
        bear_score = 50
        if e9 < e20 < e50 and price < e9:  bear_score += 15
        elif e9 < e20 and price < e9:       bear_score += 8
        if 30 < rsi < 50:                   bear_score += 10
        elif rsi <= 30:                     bear_score -= 5
        elif rsi > 60:                      bear_score -= 10
        if vol_ratio >= 1.5:                bear_score += 8
        elif vol_ratio >= 1.0:              bear_score += 3

        bull_score = max(0, min(100, bull_score))
        bear_score = max(0, min(100, bear_score))

        # Pick best direction
        if bull_score >= bear_score:
            score     = bull_score
            direction = "call"
            entry     = round(price, 2)
            stop      = round(price - atr * 1.5, 2)
            target    = round(price + atr * 2.5, 2)
        else:
            score     = bear_score
            direction = "put"
            entry     = round(price, 2)
            stop      = round(price + atr * 1.5, 2)
            target    = round(price - atr * 2.5, 2)

        rr = round(abs(target - entry) / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0

        return {"score": score, "direction": direction, "entry": entry, "stop": stop,
                "target": target, "rr": rr, "rsi": round(rsi,1),
                "vol_ratio": round(vol_ratio,2), "atr": round(atr,4)}

    except Exception as e:
        log.warning(f"Score error {ticker}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
# 4. OPTIONS EXECUTION (via Alpaca)
# ══════════════════════════════════════════════════════════════════════

def get_options_contract(ticker, price, direction="call", weeks_out=2):
    """Find the nearest ATM options contract expiring ~2 weeks out."""
    try:
        exp_target = date.today() + timedelta(weeks=weeks_out)
        exp_str    = exp_target.isoformat()

        r = httpx.get(
            f"{ALPACA_BASE}/v2/options/contracts",
            headers=ALPACA_HEADERS,
            params={
                "underlying_symbols": ticker,
                "type":               direction,
                "expiration_date_gte": date.today().isoformat(),
                "expiration_date_lte": (exp_target + timedelta(days=7)).isoformat(),
                "strike_price_gte":   str(round(price * 0.95, 0)),
                "strike_price_lte":   str(round(price * 1.05, 0)),
                "limit":              10,
            },
            timeout=10
        )
        contracts = r.json().get("option_contracts", [])
        if not contracts:
            log.warning(f"  {ticker}: no options contracts found")
            return None

        # Pick closest expiry + ATM strike
        contracts.sort(key=lambda c: (c.get("expiration_date",""), abs(float(c.get("strike_price",0)) - price)))
        return contracts[0]
    except Exception as e:
        log.warning(f"  {ticker}: options lookup error: {e}")
        return None

def place_option_order(symbol, qty, side="buy"):
    """Place an options order via Alpaca."""
    try:
        r = httpx.post(
            f"{ALPACA_BASE}/v2/orders",
            headers=ALPACA_HEADERS,
            json={"symbol": symbol, "qty": str(qty), "side": side,
                  "type": "market", "time_in_force": "day",
                  "asset_class": "us_option"},
            timeout=10
        )
        if r.status_code in (200, 201):
            log.info(f"Option order placed: {side.upper()} {qty}x {symbol}")
            return {"success": True, "order": r.json()}
        log.error(f"Option order failed: {r.status_code} {r.text[:200]}")
        return {"success": False, "error": r.text}
    except Exception as e:
        log.error(f"Option order error: {e}")
        return {"success": False, "error": str(e)}

def get_current_price(ticker):
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY}, timeout=8
        )
        t = r.json().get("ticker", {})
        return float(t.get("lastTrade",{}).get("p") or t.get("day",{}).get("c") or 0)
    except:
        return 0.0

def get_account_equity():
    try:
        r = httpx.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_HEADERS, timeout=8)
        return float(r.json().get("equity", 100_000))
    except:
        return 100_000.0

# ══════════════════════════════════════════════════════════════════════
# 5. ENTRY SCAN
# ══════════════════════════════════════════════════════════════════════

def run_entry_scan():
    init_db()
    open_pos = get_open_positions()
    if len(open_pos) >= MAX_POSITIONS:
        log.info(f"Max positions ({MAX_POSITIONS}) reached — skipping")
        return 0

    open_tickers = {p["ticker"] for p in open_pos}
    equity       = get_account_equity()
    candidates   = screen_stocks()
    entries      = 0

    log.info(f"Entry scan — equity=${equity:,.0f} | open={len(open_pos)}/{MAX_POSITIONS} | candidates={len(candidates)}")

    for c in candidates:
        ticker = c["ticker"]
        if ticker in open_tickers:
            continue
        if len(open_tickers) >= MAX_POSITIONS:
            break

        tech = score_ticker(ticker)
        if not tech:
            continue

        score = tech["score"]
        rr    = tech["rr"]

        if score < CONVICTION_MIN:
            log.info(f"  {ticker}: score={score:.0f} < {CONVICTION_MIN} — skip")
            continue
        if rr < RR_MIN:
            log.info(f"  {ticker}: R/R={rr:.1f} < {RR_MIN} — skip")
            continue

        price  = c["price"]
        entry  = tech["entry"]
        stop   = tech["stop"]
        target = tech["target"]

        # Size: 2% account risk
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            continue
        shares = max(1, int((equity * RISK_PCT) / risk_per_share))
        shares = min(shares, int(equity * 0.10 / entry))  # cap at 10% account

        log.info(f"  ✅ {ticker}: BUY score={score:.0f} rsi={tech['rsi']} "
                 f"vol={tech['vol_ratio']}x entry=${entry} stop=${stop} "
                 f"target=${target} R/R=1:{rr}")

        # Get options contract (~2 weeks out, ATM call)
        contract = get_options_contract(ticker, price, tech.get("direction","call"), weeks_out=2)
        if not contract:
            log.warning(f"  {ticker}: no options contract — skipping")
            continue

        contract_sym = contract.get("symbol", "")
        qty = max(1, shares // 100)  # options contracts = 100 shares each

        result = place_option_order(contract_sym, qty, "buy")
        if result["success"]:
            open_position(ticker, entry, target, stop, qty, score, score, "UNKNOWN", rr)
            _post_entry(ticker, entry, target, stop, qty, score, rr, contract_sym)
            open_tickers.add(ticker)
            entries += 1
        else:
            log.error(f"  {ticker}: order failed")

    log.info(f"Entry scan complete — {entries} new position(s)")
    # Post screener summary to Discord even if no trades fired
    if DISCORD_HOOK and len(candidates) > 0:
        try:
            now = datetime.now().strftime("%H:%M ET")
            fields = []
            for c in candidates[:8]:
                tech = score_ticker(c["ticker"])
                if not tech: continue
                dir_emoji = "🟢" if tech["direction"] == "call" else "🔴"
                fields.append({
                    "name": f"{dir_emoji} {c['ticker']}",
                    "value": (f"${c['price']:.2f} {c['chg_pct']:+.1f}%\n"
                              f"Score: **{tech['score']}** | {tech['direction'].upper()}\n"
                              f"R/R: {tech['rr']} | RSI: {tech['rsi']}"),
                    "inline": True,
                })
            payload = {"embeds": [{
                "title": f"🔍 CHAKRA Swing Screener — {len(candidates)} Candidates",
                "color": 0x9B59B6,
                "description": (f"Scan complete at {now} — "
                                f"**{entries}** trade(s) entered out of {len(candidates)} candidates\n"
                                f"Strategy: TP1 **+10%** · Runner stop **-5%** · Runner target **+100%**"),
                "fields": fields,
                "footer": {"text": "CHAKRA Swings Engine v3 • Bidirectional Scanner"},
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }]}
            requests.post(DISCORD_HOOK, json=payload, timeout=8)
            log.info(f"Screener summary posted to Discord — {len(candidates)} candidates")
        except Exception as e:
            log.warning(f"Discord screener post error: {e}")
    return entries

# ══════════════════════════════════════════════════════════════════════
# 6. POSITION MONITOR — TP1 + Runner trail
# ══════════════════════════════════════════════════════════════════════

def run_monitor():
    init_db()
    positions = get_open_positions()
    if not positions:
        log.info("No open swing positions to monitor")
        return

    increment_hold_days()
    log.info(f"Monitoring {len(positions)} position(s)")

    for pos in positions:
        ticker    = pos["ticker"]
        entry     = pos["entry_price"]
        target    = pos["target"]
        stop      = pos["stop"]
        peak      = pos.get("peak_price") or entry
        shares    = pos["shares"]
        hold_days = pos["hold_days"]
        tp1_hit   = bool(pos.get("tp1_hit", 0))

        price = get_current_price(ticker)
        if not price:
            continue

        pnl_pct = (price - entry) / entry * 100
        log.info(f"  {ticker}: ${price:.2f} (entry ${entry:.2f}) "
                 f"P&L {pnl_pct:+.1f}% peak={peak:.2f} hold={hold_days}d")

        # Update peak
        if price > peak:
            update_peak(ticker, price)
            peak = price

        exit_reason = None

        # ── Exit 1: Stop loss ─────────────────────────────────────────
        if price <= stop:
            exit_reason = f"Stop loss (${price:.2f} ≤ ${stop:.2f})"

        # ── Exit 2: Max hold days ─────────────────────────────────────
        elif hold_days >= MAX_HOLD_DAYS:
            exit_reason = f"Max hold {MAX_HOLD_DAYS} days reached"

        # ── TP1: First take profit at +10% ────────────────────────────
        elif not tp1_hit and pnl_pct >= FIRST_TP_PCT * 100:
            half = max(1, shares // 2)
            log.info(f"  {ticker}: TP1 HIT +{pnl_pct:.1f}% — selling {half} contracts, leaving runner")
            result = place_option_order(pos.get("contract", ticker), half, "sell")
            if result["success"]:
                mark_tp1_hit(ticker, price)
                _post_tp1(ticker, entry, price, pnl_pct, half)
            continue  # keep runner

        # ── Runner exits ──────────────────────────────────────────────
        elif tp1_hit:
            # Runner target: +100% from entry
            if pnl_pct >= RUNNER_TARGET_PCT * 100:
                exit_reason = f"Runner target hit +{pnl_pct:.1f}% (target +100%)"
            # Runner stop: -5% from entry (protect against reversal)
            elif pnl_pct <= -(RUNNER_STOP_PCT * 100):
                exit_reason = f"Runner stop -5% hit ({pnl_pct:+.1f}%)"

        if exit_reason:
            log.info(f"  {ticker}: EXIT — {exit_reason}")
            # Sell remaining position
            remaining = max(1, shares // 2) if pos.get("tp1_hit") else shares
            result = place_option_order(pos.get("contract", ticker), remaining, "sell")
            if result["success"]:
                pnl_result = close_position(ticker, price, exit_reason)
                if pnl_result:
                    pnl, pnl_pct_final, outcome = pnl_result
                    _post_exit(ticker, entry, price, shares, pnl, pnl_pct_final, exit_reason, outcome, hold_days)

    log.info("Monitor complete")

# ══════════════════════════════════════════════════════════════════════
# 7. DISCORD
# ══════════════════════════════════════════════════════════════════════

def _post_entry(ticker, entry, target, stop, qty, score, rr, contract):
    if not DISCORD_HOOK: return
    tp1 = round(entry * (1 + FIRST_TP_PCT), 2)
    embed = {
        "title": f"🏹 CHAKRA SWING ENTRY — {ticker}",
        "color": 0x00D084,
        "fields": [
            {"name": "Contract",    "value": contract,              "inline": True},
            {"name": "Entry",       "value": f"${entry:.2f}",       "inline": True},
            {"name": "TP1 (+10%)",  "value": f"${tp1:.2f}",         "inline": True},
            {"name": "Target",      "value": f"${target:.2f}",      "inline": True},
            {"name": "Stop",        "value": f"${stop:.2f}",        "inline": True},
            {"name": "R/R",         "value": f"1:{rr:.1f}",         "inline": True},
            {"name": "Contracts",   "value": str(qty),              "inline": True},
            {"name": "Score",       "value": f"{score:.0f}/100",    "inline": True},
            {"name": "Max Hold",    "value": "15 days (3 weeks)",   "inline": True},
        ],
        "footer": {"text": f"CHAKRA Swings v3 • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    try: requests.post(DISCORD_HOOK, json={"embeds": [embed]}, timeout=8)
    except: pass

def _post_tp1(ticker, entry, price, pnl_pct, qty):
    if not DISCORD_HOOK: return
    try:
        requests.post(DISCORD_HOOK, json={"content":
            f"🎯 **{ticker}** TP1 HIT +{pnl_pct:.1f}% — "
            f"sold {qty} contracts @ ${price:.2f} | "
            f"Runner continues · stop -5% from entry · target +100%"
        }, timeout=8)
    except: pass

def _post_exit(ticker, entry, exit_price, shares, pnl, pnl_pct, reason, outcome, hold_days):
    if not DISCORD_HOOK: return
    embed = {
        "title": f"{'✅' if outcome=='WIN' else '❌'} CHAKRA SWING EXIT — {ticker} {outcome}",
        "color": 0x00FF9D if outcome == "WIN" else 0xFF2D55,
        "fields": [
            {"name": "Entry",      "value": f"${entry:.2f}",                             "inline": True},
            {"name": "Exit",       "value": f"${exit_price:.2f}",                        "inline": True},
            {"name": "P&L",        "value": f"{'+'if pnl>=0 else''}${pnl:.2f} ({pnl_pct:+.1f}%)", "inline": True},
            {"name": "Hold Days",  "value": f"{hold_days}d",                             "inline": True},
            {"name": "Exit Reason","value": reason[:100],                                "inline": False},
        ],
        "footer": {"text": f"CHAKRA Swings v3 • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    try: requests.post(DISCORD_HOOK, json={"embeds": [embed]}, timeout=8)
    except: pass

# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--monitor",    action="store_true")
    p.add_argument("--status",     action="store_true")
    p.add_argument("--screen",     action="store_true", help="Just run screener and show candidates")
    p.add_argument("--premarket",  action="store_true", help="Pre-market watchlist scan (no orders)")
    p.add_argument("--postmarket", action="store_true", help="Post-market watchlist scan (no orders)")
    args = p.parse_args()

    if args.premarket or args.postmarket:
        mode = "premarket" if args.premarket else "postmarket"
        log.info(f"  📋 CHAKRA {mode} watchlist scan")
        candidates = screen_stocks()
        log.info(f"  Found {len(candidates)} candidates")
        # Save watchlist for dashboard
        import json, pathlib, datetime
        wl = {"candidates": candidates, "count": len(candidates),
              "scan_time": datetime.datetime.now().isoformat(), "mode": mode,
              "top5": candidates[:5]}
        pathlib.Path("logs/chakra").mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        pathlib.Path(f"logs/chakra/watchlist_{ts}.json").write_text(json.dumps(wl, indent=2))
        pathlib.Path("logs/chakra/watchlist_latest.json").write_text(json.dumps(wl, indent=2))
        log.info(f"  Watchlist saved: {len(candidates)} tickers")
    elif args.screen:
        init_db()
        candidates = screen_stocks()
        print(f"\nTop {len(candidates)} candidates:")
        for c in candidates:
            tech = score_ticker(c["ticker"])
            score = tech["score"] if tech else 0
            print(f"  {c['ticker']:6s} ${c['price']:7.2f}  +{c['chg_pct']:.1f}%  "
                  f"vol={c['volume']/1e6:.1f}M  score={score:.0f}")
    elif args.monitor:
        run_monitor()
    elif args.status:
        positions = get_open_positions()
        print(f"\n{len(positions)} open positions:")
        for p in positions:
            price = get_current_price(p["ticker"])
            pnl_pct = (price - p["entry_price"]) / p["entry_price"] * 100 if price else 0
            print(f"  {p['ticker']}: entry=${p['entry_price']:.2f} current=${price:.2f} "
                  f"P&L={pnl_pct:+.1f}% day={p['hold_days']}/{MAX_HOLD_DAYS} "
                  f"TP1={'✅' if p['tp1_hit'] else '⏳'}")
    else:
        run_entry_scan()
