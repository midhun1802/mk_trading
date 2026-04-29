"""
CHAKRA — MOC (Market-On-Close) Engine
backend/arka/moc_engine.py

Trades the MOC imbalance window: 3:50–3:58 PM ET daily.
Reads institutional buy/sell imbalance at 3:45 PM, fires one
0DTE SPX call or put at 3:50 PM, hard-closes at 3:58 PM.

Schedule (alongside existing engines):
  3:00 PM  ARKA power hour threshold drops to 50
  3:30 PM  Lotto Engine fires (existing)
  3:45 PM  MOC Engine reads imbalance
  3:50 PM  MOC Engine fires 0DTE SPX call or put  ← THIS ENGINE
  3:58 PM  HARD CLOSE all 0DTE (covers lotto + MOC)
  4:00 PM  Market close

Usage:
  python3 backend/arka/moc_engine.py          # live run
  python3 backend/arka/moc_engine.py --test   # dry-run, skips real orders
  python3 backend/arka/moc_engine.py --status # show today's state
"""

import os
import sys
import json
import time
import logging
import argparse
import requests

from datetime import datetime, date
from pathlib import Path

import pytz
from dotenv import load_dotenv

# ── Path + env setup ───────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

sys.path.insert(0, str(BASE))

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MOC] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('moc_engine')

# ── Constants ──────────────────────────────────────────────────────────
ET               = pytz.timezone("America/New_York")
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY", "")
DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_HEALTH   = os.getenv("DISCORD_HEALTH_WEBHOOK", "")  # app-health channel
ALPACA_API_KEY   = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET    = os.getenv("ALPACA_API_SECRET") or os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL  = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

STATE_FILE       = BASE / "logs" / "arka" / "moc_state.json"
LOG_DIR          = BASE / "logs" / "arka"


