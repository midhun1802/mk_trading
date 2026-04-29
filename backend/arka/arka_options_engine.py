"""
arka_options_engine.py — ARKA SPXW 0DTE Options Engine
=======================================================
Uses Polygon Options Advanced on I:SPX to:
1. Get live SPX price from options snapshot underlying.value
2. Find ATM 0DTE SPXW contracts (calls for bullish, puts for bearish)
3. Check options flow heatmap (GEX walls) before entering
4. Execute 0DTE options trades via Alpaca paper trading
5. Force-close all positions by 3:45pm ET

Integration with ARKA equity engine:
- ARKA equity engine sets direction (LONG/SHORT) via conviction score
- This engine translates direction into SPXW options trades
- Both engines share the same Arjun bias and risk limits
"""

import asyncio
import httpx
import json
import os
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

ET        = ZoneInfo("America/New_York")
log       = logging.getLogger("ARKA.Options")
BASE_DIR  = Path(__file__).parent.parent.parent
LOG_DIR   = BASE_DIR / "logs/arka"
LOG_DIR.mkdir(parents=True, exist_ok=True)

POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE  = "https://api.polygon.io"
ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ── Risk config ───────────────────────────────────────────────────────────────
RISK_PCT          = 0.01    # risk 1% of portfolio per 0DTE trade (aggressive)
MAX_PREMIUM_PCT   = 0.005   # never pay more than 0.5% of portfolio for one contract
PROFIT_TARGET_PCT = 0.50    # take profit at 50% gain on premium
STOP_LOSS_PCT     = 0.30    # stop loss at 30% loss on premium
MAX_0DTE_TRADES   = 4       # max 4 options trades per day
FORCE_CLOSE_HOUR  = 15
FORCE_CLOSE_MIN   = 45

# ══════════════════════════════════════════════════════════════════════════════
# POLYGON OPTIONS DATA
# ══════════════════════════════════════════════════════════════════════════════

async def get_spx_snapshot(client: httpx.AsyncClient) -> dict:
    """
    Fetch live SPX options snapshot from Polygon Options Advanced.
    Returns underlying price + full chain with Greeks.
    Uses I:SPX (confirmed working with Options Advanced plan).
    """
    url = f"{POLYGON_BASE}/v3/snapshot/options/I:SPX?limit=250&apiKey={POLYGON_KEY}"
    try:
        r    = await client.get(url, timeout=15)
        data = r.json()
        if data.get("status") == "OK":
            return data
        log.error(f"  SPX snapshot status: {data.get('status')} {data.get('message','')}")
        return {}
    except Exception as e:
        log.error(f"  SPX snapshot error: {e}")
        return {}

async def get_spx_0dte_chain(client: httpx.AsyncClient) -> list:
    """Fetch today's 0DTE SPXW contracts with live Greeks."""
    today = date.today().isoformat()
    url   = (f"{POLYGON_BASE}/v3/snapshot/options/I:SPX"
             f"?expiration_date={today}&limit=250&apiKey={POLYGON_KEY}")
    try:
        r    = await client.get(url, timeout=15)
        data = r.json()
        if data.get("status") == "OK":
            return data.get("results", [])
        return []
    except Exception as e:
        log.error(f"  0DTE chain error: {e}")
        return []

def get_spx_price(snapshot_data: dict) -> float:
    """Extract live SPX price from options snapshot underlying.value."""
    results = snapshot_data.get("results", [])
    for r in results:
        val = r.get("underlying_asset", {}).get("value", 0)
        if val and val > 1000:  # SPX is always > 1000
            return float(val)
    return 0.0

# ══════════════════════════════════════════════════════════════════════════════
# GEX HEATMAP — Follow the Money
# ══════════════════════════════════════════════════════════════════════════════

