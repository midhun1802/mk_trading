"""
ARJUN Feedback Tracker
Scores past signals against actual price outcomes.
Run at 4:05 PM ET every weekday via crontab.

Usage:
  python3 -m backend.arjun.feedback_tracker        # score today's signals
  python3 -m backend.arjun.feedback_tracker --days 7  # score last 7 days
"""
import json
import os
import sys
import logging
import glob
import argparse
import httpx
from datetime import datetime, timedelta, date
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv
load_dotenv(BASE / ".env", override=True)

from backend.arjun.agents.performance_db import init_db, DB_PATH

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")

LOG_DIR = BASE / "logs" / "arjun"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FEEDBACK] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "feedback.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("feedback")

# A BUY signal wins if price is +1.5% or better 2 trading days out.
# A SELL signal wins if price is -1.5% or worse 2 trading days out.
WIN_THRESHOLD_PCT = 1.5


def _fetch_close(ticker: str, target_date: str) -> float:
    """Fetch adjusted close for ticker on target_date (YYYY-MM-DD)."""
    try:
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{target_date}/{target_date}",
            params={"adjusted": "true", "apiKey": POLYGON_KEY},
            timeout=httpx.Timeout(connect=5, read=10, write=5, pool=5),
        )
        results = r.json().get("results", [])
        if results:
            return float(results[0].get("c", 0))
        # Weekend/holiday: try the next trading day
        dt = datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)
        r2 = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/"
            f"{dt.strftime('%Y-%m-%d')}/{dt.strftime('%Y-%m-%d')}",
            params={"adjusted": "true", "apiKey": POLYGON_KEY},
            timeout=httpx.Timeout(connect=5, read=10, write=5, pool=5),
        )
        results2 = r2.json().get("results", [])
        return float(results2[0].get("c", 0)) if results2 else 0.0
    except Exception as e:
        log.warning(f"[{ticker}] price fetch failed for {target_date}: {e}")
        return 0.0


def score_signal_accuracy(signal: dict) -> dict:
    """
    Given a signal dict, fetch the outcome price 2 trading days later
    and determine WIN / LOSS / NEUTRAL.

    Returns the signal dict enriched with:
      outcome, outcome_price, outcome_pnl_pct, scored_at
    """
    ticker     = signal.get("ticker", "")
    direction  = signal.get("signal", "HOLD")
    entry      = float(signal.get("entry") or signal.get("price") or 0)
    signal_ts  = signal.get("timestamp") or signal.get("generated_at") or ""

    if direction == "HOLD" or entry <= 0 or not ticker:
        return {**signal, "outcome": "SKIP", "outcome_price": 0, "outcome_pnl_pct": 0}

    # Parse signal date — figure out 2 trading days out
    try:
        sig_dt = datetime.fromisoformat(signal_ts.split("+")[0].split("Z")[0])
    except Exception:
        sig_dt = datetime.now() - timedelta(days=1)

    # Advance 2 trading days (skip weekends)
    out_dt = sig_dt
    days_added = 0
    while days_added < 2:
        out_dt += timedelta(days=1)
        if out_dt.weekday() < 5:
            days_added += 1
    out_date = out_dt.strftime("%Y-%m-%d")

    # Don't score if outcome date is in the future
    if out_dt.date() > date.today():
        return {**signal, "outcome": "PENDING", "outcome_price": 0, "outcome_pnl_pct": 0}

    out_price = _fetch_close(ticker, out_date)
    if out_price <= 0:
        return {**signal, "outcome": "NO_DATA", "outcome_price": 0, "outcome_pnl_pct": 0}

    pnl_pct = (out_price - entry) / entry * 100

    if direction == "BUY":
        outcome = "WIN" if pnl_pct >= WIN_THRESHOLD_PCT else "LOSS"
    elif direction == "SELL":
        outcome = "WIN" if pnl_pct <= -WIN_THRESHOLD_PCT else "LOSS"
    else:
        outcome = "SKIP"

    log.info(f"  {ticker} {direction}: entry={entry} out={out_price} pnl={pnl_pct:+.1f}% → {outcome}")
    return {
        **signal,
        "outcome":         outcome,
        "outcome_price":   round(out_price, 2),
        "outcome_pnl_pct": round(pnl_pct, 2),
        "outcome_date":    out_date,
        "scored_at":       datetime.now().isoformat(),
    }


def _load_signals_for_date(target_date: str) -> list:
    """Load all signals from signal files for the given date (YYYYMMDD format)."""
    files = sorted(glob.glob(f"{BASE}/logs/signals/signals_{target_date}*.json"), reverse=True)
    if not files:
        return []
    try:
        data = json.loads(Path(files[0]).read_text())
        return data if isinstance(data, list) else data.get("signals", [])
    except Exception:
        return []