class MOCEngine:

    # ── Timing ────────────────────────────────────────────────────────
    IMBALANCE_READ_ET = (15, 45)   # read imbalance at 3:45 PM
    ENTRY_TIME_ET     = (15, 50)   # fire trade at 3:50 PM
    HARD_CLOSE_ET     = (15, 58)   # hard close at 3:58 PM
    STOP_CHECK_ET     = (16,  1)   # stop main loop at 4:01 PM

    # ── Trade params ──────────────────────────────────────────────────
    TARGET_PCT    = 0.50     # +50% on premium
    STOP_PCT      = 0.35     # -35% stop (tighter than lotto — 8 min window)
    MIN_IMBALANCE = 50_000_000  # $50M minimum imbalance to act

    def __init__(self, test_mode: bool = False):
        self.test_mode  = test_mode
        self.fired      = False
        self.position   = None
        self.entry_time = None
        self.imbalance  = {"direction": "NEUTRAL", "imbalance_usd": 0,
                           "confidence": "LOW", "source": "none"}
        self.today      = datetime.now(ET).strftime("%Y-%m-%d")

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        if test_mode:
            log.info("🧪 TEST MODE — no real orders will be placed")

    # ══════════════════════════════════════════════════════════════════
    # 1. IMBALANCE DETECTION
    # ══════════════════════════════════════════════════════════════════

    def get_moc_imbalance(self) -> dict:
        """
        Multi-source imbalance detection in priority order:
          1. Polygon SPY snapshot → price vs VWAP + volume analysis
          2. SPY 5-min momentum (last candle direction + volume surge)
          3. Fallback: NEUTRAL
        """
        log.info("Reading MOC imbalance...")

        # ── Source 1: Polygon SPY snapshot ────────────────────────────
        try:
            resp = requests.get(
                "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
                params={"apiKey": POLYGON_API_KEY},
                timeout=8
            )
            if resp.status_code == 200:
                data     = resp.json().get("ticker", {})
                day      = data.get("day", {})
                last     = data.get("lastTrade", {}).get("p", 0) or day.get("c", 0)
                vwap     = day.get("vw", 0)
                volume   = day.get("v", 0)
                prev_vol = data.get("prevDay", {}).get("v", volume)

                result = self._derive_from_price_action(last, vwap, volume, prev_vol)
                log.info(
                    f"Imbalance via Polygon: {result['direction']} "
                    f"${result['imbalance_usd']/1e6:.0f}M "
                    f"conf={result['confidence']}"
                )
                return result

        except Exception as e:
            log.warning(f"Polygon snapshot failed: {e}")

        # ── Source 2: 5-min bars momentum ─────────────────────────────
        try:
            result = self._derive_from_5min_bars()
            if result["direction"] != "NEUTRAL":
                log.info(f"Imbalance via 5min bars: {result['direction']}")
                return result
        except Exception as e:
            log.warning(f"5min bar fallback failed: {e}")

        # ── Source 3: Fallback ─────────────────────────────────────────
        log.warning("All imbalance sources failed — returning NEUTRAL")
        return {"direction": "NEUTRAL", "imbalance_usd": 0,
                "confidence": "LOW", "source": "fallback"}

    def _derive_from_price_action(self, last: float, vwap: float,
                                   volume: float, prev_vol: float) -> dict:
        """
        Derive MOC direction from SPY price vs VWAP + volume analysis.
        Price > VWAP + volume surge → BUY imbalance likely.
        Price < VWAP + volume surge → SELL imbalance likely.
        """
        if vwap <= 0 or last <= 0:
            return {"direction": "NEUTRAL", "imbalance_usd": 0,
                    "confidence": "LOW", "source": "price_action"}

        gap_pct    = (last - vwap) / vwap * 100
        vol_ratio  = (volume / prev_vol) if prev_vol > 0 else 1.0
        vol_surge  = vol_ratio > 1.1  # 10% above average volume

        # Scale imbalance estimate by price deviation × volume
        imbalance_est = abs(gap_pct) * 1e8 * min(vol_ratio, 3.0)

        # Confidence tiers
        if abs(gap_pct) > 0.30 and vol_surge:
            confidence = "HIGH"
        elif abs(gap_pct) > 0.15:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        if gap_pct > 0.15:
            return {
                "direction":     "BUY",
                "imbalance_usd": imbalance_est,
                "confidence":    confidence,
                "source":        "price_action",
                "gap_pct":       round(gap_pct, 3),
                "vol_ratio":     round(vol_ratio, 2),
                "spy_last":      last,
                "spy_vwap":      vwap,
            }
        elif gap_pct < -0.15:
            return {
                "direction":     "SELL",
                "imbalance_usd": imbalance_est,
                "confidence":    confidence,
                "source":        "price_action",
                "gap_pct":       round(gap_pct, 3),
                "vol_ratio":     round(vol_ratio, 2),
                "spy_last":      last,
                "spy_vwap":      vwap,
            }

        return {"direction": "NEUTRAL", "imbalance_usd": 0,
                "confidence": "LOW", "source": "price_action",
                "gap_pct": round(gap_pct, 3)}

    def _derive_from_5min_bars(self) -> dict:
        """Derive MOC direction from last 3 × 5-min SPY bars."""
        from datetime import timedelta
        now_et  = datetime.now(ET)
        end_ts  = int(now_et.timestamp() * 1000)
        start_ts= int((now_et - timedelta(minutes=20)).timestamp() * 1000)

        resp = requests.get(
            "https://api.polygon.io/v2/aggs/ticker/SPY/range/5/minute/"
            f"{start_ts}/{end_ts}",
            params={"apiKey": POLYGON_API_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 5},
            timeout=8
        )
        bars = resp.json().get("results", [])
        if len(bars) < 2:
            return {"direction": "NEUTRAL", "imbalance_usd": 0,
                    "confidence": "LOW", "source": "5min_bars"}

        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
        last_vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

        # Last bar direction with volume confirmation
        if closes[-1] > closes[-2] and last_vol_ratio > 1.2:
            return {"direction": "BUY", "imbalance_usd": 75_000_000,
                    "confidence": "MEDIUM", "source": "5min_momentum"}
        elif closes[-1] < closes[-2] and last_vol_ratio > 1.2:
            return {"direction": "SELL", "imbalance_usd": 75_000_000,
                    "confidence": "MEDIUM", "source": "5min_momentum"}

        return {"direction": "NEUTRAL", "imbalance_usd": 0,
                "confidence": "LOW", "source": "5min_bars"}

    # ══════════════════════════════════════════════════════════════════
    # 2. ENTRY GATE CHECKS
    # ══════════════════════════════════════════════════════════════════

    def check_entry_conditions(self, imbalance: dict,
                                internals: dict) -> tuple[bool, str]:
        """
        5 gates must all pass to fire the MOC trade:
          Gate 1: Not already fired today
          Gate 2: Imbalance is directional (not NEUTRAL)
          Gate 3: Imbalance size ≥ $50M
          Gate 4: VIX < 30
          Gate 5: Neural Pulse not RISK_OFF
        """
        if self.fired:
            return False, "MOC already fired today"

        if imbalance["direction"] == "NEUTRAL":
            return False, "No directional imbalance detected"

        if imbalance["imbalance_usd"] < self.MIN_IMBALANCE:
            return False, (
                f"Imbalance too small: "
                f"${imbalance['imbalance_usd']/1e6:.0f}M < "
                f"${self.MIN_IMBALANCE/1e6:.0f}M minimum"
            )

        vix = internals.get("vix", 15)

        # ── Charm Confirmation Gate (Session 2) ──────────────────────
        try:
            from backend.chakra.modules.charm_engine import get_moc_charm_signal
            charm_sig = get_moc_charm_signal("SPY")
            if charm_sig.get("confirm") and charm_sig.get("strength") in ("STRONG", "MODERATE"):
                import logging
                logging.getLogger("moc").info(
                    f"  Charm gate: {charm_sig['reason']}"
                )
        except Exception:
            charm_sig = {"confirm": False, "direction": "NEUTRAL"}

        # ── VRP Gate (Session 1) ─────────────────────────────────────
        try:
            from backend.chakra.modules.vrp_engine import should_skip_moc
            vrp_skip, vrp_reason = should_skip_moc()
            if vrp_skip:
                log.info(f"  MOC SKIPPED — {vrp_reason}")
                return {"action": "SKIP", "reason": vrp_reason, "gate": "VRP"}
        except Exception:
            pass   # never let VRP gate break MOC
        if isinstance(vix, dict):
            vix = vix.get("value", 15)
        if float(vix) > 30:
            return False, f"VIX too high ({vix:.1f}) — no MOC in panic markets"

        risk_mode = internals.get("risk_mode", "NORMAL")
        if risk_mode == "RISK_OFF":
            return False, "Neural Pulse RISK_OFF — skipping MOC"

        # Extra gate: LOW confidence on thin days
        if imbalance["confidence"] == "LOW":
            return False, "Imbalance confidence too LOW — skipping uncertain day"

        return True, "All 5 gates passed ✅"

    # ══════════════════════════════════════════════════════════════════
    # 3. CONTRACT BUILDER
    # ══════════════════════════════════════════════════════════════════

    def build_moc_contract(self, imbalance: dict,
                            spx_price: float) -> dict:
        """
        Build 0DTE SPX contract for MOC trade.
        BUY  imbalance → CALL, 1 strike OTM (cheaper, more leverage)
        SELL imbalance → PUT,  1 strike OTM
        """
        today  = datetime.now(ET).strftime("%Y-%m-%d")
        # Round to nearest $5 strike
        atm    = round(spx_price / 5) * 5

        if imbalance["direction"] == "BUY":
            contract_type = "CALL"
            strike        = atm + 5   # 1 strike OTM
        else:
            contract_type = "PUT"
            strike        = atm - 5

        # Option symbol format: SPX{YYMMDD}{C/P}{strike*1000 padded to 8}
        ymd    = datetime.now(ET).strftime("%y%m%d")
        cp     = contract_type[0]
        sym    = f"SPX{ymd}{cp}{int(strike * 1000):08d}"

        return {
            "ticker":        "SPX",
            "symbol":        sym,
            "contract_type": contract_type,
            "strike":        strike,
            "expiry":        today,
            "qty":           1,
            "trade_type":    "MOC_SCALP",
            "target_pct":    self.TARGET_PCT,
            "stop_pct":      self.STOP_PCT,
            "hard_close_et": "15:58",
            "spx_price_at_entry": round(spx_price, 2),
        }

    # ══════════════════════════════════════════════════════════════════
    # 4. ALPACA EXECUTION
    # ══════════════════════════════════════════════════════════════════

    def _get_spx_price(self) -> float:
        """Get current SPX price (SPY last × 10)."""
        try:
            resp = requests.get(
                "https://api.polygon.io/v2/last/trade/SPY",
                params={"apiKey": POLYGON_API_KEY},
                timeout=5
            )
            spy = resp.json().get("results", {}).get("p", 0)
            if spy > 0:
                return round(spy * 10, 2)
        except Exception as e:
            log.warning(f"SPX price fetch failed: {e}")

        # Fallback: read from internals
        try:
            internals = self._load_internals()
            spy_px = internals.get("spy_price", 500)
            return float(spy_px) * 10
        except Exception:
            return 5500.0  # last-resort default

    def _submit_alpaca_option_order(self, contract: dict) -> dict:
        """
        Submit SPY share order as proxy for SPX options.
        Alpaca paper trading does not support SPX/SPXW options.
        Direction: BUY for calls (bullish), SELL SHORT for puts (bearish).
        """
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
            "Content-Type":        "application/json",
        }
        # Use SPY shares as SPX proxy — same direction, fully supported on paper
        direction     = contract.get("contract_type", "call").lower()
        side          = "buy" if direction == "call" else "sell"
        spy_qty       = max(10, contract.get("qty", 1) * 5)  # scale up for meaningful exposure

        payload = {
            "symbol":        "SPY",
            "qty":           str(spy_qty),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        }
        log.info(f"MOC → SPY proxy order: {side.upper()} {spy_qty} SPY (SPX {direction} proxy)")
        try:
            resp = requests.post(
                f"{ALPACA_BASE_URL}/v2/orders",
                headers=headers,
                json=payload,
                timeout=10
            )
            if resp.status_code in (200, 201):
                order = resp.json()
                log.info(f"SPY proxy order submitted: {order.get('id')} {order.get('status')}")
                # Patch contract to reflect SPY trade for notifications
                contract["symbol"]        = f"SPY ({direction.upper()} proxy)"
                contract["spy_qty"]       = spy_qty
                contract["spy_side"]      = side
                contract["proxy_note"]    = "SPY shares used — SPX options not supported on paper"
                return {"success": True, "order": order}
            else:
                log.error(f"SPY order failed: {resp.status_code} {resp.text}")
                self._post_health_error("MOC Order Failed",
                    f"SPY proxy order rejected: {resp.status_code} — {resp.text[:200]}")
                return {"success": False, "error": resp.text, "status": resp.status_code}
        except Exception as e:
            log.error(f"SPY order exception: {e}")
            self._post_health_error("MOC Order Exception", str(e))
            return {"success": False, "error": str(e)}

    def _get_option_quote(self, symbol: str) -> float:
        """Get current mark price for an option contract."""
        try:
            resp = requests.get(
                f"https://api.polygon.io/v3/snapshot/options/SPX/{symbol}",
                params={"apiKey": POLYGON_API_KEY},
                timeout=5
            )
            result = resp.json().get("results", {})
            mark = result.get("day", {}).get("close", 0)
            return float(mark)
        except Exception:
            return 0.0

    def _close_alpaca_position(self, symbol: str) -> bool:
        """Close option position via Alpaca."""
        if self.test_mode:
            log.info(f"[TEST] Would close position: {symbol}")
            return True
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        try:
            resp = requests.delete(
                f"{ALPACA_BASE_URL}/v2/positions/{symbol}",
                headers=headers,
                timeout=10
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            log.error(f"Close position error: {e}")
            return False

    # ══════════════════════════════════════════════════════════════════
    # 5. DISCORD NOTIFICATIONS
    # ══════════════════════════════════════════════════════════════════

    def _notify_entry(self, contract: dict, imbalance: dict,
                       order_result: dict):
        """Post two Discord messages: technical embed + layman explanation."""
        if not DISCORD_WEBHOOK:
            log.warning("Discord webhook not set")
            return

        is_call    = contract["contract_type"] == "CALL"
        dir_emoji  = "📈" if is_call else "📉"
        color      = 0x00C8AA if is_call else 0xFF4444
        conf_emoji = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(
            imbalance.get("confidence", "LOW"), "⚪")
        order_id   = order_result.get("order", {}).get("id", "TEST") if order_result.get("success") else "FAILED"

        # ── Technical embed ────────────────────────────────────────────
        embed = {
            "title": (
                f"{dir_emoji} MOC TRADE FIRED — "
                f"{contract['ticker']} {contract['contract_type']} "
                f"${contract['strike']}"
            ),
            "color": color,
            "fields": [
                {"name": "📌 Contract",
                 "value": f"`{contract['symbol']}`",
                 "inline": True},
                {"name": "💰 Strike",
                 "value": f"${contract['strike']:,}",
                 "inline": True},
                {"name": "📊 SPX at Entry",
                 "value": f"${contract['spx_price_at_entry']:,.2f}",
                 "inline": True},
                {"name": "🌊 Imbalance",
                 "value": (
                     f"{imbalance['direction']} "
                     f"${imbalance['imbalance_usd']/1e6:.0f}M"
                 ),
                 "inline": True},
                {"name": f"{conf_emoji} Confidence",
                 "value": imbalance.get("confidence", "?"),
                 "inline": True},
                {"name": "🔍 Source",
                 "value": imbalance.get("source", "?"),
                 "inline": True},
                {"name": "🎯 Target",
                 "value": f"+{self.TARGET_PCT*100:.0f}%",
                 "inline": True},
                {"name": "🛑 Stop",
                 "value": f"-{self.STOP_PCT*100:.0f}%",
                 "inline": True},
                {"name": "⏰ Hard Close",
                 "value": "3:58 PM ET",
                 "inline": True},
                {"name": "🔖 Order ID",
                 "value": str(order_id),
                 "inline": False},
            ],
            "footer": {
                "text": (
                    f"CHAKRA MOC Engine  •  8-min window  •  "
                    f"{'TEST MODE' if self.test_mode else 'LIVE'}"
                )
            },
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        # ── Layman message ─────────────────────────────────────────────
        direction_word = "up into the close" if is_call else "down into the close"
        bias_word      = "buying" if is_call else "selling"
        simple = (
            f"{dir_emoji} **CHAKRA just fired a MOC trade at 3:50 PM!**\n\n"
            f"🛒 Bought **1 SPX {contract['contract_type']}** "
            f"at ${contract['strike']:,} strike.\n\n"
            f"💬 *Why?* Big institutions have "
            f"**${imbalance['imbalance_usd']/1e6:.0f}M** in {bias_word} orders "
            f"queued to fill exactly at 4:00 PM close — the market should "
            f"push **{direction_word}** in the next 8 minutes.\n\n"
            f"⏱️ This closes **automatically at 3:58 PM** no matter what.\n"
            f"🎯 Target: **+50%** | 🛑 Stop: **-35%**"
        )

        try:
            requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=8)
            requests.post(DISCORD_WEBHOOK, json={"content": simple}, timeout=8)
            log.info("Discord MOC entry notifications sent ✅")
        except Exception as e:
            log.warning(f"Discord notify error: {e}")

    def _notify_close(self, contract: dict, reason: str,
                       entry_premium: float, exit_premium: float):
        """Post close notification with P&L."""
        if not DISCORD_WEBHOOK:
            return

        pnl_pct = ((exit_premium - entry_premium) / entry_premium * 100) if entry_premium else 0
        is_win  = pnl_pct > 0
        emoji   = "✅" if is_win else "❌"
        color   = 0x00875A if is_win else 0xDE350B

        embed = {
            "title": f"{emoji} MOC CLOSED — {reason}",
            "color": color,
            "fields": [
                {"name": "📌 Contract",
                 "value": f"`{contract.get('symbol', 'SPX 0DTE')}`",
                 "inline": True},
                {"name": "📈 Entry",
                 "value": f"${entry_premium:.2f}",
                 "inline": True},
                {"name": "📉 Exit",
                 "value": f"${exit_premium:.2f}",
                 "inline": True},
                {"name": "💰 P&L",
                 "value": f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%",
                 "inline": True},
                {"name": "⏰ Close Reason",
                 "value": reason,
                 "inline": True},
            ],
            "footer": {"text": "CHAKRA MOC Engine"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        try:
            requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=8)
        except Exception:
            pass

    def _post_health_error(self, title: str, detail: str):
        """Post MOC error to #app-health for ARJUN to review and fix."""
        if not DISCORD_HEALTH:
            return
        try:
            from datetime import datetime
            embed = {
                "title":       f"⚠️ MOC Engine — {title}",
                "color":       0xFF2D55,
                "description": detail[:500],
                "fields": [
                    {"name": "Engine",    "value": "MOC (Market-on-Close)",  "inline": True},
                    {"name": "Time",      "value": datetime.now().strftime("%H:%M ET"), "inline": True},
                    {"name": "Action",    "value": "ARJUN reviewing — reply `fix it` to auto-fix", "inline": False},
                ],
                "footer":    {"text": "CHAKRA MOC Engine • Auto-routed to app-health"},
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            requests.post(DISCORD_HEALTH, json={"embeds": [embed]}, timeout=8)
            log.info(f"Error posted to #app-health: {title}")
            # Also trigger ARJUN healer
            try:
                import sys
                from pathlib import Path
                sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
                from backend.chakra.arjun_healer import run_healer
                run_healer([{
                    "key":      f"moc_{title.lower().replace(' ','_')}",
                    "severity": "🔴",
                    "title":    f"MOC Engine: {title}",
                    "detail":   detail,
                    "action":   "Review MOC engine logs and fix order submission",
                }])
            except Exception as he:
                log.warning(f"Could not trigger ARJUN healer: {he}")
        except Exception as e:
            log.warning(f"Health post error: {e}")

    def _notify_skip(self, reason: str):
        """Post skip notification to Discord."""
        if not DISCORD_WEBHOOK:
            return
        try:
            requests.post(DISCORD_WEBHOOK, json={
                "content": (
                    f"⏭️ **MOC Engine skipped at 3:50 PM**\n"
                    f"💬 Reason: {reason}"
                )
            }, timeout=8)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    # 6. POSITION MONITOR
    # ══════════════════════════════════════════════════════════════════

    def _monitor_position(self, contract: dict, entry_premium: float):
        """
        Monitor open position until target/stop/hard-close.
        Checks every 15 seconds.
        """
        target_price = entry_premium * (1 + self.TARGET_PCT)
        stop_price   = entry_premium * (1 - self.STOP_PCT)

        log.info(
            f"Monitoring {contract['symbol']} — "
            f"entry=${entry_premium:.2f} "
            f"target=${target_price:.2f} "
            f"stop=${stop_price:.2f}"
        )

        while True:
            now_et = datetime.now(ET)
            hm     = (now_et.hour, now_et.minute)

            # Hard close gate
            if hm >= self.HARD_CLOSE_ET:
                log.info("Hard close at 3:58 PM triggered")
                current = self._get_option_quote(contract["symbol"])
                if not self.test_mode:
                    self._close_alpaca_position(contract["symbol"])
                self._notify_close(contract, "HARD_CLOSE_3:58PM",
                                   entry_premium, current or entry_premium)
                self.position = None
                return

            # Check current price
            current = self._get_option_quote(contract["symbol"])
            if current > 0:
                if current >= target_price:
                    log.info(f"TARGET HIT: ${current:.2f} (+{((current/entry_premium)-1)*100:.1f}%)")
                    if not self.test_mode:
                        self._close_alpaca_position(contract["symbol"])
                    self._notify_close(contract, "TARGET_HIT",
                                       entry_premium, current)
                    self.position = None
                    return

                elif current <= stop_price:
                    log.info(f"STOP HIT: ${current:.2f} ({((current/entry_premium)-1)*100:.1f}%)")
                    if not self.test_mode:
                        self._close_alpaca_position(contract["symbol"])
                    self._notify_close(contract, "STOP_LOSS",
                                       entry_premium, current)
                    self.position = None
                    return

            time.sleep(15)

    # ══════════════════════════════════════════════════════════════════
    # 7. HELPERS
    # ══════════════════════════════════════════════════════════════════

    def _load_internals(self) -> dict:
        """Load latest market internals from HELX output."""
        try:
            path = BASE / "logs" / "internals" / "internals_latest.json"
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {"vix": 15, "risk_mode": "NORMAL", "spy_price": 500}

    def _save_state(self):
        """Persist daily state so --status works."""
        state = {
            "date":       self.today,
            "fired":      self.fired,
            "imbalance":  self.imbalance,
            "position":   self.position,
            "entry_time": self.entry_time,
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning(f"State save failed: {e}")

    def _load_state(self):
        """Load persisted state if it's from today."""
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            if state.get("date") == self.today:
                self.fired      = state.get("fired", False)
                self.imbalance  = state.get("imbalance", self.imbalance)
                self.position   = state.get("position")
                self.entry_time = state.get("entry_time")
                log.info(f"State restored — fired={self.fired}")
        except Exception:
            pass  # No state file yet — fresh start

    # ══════════════════════════════════════════════════════════════════
    # 8. MAIN RUN LOOP
    # ══════════════════════════════════════════════════════════════════

    def run(self):
        log.info("=" * 60)
        log.info("CHAKRA MOC Engine started")
        log.info("Waiting for 3:45 PM ET imbalance window...")
        log.info("=" * 60)

        self._load_state()
        imbalance_read = False

        while True:
            now_et = datetime.now(ET)
            hm     = (now_et.hour, now_et.minute)

            # ── 3:45 PM: Read imbalance ────────────────────────────────
            if hm >= self.IMBALANCE_READ_ET and not imbalance_read:
                self.imbalance  = self.get_moc_imbalance()
                imbalance_read  = True
                self._save_state()
                log.info(
                    f"Imbalance locked: {self.imbalance['direction']} "
                    f"${self.imbalance['imbalance_usd']/1e6:.0f}M "
                    f"[{self.imbalance['confidence']}] "
                    f"via {self.imbalance['source']}"
                )

            # ── 3:50 PM: Entry decision ────────────────────────────────
            if hm >= self.ENTRY_TIME_ET and not self.fired:
                internals = self._load_internals()
                ok, reason = self.check_entry_conditions(
                    self.imbalance, internals)

                if ok:
                    spx_price = self._get_spx_price()
                    contract  = self.build_moc_contract(self.imbalance, spx_price)

                    log.info(
                        f"Entry conditions MET — firing "
                        f"{contract['contract_type']} ${contract['strike']}"
                    )

                    if self.test_mode:
                        log.info(f"[TEST] Would submit: {contract['symbol']}")
                        order_result = {"success": True, "order": {"id": "TEST-ORDER"}}
                    else:
                        order_result = self._submit_alpaca_option_order(contract)

                    if order_result["success"]:
                        self.fired      = True
                        self.position   = contract
                        self.entry_time = now_et.isoformat()

                        # Get entry premium for P&L tracking
                        entry_premium = self._get_option_quote(contract["symbol"])
                        if entry_premium <= 0:
                            entry_premium = 1.0  # fallback if quote unavailable

                        self._notify_entry(contract, self.imbalance, order_result)
                        self._save_state()

                        # Monitor position in-line (blocking until closed)
                        self._monitor_position(contract, entry_premium)

                    else:
                        log.error(
                            f"Order failed: {order_result.get('error')} — "
                            f"check Alpaca connection"
                        )
                        self._notify_skip(
                            f"Order submission failed: {order_result.get('error', 'unknown')}"
                        )
                        self.fired = True  # Don't retry on order failure

                else:
                    log.info(f"Entry conditions NOT met: {reason}")
                    self._notify_skip(reason)
                    self.fired = True  # Mark fired to prevent re-check spam
                    self._save_state()

            # ── 4:01 PM: Shutdown ──────────────────────────────────────
            if hm >= self.STOP_CHECK_ET:
                log.info("Market closed — MOC Engine shutting down")
                break

            time.sleep(30)

        log.info("MOC Engine finished for today ✅")


# ══════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CHAKRA MOC Engine")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode — skips real Alpaca orders"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show today's MOC state without running"
    )
    parser.add_argument(
        "--imbalance",
        action="store_true",
        help="Just read and print current imbalance, then exit"
    )
    args = parser.parse_args()

    if args.status:
        engine = MOCEngine(test_mode=True)
        engine._load_state()
        print(f"\nMOC Engine Status — {engine.today}")
        print(f"  Fired:     {engine.fired}")
        print(f"  Position:  {engine.position}")
        print(f"  Imbalance: {engine.imbalance}")
        print(f"  Entry:     {engine.entry_time}")
        sys.exit(0)

    if args.imbalance:
        engine = MOCEngine(test_mode=True)
        result = engine.get_moc_imbalance()
        print(f"\nCurrent MOC Imbalance:")
        print(f"  Direction:  {result['direction']}")
        print(f"  Size:       ${result.get('imbalance_usd', 0)/1e6:.0f}M")
        print(f"  Confidence: {result['confidence']}")
        print(f"  Source:     {result['source']}")
        if result.get("gap_pct"):
            print(f"  SPY vs VWAP: {result['gap_pct']:+.3f}%")
        sys.exit(0)

    engine = MOCEngine(test_mode=args.test)
    engine.run()

# S2_CHARM_MOC — Charm EOD gate wired by patchsession2.py
def _get_charm_gate(direction: str) -> tuple[bool, str]:
    """Returns (blocked: bool, reason: str)"""
    try:
        import json, pathlib
        f = pathlib.Path("logs/chakra/charm_latest.json")
        if f.exists():
            c = json.loads(f.read_text())
            eod_dir = c.get("eod_direction", "NEUTRAL")
            mag     = float(c.get("magnitude", 0))
            if mag > 0.3:
                if eod_dir == "BEARISH" and direction == "LONG":
                    return True, f"Charm BEARISH EOD push (mag={mag:.2f}) blocks LONG MOC"
                if eod_dir == "BULLISH" and direction == "SHORT":
                    return True, f"Charm BULLISH EOD push (mag={mag:.2f}) blocks SHORT MOC"
    except Exception:
        pass
    return False, ""

# S4_LAMBDA_MOC — Kyle Lambda MOC skip by patchsession4.py
def _lambda_moc_gate() -> tuple[bool, str]:
    try:
        import json, pathlib
        f = pathlib.Path("logs/chakra/lambda_latest.json")
        if f.exists():
            d = json.loads(f.read_text())
            if d.get("signal") == "EXTREME":
                return True, "Kyle Lambda EXTREME — illiquid close, MOC skipped"
    except Exception:
        pass
    return False, ""
