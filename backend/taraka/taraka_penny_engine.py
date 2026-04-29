"""
TARAKA Penny Stock Engine v2.0
================================
Strategy:
  - Scans for penny stocks ($0.10–$5.00) via Polygon screener
  - Buys ACTUAL SHARES (not options)
  - Budget: $100–$250 per position
  - Hold: 4–6 months
  - TP: +30%, Stop loss: -30%
  - Max 5 concurrent positions
  - Discord: optional input channel (manually forwarded alerts welcome)
    but engine runs fully autonomous via screener

Screener criteria:
  - Price: $0.10–$5.00
  - Volume: > 500K (liquid enough to exit)
  - Market cap filter via Polygon reference
  - Positive momentum (up > 2% on the day)
  - Not already held

Run:
  python3 backend/taraka/taraka_penny_engine.py            # entry scan
  python3 backend/taraka/taraka_penny_engine.py --monitor  # check positions
  python3 backend/taraka/taraka_penny_engine.py --status   # print holdings
  python3 backend/taraka/taraka_penny_engine.py --screen   # show candidates
"""

import os, sys, json, sqlite3, logging, httpx, requests
from datetime import datetime, date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[0]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TARAKA-PENNY] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('taraka_penny')

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
MIN_POSITION_SIZE = 100     # $100 minimum per position
MAX_POSITION_SIZE = 250     # $250 maximum per position
MAX_POSITIONS     = 5       # max concurrent penny stock holds
MIN_HOLD_DAYS     = 80      # ~4 months minimum
MAX_HOLD_DAYS     = 120     # ~6 months maximum
TAKE_PROFIT_PCT   = 0.30    # +30% exit
STOP_LOSS_PCT     = 0.30    # -30% exit
MIN_CONVICTION    = 55      # technical score minimum

# Screener params
PENNY_MIN_PRICE   = 0.10
PENNY_MAX_PRICE   = 5.00
PENNY_MIN_VOL     = 500_000
PENNY_MIN_CHG_PCT = 2.0     # must be up > 2% today (momentum confirmation)

DB_PATH = Path("logs/taraka/penny_stocks.db")
LOG_DIR = Path("logs/taraka")

# ══════════════════════════════════════════════════════════════════════
# 1. DATABASE
# ══════════════════════════════════════════════════════════════════════

def init_db():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS penny_holdings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT NOT NULL,
            entry_date    TEXT NOT NULL,
            entry_price   REAL NOT NULL,
            shares        INTEGER NOT NULL,
            cost_basis    REAL NOT NULL,
            target_price  REAL NOT NULL,
            stop_price    REAL NOT NULL,
            score         REAL,
            volume_at_entry REAL,
            chg_pct_entry REAL,
            source        TEXT DEFAULT 'screener',
            status        TEXT DEFAULT 'OPEN',
            hold_days     INTEGER DEFAULT 0,
            exit_date     TEXT,
            exit_price    REAL,
            exit_reason   TEXT,
            pnl           REAL,
            pnl_pct       REAL,
            outcome       TEXT,
            notes         TEXT,
            created_at    TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info(f"DB ready: {DB_PATH}")

def get_open_holdings():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM penny_holdings WHERE status='OPEN'").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_holding(ticker):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM penny_holdings WHERE ticker=? AND status='OPEN'", (ticker,)).fetchone()
    conn.close()
    return dict(row) if row else None