def run_eod_feedback(days_back: int = 3) -> dict:
    """
    Score all signals from the past N days that are now scoreable (2-day outcome).
    Writes results to logs/arjun/feedback_{date}.json and updates performance DB.
    Returns summary stats.
    """
    import sqlite3

    init_db()
    results = []
    total = wins = losses = pending = skips = 0

    today = date.today()

    for delta in range(1, days_back + 1):
        target = today - timedelta(days=delta)
        date_str = target.strftime("%Y%m%d")
        log.info(f"Scoring signals for {target.isoformat()}...")

        signals = _load_signals_for_date(date_str)
        if not signals:
            log.info(f"  No signal file for {date_str}")
            continue

        for sig in signals:
            if sig.get("signal") == "HOLD":
                skips += 1
                continue
            total += 1
            scored = score_signal_accuracy(sig)
            out = scored.get("outcome", "SKIP")
            if out == "WIN":
                wins += 1
            elif out == "LOSS":
                losses += 1
            elif out == "PENDING":
                pending += 1
            else:
                skips += 1
            results.append(scored)

            # Update performance DB outcome
            if out in ("WIN", "LOSS"):
                try:
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    # Match by date + ticker + signal
                    sig_date = target.isoformat()
                    c.execute("""
                        UPDATE signals SET exit_price=?, outcome=?, pnl=?
                        WHERE date=? AND ticker=? AND signal=? AND outcome IS NULL
                        LIMIT 1
                    """, (
                        scored.get("outcome_price", 0),
                        out,
                        scored.get("outcome_pnl_pct", 0),
                        sig_date,
                        sig.get("ticker", ""),
                        sig.get("signal", ""),
                    ))
                    conn.commit()
                    conn.close()
                except Exception as dbe:
                    log.warning(f"DB update failed: {dbe}")

    # Write feedback JSON
    if results:
        feedback_path = LOG_DIR / f"feedback_{today.isoformat()}.json"
        feedback_path.write_text(json.dumps(results, indent=2, default=str))
        log.info(f"Feedback saved → {feedback_path}")

    summary = {
        "date":       today.isoformat(),
        "total":      total,
        "wins":       wins,
        "losses":     losses,
        "pending":    pending,
        "skips":      skips,
        "win_rate":   round(wins / max(wins + losses, 1), 3),
        "signals":    results,
    }
    return summary


def get_historical_accuracy(days: int = 30) -> dict:
    """
    Read performance DB and compute accuracy stats for the last N days.
    Returns per-ticker, per-signal-type breakdown.
    """
    import sqlite3

    init_db()
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        SELECT ticker, signal, confidence, bull_score, bear_score,
               gex_regime, outcome, pnl, risk_decision, date
        FROM signals
        WHERE date >= date('now', ?) AND outcome IN ('WIN', 'LOSS')
        ORDER BY date DESC
    """, (f"-{days} days",))

    rows = c.fetchall()
    conn.close()

    if not rows:
        return {
            "period_days": days,
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "by_signal": {},
            "by_ticker": {},
            "by_regime": {},
            "message": "No scored signals yet — feedback runs at 4:05 PM ET daily",
        }

    total  = len(rows)
    wins   = sum(1 for r in rows if r[6] == "WIN")
    losses = total - wins

    # By signal type
    by_signal: dict = {}
    for r in rows:
        sig = r[1]
        if sig not in by_signal:
            by_signal[sig] = {"wins": 0, "losses": 0, "avg_conf": 0, "confs": []}
        by_signal[sig]["wins" if r[6] == "WIN" else "losses"] += 1
        by_signal[sig]["confs"].append(r[2] or 50)
    for sig in by_signal:
        d = by_signal[sig]
        tot = d["wins"] + d["losses"]
        d["win_rate"] = round(d["wins"] / tot, 3) if tot > 0 else 0
        d["avg_conf"] = round(sum(d["confs"]) / len(d["confs"]), 1) if d["confs"] else 0
        del d["confs"]

    # By ticker
    by_ticker: dict = {}
    for r in rows:
        t = r[0]
        if t not in by_ticker:
            by_ticker[t] = {"wins": 0, "losses": 0}
        by_ticker[t]["wins" if r[6] == "WIN" else "losses"] += 1
    for t in by_ticker:
        d = by_ticker[t]
        tot = d["wins"] + d["losses"]
        d["win_rate"] = round(d["wins"] / tot, 3) if tot > 0 else 0

    # By GEX regime
    by_regime: dict = {}
    for r in rows:
        reg = r[5] or "UNKNOWN"
        if reg not in by_regime:
            by_regime[reg] = {"wins": 0, "losses": 0}
        by_regime[reg]["wins" if r[6] == "WIN" else "losses"] += 1

    # Best/worst tickers
    best  = max(by_ticker, key=lambda t: by_ticker[t]["win_rate"]) if by_ticker else None
    worst = min(by_ticker, key=lambda t: by_ticker[t]["win_rate"]) if by_ticker else None

    return {
        "period_days":  days,
        "total":        total,
        "wins":         wins,
        "losses":       losses,
        "win_rate":     round(wins / total, 3),
        "win_rate_pct": round(wins / total * 100, 1),
        "by_signal":    by_signal,
        "by_ticker":    by_ticker,
        "by_regime":    by_regime,
        "best_ticker":  best,
        "worst_ticker": worst,
        "generated_at": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3, help="Days back to score")
    parser.add_argument("--stats", action="store_true",  help="Print historical accuracy stats")
    args = parser.parse_args()

    if args.stats:
        stats = get_historical_accuracy(30)
        print(json.dumps(stats, indent=2))
    else:
        log.info("=" * 50)
        log.info(f"ARJUN EOD Feedback — scoring last {args.days} days")
        log.info("=" * 50)
        summary = run_eod_feedback(args.days)
        log.info(f"Done: {summary['wins']}W / {summary['losses']}L / {summary['pending']} pending")
        log.info(f"Win rate: {summary['win_rate']*100:.1f}%")