def build_gex_heatmap(chain: list, spx_price: float) -> dict:
    """
    Build GEX (Gamma Exposure) heatmap from options chain.

    GEX = gamma * open_interest * 100 (shares per contract) * spot_price
    Positive GEX (calls) = dealers are long gamma = price repeller
    Negative GEX (puts)  = dealers are short gamma = price accelerator

    Key levels:
    - Biggest call GEX strike = resistance wall (dealers sell as price approaches)
    - Biggest put GEX strike  = support floor (dealers buy as price approaches)
    - Net GEX positive = low volatility regime (price pinned)
    - Net GEX negative = high volatility regime (price can run)
    """
    gex_by_strike = {}

    for contract in chain:
        greeks  = contract.get("greeks", {})
        details = contract.get("details", {})
        day     = contract.get("day", {})

        gamma   = greeks.get("gamma", 0) or 0
        delta   = greeks.get("delta", 0) or 0
        oi      = day.get("open_interest", contract.get("open_interest", 0)) or 0
        strike  = details.get("strike_price", 0) or 0
        ctype   = details.get("contract_type", "")
        iv      = contract.get("implied_volatility", 0) or 0

        if not strike or not gamma:
            continue

        # GEX contribution: calls add positive, puts subtract
        gex_val = float(gamma) * float(oi) * 100 * float(spx_price)
        if ctype == "put":
            gex_val = -gex_val

        if strike not in gex_by_strike:
            gex_by_strike[strike] = {
                "strike":    strike,
                "call_gex":  0,
                "put_gex":   0,
                "net_gex":   0,
                "call_oi":   0,
                "put_oi":    0,
                "call_iv":   0,
                "put_iv":    0,
                "call_delta": 0,
                "put_delta":  0,
            }

        if ctype == "call":
            gex_by_strike[strike]["call_gex"]   += gex_val
            gex_by_strike[strike]["call_oi"]    += float(oi)
            gex_by_strike[strike]["call_iv"]     = float(iv)
            gex_by_strike[strike]["call_delta"]  = float(delta)
        else:
            gex_by_strike[strike]["put_gex"]    += abs(gex_val)
            gex_by_strike[strike]["put_oi"]     += float(oi)
            gex_by_strike[strike]["put_iv"]      = float(iv)
            gex_by_strike[strike]["put_delta"]   = float(delta)

    for s in gex_by_strike.values():
        s["net_gex"] = s["call_gex"] - s["put_gex"]

    strikes = sorted(gex_by_strike.values(), key=lambda x: x["strike"])

    # Key levels
    call_walls = sorted(strikes, key=lambda x: x["call_gex"], reverse=True)
    put_walls  = sorted(strikes, key=lambda x: x["put_gex"],  reverse=True)

    # Strikes near current price (within 2%)
    nearby = [s for s in strikes if abs(s["strike"] - spx_price) / spx_price < 0.02]

    net_total_gex = sum(s["net_gex"] for s in strikes)

    # IV skew: compare ATM put IV vs ATM call IV
    atm_calls = [s for s in nearby if s["call_iv"] > 0]
    atm_puts  = [s for s in nearby if s["put_iv"]  > 0]
    avg_call_iv = sum(s["call_iv"] for s in atm_calls) / len(atm_calls) if atm_calls else 0
    avg_put_iv  = sum(s["put_iv"]  for s in atm_puts)  / len(atm_puts)  if atm_puts  else 0
    iv_skew     = avg_put_iv - avg_call_iv  # positive = fear/bearish skew

    # Find ATM strike (nearest to current price)
    atm_strike = min(strikes, key=lambda x: abs(x["strike"] - spx_price))["strike"] if strikes else 0

    heatmap = {
        "spx_price":      round(spx_price, 2),
        "atm_strike":     atm_strike,
        "net_total_gex":  round(net_total_gex, 0),
        "regime":         "LOW_VOL" if net_total_gex > 0 else "HIGH_VOL",
        "top_call_wall":  call_walls[0]["strike"] if call_walls else 0,
        "top_put_wall":   put_walls[0]["strike"]  if put_walls  else 0,
        "second_call_wall": call_walls[1]["strike"] if len(call_walls) > 1 else 0,
        "second_put_wall":  put_walls[1]["strike"]  if len(put_walls)  > 1 else 0,
        "iv_skew":        round(iv_skew, 4),
        "bearish_skew":   iv_skew > 0.02,
        "call_put_oi_ratio": (
            sum(s["call_oi"] for s in strikes) /
            max(sum(s["put_oi"] for s in strikes), 1)
        ),
        "nearby_strikes": nearby[:10],
        "all_strikes":    strikes,
        "timestamp":      datetime.now(ET).isoformat(),
    }

    # Trading bias from heatmap
    above_put_wall = spx_price > heatmap["top_put_wall"]
    below_call_wall = spx_price < heatmap["top_call_wall"]
    room_to_call_wall = (heatmap["top_call_wall"] - spx_price) if heatmap["top_call_wall"] > 0 else 0
    room_to_put_wall  = (spx_price - heatmap["top_put_wall"])  if heatmap["top_put_wall"]  > 0 else 0

    # Bullish if: above put wall, below call wall, room to run, no bearish IV skew
    bullish_bias = (above_put_wall and below_call_wall and
                    room_to_call_wall > 5 and not heatmap["bearish_skew"])
    # Bearish if: below call wall, near or below put wall, bearish IV skew
    bearish_bias = (room_to_call_wall < room_to_put_wall or heatmap["bearish_skew"])

    heatmap["bullish_bias"]      = bullish_bias
    heatmap["bearish_bias"]      = bearish_bias
    heatmap["room_to_call_wall"] = round(room_to_call_wall, 2)
    heatmap["room_to_put_wall"]  = round(room_to_put_wall, 2)

    return heatmap