def open_holding(ticker, entry_price, shares, cost, target, stop, score, volume, chg, source="screener"):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO penny_holdings
        (ticker,entry_date,entry_price,shares,cost_basis,target_price,stop_price,
         score,volume_at_entry,chg_pct_entry,source,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (ticker, date.today().isoformat(), entry_price, shares, cost,
          target, stop, score, volume, chg, source, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def close_holding(ticker, exit_price, reason):
    pos = get_holding(ticker)
    if not pos: return None
    pnl     = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
    pnl_pct = round((exit_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)
    outcome = "WIN" if pnl > 0 else "LOSS"
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        UPDATE penny_holdings SET status='CLOSED', exit_date=?, exit_price=?,
        exit_reason=?, pnl=?, pnl_pct=?, outcome=?
        WHERE ticker=? AND status='OPEN'
    """, (date.today().isoformat(), exit_price, reason, pnl, pnl_pct, outcome, ticker))
    conn.commit()
    conn.close()
    return pnl, pnl_pct, outcome

def increment_hold_days():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE penny_holdings SET hold_days=hold_days+1 WHERE status='OPEN'")
    conn.commit()
    conn.close()

# ══════════════════════════════════════════════════════════════════════
# 2. POLYGON PENNY SCREENER
# ══════════════════════════════════════════════════════════════════════

def screen_penny_stocks():
    """
    Screen for penny stocks with momentum.
    Price $0.10-$5.00, volume > 500K, up > 2% today.
    """
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"apiKey": POLYGON_KEY, "include_otc": "false"},
            timeout=20
        )
        tickers_data = r.json().get("tickers", [])
        log.info(f"Screener: {len(tickers_data)} total tickers")

        candidates = []
        for t in tickers_data:
            day    = t.get("day", {})
            prev   = t.get("prevDay", {})
            price  = float(day.get("c") or prev.get("c") or 0)
            volume = float(day.get("v") or 0)
            chg    = float(t.get("todaysChangePerc") or 0)

            # Penny stock filter
            if price < PENNY_MIN_PRICE or price > PENNY_MAX_PRICE:
                continue
            if volume < PENNY_MIN_VOL:
                continue
            if chg < PENNY_MIN_CHG_PCT:
                continue

            sym = t.get("ticker", "")
            if not sym or len(sym) > 5:
                continue

            candidates.append({
                "ticker":  sym,
                "price":   price,
                "volume":  volume,
                "chg_pct": chg,
                "score":   volume * chg,  # momentum rank
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:15]
        log.info(f"Screener: {len(top)} penny candidates")
        for c in top[:8]:
            log.info(f"  {c['ticker']}: ${c['price']:.2f} +{c['chg_pct']:.1f}% vol={c['volume']/1e6:.1f}M")
        return top

    except Exception as e:
        log.error(f"Penny screener error: {e}")
        return []

# ══════════════════════════════════════════════════════════════════════
# 3. TECHNICAL SCORE (simplified for penny stocks)
# ══════════════════════════════════════════════════════════════════════

def score_penny(ticker):
    """Basic technical scoring for penny stocks using daily bars."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=60)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"adjusted": "true", "sort": "asc", "limit": 80, "apiKey": POLYGON_KEY},
            timeout=10
        )
        bars = r.json().get("results", [])
        if len(bars) < 10:
            return None

        closes = [b["c"] for b in bars]
        vols   = [b["v"] for b in bars]
        price  = closes[-1]

        def ema_s(arr, n):
            k, e = 2/(n+1), arr[0]
            for v in arr[1:]: e = v*k + e*(1-k)
            return e

        e9  = ema_s(closes, 9)
        e20 = ema_s(closes, min(20, len(closes)))

        # RSI
        deltas = [closes[i]-closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d,0) for d in deltas[-14:]]
        losses = [abs(min(d,0)) for d in deltas[-14:]]
        ag, al = sum(gains)/14, sum(losses)/14
        rsi    = 100 - (100/(1+ag/al)) if al > 0 else 50

        # Volume
        vol_avg   = sum(vols[-10:])/10
        vol_ratio = vols[-1]/vol_avg if vol_avg > 0 else 1

        # Score
        score = 50
        if price > e9 > e20:       score += 15
        elif price > e9:            score += 7
        if 45 < rsi < 75:          score += 10
        elif rsi >= 75:             score -= 5
        if vol_ratio >= 2.0:        score += 12  # penny stocks need big vol surge
        elif vol_ratio >= 1.5:      score += 6

        # Penny stock specific: reward if near 52-week low (contrarian bounce potential)
        wk52_low = min(closes)
        pct_from_low = (price - wk52_low) / wk52_low * 100 if wk52_low > 0 else 0
        if pct_from_low < 20:       score += 8   # near low = accumulation zone

        return {"score": min(100, max(0, score)), "rsi": round(rsi,1), "vol_ratio": round(vol_ratio,2)}

    except Exception as e:
        log.warning(f"Score error {ticker}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════
# 4. ORDER EXECUTION (shares, not options)
# ══════════════════════════════════════════════════════════════════════

def place_stock_order(ticker, shares, side="buy"):
    try:
        r = httpx.post(
            f"{ALPACA_BASE}/v2/orders",
            headers=ALPACA_HEADERS,
            json={"symbol": ticker, "qty": str(shares), "side": side,
                  "type": "market", "time_in_force": "day"},
            timeout=10
        )
        if r.status_code in (200, 201):
            log.info(f"Order: {side.upper()} {shares}x {ticker}")
            return {"success": True, "order": r.json()}
        log.error(f"Order failed {r.status_code}: {r.text[:200]}")
        return {"success": False, "error": r.text}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_price(ticker):
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY}, timeout=8
        )
        t = r.json().get("ticker", {})
        return float(t.get("lastTrade",{}).get("p") or t.get("day",{}).get("c") or 0)
    except:
        return 0.0

# ══════════════════════════════════════════════════════════════════════
# 5. ENTRY SCAN
# ══════════════════════════════════════════════════════════════════════

