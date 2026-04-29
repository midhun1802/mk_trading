from dotenv import load_dotenv
load_dotenv(override=True)
"""
arka_journal.py — ARKA Decision Journal
Runs daily at 4:00pm ET via cron.

What it does:
  1. Reads today's ARKA log (every flat/trade decision with conv score)
  2. Fetches actual SPY/QQQ price 15 mins after each decision from Polygon
  3. Calculates: would that trade have won? by how much?
  4. Appends to logs/arka/training_data.csv  (grows every day)
  5. Writes logs/arka/journal_YYYY-MM-DD.json (human-readable daily summary)

Run via cron (4:02pm ET = 3:02pm CST on your Mac):
  2 15 * * 1-5 cd /Users/midhunkrothapalli/trading-ai && source venv/bin/activate && python3 backend/arka/arka_journal.py >> logs/arka/journal.log 2>&1
"""

import os, re, json, csv, logging
from datetime import datetime, timedelta
from pathlib import Path
import requests
import pytz

# ── CONFIG ──────────────────────────────────────────────────────────────────
POLYGON_KEY  = os.getenv("POLYGON_API_KEY", "rrJ5P3S52kvCzQzdQRim8qQZwTjqYhba")
LOG_DIR      = Path("logs/arka")
TRAINING_CSV = LOG_DIR / "training_data.csv"
ET           = pytz.timezone("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("journal")

# ── HELPERS ─────────────────────────────────────────────────────────────────

def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")

def fetch_price_at(ticker: str, date: str, minute_ts: str) -> float | None:
    """Get the close price of a 1-min bar at or after a given HH:MM timestamp."""
    # minute_ts like "14:32:00" in ET
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute"
        f"/{date}/{date}?adjusted=true&sort=asc&limit=500&apiKey={POLYGON_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        bars = r.json().get("results", [])
    except Exception as e:
        log.warning(f"Polygon fetch failed for {ticker}: {e}")
        return None

    # Convert bars to {HH:MM: close}
    bar_map = {}
    for b in bars:
        dt = datetime.fromtimestamp(b["t"] / 1000, tz=ET)
        bar_map[dt.strftime("%H:%M")] = b["c"]

    # Find the bar at minute_ts + 15min
    h, m, *_ = minute_ts.split(":")
    target_dt = datetime.strptime(f"{date} {h}:{m}", "%Y-%m-%d %H:%M")
    target_15  = (target_dt + timedelta(minutes=15)).strftime("%H:%M")
    target_0   = f"{h}:{m}"

    entry_price  = bar_map.get(target_0)
    outcome_price = bar_map.get(target_15)
    return entry_price, outcome_price


def parse_log(log_path: Path) -> list[dict]:
    """
    Parse ARKA daily log, extract every scan decision.
    Lines look like:
      14:39:56  INFO        ⏸  FLAT   SPY  $ 685.38  conv= 33.5  fakeout=0.32  session=POWER_HOUR
      14:41:57  INFO        🟢 TRADE  QQQ  $ 606.41  conv= 38.3  fakeout=0.44  session=LUNCH
    """
    decisions = []
    pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2}).*?(FLAT|TRADE)\s+(SPY|QQQ)\s+\$\s*([\d.]+)"
        r"\s+conv=\s*([\d.]+)\s+fakeout=([\d.]+)\s+session=(\w+)"
    )
    if not log_path.exists():
        log.warning(f"Log not found: {log_path}")
        return []

    with open(log_path) as f:
        for line in f:
            m = pattern.search(line)
            if m:
                decisions.append({
                    "time":     m.group(1),
                    "decision": m.group(2),   # FLAT or TRADE
                    "ticker":   m.group(3),
                    "price":    float(m.group(4)),
                    "conv":     float(m.group(5)),
                    "fakeout":  float(m.group(6)),
                    "session":  m.group(7),
                })

    # Deduplicate — keep only the last decision per ticker per minute
    seen = {}
    for d in decisions:
        key = (d["ticker"], d["time"][:5])   # HH:MM
        seen[key] = d
    return list(seen.values())


def load_summary(date: str) -> dict:
    p = LOG_DIR / f"summary_{date}.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def compute_outcome(entry_price: float, exit_price: float, decision: str) -> dict:
    """
    Given entry and 15-min-later price, compute what would have happened.
    For FLAT decisions: was staying flat the right call?
    For TRADE decisions: did the trade work?
    """
    if not entry_price or not exit_price:
        return {"move_pct": None, "would_win": None, "correct": None}

    move_pct = (exit_price - entry_price) / entry_price * 100

    if decision == "TRADE":
        # ARKA only goes LONG — win if price went up
        would_win = move_pct > 0
        correct   = would_win  # trade was correct if it went up
    else:  # FLAT
        # Staying flat was correct if market didn't make a clean move up
        # (i.e., we didn't miss a >0.3% move)
        would_win = None
        correct   = move_pct < 0.3   # True = right to stay flat

    return {
        "move_pct": round(move_pct, 3),
        "would_win": would_win,
        "correct": correct,
    }


