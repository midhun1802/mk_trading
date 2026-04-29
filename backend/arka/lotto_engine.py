"""
CHAKRA Lotto Engine — Power Hour 0DTE Single Trade
Trigger: 3:30 PM ET | Conviction >= 50 | Max 1 trade/day
Target: +100% | Stop: -50% | Hard close: 3:58 PM
"""
import os, json, logging
from datetime import datetime, date, time
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

ET       = ZoneInfo("America/New_York")
LOG_DIR  = BASE / "logs" / "arka"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("CHAKRA.Lotto")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE   = "https://paper-api.alpaca.markets"
POLYGON_KEY   = os.getenv("POLYGON_API_KEY", "")

def _state_file() -> Path:
    return LOG_DIR / f"lotto_state_{date.today()}.json"

def _notify_lotto_trade(trade_record: dict, pulse_score: int = 50):
    """Send Discord notification for lotto trade — uses main discord_notifier."""
    try:
        import asyncio, json as _j, os as _o
        from backend.arka.discord_notifier import post_embed, post_embed_sync
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ET      = ZoneInfo("America/New_York")
        contract= trade_record.get("contract", {})
        direction = trade_record.get("direction", "BUY")
        is_call = direction in ("BUY", "CALL")
        ticker  = trade_record.get("ticker", "SPX")
        strike  = contract.get("strike", "?")
        premium = float(contract.get("mark", 0))
        conv    = int(trade_record.get("conviction", 50))
        gex_reg = trade_record.get("gex_regime", "UNKNOWN")
        now_str = datetime.now(ET).strftime("%I:%M %p ET")

        # Read pulse from internals
        pulse = pulse_score
        try:
            ip = "logs/internals/internals_latest.json"
            if _o.path.exists(ip):
                with open(ip) as f: pulse = _j.load(f).get("neural_pulse",{}).get("score", pulse_score)
        except: pass

        emoji = "🎰🟢" if is_call else "🎰🔴"
        color = 0x00FF88 if is_call else 0xFF4444
        ct    = "CALL" if is_call else "PUT"

        embed = {
            "color":  color,
            "author": {"name": f"{emoji} LOTTO TRADE — {ticker} 0DTE {ct}"},
            "description": "**⚡ Power Hour lotto position opened (3:30 PM ET)**",
            "fields": [
                {"name": "📌 Contract",     "value": f"`{ticker} 0DTE {strike} {ct}`",     "inline": True},
                {"name": "💰 Premium",      "value": f"${premium:.2f} (1 contract)",         "inline": True},
                {"name": "🧠 Conviction",   "value": f"{conv}/100",                          "inline": True},
                {"name": "⚡ Neural Pulse", "value": f"{pulse}/100 {'🟢' if int(pulse)>=65 else '🟡' if int(pulse)>=50 else '🔴'}", "inline": True},
                {"name": "📊 GEX Regime",   "value": f"{'🔴' if 'NEG' in str(gex_reg) else '🟢'} {gex_reg}", "inline": True},
                {"name": "⏰ Session",      "value": "⚡ Power Hour (3:30–3:58 PM)",         "inline": True},
                {"name": "🎯 Target",       "value": f"+100% → ${premium*2:.2f}",            "inline": True},
                {"name": "🛑 Stop",         "value": f"-50% → ${premium*0.5:.2f}",           "inline": True},
                {"name": "🔒 Hard Close",   "value": "3:58 PM ET — auto exit regardless",    "inline": True},
                {"name": "📝 Rules",        "value": "Max 1 contract • Max 1 lotto/day • No override", "inline": False},
            ],
            "footer": {"text": f"CHAKRA Lotto Engine • {now_str} • Paper Trading"}
        }

        # Rich embed
        asyncio.run(post_embed(embed, username="ARKA Lotto 🎰"))

        # Layman message
        reason_parts = [f"conviction is {conv}/100"]
        if int(pulse) >= 65: reason_parts.append("market internals are strong")
        if "POSITIVE" in str(gex_reg): reason_parts.append("dealers are stabilizing the market")
        elif "NEGATIVE" in str(gex_reg): reason_parts.append("dealers may amplify the move")

        layman = (
            f"🎰 **ARKA just placed a Power Hour LOTTO!**\n\n"
            f"🛒 Buying **1 {ticker} {ct}** at **${strike} strike** "
            f"expiring TODAY for **${premium:.2f}** (1 contract = **${premium*100:.0f}** total).\n\n"
            f"💬 *Why?* Because {', and '.join(reason_parts)}.\n\n"
            f"🎯 Target: **double it (+100%)** → ${premium*2:.2f} "
            f"| 🛑 Stop: **-50%** → ${premium*0.5:.2f}\n"
            f"⏰ *Hard close at 3:58 PM no matter what.*"
        )
        post_embed_sync({"description": layman, "color": color}, username="ARKA Lotto 🎰")

    except Exception as e:
        log.error(f"  [Lotto] Discord notify failed: {e}")


