"""
ARJUN Live Engine v2.0 — Multi-Agent Signal Generator
Replaces single XGBoost model with 5-agent reasoning system:
  Analyst → Bull + Bear → Risk Manager → Master Coordinator

Runs at 8:00am ET, generates rich signals with full agent reasoning.
"""
import json
import os
import sys
import time
import logging
import numpy as np
import httpx
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv


class _SafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):    return bool(obj)
        if isinstance(obj, (np.integer,)):  return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, (np.ndarray,)):  return obj.tolist()
        return super().default(obj)

# ── Path setup ─────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).parent / "agents"))

load_dotenv(BASE / ".env", override=True)

DISCORD_WEBHOOK  = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_TRADES   = os.getenv("DISCORD_TRADES_WEBHOOK", "")
POLYGON_KEY      = os.getenv("POLYGON_API_KEY", "")

# ── Logging ────────────────────────────────────────────────────────────
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARJUN] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "arjun.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("arjun")

# ── Tickers ────────────────────────────────────────────────────────────
# Tickers loaded from backend/arjun/tickers.json — edit that file to add/remove
try:
    import json as _tj
    TICKERS = _tj.loads(open(str(BASE / "backend/arjun/tickers.json")).read())["tickers"]
except Exception:
    TICKERS = ["SPY", "QQQ", "IWM", "DIA"]

# ── Import agents ──────────────────────────────────────────────────────
AGENTS_DIR = Path(__file__).parent / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from analyst_agent      import run as analyst_run
from bull_agent         import run as bull_run
from bear_agent         import run as bear_run
from risk_manager_agent import run as risk_run
from coordinator        import run as coord_run
from gex_calculator     import get_gex_for_ticker
from performance_db     import log_signal, init_db


def fetch_live_price(ticker: str) -> float:
    """Get latest price from Polygon snapshot."""
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": POLYGON_KEY},
            timeout=8,
        )
        snap = r.json().get("ticker", {})
        return float(snap.get("lastTrade", {}).get("p", 0) or
                     snap.get("day", {}).get("c", 0) or 0)
    except Exception:
        return 0.0


def generate_signal_for_ticker(ticker: str, gex_cache: dict) -> dict | None:
    """
    Run the full 5-agent pipeline for one ticker.
    Returns complete signal dict or None on failure.
    """
    log.info(f"━━━ Processing {ticker} ━━━")
    t0 = time.time()

    try:
        # ── Agent 1: Analyst ──────────────────────────────────────────
        analyst = analyst_run(ticker)
        if "error" in analyst:
            log.warning(f"[{ticker}] Analyst failed: {analyst['error']}")
            return None

        price = analyst["indicators"].get("price", 0)
        if price <= 0:
            log.warning(f"[{ticker}] No price data")
            return None

        # ── GEX (use SPY GEX for all tickers — proxy for market regime) ──
        gex_ticker = "SPY" if ticker != "SPY" else "SPY"
        if gex_ticker not in gex_cache:
            spy_price = price if ticker == "SPY" else fetch_live_price("SPY")
            gex_cache[gex_ticker] = get_gex_for_ticker("SPY", spy_price or price)
        gex = gex_cache[gex_ticker]

        # ── Agent 2: Bull ─────────────────────────────────────────────
        bull = bull_run(ticker, analyst, gex)

        # ── Agent 3: Bear ─────────────────────────────────────────────
        bear = bear_run(ticker, analyst, gex)

        # ── Agent 4: Risk Manager ─────────────────────────────────────
        risk = risk_run(ticker, analyst, bull, bear, gex)

        # ── Agent 5: Master Coordinator ───────────────────────────────
        signal = coord_run(ticker, analyst, bull, bear, risk, gex)

        elapsed = round(time.time() - t0, 1)
        log.info(f"[{ticker}] → {signal['signal']} @ ${signal['price']} | "
                 f"conf={signal['confidence']}% | "
                 f"bull={bull['score']} bear={bear['score']} risk={risk['decision']} | "
                 f"{elapsed}s")

        return signal

    except Exception as e:
        log.error(f"[{ticker}] Pipeline error: {e}", exc_info=True)
        return None