def run_entry_scan():
    init_db()
    open_pos     = get_open_holdings()
    open_tickers = {p["ticker"] for p in open_pos}

    if len(open_pos) >= MAX_POSITIONS:
        log.info(f"Max positions ({MAX_POSITIONS}) — skipping")
        return 0

    candidates = screen_penny_stocks()
    entries    = 0

    log.info(f"Entry scan — open={len(open_pos)}/{MAX_POSITIONS} candidates={len(candidates)}")

    for c in candidates:
        ticker = c["ticker"]
        if ticker in open_tickers:
            continue
        if len(open_tickers) >= MAX_POSITIONS:
            break

        tech = score_penny(ticker)
        if not tech or tech["score"] < MIN_CONVICTION:
            log.info(f"  {ticker}: score={tech['score'] if tech else '?':.0f} — skip")
            continue

        price = c["price"]

        # Position sizing: $100-$250
        # Buy as many shares as fit in budget
        shares = int(MAX_POSITION_SIZE / price)
        cost   = round(shares * price, 2)

        if cost < MIN_POSITION_SIZE:
            shares = int(MIN_POSITION_SIZE / price)
            cost   = round(shares * price, 2)

        if shares < 1:
            continue

        # Cap at $250
        if cost > MAX_POSITION_SIZE:
            shares = int(MAX_POSITION_SIZE / price)
            cost   = round(shares * price, 2)

        target = round(price * (1 + TAKE_PROFIT_PCT), 4)
        stop   = round(price * (1 - STOP_LOSS_PCT), 4)

        log.info(f"  ✅ {ticker}: BUY {shares}sh @ ${price:.4f} "
                 f"cost=${cost:.2f} target=${target:.4f} stop=${stop:.4f} "
                 f"score={tech['score']:.0f} vol={tech['vol_ratio']}x")

        result = place_stock_order(ticker, shares, "buy")
        if result["success"]:
            open_holding(ticker, price, shares, cost, target, stop,
                        tech["score"], c["volume"], c["chg_pct"], "screener")
            _post_entry(ticker, price, shares, cost, target, stop, tech["score"])
            open_tickers.add(ticker)
            entries += 1
        else:
            log.error(f"  {ticker}: order failed — {result.get('error','')[:100]}")

    log.info(f"Entry scan complete — {entries} new holding(s)")
    return entries

# ══════════════════════════════════════════════════════════════════════
# 6. POSITION MONITOR
# ══════════════════════════════════════════════════════════════════════

def run_monitor():
    init_db()
    holdings = get_open_holdings()
    if not holdings:
        log.info("No open penny holdings to monitor")
        return

    increment_hold_days()
    log.info(f"Monitoring {len(holdings)} penny holding(s)")

    for h in holdings:
        ticker    = h["ticker"]
        entry     = h["entry_price"]
        target    = h["target_price"]
        stop_p    = h["stop_price"]
        shares    = h["shares"]
        hold_days = h["hold_days"]

        price = get_price(ticker)
        if not price:
            continue

        pnl_pct = (price - entry) / entry * 100
        log.info(f"  {ticker}: ${price:.4f} (entry ${entry:.4f}) "
                 f"P&L {pnl_pct:+.1f}% day={hold_days}/{MAX_HOLD_DAYS}")

        exit_reason = None

        if price <= stop_p:
            exit_reason = f"Stop loss -30% (${price:.4f} ≤ ${stop_p:.4f})"
        elif price >= target:
            exit_reason = f"Take profit +30% (${price:.4f} ≥ ${target:.4f})"
        elif hold_days >= MAX_HOLD_DAYS:
            exit_reason = f"Max hold {MAX_HOLD_DAYS} days (6 months) reached"

        if exit_reason:
            log.info(f"  {ticker}: EXIT — {exit_reason}")
            result = place_stock_order(ticker, shares, "sell")
            if result["success"]:
                pnl_result = close_holding(ticker, price, exit_reason)
                if pnl_result:
                    pnl, pnl_pct_final, outcome = pnl_result
                    _post_exit(ticker, entry, price, shares, pnl, pnl_pct_final,
                               exit_reason, outcome, hold_days)

    log.info("Monitor complete")

# ══════════════════════════════════════════════════════════════════════
# 7. DISCORD (optional — works even without Discord configured)
# ══════════════════════════════════════════════════════════════════════