def find_atm_contract(chain: list, spx_price: float, direction: str) -> dict | None:
    """
    Find the best ATM 0DTE contract for the given direction.
    - LONG  → buy ATM call
    - SHORT → buy ATM put
    Prefers delta between 0.40-0.60 (true ATM)
    """
    ctype    = "call" if direction == "LONG" else "put"
    filtered = [
        c for c in chain
        if c.get("details", {}).get("contract_type") == ctype
        and c.get("greeks", {}).get("delta") is not None
    ]

    if not filtered:
        return None

    # Sort by proximity to 0.50 delta (true ATM)
    def atm_score(c):
        delta = abs(float(c.get("greeks", {}).get("delta", 0)))
        return abs(delta - 0.50)

    filtered.sort(key=atm_score)
    best = filtered[0]

    # Validate: must have some liquidity (OI > 0 or IV > 0)
    iv = best.get("implied_volatility", 0)
    if not iv:
        # Fallback: nearest strike to SPX price
        filtered.sort(key=lambda c: abs(
            float(c.get("details", {}).get("strike_price", 99999)) - spx_price
        ))
        best = filtered[0]

    return best

# ══════════════════════════════════════════════════════════════════════════════
# ALPACA OPTIONS ORDER
# ══════════════════════════════════════════════════════════════════════════════

async def get_account(client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(
            f"{ALPACA_BASE}/v2/account",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f"  Alpaca account: {e}")
        return {}

async def get_options_positions(client: httpx.AsyncClient) -> list:
    try:
        r = await client.get(
            f"{ALPACA_BASE}/v2/positions",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=10
        )
        positions = r.json()
        # Filter to SPXW options only
        return [p for p in positions if "SPXW" in p.get("symbol", "") or "SPX" in p.get("symbol", "")]
    except Exception as e:
        log.error(f"  Alpaca positions: {e}")
        return []

async def place_options_order(client: httpx.AsyncClient, contract_ticker: str, qty: int) -> dict:
    """Buy to open an options contract."""
    try:
        r = await client.post(
            f"{ALPACA_BASE}/v2/orders",
            json={
                "symbol":        contract_ticker,
                "qty":           qty,
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
            },
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
                "Content-Type":        "application/json"
            },
            timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f"  Options order error: {e}")
        return {}

async def close_options_position(client: httpx.AsyncClient, symbol: str) -> bool:
    """Close an options position (sell to close)."""
    try:
        r = await client.delete(
            f"{ALPACA_BASE}/v2/positions/{symbol}",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=10
        )
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"  Close position error: {e}")
        return False