def save_signals(signals: list) -> Path:
    """Save all signals to timestamped JSON file."""
    sig_dir = LOG_DIR / "signals"
    sig_dir.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d%H%M")
    path = sig_dir / f"signals_{ts}.json"
    with open(path, "w") as f:
        json.dump(signals, f, indent=2, cls=_SafeEncoder)
    log.info(f"Signals saved → {path}")
    return path


def post_to_discord(signals: list):
    """Post signal summary to Discord webhook."""
    if not DISCORD_WEBHOOK:
        return

    buy_sigs  = [s for s in signals if s["signal"] == "BUY"]
    sell_sigs = [s for s in signals if s["signal"] == "SELL"]
    hold_sigs = [s for s in signals if s["signal"] == "HOLD"]

    # Header embed
    header = {
        "embeds": [{
            "title":       "🧠 ARJUN v2.0 — Morning Signals",
            "description": f"Multi-agent analysis complete | {len(signals)} tickers\n"
                           f"🟢 {len(buy_sigs)} BUY  🔴 {len(sell_sigs)} SELL  ⚪ {len(hold_sigs)} HOLD",
            "color":       0x1e4fd8,
            "timestamp":   datetime.utcnow().isoformat(),
            "footer":      {"text": "ARJUN: Analyst → Bull → Bear → Risk → Coordinator"},
        }]
    }

    try:
        httpx.post(DISCORD_WEBHOOK, json=header, timeout=10)
    except Exception:
        pass

    # Individual signal cards
    for sig in signals:
        if sig["signal"] == "HOLD":
            continue  # Only post actionable signals

        color = 0x00c878 if sig["signal"] == "BUY" else 0xff4466
        agents = sig.get("agents", {})
        bull   = agents.get("bull", {})
        bear   = agents.get("bear", {})
        risk   = agents.get("risk_manager", {})
        sizing = risk.get("position_size", {})
        gex    = sig.get("gex", {})

        conf = float(sig.get("confidence", 50))
        conf_bar = "█" * int(conf / 10) + "░" * (10 - int(conf / 10))

        desc = (
            f"**Entry:** `${sig['entry']:,.2f}`  "
            f"**Target:** `${sig['target']:,.2f}`  "
            f"**Stop:** `${sig['stop_loss']:,.2f}`\n"
            f"**R/R:** `1:{sig.get('risk_reward', 0):.2f}`  "
            f"**Size:** `{sizing.get('shares', 0)} shares` "
            f"(`{sizing.get('risk_pct', 0):.1f}% risk`)\n\n"
            f"**Agent Scores**\n"
            f"🐂 Bull: `{bull.get('score', 50)}/100` — {bull.get('key_catalyst', '')[:50]}\n"
            f"🐻 Bear: `{bear.get('score', 50)}/100` — {bear.get('key_risk', '')[:50]}\n"
            f"⚖️  Risk: `{risk.get('decision', '?')}` — {risk.get('reason', '')[:50]}\n\n"
            f"**GEX:** `{gex.get('regime', '?')}` | "
            f"Call Wall: `${gex.get('call_wall', 0):,.0f}` | "
            f"Put Wall: `${gex.get('put_wall', 0):,.0f}`\n\n"
            f"**Confidence:** `{conf_bar}` {conf:.0f}%"
        )

        payload = {
            "embeds": [{
                "title":       f"{'🟢' if sig['signal'] == 'BUY' else '🔴'} {sig['signal']} {sig['ticker']} @ ${sig['price']:,.2f}",
                "description": desc,
                "color":       color,
                "fields": [
                    {
                        "name":   "📋 Trade Thesis",
                        "value":  sig.get("explanation", "")[:900],
                        "inline": False,
                    }
                ],
                "footer": {"text": f"CHAKRA ARJUN v2.0 • {datetime.now().strftime('%I:%M %p ET')}"},
            }]
        }

        try:
            httpx.post(DISCORD_WEBHOOK, json=payload, timeout=10)
            time.sleep(1)  # Rate limit
        except Exception as e:
            log.warning(f"Discord post failed for {sig['ticker']}: {e}")