def _post_entry(ticker, price, shares, cost, target, stop, score):
    msg = (f"💎 TARAKA PENNY BUY: {ticker} @ ${price:.4f} | "
           f"{shares} shares | Cost ${cost:.2f} | "
           f"TP +30% @ ${target:.4f} | SL -30% @ ${stop:.4f} | "
           f"Score {score:.0f}/100 | Hold 4-6 months")
    log.info(f"  ENTRY ALERT: {msg}")
    if not DISCORD_HOOK: return
    try:
        requests.post(DISCORD_HOOK, json={"embeds": [{
            "title": f"💎 TARAKA PENNY ENTRY — {ticker}",
            "color": 0x9B59B6,
            "fields": [
                {"name": "Price",     "value": f"${price:.4f}",   "inline": True},
                {"name": "Shares",    "value": str(shares),        "inline": True},
                {"name": "Cost",      "value": f"${cost:.2f}",     "inline": True},
                {"name": "TP (+30%)", "value": f"${target:.4f}",   "inline": True},
                {"name": "SL (-30%)", "value": f"${stop:.4f}",     "inline": True},
                {"name": "Score",     "value": f"{score:.0f}/100", "inline": True},
                {"name": "Hold",      "value": "4–6 months",       "inline": True},
                {"name": "Source",    "value": "Polygon screener", "inline": True},
            ],
            "footer": {"text": f"TARAKA Penny Engine v2 • {datetime.now().strftime('%H:%M ET')}"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]}, timeout=8)
    except: pass

def _post_exit(ticker, entry, exit_p, shares, pnl, pnl_pct, reason, outcome, hold_days):
    log.info(f"  EXIT ALERT: {ticker} {outcome} P&L=${pnl:.2f} ({pnl_pct:+.1f}%) after {hold_days}d")
    if not DISCORD_HOOK: return
    try:
        requests.post(DISCORD_HOOK, json={"embeds": [{
            "title": f"{'✅' if outcome=='WIN' else '❌'} TARAKA PENNY EXIT — {ticker} {outcome}",
            "color": 0x00FF9D if outcome == "WIN" else 0xFF2D55,
            "fields": [
                {"name": "Entry",      "value": f"${entry:.4f}",                               "inline": True},
                {"name": "Exit",       "value": f"${exit_p:.4f}",                              "inline": True},
                {"name": "P&L",        "value": f"{'+'if pnl>=0 else''}${pnl:.2f} ({pnl_pct:+.1f}%)", "inline": True},
                {"name": "Hold Days",  "value": f"{hold_days}d",                               "inline": True},
                {"name": "Exit Reason","value": reason[:100],                                  "inline": False},
            ],
            "footer": {"text": f"TARAKA Penny Engine v2 • {datetime.now().strftime('%H:%M ET')}"},
        }]}, timeout=8)
    except: pass

# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TARAKA Penny Stock Engine v2.0")
    p.add_argument("--monitor", action="store_true", help="Check open positions")
    p.add_argument("--status",  action="store_true", help="Print holdings")
    p.add_argument("--screen",  action="store_true", help="Show screener candidates only")
    p.add_argument("--close",   type=str,            help="Force close: --close TICKER")
    args = p.parse_args()

    if args.screen:
        init_db()
        candidates = screen_penny_stocks()
        print(f"\n{'='*60}")
        print(f"  TARAKA Penny Screener — {len(candidates)} candidates")
        print(f"{'='*60}")
        for c in candidates:
            tech = score_penny(c["ticker"])
            score = tech["score"] if tech else 0
            status = "✅ PASS" if score >= MIN_CONVICTION else "⏸  skip"
            print(f"  {c['ticker']:6s} ${c['price']:.4f}  +{c['chg_pct']:.1f}%  "
                  f"vol={c['volume']/1e6:.1f}M  score={score:.0f}  {status}")
        print(f"{'='*60}\n")

    elif args.monitor:
        run_monitor()

    elif args.status:
        init_db()
        holdings = get_open_holdings()
        print(f"\n{'='*60}")
        print(f"  TARAKA — {len(holdings)} open penny holding(s)")
        print(f"{'='*60}")
        for h in holdings:
            price   = get_price(h["ticker"])
            pnl_pct = (price - h["entry_price"]) / h["entry_price"] * 100 if price else 0
            sign    = "+" if pnl_pct >= 0 else ""
            print(f"  {h['ticker']:6s}  entry=${h['entry_price']:.4f}  "
                  f"current=${price:.4f}  P&L={sign}{pnl_pct:.1f}%  "
                  f"day {h['hold_days']}/{MAX_HOLD_DAYS}  "
                  f"cost=${h['cost_basis']:.2f}")
        print(f"{'='*60}\n")

    elif args.close:
        init_db()
        ticker = args.close.upper()
        h = get_holding(ticker)
        if not h:
            print(f"No open holding for {ticker}")
            sys.exit(1)
        price = get_price(ticker)
        result = place_stock_order(ticker, h["shares"], "sell")
        if result["success"]:
            pnl_result = close_holding(ticker, price, "Manual close")
            if pnl_result:
                pnl, pnl_pct, outcome = pnl_result
                print(f"✅ {ticker} closed @ ${price:.4f} | P&L ${pnl:+.2f} ({pnl_pct:+.1f}%) {outcome}")
        else:
            print(f"❌ Close failed: {result.get('error')}")

    else:
        run_entry_scan()