# ══════════════════════════════════════════════════════════════════════════════
# MAIN OPTIONS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class ARKAOptionsEngine:
    """
    0DTE SPX options trading engine.
    Called from ARKA equity engine when a high-conviction signal fires.
    Translates equity direction into SPXW options trade with GEX confirmation.
    """

    def __init__(self):
        self.trades_today = 0
        self.positions    = {}  # contract_ticker → {entry_premium, qty, direction}
        self._last_heatmap = None
        self._last_heatmap_time = 0

    async def get_heatmap(self, client: httpx.AsyncClient, force: bool = False) -> dict:
        """Get GEX heatmap, cached for 5 minutes."""
        import time
        if not force and self._last_heatmap and time.time() - self._last_heatmap_time < 300:
            return self._last_heatmap

        chain     = await get_spx_0dte_chain(client)
        snapshot  = await get_spx_snapshot(client)
        spx_price = get_spx_price(snapshot)

        if not chain or spx_price <= 0:
            log.warning("  ⚠️  Could not build heatmap — no chain data")
            return {}

        heatmap = build_gex_heatmap(chain, spx_price)
        self._last_heatmap      = heatmap
        self._last_heatmap_time = time.time()

        # Save to file for dashboard
        heatmap_path = LOG_DIR / f"gex_heatmap_{date.today()}.json"
        with open(heatmap_path, "w") as f:
            # Save summary without huge arrays
            summary = {k: v for k, v in heatmap.items() if k != "all_strikes"}
            json.dump(summary, f, indent=2)

        _bias = 'BULL' if heatmap['bullish_bias'] else ('BEAR' if heatmap['bearish_bias'] else 'NEUTRAL')
        _hmsg = f"  GEX: SPX={spx_price} | CW={heatmap['top_call_wall']} | PW={heatmap['top_put_wall']} | {heatmap['regime']} | {_bias}"
        log.info(_hmsg)
        return heatmap

    async def evaluate_options_trade(
        self,
        direction: str,         # LONG or SHORT from ARKA conviction
        conviction: float,      # ARKA conviction score
        arjun_signal: str,      # Arjun swing signal
        heatmap: dict
    ) -> tuple[bool, str]:
        """
        Decide if an options trade is valid given ARKA conviction + GEX heatmap.
        Returns (should_trade, reason)
        """
        if self.trades_today >= MAX_0DTE_TRADES:
            return False, f"max 0DTE trades reached ({MAX_0DTE_TRADES})"

        if not heatmap:
            return False, "no heatmap data"

        spx_price      = heatmap.get("spx_price", 0)
        top_call_wall  = heatmap.get("top_call_wall", 0)
        top_put_wall   = heatmap.get("top_put_wall", 0)
        bullish_bias   = heatmap.get("bullish_bias", False)
        bearish_bias   = heatmap.get("bearish_bias", False)
        bearish_skew   = heatmap.get("bearish_skew", False)
        regime         = heatmap.get("regime", "LOW_VOL")
        room_to_call   = heatmap.get("room_to_call_wall", 0)
        room_to_put    = heatmap.get("room_to_put_wall",  0)

        reasons = []

        if direction == "LONG":
            # Only go long if heatmap agrees
            if top_put_wall > 0 and spx_price < top_put_wall:
                return False, f"SPX {spx_price} below put wall {top_put_wall} — no long"
            if room_to_call < 5:
                return False, f"only {room_to_call:.0f}pts to call wall — too close"
            if bearish_skew:
                reasons.append("⚠️ bearish IV skew — reducing confidence")
            if arjun_signal == "SELL":
                return False, "Arjun says SELL — no 0DTE call against swing signal"
            if not bullish_bias and conviction < 75:
                return False, f"no bullish GEX bias and conviction {conviction:.0f} < 75"
            reasons.append(f"✅ LONG confirmed: above put wall, {room_to_call:.0f}pts to call wall")

        elif direction == "SHORT":
            # Only go short if heatmap agrees
            if top_call_wall > 0 and spx_price > top_call_wall:
                return False, f"SPX {spx_price} above call wall {top_call_wall} — no short"
            if room_to_put < 5:
                return False, f"only {room_to_put:.0f}pts to put wall — too close"
            if arjun_signal == "BUY":
                return False, "Arjun says BUY — no 0DTE put against swing signal"
            if not bearish_bias and conviction < 75:
                return False, f"no bearish GEX bias and conviction {conviction:.0f} < 75"
            reasons.append(f"✅ SHORT confirmed: below call wall, {room_to_put:.0f}pts to put wall")

        log.info(f"  ✅ Options trade approved: {direction} | {' | '.join(reasons)}")
        return True, " | ".join(reasons)

    async def enter_options_trade(
        self,
        client: httpx.AsyncClient,
        direction: str,
        conviction: float,
        heatmap: dict
    ) -> bool:
        """Execute a 0DTE options trade."""
        spx_price = heatmap.get("spx_price", 0)
        if spx_price <= 0:
            log.error("  ❌ No SPX price for options entry")
            return False

        # Get 0DTE chain
        chain = await get_spx_0dte_chain(client)
        if not chain:
            log.error("  ❌ No 0DTE chain available")
            return False

        # Find ATM contract
        contract = find_atm_contract(chain, spx_price, direction)
        if not contract:
            log.error(f"  ❌ No ATM {direction} contract found")
            return False

        details  = contract.get("details", {})
        greeks   = contract.get("greeks", {})
        ticker   = details.get("ticker", "")
        strike   = details.get("strike_price", 0)
        delta    = greeks.get("delta", 0)
        iv       = contract.get("implied_volatility", 0)

        # Get premium from last trade price or estimate from IV
        day_data = contract.get("day", {})
        premium  = float(day_data.get("close", 0) or day_data.get("last", 0) or 0)
        if premium <= 0:
            # Estimate from IV: premium ≈ IV * SPX * sqrt(1/252) * 0.4 (ATM approx)
            import math
            premium = float(iv) * float(spx_price) * math.sqrt(1/252) * 0.4 if iv else 5.0

        if premium <= 0:
            log.error(f"  ❌ Cannot determine premium for {ticker}")
            return False

        # Position sizing
        acct = await get_account(client)
        pv   = float(acct.get("portfolio_value", 100000))
        risk_budget   = pv * RISK_PCT
        max_premium   = pv * MAX_PREMIUM_PCT
        cost_per_contract = premium * 100  # 1 contract = 100 shares

        if cost_per_contract > max_premium * 100:
            log.info(f"  ⚠️  Premium ${cost_per_contract:.0f} > max ${max_premium*100:.0f} — skipping")
            return False

        qty = max(1, int(risk_budget / cost_per_contract))
        total_cost = qty * cost_per_contract

        log.info(f"\n  ⚡ 0DTE OPTIONS ENTRY")
        log.info(f"     Contract: {ticker}")
        log.info(f"     Strike:   ${strike}  |  Direction: {direction}")
        _iv_str = f"     Delta: {delta:.3f}  |  IV: {float(iv):.1%}" if iv else f"     Delta: {delta:.3f}"
        log.info(_iv_str)
        log.info(f"     Premium:  ${premium:.2f}/share  |  Cost: ${total_cost:.0f}")
        log.info(f"     Qty:      {qty} contract(s)  |  SPX: ${spx_price:.2f}")
        log.info(f"     Targets:  +50% (${premium*1.5:.2f}) | Stop: -30% (${premium*0.7:.2f})")

        # Place order
        order = await place_options_order(client, ticker, qty)
        if order.get("id"):
            self.trades_today += 1
            self.positions[ticker] = {
                "entry_premium":  premium,
                "qty":            qty,
                "direction":      direction,
                "strike":         strike,
                "spx_at_entry":   spx_price,
                "profit_target":  premium * (1 + PROFIT_TARGET_PCT),
                "stop_loss":      premium * (1 - STOP_LOSS_PCT),
                "entry_time":     datetime.now(ET).isoformat(),
            }
            log.info(f"  ✅ ORDER PLACED: {ticker} x{qty}")
            return True
        else:
            log.error(f"  ❌ Order failed: {order}")
            return False

    async def check_exit_conditions(self, client: httpx.AsyncClient, chain: list):
        """Check if any open positions hit profit target or stop loss."""
        if not self.positions:
            return

        now = datetime.now(ET)

        # Force close at 3:45pm
        if now.hour == FORCE_CLOSE_HOUR and now.minute >= FORCE_CLOSE_MIN:
            log.info(f"  ⏰ {FORCE_CLOSE_HOUR}:{FORCE_CLOSE_MIN}pm — force closing all 0DTE positions")
            for ticker in list(self.positions.keys()):
                await close_options_position(client, ticker)
                log.info(f"  Closed {ticker} (EOD)")
            self.positions.clear()
            return

        # Build live premium map from chain
        premium_map = {}
        for c in chain:
            t = c.get("details", {}).get("ticker", "")
            d = c.get("day", {})
            p = float(d.get("last", 0) or d.get("close", 0) or 0)
            if t and p > 0:
                premium_map[t] = p

        for ticker, pos in list(self.positions.items()):
            live_premium = premium_map.get(ticker, 0)
            if live_premium <= 0:
                continue

            entry   = pos["entry_premium"]
            target  = pos["profit_target"]
            stop    = pos["stop_loss"]
            pct_pnl = (live_premium - entry) / entry

            if live_premium >= target:
                log.info(f"  🎯 PROFIT TARGET: {ticker} premium ${live_premium:.2f} (+{pct_pnl:.0%})")
                await close_options_position(client, ticker)
                del self.positions[ticker]
            elif live_premium <= stop:
                log.info(f"  🛑 STOP LOSS: {ticker} premium ${live_premium:.2f} ({pct_pnl:.0%})")
                await close_options_position(client, ticker)
                del self.positions[ticker]

    async def run_once(
        self,
        client: httpx.AsyncClient,
        arka_direction: str,    # LONG or SHORT from equity engine
        arka_conviction: float, # conviction score
        arjun_signal: str       # BUY/SELL/HOLD from Arjun
    ) -> dict:
        """
        Single scan: evaluate heatmap, decide if options trade is valid, execute.
        Returns summary dict for logging.
        """
        summary = {
            "action":    "SKIP",
            "reason":    "",
            "heatmap":   {},
            "trade":     None,
        }

        # Get heatmap
        heatmap = await self.get_heatmap(client)
        summary["heatmap"] = {
            k: v for k, v in heatmap.items()
            if k not in ("all_strikes", "nearby_strikes")
        } if heatmap else {}

        if not heatmap:
            summary["reason"] = "no heatmap"
            return summary

        # Check exit conditions on existing positions
        chain = await get_spx_0dte_chain(client)
        await self.check_exit_conditions(client, chain)

        # Evaluate new trade
        if arka_direction not in ("LONG", "SHORT"):
            summary["reason"] = f"no direction ({arka_direction})"
            return summary

        should_trade, reason = await self.evaluate_options_trade(
            arka_direction, arka_conviction, arjun_signal, heatmap
        )

        if should_trade:
            success = await self.enter_options_trade(
                client, arka_direction, arka_conviction, heatmap
            )
            summary["action"] = "TRADE" if success else "FAILED"
            summary["reason"] = reason
        else:
            summary["action"] = "SKIP"
            summary["reason"] = reason

        return summary