def _lotto_discord_exit(symbol: str, entry: float, exit_px: float, reason: str) -> None:
    """Send Discord notification when lotto position closes (stop, target, or EOD)."""
    try:
        from backend.arka.discord_notifier import post_embed_sync, CH_ARKA_LOTTO, WEBHOOK_URL
        from zoneinfo import ZoneInfo as _ZI
        from datetime import datetime as _dt

        _ET      = _ZI("America/New_York")
        _won     = exit_px > entry
        _pnl_pct = ((exit_px - entry) / entry * 100) if entry > 0 else 0
        _pnl_abs = round((exit_px - entry) * 100, 2)   # 1 contract = 100 shares
        _color   = 0x00D084 if _won else 0xFF3D5A
        _icon    = "🎯" if _won else "🛑"
        _ts      = _dt.now(_ET).strftime("%I:%M %p ET")
        _ch      = CH_ARKA_LOTTO if CH_ARKA_LOTTO else WEBHOOK_URL

        embed = {
            "color":  _color,
            "author": {"name": f"{_icon} LOTTO CLOSED — {symbol}"},
            "description": f"{'🏆 Winner! Target hit.' if _won else '📉 Stopped out. On to the next.'}",
            "fields": [
                {"name": "📋 Contract",  "value": f"`{symbol}`",                              "inline": True},
                {"name": "📥 Entry",     "value": f"${entry:.2f}",                            "inline": True},
                {"name": "📤 Exit",      "value": f"${exit_px:.2f}" if exit_px != entry else "EOD (market close)", "inline": True},
                {"name": "💰 P&L",       "value": f"**{'+'if _won else ''}{_pnl_pct:.1f}%** (≈ {'+'if _won else ''}{_pnl_abs:.0f}$)", "inline": True},
                {"name": "📝 Reason",    "value": reason,                                     "inline": True},
            ],
            "footer": {"text": f"ARKA Lotto Engine • {_ts} • Paper Trading"}
        }
        post_embed_sync(embed, username="ARKA Lotto 🎰", webhook_url=_ch)
        log.info(f"  [Lotto] 📣 Exit Discord posted: {symbol} {reason}")
    except Exception as _e:
        log.error(f"  [Lotto] Exit Discord failed: {_e}")


# ── State management ──────────────────────────────────────────────────────

def load_state() -> dict:
    f = _state_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {
        "date":         date.today().isoformat(),
        "enabled":      True,
        "trades_today": 0,
        "spent_today":  0.0,
        "active":       False,
        "trade":        None,
        "status":       "WATCHING",
        "trigger_time": "15:00:00",
        "last_checked": None,
    }

def save_state(state: dict):
    _state_file().write_text(json.dumps(state, indent=2, default=str))

# ── Market data ───────────────────────────────────────────────────────────

def get_conviction_and_gex() -> dict:
    """Read latest ARKA conviction + GEX from log files."""
    try:
        import glob
        # Read conviction from ARKA summary (scan_history has latest scores)
        conviction = {"SPY": 0, "QQQ": 0}
        summary_files = sorted(glob.glob(str(LOG_DIR / "summary_*.json")), reverse=True)
        if summary_files:
            data = json.loads(Path(summary_files[0]).read_text())
            # Get latest score per ticker from scan_history
            for scan in reversed(data.get("scan_history", [])):
                tk = scan.get("ticker", "")
                if tk in conviction and conviction[tk] == 0:
                    conviction[tk] = scan.get("score", 0)
                if all(v > 0 for v in conviction.values()):
                    break

        gex_regime = "UNKNOWN"
        # Prefer gex_latest_SPY.json written by options_engine every 5 min
        import time as _ltime
        _gex_fresh_file = LOG_DIR.parent / "gex" / "gex_latest_SPY.json"
        _used_fresh = False
        if _gex_fresh_file.exists():
            try:
                _gex_fresh = json.loads(_gex_fresh_file.read_text())
                _age_s = _ltime.time() - float(_gex_fresh.get("ts", 0))
                if _age_s < 1800:  # under 30 min
                    gex_regime = _gex_fresh.get("regime", "UNKNOWN")
                    _used_fresh = True
                    log.debug(f"  [Lotto] GEX regime={gex_regime} (fresh, age={_age_s:.0f}s)")
            except Exception as _ge:
                log.debug(f"  [Lotto] gex_latest_SPY read failed: {_ge}")
        if not _used_fresh:
            gex_files = sorted(glob.glob(str(LOG_DIR / "gex_heatmap_*.json")), reverse=True)
            if gex_files:
                gex = json.loads(Path(gex_files[0]).read_text())
                gex_regime = gex.get("regime", "UNKNOWN")
                log.debug(f"  [Lotto] GEX regime={gex_regime} (heatmap fallback)")

        best_ticker  = max(conviction, key=conviction.get)
        best_score   = conviction[best_ticker]

        # Primary conviction: flow_signals_latest.json is {ticker: {bias, confidence, ...}}
        flow_score     = 0
        flow_ticker    = best_ticker
        flow_direction = "BULLISH"
        try:
            flow_file = LOG_DIR.parent / "chakra" / "flow_signals_latest.json"
            if flow_file.exists():
                flow_data = json.loads(flow_file.read_text())
                for tk in ("SPY", "QQQ", "SPX"):
                    sig = flow_data.get(tk)
                    if not isinstance(sig, dict):
                        continue
                    conf = float(sig.get("confidence", 0))
                    bias = sig.get("bias", "NEUTRAL")
                    if conf > flow_score and bias in ("BULLISH", "BEARISH"):
                        flow_score     = conf
                        flow_ticker    = tk
                        flow_direction = bias
        except Exception as _fe:
            log.warning(f"  [Lotto] Flow read error: {_fe}")

        # Flow is the primary signal — ARKA score is secondary fallback
        effective_score     = flow_score if flow_score > 0 else best_score
        effective_ticker    = flow_ticker if flow_score > 0 else best_ticker
        effective_direction = flow_direction if flow_score > 0 else "BULLISH"

        return {
            "best_ticker":      effective_ticker,
            "best_score":       effective_score,
            "conviction":       conviction,
            "gex_regime":       gex_regime,
            "qualifies":        effective_score >= 60,
            "flow_score":       flow_score,
            "flow_direction":   effective_direction,
        }
    except Exception as e:
        log.error(f"  [Lotto] Failed to read conviction/GEX: {e}")
        return {"best_ticker": "SPY", "best_score": 0, "conviction": {}, "gex_regime": "UNKNOWN", "qualifies": False}