def generate_all_signals(post_discord: bool = True) -> list:
    """
    Main entry point: Generate signals for all tickers.
    Called at 8:00am ET by LaunchAgent.
    post_discord=False: update signal files only, skip Discord (used for intraday refreshes).
    """
    log.info("=" * 60)
    log.info("ARJUN v2.0 — Multi-Agent Signal Generation Starting")
    log.info(f"Tickers: {TICKERS}")
    log.info("=" * 60)

    init_db()
    gex_cache = {}  # Shared GEX data — fetch once for SPY
    signals   = []
    t_start   = time.time()

    for ticker in TICKERS:
        time.sleep(2)  # rate limit buffer
        signal = generate_signal_for_ticker(ticker, gex_cache)
        if signal:
            signals.append(signal)
            # Log to performance DB for continuous learning
            try:
                log_signal(signal)
            except Exception as e:
                log.warning(f"Performance DB log failed: {e}")
        time.sleep(2)  # Respect API rate limits

    elapsed = round(time.time() - t_start, 1)
    log.info(f"{'='*60}")
    log.info(f"Complete: {len(signals)}/{len(TICKERS)} signals in {elapsed}s")

    buys  = [s for s in signals if s["signal"] == "BUY"]
    sells = [s for s in signals if s["signal"] == "SELL"]
    holds = [s for s in signals if s["signal"] == "HOLD"]
    log.info(f"BUY: {len(buys)}  SELL: {len(sells)}  HOLD: {len(holds)}")

    if signals:
        path = save_signals(signals)
        if not post_discord:
            log.info("📵 Discord suppressed (intraday refresh mode)")
            return signals
        post_to_discord(signals)
        # Step 9: Post ARJUN morning brief to arjun-alerts channel
        try:
            from backend.arjun.arjun_discord import post_morning_brief as _post_brief
            # Pull GEX regime + pulse from latest internals cache
            import json as _j, pathlib as _pl
            _gex_regime = "UNKNOWN"
            _pulse = 50
            _risk_mode = "NORMAL"
            try:
                _if = _pl.Path("logs/internals/internals_latest.json")
                if _if.exists():
                    _id = _j.loads(_if.read_text())
                    _gex_regime = _id.get("gex_regime", "UNKNOWN")
                    _pulse = int(_id.get("neural_pulse", {}).get("score", 50))
            except Exception:
                pass
            # Derive regime_call from GEX regime
            _rc_map = {
                "POSITIVE_GAMMA": "SHORT_THE_POPS",
                "NEGATIVE_GAMMA": "FOLLOW_MOMENTUM",
                "LOW_VOL":        "BUY_THE_DIPS",
            }
            _regime_call = _rc_map.get(_gex_regime, "FOLLOW_MOMENTUM")
            _post_brief(
                signals=signals,
                regime_call=_regime_call,
                gex_regime=_gex_regime,
                neural_pulse=_pulse,
                risk_mode=_risk_mode,
            )
            log.info("📊 ARJUN morning brief posted to Discord")
        except Exception as _be:
            log.warning(f"ARJUN morning brief failed: {_be}")

    return signals


if __name__ == "__main__":
    # Support single-ticker test mode
    if len(sys.argv) > 1:
        ticker = sys.argv[1].upper()
        log.info(f"Single-ticker test mode: {ticker}")
        gex_cache = {}
        signal = generate_signal_for_ticker(ticker, gex_cache)
        if signal:
            # Print clean output
            print(f"\n{'='*60}")
            print(f"SIGNAL: {signal['signal']} {signal['ticker']} @ ${signal['price']}")
            print(f"Confidence: {signal['confidence']}%")
            print(f"Entry: ${signal['entry']} | Target: ${signal['target']} | Stop: ${signal['stop_loss']}")
            agents = signal.get("agents", {})
            print(f"Bull: {agents.get('bull',{}).get('score',0)} | "
                  f"Bear: {agents.get('bear',{}).get('score',0)} | "
                  f"Risk: {agents.get('risk_manager',{}).get('decision','?')}")
            print(f"\nExplanation:\n{signal['explanation']}")
            # Save single signal
            out = BASE / "logs" / "signals" / f"test_{ticker}_{datetime.now().strftime('%Y%m%d%H%M')}.json"
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(signal, indent=2, cls=_SafeEncoder))
            print(f"\nSaved to: {out}")
    else:
        generate_all_signals()
