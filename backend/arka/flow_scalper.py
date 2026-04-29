#!/usr/bin/env python3
"""
ARKA Flow Scalper
=================
Pure institutional-flow-driven scalper.
Runs alongside the main ARKA engine as a separate process.

Entry triggers (in order of priority):
  1. Confidence = 100%              → enter 1 contract, always
  2. is_extreme AND confidence ≥ 85 → enter 1 contract
  3. vol_oi_ratio ≥ 500 AND confidence ≥ 70 → enter (MEGA sweep signals)

Targets per trade:
  TP:  +10% on premium
  SL:  -25% on premium
  EOD: Force close at 3:58pm ET

Contract selection:
  - 0DTE preferred (expires today)
  - 1DTE fallback if 0DTE unavailable
  - ATM ± 1 strike
  - CALL if BULLISH/CALL, PUT if BEARISH/PUT

Limits:
  - Max 2 concurrent flow scalp positions
  - Max $300/contract (actual premium check via Polygon)
  - Market hours only: 9:30am–3:58pm ET
  - Only indexes by default (SPY/QQQ/SPX/IWM) — stocks need explicit enable

Usage:
  python3 -m backend.arka.flow_scalper          # run continuously
  python3 -m backend.arka.flow_scalper --once   # single scan + monitor pass
  python3 -m backend.arka.flow_scalper --status # show open positions
"""

import os, sys, json, logging, argparse, time
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)

import httpx

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.FlowScalper")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FLOW-SCALP] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

# ── Config ─────────────────────────────────────────────────────────────────────
POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")
ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE   = "https://paper-api.alpaca.markets"

FLOW_CACHE    = BASE / "logs/chakra/flow_signals_latest.json"
STATE_FILE    = BASE / "logs/arka/flow_scalper_state.json"
DISCORD_WH    = os.getenv("DISCORD_ARJUN_ALERTS", os.getenv("DISCORD_ALERTS", ""))

# ── Thresholds ─────────────────────────────────────────────────────────────────
CONF_ALWAYS      = 100    # confidence >= this → always enter
CONF_EXTREME_MIN = 85     # minimum confidence for extreme flow entry
VOL_MEGA_MIN     = 500    # vol_oi_ratio >= this → mega sweep trigger
CONF_MEGA_MIN    = 70     # minimum confidence for mega sweep entry
TP_PCT           = 0.20   # +20% take profit on premium (was 10% — needs 71%+ win rate, too tight)
SL_PCT           = 0.20   # -20% stop loss on premium (1:1 ratio, break-even at 50% win rate)
MAX_POSITIONS    = 2      # max concurrent flow scalp positions
MAX_PREMIUM      = 8.00   # $800/contract hard ceiling (handles high-IV morning entries for SPY/QQQ)
MAX_PREMIUM_PCT  = 0.02   # also reject if premium > 2% of underlying (stale data check)
MIN_PREMIUM      = 0.50   # $50/contract minimum — filter out lottery tickets (<$0.50 = no edge)
EOD_HOUR         = 15     # 3pm ET
EOD_MINUTE       = 58     # 3:58pm exit

# Only trade these tickers for flow scalps (indexes only for now)
ALLOWED_TICKERS  = {"SPY", "QQQ", "SPX", "IWM", "DIA"}

SCAN_INTERVAL    = 30     # seconds between scans


# ══════════════════════════════════════════════════════════════════════════════
#  STATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"positions": {}, "acted_keys": [], "daily_date": "", "daily_trades": 0}


def _save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning(f"Could not save state: {e}")


def _make_signal_key(ticker: str, sig: dict) -> str:
    """One entry per ticker per direction per calendar day.
    Using timestamp caused re-entry every time flow_monitor updated the signal.
    """
    day = str(date.today())
    direction = sig.get("direction") or ("CALL" if sig.get("bias") == "BULLISH" else "PUT")
    return f"{ticker}_{day}_{direction}"


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def _is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    market_open  = now.hour > 9 or (now.hour == 9 and now.minute >= 30)
    market_close = now.hour < EOD_HOUR or (now.hour == EOD_HOUR and now.minute < EOD_MINUTE)
    return market_open and market_close