def get_atm_option(ticker: str, direction: str) -> dict:
    """Find OTM 0DTE option contract from Polygon (~1% OTM target, max 3% OTM)."""
    import httpx
    today_str = date.today().isoformat()
    option_type = "call" if direction == "BUY" else "put"
    try:
        # Get current price
        r = httpx.get(
            f"https://api.polygon.io/v2/last/trade/{ticker}",
            params={"apiKey": POLYGON_KEY}, timeout=8
        )
        price = r.json().get("results", {}).get("p", 0)
        if not price:
            return {}

        # OTM target: ~0.35% beyond spot
        if option_type == "call":
            otm_target = price * 1.0035  # 0.35% above spot
            strike_lo  = str(round(price * 1.000, 0))   # no ITM calls
            strike_hi  = str(round(price * 1.030, 0))   # max 3% OTM
        else:
            otm_target = price * 0.9965  # 0.35% below spot
            strike_lo  = str(round(price * 0.970, 0))   # max 3% OTM
            strike_hi  = str(round(price * 1.000, 0))   # no ITM puts

        # Fetch contracts in OTM range
        r2 = httpx.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker}",
            params={
                "apiKey": POLYGON_KEY,
                "expiration_date": today_str,
                "contract_type": option_type,
                "strike_price.gte": strike_lo,
                "strike_price.lte": strike_hi,
                "limit": 20,
            },
            timeout=8,
        )
        results = r2.json().get("results", [])
        if not results:
            return {}

        # Sort by closest to OTM target, filter bad delta range (0.20–0.50)
        valid = []
        for opt in results:
            greeks = opt.get("greeks") or {}
            delta  = abs(float(greeks.get("delta", 0.35)))
            strike = float(opt.get("details", {}).get("strike_price", 0))
            if strike == 0:
                continue
            # Hard ITM block
            if option_type == "call" and strike < price * 0.999:
                continue
            if option_type == "put" and strike > price * 1.001:
                continue
            # Far-OTM block
            otm_pct = abs(strike - price) / price
            if otm_pct > 0.030:
                continue
            valid.append((abs(strike - otm_target), opt))

        if not valid:
            return {}

        valid.sort(key=lambda x: x[0])
        opt = valid[0][1]
        details = opt.get("details", {})
        mark = opt.get("day", {}).get("close", 0) or opt.get("last_quote", {}).get("midpoint", 0)
        chosen_strike = float(details.get("strike_price", 0))
        exp = date.today().strftime("%y%m%d")
        strike_str = f"{int(chosen_strike * 1000):08d}"
        option_type_char = "C" if option_type == "call" else "P"
        fallback_sym = f"O:{ticker}{exp}{option_type_char}{strike_str}"
        return {
            "symbol":     details.get("ticker", fallback_sym),
            "strike":     chosen_strike,
            "expiry":     today_str,
            "type":       option_type,
            "mark":       round(mark, 2),
            "underlying": ticker,
        }
    except Exception as e:
        log.error(f"  [Lotto] Option lookup failed: {e}")
    return {}