def append_to_csv(rows: list[dict]):
    fieldnames = [
        "date","time","ticker","decision","conv","fakeout","session",
        "entry_price","exit_price_15m","move_pct","would_win","correct",
        "threshold","margin",   # how far conv was from threshold
    ]
    write_header = not TRAINING_CSV.exists()
    with open(TRAINING_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        w.writerows(rows)


def session_threshold(session: str) -> int:
    return {
        "OPEN": 60, "NORMAL": 60, "POWER_HOUR": 50,
        "LUNCH": 999, "CLOSE": 999, "PRE": 999, "CLOSED": 999
    }.get(session, 60)


def build_journal():
    date = today_et()
    log_path = LOG_DIR / f"arka_{date}.log"
    log.info(f"── ARKA Journal: {date} ──")

    decisions = parse_log(log_path)
    log.info(f"  Parsed {len(decisions)} unique scan decisions")

    if not decisions:
        log.info("  No decisions found — market may have been closed")
        return

    summary_data = load_summary(date)
    csv_rows = []
    journal_entries = []
    correct_count  = 0
    total_assessed = 0

    for d in decisions:
        prices = fetch_price_at(d["ticker"], date, d["time"])
        if prices is None or prices[0] is None:
            continue
        entry_price, exit_price = prices

        outcome = compute_outcome(entry_price, exit_price, d["decision"])
        thr = session_threshold(d["session"])
        margin = round(d["conv"] - thr, 1)   # negative = below threshold

        row = {
            "date":          date,
            "time":          d["time"],
            "ticker":        d["ticker"],
            "decision":      d["decision"],
            "conv":          d["conv"],
            "fakeout":       d["fakeout"],
            "session":       d["session"],
            "entry_price":   entry_price,
            "exit_price_15m": exit_price,
            "move_pct":      outcome["move_pct"],
            "would_win":     outcome["would_win"],
            "correct":       outcome["correct"],
            "threshold":     thr,
            "margin":        margin,
        }
        csv_rows.append(row)

        if outcome["correct"] is not None:
            total_assessed += 1
            if outcome["correct"]:
                correct_count += 1

        # Flag interesting cases for human review
        flag = None
        if d["decision"] == "FLAT" and outcome["move_pct"] and outcome["move_pct"] > 0.4:
            flag = f"⚠️  MISSED MOVE  +{outcome['move_pct']:.2f}% — conv was {d['conv']} (needed {thr})"
        elif d["decision"] == "TRADE" and outcome["move_pct"] and outcome["move_pct"] < -0.2:
            flag = f"❌  BAD ENTRY   {outcome['move_pct']:.2f}% — conv was {d['conv']}"
        elif d["decision"] == "TRADE" and outcome["move_pct"] and outcome["move_pct"] > 0.2:
            flag = f"✅  GOOD ENTRY  +{outcome['move_pct']:.2f}%"

        entry = {**row, "flag": flag}
        journal_entries.append(entry)
        if flag:
            log.info(f"  {d['ticker']} {d['time']}  {flag}")

    # ── Accuracy summary ──
    accuracy = correct_count / total_assessed * 100 if total_assessed else 0
    missed_moves = [e for e in journal_entries if e.get("flag","") and "MISSED" in e.get("flag","")]
    bad_entries  = [e for e in journal_entries if e.get("flag","") and "BAD"    in e.get("flag","")]

    # ── Threshold calibration hints ──
    hints = []
    if missed_moves:
        avg_conv_missed = sum(m["conv"] for m in missed_moves) / len(missed_moves)
        hints.append({
            "type": "threshold_too_high",
            "detail": f"Missed {len(missed_moves)} moves. Avg conv at miss: {avg_conv_missed:.1f}",
            "suggestion": f"Consider lowering NORMAL threshold from 60 → {int(avg_conv_missed)-2}",
        })
    if bad_entries:
        avg_conv_bad = sum(b["conv"] for b in bad_entries) / len(bad_entries)
        hints.append({
            "type": "threshold_too_low",
            "detail": f"{len(bad_entries)} bad entries. Avg conv at bad entry: {avg_conv_bad:.1f}",
            "suggestion": f"Consider raising threshold or tightening fakeout filter",
        })

    # ── Write daily journal JSON ──
    journal = {
        "date":         date,
        "decisions":    len(decisions),
        "assessed":     total_assessed,
        "correct":      correct_count,
        "accuracy_pct": round(accuracy, 1),
        "missed_moves": len(missed_moves),
        "bad_entries":  len(bad_entries),
        "hints":        hints,
        "entries":      journal_entries,
    }
    out_path = LOG_DIR / f"journal_{date}.json"
    with open(out_path, "w") as f:
        json.dump(journal, f, indent=2)

    log.info(f"\n  ── Summary ──")
    log.info(f"  Decisions assessed: {total_assessed}")
    log.info(f"  Correct decisions:  {correct_count} ({accuracy:.1f}%)")
    log.info(f"  Missed moves:       {len(missed_moves)}")
    log.info(f"  Bad entries:        {len(bad_entries)}")
    for h in hints:
        log.info(f"  💡 {h['suggestion']}")
    log.info(f"  Saved → {out_path}")

    # ── Append to training CSV ──
    append_to_csv(csv_rows)
    log.info(f"  Appended {len(csv_rows)} rows → {TRAINING_CSV}")
    log.info(f"  Total training rows: {sum(1 for _ in open(TRAINING_CSV))-1}")


if __name__ == "__main__":
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    build_journal()

# SIGNAL_MEMORY_JOURNAL — store + resolve signals in memory (Mastermind Session 2)
try:
    from backend.arjun.signal_memory import get_signal_memory as _get_sm
    _sm = _get_sm()
    for _t in closed_trades if "closed_trades" in dir() else []:
        try:
            _sid = str(_t.get("id", _t.get("trade_id", "")))
            _tkr = str(_t.get("symbol", _t.get("ticker", "SPY")))
            _pnl = float(_t.get("pnl_pct", 0))
            _dir = str(_t.get("direction", _t.get("signal", "")))
            if _sid and _pnl != 0:
                _sm.update_outcome(_sid, _pnl)
        except Exception:
            pass
except Exception as _smje:
    pass