def _is_eod() -> bool:
    now = datetime.now(ET)
    return now.hour > EOD_HOUR or (now.hour == EOD_HOUR and now.minute >= EOD_MINUTE)


def _get_spot(ticker: str) -> float:
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY}, timeout=5
        )
        snap = r.json().get("ticker", {})
        return float(snap.get("day", {}).get("c", 0) or snap.get("prevDay", {}).get("c", 0))
    except Exception:
        return 0.0


def _get_option_price(contract_sym: str, ticker: str, spot: float = 0) -> float | None:
    """Fetch current mid-price for an options contract from Polygon snapshot.
    Uses the single-contract endpoint (/v3/snapshot/options/{underlying}/{contract})
    for exact lookup. Falls back to list endpoint if needed.
    Rejects prices > 20% of underlying as stale/erroneous data.
    """
    try:
        # Correct single-contract endpoint — avoids the list endpoint returning wrong results
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker}/{contract_sym}"
        r   = httpx.get(url, params={"apiKey": POLYGON_KEY}, timeout=6)
        if r.status_code == 200:
            # Single-contract endpoint returns {"results": {...}} (object, not list)
            result = r.json().get("results", None)
            if result and isinstance(result, dict):
                q   = result.get("last_quote", {})
                bid = float(q.get("bid", 0) or 0)
                ask = float(q.get("ask", 0) or 0)
                if bid > 0 and ask > 0 and ask <= bid * 10:
                    price = round((bid + ask) / 2, 3)
                else:
                    price = float(result.get("details", {}).get("mark", 0) or 0)
                    if price == 0:
                        price = float(q.get("ask", 0) or 0)
                if price > 0:
                    if spot > 0 and price > spot * 0.20:
                        log.warning(f"  🚫 Price sanity FAIL: {contract_sym} ${price:.2f} > "
                                    f"20% of {ticker} ${spot:.2f} — stale data")
                        return None
                    return round(price, 3)

        # Fallback: list endpoint with exact ticker param (some API versions need this)
        url2 = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
        r2   = httpx.get(url2, params={"apiKey": POLYGON_KEY,
                                        "ticker": contract_sym, "limit": 1}, timeout=6)
        if r2.status_code == 200:
            results = r2.json().get("results", [])
            # Verify we got the right contract — list endpoint may ignore ticker filter
            if results and isinstance(results, list):
                hit = next((x for x in results
                            if x.get("details", {}).get("ticker") == contract_sym), None)
                if hit:
                    q    = hit.get("last_quote", {})
                    bid  = float(q.get("bid", 0) or 0)
                    ask  = float(q.get("ask", 0) or 0)
                    if bid > 0 and ask > 0 and ask <= bid * 10:
                        price = round((bid + ask) / 2, 3)
                    else:
                        price = float(hit.get("details", {}).get("mark", 0) or 0)
                    if price > 0:
                        if spot > 0 and price > spot * 0.20:
                            log.warning(f"  🚫 Price sanity FAIL (fb): {contract_sym} ${price:.2f}")
                            return None
                        return round(price, 3)
    except Exception as e:
        log.debug(f"  option price fetch failed ({contract_sym}): {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  CONTRACT SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def _find_0dte_contract(ticker: str, direction: str, spot: float) -> dict | None:
    """
    Find 0DTE ATM contract via Alpaca.
    Falls back to 1DTE if 0DTE unavailable.
    direction: "CALL" or "PUT"
    """
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error("  Alpaca credentials missing")
        return None

    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    today   = date.today().isoformat()
    tmrw    = (date.today() + timedelta(days=1)).isoformat()

    # Try 0DTE first, then 1DTE
    for exp in [today, tmrw]:
        try:
            # Calls: search ATM→OTM only (spot to spot+3%)
            # Puts:  search OTM→ATM only (spot-3% to spot)
            if direction.lower() == "call":
                strike_lo = round(spot * 0.999, 0)   # ATM floor — no ITM calls
                strike_hi = round(spot * 1.03, 0)
            else:
                strike_lo = round(spot * 0.97, 0)
                strike_hi = round(spot * 1.001, 0)   # ATM ceiling — no ITM puts
            params = {
                "underlying_symbols":  ticker,
                "type":                direction.lower(),
                "expiration_date_gte": exp,
                "expiration_date_lte": exp,
                "strike_price_gte":    str(strike_lo),
                "strike_price_lte":    str(strike_hi),
                "limit":               10,
            }
            r = httpx.get(f"{ALPACA_BASE}/v2/options/contracts",
                          headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                contracts = r.json().get("option_contracts", [])
                if contracts:
                    # Pick closest ATM strike
                    contracts.sort(key=lambda c: abs(float(c.get("strike_price", 0)) - spot))
                    c   = contracts[0]
                    dte = (date.fromisoformat(c["expiration_date"]) - date.today()).days
                    log.info(f"  Found {direction} {c['symbol']} "
                             f"strike=${c['strike_price']} exp={c['expiration_date']} ({dte}DTE)")
                    return c
        except Exception as e:
            log.warning(f"  Contract lookup failed (exp={exp}): {e}")

    log.warning(f"  No 0/1DTE {direction} contract found for {ticker}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def _get_fill_price(order_id: str, retries: int = 5, delay: float = 1.5) -> float:
    """Poll Alpaca order by ID until filled, return filled_avg_price.
    Falls back to 0.0 if not filled within retries attempts.
    """
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    for attempt in range(retries):
        try:
            time.sleep(delay)
            r = httpx.get(f"{ALPACA_BASE}/v2/orders/{order_id}",
                          headers=headers, timeout=8)
            if r.status_code == 200:
                o = r.json()
                status = o.get("status", "")
                fill   = o.get("filled_avg_price")
                if fill and status in ("filled", "partially_filled"):
                    price = float(fill)
                    log.info(f"  ✅ Fill confirmed: ${price:.3f}/share (attempt {attempt+1})")
                    return price
                log.debug(f"  Waiting for fill (status={status}, attempt {attempt+1}/{retries})...")
        except Exception as e:
            log.debug(f"  Fill poll error: {e}")
    log.warning(f"  ⚠️ Fill price not confirmed after {retries} attempts — using Polygon quote")
    return 0.0


def _place_order(contract_sym: str, qty: int, side: str,
                 limit_price: float = 0.0) -> dict:
    """Place options limit order at ask (buy) or bid (sell) via Alpaca paper.
    Limit orders prevent above-ask fills that destroy P&L on market orders.
    Falls back to market order only if no price provided.
    """
    try:
        from backend.arka.order_guard import validate_options_order
        valid, reason = validate_options_order(contract_sym, qty, side)
        if not valid:
            log.error(f"  🛡️ ORDER BLOCKED: {reason}")
            return {"success": False, "error": reason}
        log.info(f"  🛡️ {reason}")
    except Exception:
        pass

    # Build order payload — limit at ask for buys, bid for sells
    order_type = "limit" if limit_price > 0 else "market"
    payload: dict = {
        "symbol":        contract_sym,
        "qty":           str(qty),
        "side":          side,
        "type":          order_type,
        "time_in_force": "day",
        "asset_class":   "us_option",
    }
    if order_type == "limit":
        payload["limit_price"] = str(round(limit_price, 2))
        log.info(f"  📋 Limit {side.upper()} {qty}x {contract_sym} @ ${limit_price:.2f}")
    else:
        log.info(f"  📋 Market {side.upper()} {qty}x {contract_sym}")

    try:
        r = httpx.post(
            f"{ALPACA_BASE}/v2/orders",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            json=payload,
            timeout=10,
        )
        if r.status_code in (200, 201):
            result = r.json()
            log.info(f"  ✅ {side.upper()} {qty}x {contract_sym} → order {result.get('id','?')[:8]}")
            return {"success": True, "order_id": result.get("id", ""), "qty": qty}
        else:
            log.error(f"  ❌ Order failed {r.status_code}: {r.text[:100]}")
            return {"success": False, "error": r.text[:100]}
    except Exception as e:
        log.error(f"  ❌ Order exception: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD
# ══════════════════════════════════════════════════════════════════════════════

def _post_discord(embed: dict):
    if not DISCORD_WH:
        return
    try:
        httpx.post(DISCORD_WH, json={"username": "ARKA Flow Scalper", "embeds": [embed]}, timeout=6)
    except Exception:
        pass


def _entry_embed(ticker: str, direction: str, contract_sym: str,
                 entry_px: float, tp_px: float, sl_px: float,
                 conf: int, vol_ratio: float, tier: str, premium: float) -> dict:
    color = 0x00FF9D if direction == "CALL" else 0xFF2D55
    icon  = "📈" if direction == "CALL" else "📉"
    prem_str = f"${premium/1e6:.1f}M" if premium >= 1e6 else (
               f"${premium/1e3:.0f}K" if premium >= 1e3 else "—")
    tier_badge = {"MEGA": "🔥⚡ MEGA", "WHALE": "⚡ WHALE", "LARGE": "📊 LARGE"}.get(tier, tier)
    return {
        "title":       f"{icon} FLOW SCALP ENTRY — {ticker} {direction}",
        "color":       color,
        "description": f"**{tier_badge}** institutional flow triggered entry",
        "fields": [
            {"name": "Contract",    "value": f"`{contract_sym}`",              "inline": True},
            {"name": "Entry",       "value": f"${entry_px:.3f}/share",         "inline": True},
            {"name": "Contracts",   "value": "1",                              "inline": True},
            {"name": "Take Profit", "value": f"${tp_px:.3f} (+10%)",           "inline": True},
            {"name": "Stop Loss",   "value": f"${sl_px:.3f} (-25%)",           "inline": True},
            {"name": "Flow Score",  "value": f"{conf}/100 | {vol_ratio:.0f}x vol | {prem_str}", "inline": True},
        ],
        "footer": {"text": f"ARKA Flow Scalper • {datetime.now(ET).strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def _exit_embed(ticker: str, direction: str, contract_sym: str,
                entry_px: float, exit_px: float, reason: str, pnl: float, pnl_pct: float) -> dict:
    color = 0x00FF9D if pnl >= 0 else 0xFF2D55
    icon  = "✅" if pnl >= 0 else "🛑"
    return {
        "title":       f"{icon} FLOW SCALP EXIT — {ticker} {direction}",
        "color":       color,
        "description": f"Closed: **{reason}**",
        "fields": [
            {"name": "Contract",    "value": f"`{contract_sym}`",                  "inline": True},
            {"name": "Entry → Exit","value": f"${entry_px:.3f} → ${exit_px:.3f}", "inline": True},
            {"name": "P&L",        "value": f"{pnl_pct:+.1f}% (${pnl*100:+.2f})", "inline": True},
        ],
        "footer": {"text": f"ARKA Flow Scalper • {datetime.now(ET).strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOGIC
# ══════════════════════════════════════════════════════════════════════════════

class FlowScalper:

    def __init__(self):
        self.state = _load_state()
        self._reset_daily()

    def _reset_daily(self):
        today = date.today().isoformat()
        if self.state.get("daily_date") != today:
            log.info("  📅 New trading day — resetting daily counters")
            self.state["daily_date"]   = today
            self.state["daily_trades"] = 0
            # Clear acted_keys from previous days to avoid growing unbounded
            self.state["acted_keys"] = []
            _save_state(self.state)

    # ── Signal evaluation ────────────────────────────────────────────────────

    def _should_enter(self, ticker: str, sig: dict) -> tuple[bool, str]:
        """Return (should_enter, reason). Checks all trigger conditions."""
        conf       = int(sig.get("confidence", 0))
        is_extreme = bool(sig.get("is_extreme", False))
        vol_ratio  = float(sig.get("vol_oi_ratio", 0))
        tier       = sig.get("tier", "")
        bias       = sig.get("bias", "NEUTRAL")
        direction  = sig.get("direction", "CALL" if bias == "BULLISH" else "PUT")

        if bias == "NEUTRAL":
            return False, "neutral bias — no trade"
        if direction not in ("CALL", "PUT"):
            return False, f"direction {direction} not actionable"
        if ticker.upper() not in ALLOWED_TICKERS:
            return False, f"{ticker} not in allowed ticker list"

        # Deduplicate
        key = _make_signal_key(ticker, sig)
        if key in self.state.get("acted_keys", []):
            return False, "already acted on this signal"

        # Trigger 1: confidence = 100 (always enter)
        if conf >= CONF_ALWAYS:
            return True, f"confidence={conf}% — unconditional entry"

        # Trigger 2: extreme flow + high confidence
        if is_extreme and conf >= CONF_EXTREME_MIN:
            return True, f"EXTREME flow confidence={conf}% ≥ {CONF_EXTREME_MIN}"

        # Trigger 3: MEGA volume sweep (841x+ type signals)
        if vol_ratio >= VOL_MEGA_MIN and conf >= CONF_MEGA_MIN:
            return True, f"MEGA sweep {vol_ratio:.0f}x vol confidence={conf}%"

        # Trigger 4: MEGA tier from flow monitor (tier field)
        if tier == "MEGA" and conf >= CONF_EXTREME_MIN:
            return True, f"MEGA tier confidence={conf}%"

        return False, f"conf={conf} vol={vol_ratio:.0f}x — below thresholds"

    # ── Entry ────────────────────────────────────────────────────────────────

    def _enter_position(self, ticker: str, sig: dict, reason: str):
        """Find contract, verify price, place order, save position."""
        bias      = sig.get("bias", "NEUTRAL")
        direction = sig.get("direction", "CALL" if bias == "BULLISH" else "PUT")
        conf      = int(sig.get("confidence", 0))
        vol_ratio = float(sig.get("vol_oi_ratio", 0))
        tier      = sig.get("tier", "")
        premium   = float(sig.get("premium", 0))

        log.info(f"\n  {'='*50}")
        log.info(f"  🎯 FLOW SCALP TRIGGER: {ticker} {direction}")
        log.info(f"     Reason: {reason}")
        log.info(f"     conf={conf} vol={vol_ratio:.0f}x tier={tier}")

        # Direction/bias consistency check — skip contradictions (e.g. bias=BULLISH dir=PUT)
        if direction == "CALL" and bias == "BEARISH":
            log.warning(f"  ⛔ Direction conflict: bias={bias} but dir=CALL — skip")
            return
        if direction == "PUT" and bias == "BULLISH":
            log.warning(f"  ⛔ Direction conflict: bias={bias} but dir=PUT — skip")
            return

        # 0DTE time gate: no entries after 2:30pm ET (theta decay crushes premium last 90min)
        now_et = datetime.now(ET)
        if now_et.hour > 14 or (now_et.hour == 14 and now_et.minute >= 30):
            log.warning(f"  ⛔ Time gate: {now_et.strftime('%H:%M ET')} — no new 0DTE entries after 2:30pm")
            return

        # Get spot price
        spot = _get_spot(ticker)
        if not spot:
            log.warning(f"  {ticker}: could not get spot price — skip")
            return

        # Find 0DTE ATM contract
        contract = _find_0dte_contract(ticker, direction, spot)
        if not contract:
            log.warning(f"  {ticker}: no 0DTE contract found — skip")
            return

        contract_sym  = contract["symbol"]
        contract_strike = float(contract.get("strike_price", 0))

        # Hard ITM block — never buy ITM options in flow_scalper
        if direction.upper() == "CALL" and contract_strike < spot * 0.999:
            log.warning(f"  ⛔ ITM block: {contract_sym} strike=${contract_strike:.2f} < spot=${spot:.2f} — skip ITM call")
            return
        if direction.upper() == "PUT" and contract_strike > spot * 1.001:
            log.warning(f"  ⛔ ITM block: {contract_sym} strike=${contract_strike:.2f} > spot=${spot:.2f} — skip ITM put")
            return

        # Verify actual premium via Polygon — HARD block if price unavailable or too expensive
        actual_px = _get_option_price(contract_sym, ticker, spot=spot)
        if actual_px is None:
            log.warning(f"  ⛔ {ticker}: could not verify contract price — SKIP (unknown cost)")
            return
        elif actual_px < MIN_PREMIUM:
            log.warning(f"  ⛔ Min premium: {contract_sym} ${actual_px:.2f}/share = "
                        f"${actual_px*100:.0f}/contract < ${MIN_PREMIUM*100:.0f} minimum — SKIP lottery ticket")
            return
        elif actual_px > MAX_PREMIUM:
            log.warning(f"  ⛔ Cost gate: {contract_sym} ${actual_px:.2f}/share = "
                        f"${actual_px*100:.0f}/contract > ${MAX_PREMIUM*100:.0f} ceiling — SKIP")
            return
        else:
            log.info(f"  ✅ Cost OK: {contract_sym} ${actual_px:.2f}/share = ${actual_px*100:.0f}/contract")

        # Fetch live ask price for limit order (prevents above-ask fills from market orders)
        # Use a fresh snapshot to get the true ask at order time
        try:
            _snap_url = f"https://api.polygon.io/v3/snapshot/options/{ticker}/{contract_sym}"
            _snap_r   = httpx.get(_snap_url, params={"apiKey": POLYGON_KEY}, timeout=6)
            if _snap_r.status_code == 200:
                _result = _snap_r.json().get("results", {})
                _ask    = float(_result.get("last_quote", {}).get("ask", 0) or 0)
                limit_px = round(_ask, 2) if _ask > 0 else round(actual_px * 1.05, 2)
            else:
                limit_px = round(actual_px * 1.05, 2)  # 5% above Polygon mid as fallback
        except Exception:
            limit_px = round(actual_px * 1.05, 2)

        log.info(f"  💰 Limit order at ${limit_px:.2f} (ask) vs Polygon mid ${actual_px:.2f}")

        # Place limit order at ask
        result = _place_order(contract_sym, 1, "buy", limit_price=limit_px)
        if not result["success"]:
            log.error(f"  {ticker}: order failed — {result.get('error','?')}")
            return

        # Use actual Alpaca fill price for TP/SL (more accurate than Polygon snapshot)
        order_id  = result.get("order_id", "")
        fill_px   = _get_fill_price(order_id) if order_id else 0.0
        entry_px  = fill_px if fill_px > 0 else actual_px
        if fill_px > 0 and abs(fill_px - actual_px) / actual_px > 0.15:
            log.warning(f"  ⚠️ Slippage: Polygon quoted ${actual_px:.3f}, filled at ${fill_px:.3f} "
                        f"({(fill_px-actual_px)/actual_px*100:+.1f}%)")
        tp_px    = round(entry_px * (1 + TP_PCT), 3)
        sl_px    = round(entry_px * (1 - SL_PCT), 3)

        # Save position
        pos = {
            "ticker":       ticker,
            "direction":    direction,
            "contract_sym": contract_sym,
            "entry_px":     entry_px,
            "tp_px":        tp_px,
            "sl_px":        sl_px,
            "qty":          1,
            "entry_time":   datetime.now(ET).isoformat(),
            "signal_ts":    sig.get("timestamp", ""),
            "signal_conf":  conf,
            "vol_ratio":    vol_ratio,
            "tier":         tier,
            "premium":      premium,
            "peak_px":      entry_px,
            "order_id":     result.get("order_id", ""),
        }
        self.state["positions"][contract_sym] = pos

        # Mark signal as acted
        key = _make_signal_key(ticker, sig)
        self.state.setdefault("acted_keys", []).append(key)
        self.state["daily_trades"] = self.state.get("daily_trades", 0) + 1
        _save_state(self.state)

        log.info(f"  ✅ ENTERED: {ticker} {direction} {contract_sym} "
                 f"entry=${entry_px:.3f} TP=${tp_px:.3f} SL=${sl_px:.3f}")

        # Discord alert
        dte = (date.fromisoformat(contract["expiration_date"]) - date.today()).days
        _post_discord(_entry_embed(ticker, direction, contract_sym,
                                   entry_px, tp_px, sl_px,
                                   conf, vol_ratio, tier, premium))

    # ── Monitor ──────────────────────────────────────────────────────────────

    def _monitor_position(self, contract_sym: str, pos: dict):
        """Check TP/SL/EOD for one open position. Returns True if closed."""
        ticker    = pos["ticker"]
        direction = pos["direction"]
        entry_px  = float(pos["entry_px"])
        tp_px     = float(pos["tp_px"])
        sl_px     = float(pos["sl_px"])
        qty       = int(pos["qty"])

        # Get spot for sanity check (same guard used at entry)
        spot = _get_spot(ticker)

        # Get current price — pass spot so the 20%-of-underlying guard fires
        px = _get_option_price(contract_sym, ticker, spot=spot)
        if px is None:
            log.debug(f"  {contract_sym}: price unavailable")
            return False

        # Hard sanity: option price should never be >5x entry in a single session
        if px > entry_px * 5:
            log.warning(f"  ⚠️ Price sanity FAIL on exit: {contract_sym} "
                        f"${px:.3f} > 5x entry ${entry_px:.3f} — Polygon stale data, skipping")
            return False

        # Track peak (trailing in future)
        pos["peak_px"] = max(float(pos.get("peak_px", entry_px)), px)
        pnl_pct = (px - entry_px) / entry_px * 100

        log.info(f"  📊 {contract_sym}: ${px:.3f} | entry=${entry_px:.3f} "
                 f"P&L={pnl_pct:+.1f}% | TP=${tp_px:.3f} SL=${sl_px:.3f}")

        close_reason = None

        # EOD check first
        if _is_eod():
            close_reason = "EOD close (3:58pm)"

        # Take profit
        elif px >= tp_px:
            close_reason = f"Take profit hit (+{pnl_pct:.1f}%)"

        # Stop loss
        elif px <= sl_px:
            close_reason = f"Stop loss hit ({pnl_pct:.1f}%)"

        if close_reason:
            log.info(f"  🔔 {ticker}: {close_reason} → closing {contract_sym}")
            # Get live bid for limit sell — prevents selling below bid
            try:
                _snap_url = f"https://api.polygon.io/v3/snapshot/options/{ticker}/{contract_sym}"
                _snap_r   = httpx.get(_snap_url, params={"apiKey": POLYGON_KEY}, timeout=6)
                if _snap_r.status_code == 200:
                    _res = _snap_r.json().get("results", {})
                    _bid = float(_res.get("last_quote", {}).get("bid", 0) or 0)
                    exit_limit = round(_bid, 2) if _bid > 0 else 0.0
                else:
                    exit_limit = 0.0
            except Exception:
                exit_limit = 0.0

            result = _place_order(contract_sym, qty, "sell", limit_price=exit_limit)
            if result["success"]:
                # Use actual fill price if available, otherwise use current px
                fill_px   = _get_fill_price(result.get("order_id", ""), retries=4, delay=1.0)
                exit_px   = fill_px if fill_px > 0 else px
                pnl       = (exit_px - entry_px) * qty
                pnl_pct_r = (exit_px - entry_px) / entry_px * 100
                _post_discord(_exit_embed(ticker, direction, contract_sym,
                                          entry_px, exit_px, close_reason,
                                          pnl, pnl_pct_r))
                del self.state["positions"][contract_sym]
                _save_state(self.state)
                log.info(f"  ✅ {ticker}: CLOSED {pnl_pct_r:+.1f}% (${pnl*100:+.2f})")
                return True
            else:
                log.error(f"  ❌ Exit order failed: {result.get('error','?')}")

        return False

    # ── Main scan ────────────────────────────────────────────────────────────

    def run_scan(self):
        """Read flow signals, enter new positions if criteria met."""
        if not _is_market_open():
            log.info("  Market closed — skipping scan")
            return

        open_count = len(self.state.get("positions", {}))
        if open_count >= MAX_POSITIONS:
            log.info(f"  Max positions ({MAX_POSITIONS}) reached — skipping entry scan")
            return

        slots = MAX_POSITIONS - open_count

        # Load flow signals
        if not FLOW_CACHE.exists():
            log.warning("  flow_signals_latest.json not found")
            return

        try:
            signals = json.loads(FLOW_CACHE.read_text())
        except Exception as e:
            log.warning(f"  Could not read flow cache: {e}")
            return

        entered = 0
        for ticker, sig in signals.items():
            if entered >= slots:
                break

            # Check signal freshness: must be from today AND within last 10 minutes
            _sig_ts_raw = sig.get("timestamp", "")
            _skip_stale = True  # default: reject if we can't parse timestamp
            if _sig_ts_raw:
                try:
                    # Also reject signals not from today (catches month-old cache entries)
                    if str(date.today()) not in _sig_ts_raw[:10]:
                        log.debug(f"  {ticker}: signal from {_sig_ts_raw[:10]} — not today, skip")
                        continue
                    sig_ts = datetime.fromisoformat(_sig_ts_raw)
                    age_min = (datetime.now() - sig_ts.replace(tzinfo=None)).total_seconds() / 60
                    if age_min > 10:
                        log.debug(f"  {ticker}: signal {age_min:.1f}min old — skip")
                        continue
                    _skip_stale = False
                except Exception as _e:
                    log.debug(f"  {ticker}: ts parse error ({_e}) — skipping")
            if _skip_stale:
                continue

            should_enter, reason = self._should_enter(ticker, sig)
            if should_enter:
                self._enter_position(ticker, sig, reason)
                entered += 1
            else:
                log.debug(f"  {ticker}: skip — {reason}")

        if entered == 0:
            log.info("  No qualifying flow signals this scan")

    def run_monitor(self):
        """Check all open positions for TP/SL/EOD exits."""
        positions = dict(self.state.get("positions", {}))
        if not positions:
            return

        log.info(f"  Monitoring {len(positions)} flow scalp position(s)...")
        for contract_sym, pos in positions.items():
            try:
                self._monitor_position(contract_sym, pos)
            except Exception as e:
                log.warning(f"  Monitor error ({contract_sym}): {e}")

    def run_forever(self):
        """Main loop: scan + monitor every SCAN_INTERVAL seconds."""
        log.info("=" * 55)
        log.info("  ARKA FLOW SCALPER — Starting")
        log.info(f"  Triggers: conf≥{CONF_ALWAYS}% | extreme≥{CONF_EXTREME_MIN}% | vol≥{VOL_MEGA_MIN}x")
        log.info(f"  TP={TP_PCT*100:.0f}%  SL={SL_PCT*100:.0f}%  MaxPos={MAX_POSITIONS}")
        log.info(f"  Scan interval: {SCAN_INTERVAL}s")
        log.info("=" * 55)

        while True:
            try:
                now = datetime.now(ET)
                log.info(f"\n─── Flow Scan {now.strftime('%H:%M:%S ET')} ───────────────────")
                self._reset_daily()
                self.run_monitor()
                self.run_scan()
            except Exception as e:
                log.error(f"  Loop error: {e}")
            time.sleep(SCAN_INTERVAL)

    def show_status(self):
        """Print current open positions."""
        positions = self.state.get("positions", {})
        if not positions:
            print("No open flow scalp positions.")
            return
        print(f"\n{'='*60}")
        print(f"  ARKA FLOW SCALPER — {len(positions)} open position(s)")
        print(f"{'='*60}")
        for sym, pos in positions.items():
            px = _get_option_price(sym, pos["ticker"]) or 0
            ep = float(pos["entry_px"])
            pnl_pct = (px - ep) / ep * 100 if ep else 0
            print(f"  {pos['ticker']} {pos['direction']} | {sym}")
            print(f"    entry=${ep:.3f}  now=${px:.3f}  P&L={pnl_pct:+.1f}%")
            print(f"    TP=${pos['tp_px']:.3f}  SL=${pos['sl_px']:.3f}")
            print(f"    conf={pos['signal_conf']} vol={pos['vol_ratio']:.0f}x tier={pos['tier']}")
            print()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARKA Flow Scalper")
    parser.add_argument("--once",   action="store_true", help="Single scan + monitor pass")
    parser.add_argument("--status", action="store_true", help="Show open positions")
    args = parser.parse_args()

    scalper = FlowScalper()

    if args.status:
        scalper.show_status()
    elif args.once:
        scalper._reset_daily()
        scalper.run_monitor()
        scalper.run_scan()
    else:
        scalper.run_forever()