def place_lotto_order(contract: dict, entry_price: float, mkt: dict = None) -> dict:
    """Place ATM 0DTE order via Alpaca. 1 contract only. OPTIONS ONLY."""
    import httpx
    if mkt is None:
        mkt = {}
    try:
        _lotto_sym = contract.get("symbol", "").removeprefix("O:").strip()  # strip Polygon O: prefix for Alpaca
        _lotto_qty = 1  # always 1 contract — lotto rules: no scaling
        # ── Options-only guard ────────────────────────────────────────────
        from backend.arka.order_guard import validate_options_order as _voo_lotto
        _ok, _why = _voo_lotto(_lotto_sym, _lotto_qty, "buy")
        if not _ok:
            log.error(f"  [Lotto] 🛡️  BLOCKED: {_why}")
            return {"error": _why, "blocked": True}
        log.info(f"  [Lotto] 🛡️  {_why}")
        r = httpx.post(
            f"{ALPACA_BASE}/v2/orders",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            json={
                "symbol":        _lotto_sym,
                "qty":           str(_lotto_qty),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
                "asset_class":   "us_option",
            },
            timeout=10,
        )
        result = r.json()
        if result.get("id"):
            log.info(f"  [Lotto] ✅ Order placed: {contract['symbol']} | ID: {result['id']}")
            # Discord notification
            try:
                from backend.arka.discord_notifier import notify_lotto_entry_sync
                notify_lotto_entry_sync(
                    ticker=contract.get("underlying", "SPY"),
                    strike=contract.get("strike", 0),
                    contract_type=contract.get("type", "call"),
                    premium=entry_price,
                    conviction=mkt.get("best_score", 0),
                    gex_regime=mkt.get("gex_regime", "UNKNOWN"),
                )
            except Exception as _ne:
                log.warning(f"  [Lotto] Discord notify failed: {_ne}")
            return {"success": True, "order_id": result["id"], "contract": contract}
        else:
            log.error(f"  [Lotto] Order failed: {result}")
            return {"success": False, "error": str(result)}
    except Exception as e:
        log.error(f"  [Lotto] Order exception: {e}")
        return {"success": False, "error": str(e)}

def close_lotto_position(symbol: str, reason: str = "AUTO_CLOSE") -> dict:
    """Market sell to close lotto position."""
    import httpx
    try:
        r = httpx.delete(
            f"{ALPACA_BASE}/v2/positions/{symbol}",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=10,
        )
        log.info(f"  [Lotto] 🔴 Closed {symbol} — reason: {reason}")
        return {"success": True, "reason": reason}
    except Exception as e:
        log.error(f"  [Lotto] Close failed: {e}")
        return {"success": False, "error": str(e)}

# ── Wall Rejection Pattern Detector ─────────────────────────────────────────
# Detects: Parabolic spike into GEX wall + rejection candle on last 10 bars.
# Pattern seen in image: V-bottom → sharp rally → hits call wall → bearish rejection.
# Returns {"detected": bool, "direction": "PUT"|"CALL", "confidence": int, "reason": str}
_WALL_REJECTION_NOTIFIED: set = set()   # tracks (ticker, direction, date) already alerted

def detect_wall_rejection_setup(ticker: str = "SPY") -> dict:
    """
    Scan last 10 1-min bars for:
      • Parabolic move (≥0.8% in 6 bars) OR V-reversal (sharp drop + sharp recovery)
      • Price within 0.4% of a known GEX wall (call or put)
      • Rejection candle: long wick pointing INTO the wall, body closes away
    """
    import httpx
    today_str = date.today().isoformat()
    NO = {"detected": False}

    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{today_str}/{today_str}",
            params={"apiKey": POLYGON_KEY, "sort": "asc", "limit": 500},
            timeout=8,
        )
        bars = r.json().get("results", [])
    except Exception as e:
        log.debug(f"  [Lotto.WR] Bar fetch failed for {ticker}: {e}")
        return NO

    if len(bars) < 10:
        return dict(NO, reason="Not enough bars")

    bars = bars[-10:]   # last 10 candles
    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    spot   = closes[-1]

    # ── Parabolic move over last 6 bars ─────────────────────────────────
    move_up   = (closes[-1] - closes[-6]) / closes[-6] * 100
    move_down = (closes[-6] - closes[-1]) / closes[-6] * 100

    # ── V-reversal detection (trough in middle, recovery at end) ────────
    trough      = min(lows[1:8])
    v_drop      = (closes[0]  - trough) / closes[0] * 100
    v_recover   = (closes[-1] - trough) / trough    * 100
    is_v_reversal = v_drop > 0.35 and v_recover > 0.55

    # ── Last candle rejection analysis ──────────────────────────────────
    last    = bars[-1]
    h, l, o, c = last["h"], last["l"], last["o"], last["c"]
    rng     = h - l
    if rng < 1e-6:
        return dict(NO, reason="Flat candle — no range")
    upper_wick      = h - max(o, c)
    lower_wick      = min(o, c) - l
    body            = abs(c - o)
    bearish_reject  = upper_wick > max(body, rng * 0.35) and c <= o   # wick up, closes down
    bullish_reject  = lower_wick > max(body, rng * 0.35) and c >= o   # wick down, closes up

    # ── Load GEX walls ───────────────────────────────────────────────────
    call_wall = put_wall = zero_gamma = 0.0
    try:
        gex_file = BASE / f"logs/gex/gex_latest_{ticker}.json"
        if gex_file.exists():
            import time as _time_mod
            gd = json.loads(gex_file.read_text())
            # Only trust if < 15 min old
            if _time_mod.time() - float(gd.get("ts", 0)) < 900:
                call_wall  = float(gd.get("call_wall",  0))
                put_wall   = float(gd.get("put_wall",   0))
                zero_gamma = float(gd.get("zero_gamma", 0))
    except Exception:
        pass

    # Fallback: use flow_signals GEX if file missing
    if not call_wall:
        try:
            flow_file = BASE / "logs/chakra" / "flow_signals_latest.json"
            if flow_file.exists():
                fd = json.loads(flow_file.read_text())
                sig = fd.get(ticker, {})
                call_wall  = float(sig.get("call_wall",  0))
                put_wall   = float(sig.get("put_wall",   0))
                zero_gamma = float(sig.get("zero_gamma", 0))
        except Exception:
            pass

    pct_to_call = abs(spot - call_wall) / spot * 100 if call_wall else 99
    pct_to_put  = abs(spot - put_wall)  / spot * 100 if put_wall  else 99
    at_call     = pct_to_call < 0.45
    at_put      = pct_to_put  < 0.45

    # ── Classify setup ───────────────────────────────────────────────────
    parabolic_up   = move_up   > 0.75
    parabolic_down = move_down > 0.75

    # PUT setup: parabolic up OR V-reversal top → at call wall → bearish rejection
    if (parabolic_up or is_v_reversal) and at_call and bearish_reject:
        conf = min(95, 55 + int(move_up * 6) + (12 if is_v_reversal else 0) + (8 if body < rng * 0.2 else 0))
        return {
            "detected":      True,
            "direction":     "PUT",
            "ticker":        ticker,
            "spot":          round(spot, 2),
            "wall":          round(call_wall, 2),
            "wall_type":     "call_wall",
            "pct_to_wall":   round(pct_to_call, 2),
            "move_pct":      round(move_up, 2),
            "is_v_reversal": is_v_reversal,
            "confidence":    conf,
            "reason":        f"Parabolic +{move_up:.1f}% into call wall ${call_wall:.2f} ({pct_to_call:.2f}% away) — bearish rejection candle → BUY PUTS",
        }

    # CALL setup: parabolic down → at put wall → bullish rejection / hammer
    if (parabolic_down or is_v_reversal) and at_put and bullish_reject:
        conf = min(95, 55 + int(move_down * 6) + (12 if is_v_reversal else 0) + (8 if body < rng * 0.2 else 0))
        return {
            "detected":      True,
            "direction":     "CALL",
            "ticker":        ticker,
            "spot":          round(spot, 2),
            "wall":          round(put_wall, 2),
            "wall_type":     "put_wall",
            "pct_to_wall":   round(pct_to_put, 2),
            "move_pct":      round(move_down, 2),
            "is_v_reversal": is_v_reversal,
            "confidence":    conf,
            "reason":        f"Parabolic -{move_down:.1f}% into put wall ${put_wall:.2f} ({pct_to_put:.2f}% away) — bullish hammer → BUY CALLS",
        }

    return {
        "detected": False,
        "reason": (f"No setup: up={move_up:.1f}% dn={move_down:.1f}% "
                   f"at_call={at_call}({pct_to_call:.2f}%) at_put={at_put}({pct_to_put:.2f}%) "
                   f"bear_rej={bearish_reject} bull_rej={bullish_reject} v_rev={is_v_reversal}"),
    }