# ── Standalone run (for testing) ──────────────────────────────────────────────
async def test_options_engine():
    """Quick test: fetch heatmap and print key levels."""
    engine = ARKAOptionsEngine()
    async with httpx.AsyncClient() as client:
        print("\\nFetching SPX 0DTE heatmap...")
        heatmap = await engine.get_heatmap(client, force=True)
        if heatmap:
            print(f"  SPX Price:      ${heatmap['spx_price']:.2f}")
            print(f"  Top Call Wall:  ${heatmap['top_call_wall']:.0f}")
            print(f"  Top Put Wall:   ${heatmap['top_put_wall']:.0f}")
            print(f"  Net GEX:        {heatmap['net_total_gex']:,.0f}")
            print(f"  Regime:         {heatmap['regime']}")
            print(f"  IV Skew:        {heatmap['iv_skew']:.4f} ({'BEARISH' if heatmap['bearish_skew'] else 'NEUTRAL'})")
            print(f"  Bullish Bias:   {heatmap['bullish_bias']}")
            print(f"  Bearish Bias:   {heatmap['bearish_bias']}")
            print(f"  Room to Call:   {heatmap['room_to_call_wall']:.0f} pts")
            print(f"  Room to Put:    {heatmap['room_to_put_wall']:.0f} pts")
        else:
            print("  ❌ No heatmap data returned")

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(test_options_engine())

# ── IV Rank & Regime ──────────────────────────────────────────────────────

def calculate_iv_rank(current_iv, historical_iv_array):
    """IV Rank = (current - min) / (max - min) * 100. Returns 0-100."""
    iv_high = max(historical_iv_array)
    iv_low  = min(historical_iv_array)
    if iv_high == iv_low:
        return 50
    return round(((current_iv - iv_low) / (iv_high - iv_low)) * 100, 1)

def get_iv_regime(iv_rank):
    """Returns EXPENSIVE (>70), CHEAP (<30), or NEUTRAL."""
    if iv_rank > 70:
        return "EXPENSIVE"   # Sell premium: credit spreads, iron condors
    elif iv_rank < 30:
        return "CHEAP"       # Buy options: long calls/puts, debit spreads
    return "NEUTRAL"

def enrich_gex_with_iv(gex_data, current_iv, historical_iv_52w):
    """Add iv_rank and iv_regime fields to existing GEX heatmap dict."""
    iv_rank = calculate_iv_rank(current_iv, historical_iv_52w)
    gex_data["iv_rank"]   = iv_rank
    gex_data["iv_regime"] = get_iv_regime(iv_rank)
    return gex_data