def _notify_wall_rejection_alert(setup: dict) -> None:
    """Discord alert when wall rejection setup is detected — fires BEFORE trade execution."""
    try:
        from backend.arka.discord_notifier import post_embed_sync
        ticker    = setup["ticker"]
        direction = setup["direction"]
        is_call   = direction == "CALL"
        color     = 0x00D084 if is_call else 0xFF3D5A
        emoji     = "🟢📈" if is_call else "🔴📉"
        wall_type = "Put Wall" if setup["wall_type"] == "put_wall" else "Call Wall"
        ct        = "CALL" if is_call else "PUT"
        now_str   = datetime.now(ET).strftime("%I:%M %p ET")
        v_tag     = " ⚡ V-REVERSAL" if setup.get("is_v_reversal") else ""

        embed = {
            "color":  color,
            "author": {"name": f"{emoji} LOTTO SETUP DETECTED — {ticker}{v_tag}"},
            "description": (
                f"**🎯 Wall Rejection Pattern — Power Hour**\n"
                f"Parabolic move into {wall_type} with rejection candle.\n"
                f"ARKA will auto-execute if conviction confirms."
            ),
            "fields": [
                {"name": "📌 Setup",       "value": f"`{ticker} {ct} — {wall_type} rejection`",           "inline": True},
                {"name": "📍 Spot",        "value": f"${setup['spot']:,.2f}",                              "inline": True},
                {"name": "🧱 Wall",        "value": f"${setup['wall']:,.2f} ({setup['pct_to_wall']:.2f}% away)", "inline": True},
                {"name": "🚀 Move",        "value": f"{'+'if is_call else '-'}{setup['move_pct']:.1f}% in last 6 bars", "inline": True},
                {"name": "🧠 Confidence",  "value": f"{setup['confidence']}/100",                          "inline": True},
                {"name": "📝 Pattern",     "value": f"{'V-Reversal + rejection' if setup.get('is_v_reversal') else 'Parabolic spike + rejection'}", "inline": True},
                {"name": "💬 Plain Read",  "value": setup["reason"],                                       "inline": False},
            ],
            "footer": {"text": f"CHAKRA Wall Rejection • {now_str} • Auto-executing via Lotto Engine"},
        }
        post_embed_sync(embed, username="ARKA Lotto 🎯")
        log.info(f"  [Lotto.WR] 📣 Wall rejection alert posted: {ticker} {ct}")
    except Exception as e:
        log.error(f"  [Lotto.WR] Discord alert failed: {e}")


# ── Core lotto logic ──────────────────────────────────────────────────────

# ── VRP size multiplier (Session 1) ─────────────────────────────────
try:
    from backend.chakra.modules.vrp_engine import get_lotto_size_mult as _get_vrp_mult
    _VRP_AVAILABLE = True
except ImportError:
    _VRP_AVAILABLE = False

def check_lotto_trigger() -> dict:
    """
    Main lotto evaluation. Call every minute during Power Hour.
    Returns action: WAIT | EXECUTE | HOLD | CLOSE | BLOCKED
    """
    now   = datetime.now(ET)
    state = load_state()

    # Reset if new day
    if state.get("date") != date.today().isoformat():
        state = load_state.__wrapped__() if hasattr(load_state, "__wrapped__") else {
            "date": date.today().isoformat(), "enabled": True,
            "trades_today": 0, "active": False, "trade": None,
            "status": "WATCHING", "trigger_time": "15:30:00"
        }

    state["last_checked"] = now.isoformat()
    current_time = now.time()

    # ── Hard close at 3:58 PM ──────────────────────────────────────────
    if current_time >= time(15, 58):
        if state.get("active") and state.get("trade"):
            _hc_trade  = state["trade"]
            _hc_entry  = _hc_trade.get("entry_price", 0)
            symbol     = _hc_trade.get("contract", {}).get("symbol", "").removeprefix("O:").strip()
            if symbol:
                close_lotto_position(symbol, "HARD_CLOSE_3:58PM")
            state["active"] = False
            state["status"] = "CLOSED_EOD"
            save_state(state)
            _lotto_discord_exit(symbol, _hc_entry, _hc_entry, "⏰ Hard Close 3:58 PM — EOD auto-exit")
        return {"action": "HARD_CLOSE", "reason": "3:58 PM auto-close", "state": state}

    # ── Not in Power Hour yet ──────────────────────────────────────────
    if current_time < time(15, 0):
        state["status"] = "WAITING_FOR_POWER_HOUR"
        save_state(state)
        return {"action": "WAIT", "reason": "Not yet Power Hour (3:00 PM)", "state": state}

    # ── Already traded today ───────────────────────────────────────────
    if state.get("trades_today", 0) >= 2:
        state["status"] = "DAILY_LIMIT_REACHED"
        save_state(state)
        return {"action": "BLOCKED", "reason": "2 lotto trades per day limit reached", "state": state}

    # ── Check ARKA engine stopped/paused flag — respect day-stop ──────
    try:
        import json as _lj, pathlib as _lp
        _summary = _lp.Path(f"{LOG_DIR}/summary_{date.today()}.json")
        if _summary.exists():
            _sd = _lj.loads(_summary.read_text())
            if _sd.get("stopped"):
                state["status"] = "ARKA_STOPPED"
                save_state(state)
                return {"action": "BLOCKED", "reason": "ARKA engine stopped for day — lotto suppressed", "state": state}
            _daily_pnl = float(_sd.get("daily_pnl", 0))
            if _daily_pnl <= -400:
                state["status"] = "DAILY_LOSS_LIMIT"
                save_state(state)
                return {"action": "BLOCKED", "reason": f"Daily P&L ${_daily_pnl:.0f} — lotto suppressed on bad day", "state": state}
    except Exception:
        pass

    # ── Active trade — check stop/target ──────────────────────────────
    if state.get("active") and state.get("trade"):
        import httpx as _hx
        trade    = state["trade"]
        entry    = trade.get("entry_price", 0)
        target   = entry * 2.0   # +100%
        stop     = entry * 0.5   # -50%
        contract = trade.get("contract", {})
        symbol   = contract.get("symbol", "").removeprefix("O:").strip()

        # Poll Alpaca for current mark price
        current_px = None
        try:
            _r = _hx.get(
                f"{ALPACA_BASE}/v2/positions/{symbol}",
                headers={
                    "APCA-API-KEY-ID":     ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                },
                timeout=5,
            )
            if _r.status_code == 200:
                _pos = _r.json()
                current_px = float(_pos.get("current_price") or _pos.get("lastday_price") or 0)
            elif _r.status_code == 404:
                # Position no longer exists — was filled/expired
                log.info(f"  [Lotto] Position {symbol} not found (404) — marking closed")
                state["active"] = False
                state["status"] = "CLOSED_EXTERNAL"
                save_state(state)
                return {"action": "CLOSED", "reason": "Position closed externally", "state": state}
        except Exception as _pe:
            log.warning(f"  [Lotto] Price poll failed: {_pe}")

        if current_px and entry > 0:
            pct_chg = (current_px - entry) / entry * 100
            log.info(f"  [Lotto] 📊 {symbol} | entry=${entry:.2f} | now=${current_px:.2f} | {pct_chg:+.1f}% | target=${target:.2f} | stop=${stop:.2f}")

            if current_px >= target:
                log.info(f"  [Lotto] 🎯 TARGET HIT {pct_chg:+.1f}% — closing position")
                close_lotto_position(symbol, f"TARGET_HIT_{pct_chg:+.0f}pct")
                state["active"] = False
                state["status"] = "CLOSED_TARGET"
                save_state(state)
                _lotto_discord_exit(symbol, entry, current_px, f"🎯 TARGET HIT +{pct_chg:.1f}%")
                return {"action": "CLOSE_TARGET", "reason": f"Target hit {pct_chg:+.1f}%", "state": state}

            if current_px <= stop:
                log.info(f"  [Lotto] 🛑 STOP HIT {pct_chg:+.1f}% — closing position")
                close_lotto_position(symbol, f"STOP_HIT_{pct_chg:+.0f}pct")
                state["active"] = False
                state["status"] = "CLOSED_STOP"
                save_state(state)
                _lotto_discord_exit(symbol, entry, current_px, f"🛑 STOP HIT {pct_chg:.1f}%")
                return {"action": "CLOSE_STOP", "reason": f"Stop hit {pct_chg:+.1f}%", "state": state}

        state["status"] = "ACTIVE"
        save_state(state)
        return {
            "action":  "HOLD",
            "reason":  f"Active — Entry: ${entry:.2f} | Now: ${current_px:.2f if current_px else 0:.2f} | Target: ${target:.2f} | Stop: ${stop:.2f}",
            "trade":   trade,
            "state":   state,
        }

    # ── Lotto trigger window: 3:00–3:57 PM ────────────────────────────
    if time(15, 0) <= current_time < time(15, 57):
        mkt = get_conviction_and_gex()
        log.info(f"  [Lotto] 🎯 Power Hour | {mkt['best_ticker']} flow={mkt['flow_score']} score={mkt['best_score']} dir={mkt['flow_direction']} | GEX={mkt['gex_regime']}")

        # ── Wall Rejection Setup — HIGH PRIORITY override ─────────────
        # Scan SPY/QQQ/SPX for parabolic spike + rejection at GEX wall.
        # This pattern has edge and fires regardless of flow conviction.
        _wr_setup = {"detected": False}
        for _wr_tk in ("SPY", "QQQ", "SPX"):
            _wr = detect_wall_rejection_setup(_wr_tk)
            if _wr.get("detected"):
                _wr_setup = _wr
                log.info(f"  [Lotto] 🎯 WALL REJECTION: {_wr['reason']}")
                # Alert Discord once per (ticker, direction, date) to avoid spam
                _alert_key = (_wr_tk, _wr["direction"], date.today().isoformat())
                if _alert_key not in _WALL_REJECTION_NOTIFIED:
                    _WALL_REJECTION_NOTIFIED.add(_alert_key)
                    _notify_wall_rejection_alert(_wr)
                break
            else:
                log.debug(f"  [Lotto] WR {_wr_tk}: {_wr.get('reason','—')}")

        if _wr_setup["detected"] and _wr_setup.get("confidence", 0) >= 60:
            # Override direction and ticker from wall rejection signal
            direction = "SELL" if _wr_setup["direction"] == "PUT" else "BUY"
            ticker    = _wr_setup["ticker"]
            # Boost conviction score
            mkt = dict(mkt,
                       best_ticker=ticker,
                       best_score=max(mkt["best_score"], _wr_setup["confidence"]),
                       flow_direction="BEARISH" if direction == "SELL" else "BULLISH",
                       qualifies=True)
            log.info(f"  [Lotto] 🚀 WR override: {ticker} {direction} | conf={mkt['best_score']}")
        elif not mkt["qualifies"]:
            state["status"] = "WATCHING_LOW_CONVICTION"
            save_state(state)
            return {"action": "WAIT", "reason": f"Flow conviction {mkt['best_score']:.0f} < threshold, no WR setup", "state": state}
        else:
            # Direction comes from flow bias (BULLISH→CALL buy, BEARISH→PUT buy)
            direction = "BUY" if mkt["flow_direction"] == "BULLISH" else "SELL"
            ticker    = mkt["best_ticker"]

        # ── MOC Imbalance boost (3:00–3:58 PM ET) ─────────────────────────
        try:
            from backend.chakra.moc_imbalance import get_moc_conviction_modifier as _moc_mod
            _lotto_dir = mkt.get("flow_direction", "BULLISH")
            _moc_adj   = _moc_mod(ticker, _lotto_dir)
            if _moc_adj != 0:
                mkt = dict(mkt, best_score=max(0, min(100, mkt["best_score"] + _moc_adj)))
                log.info(f"  [Lotto] MOC imbalance {_lotto_dir} → conviction {_moc_adj:+d} → {mkt['best_score']:.0f}")
        except Exception:
            pass  # never let MOC break lotto

        # Get ATM option
        contract = get_atm_option(ticker, direction)
        if not contract or not contract.get("mark"):
            state["status"] = "NO_CONTRACT_FOUND"
            save_state(state)
            return {"action": "WAIT", "reason": "Could not find ATM 0DTE contract", "state": state}

        entry_price = contract["mark"]
        target_price = round(entry_price * 2.0, 2)
        stop_price   = round(entry_price * 0.5, 2)
        est_cost     = round(entry_price * 100, 2)  # 1 contract = 100 shares

        # ── $150 daily budget cap ─────────────────────────────────────────
        spent_today = state.get("spent_today", 0.0)
        if spent_today + est_cost > 150.0:
            state["status"] = "BUDGET_EXHAUSTED"
            save_state(state)
            log.info(f"  [Lotto] 💸 Budget cap: spent=${spent_today:.0f} + ${est_cost:.0f} > $150 limit — skip")
            return {"action": "BLOCKED", "reason": f"Daily lotto budget $150 exhausted (spent ${spent_today:.0f})", "state": state}

        # Place order
        order = place_lotto_order(contract, entry_price, mkt)
        if not order.get("success"):
            state["status"] = "ORDER_FAILED"
            save_state(state)
            return {"action": "FAILED", "reason": order.get("error", "Order failed"), "state": state}

        # Update state
        trade_record = {
            "entry_time":   now.isoformat(),
            "ticker":       ticker,
            "direction":    direction,
            "contract":     contract,
            "entry_price":  entry_price,
            "target_price": target_price,
            "stop_price":   stop_price,
            "order_id":     order.get("order_id"),
            "conviction":   mkt["best_score"],
            "gex_regime":   mkt["gex_regime"],
            "setup_type":   "WALL_REJECTION" if _wr_setup.get("detected") else "FLOW",
            "setup_reason": _wr_setup.get("reason", "") if _wr_setup.get("detected") else "",
        }
        state["active"]       = True
        state["trades_today"] = state.get("trades_today", 0) + 1
        state["spent_today"]  = round(state.get("spent_today", 0.0) + est_cost, 2)
        state["trade"]        = trade_record
        state["status"]       = "ACTIVE"
        save_state(state)

        vrp_mult = round(_get_vrp_mult(), 2) if _VRP_AVAILABLE else 1.0
        log.info(f"  [Lotto] 🚀 EXECUTED {ticker} {direction} | "
                 f"Contract: {contract['symbol']} @ ${entry_price} | "
                 f"Target: ${target_price} | Stop: ${stop_price} | "
                 f"VRP size={vrp_mult}x")
        _notify_lotto_trade(trade_record)

        return {"action": "EXECUTED", "trade": trade_record, "state": state}

    # Before 3:00 PM (shouldn't reach here but safe fallback)
    state["status"] = "WAITING_FOR_POWER_HOUR"
    save_state(state)
    return {"action": "WAIT", "reason": "Waiting for Power Hour (3:00 PM ET)", "state": state}


def get_lotto_status() -> dict:
    """API-friendly status for /api/lotto/status endpoint.
    When state says active=True, cross-checks Alpaca to avoid stale display."""
    import httpx as _hx
    state = load_state()
    now   = datetime.now(ET)

    # ── Stale-state guard: if state says active but position is gone, self-heal ──
    if state.get("active") and state.get("trade"):
        symbol = (state["trade"].get("contract", {}).get("symbol") or "").removeprefix("O:")
        if symbol:
            try:
                _r = _hx.get(
                    f"{ALPACA_BASE}/v2/positions/{symbol}",
                    headers={"APCA-API-KEY-ID": ALPACA_KEY,
                             "APCA-API-SECRET-KEY": ALPACA_SECRET},
                    timeout=5,
                )
                if _r.status_code == 404:
                    log.info(f"  [Lotto] State heal: {symbol} not in Alpaca — marking closed")
                    state["active"] = False
                    state["status"] = "CLOSED_EXTERNAL"
                    save_state(state)
            except Exception as _e:
                log.warning(f"  [Lotto] State-heal check failed: {_e}")

    return {
        "enabled":         state.get("enabled", True),
        "active":          state.get("active", False),
        "status":          state.get("status", "WATCHING"),
        "trades_today":    state.get("trades_today", 0),
        "max_trades":      2,
        "spent_today":     state.get("spent_today", 0.0),
        "daily_budget":    150.0,
        "min_score":       60,
        "trigger_time":    "15:00:00",
        "close_time":      "15:58:00",
        "trade":           state.get("trade") if state.get("active") else None,
        "current_time":    now.strftime("%H:%M:%S ET"),
        "in_power_hour":   now.time() >= time(15, 0),
        "in_lotto_window": time(15, 0) <= now.time() < time(15, 57),
    }


def clear_lotto_state() -> dict:
    """Force-clear active trade from state (used when position closed externally)."""
    state = load_state()
    state["active"] = False
    state["status"] = "CLEARED_MANUALLY"
    save_state(state)
    return {"cleared": True, "status": state["status"]}


if __name__ == "__main__":
    import sys
    if "--status" in sys.argv:
        print(json.dumps(get_lotto_status(), indent=2, default=str))
    else:
        result = check_lotto_trigger()
        print(json.dumps(result, indent=2, default=str))

# S2_CHARM_LOTTO — Charm directional bias wired by patchsession2.py
def _charm_directional_bias() -> str:
    """Returns CALL / PUT / NEUTRAL based on Charm EOD direction."""
    try:
        import json, pathlib
        f = pathlib.Path("logs/chakra/charm_latest.json")
        if f.exists():
            c = json.loads(f.read_text())
            eod_dir = c.get("eod_direction", "NEUTRAL")
            mag     = float(c.get("magnitude", 0))
            if mag > 0.4:
                if eod_dir == "BULLISH":  return "CALL"
                if eod_dir == "BEARISH":  return "PUT"
    except Exception:
        pass
    return "NEUTRAL"
