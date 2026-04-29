"""
ARKA / ARJUN Weekly Post-Mortem
================================
Pulls this week's closed trades from Alpaca, pairs buys→sells (FIFO),
computes real P&L per strategy, writes labeled outcomes to the ARJUN
performance DB, retrains the XGBoost model, and posts a full Discord
analysis report.

Usage:
    venv/bin/python3 backend/arka/weekly_postmortem.py
    venv/bin/python3 backend/arka/weekly_postmortem.py --dry-run    # no DB writes / no Discord
    venv/bin/python3 backend/arka/weekly_postmortem.py --days 7     # look-back window (default 7)
"""

import os, sys, json, sqlite3, argparse, logging
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
load_dotenv(BASE / ".env", override=True)

ET  = ZoneInfo("America/New_York")
log = logging.getLogger("ARKA.Postmortem")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PostMortem] %(message)s",
    datefmt="%H:%M:%S",
)

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE   = "https://paper-api.alpaca.markets"
ARJUN_DB      = BASE / "logs/arjun_performance.db"
SWINGS_DB     = BASE / "logs/swings/swings_v3.db"


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — Pull closed orders from Alpaca
# ══════════════════════════════════════════════════════════════════════════════

def fetch_alpaca_orders(days: int = 7) -> list[dict]:
    """Fetch all filled orders for the last N calendar days."""
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error("Alpaca credentials not set"); return []

    after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    orders = []
    page_token = None

    while True:
        params = {
            "status":    "closed",
            "after":     after,
            "limit":     500,
            "direction": "asc",
        }
        if page_token:
            params["page_token"] = page_token

        try:
            r = httpx.get(f"{ALPACA_BASE}/v2/orders", headers=headers,
                          params=params, timeout=12)
            if r.status_code != 200:
                log.error(f"Alpaca orders error: {r.status_code} {r.text[:120]}")
                break
            batch = r.json()
            if not batch:
                break
            orders.extend(batch)
            # Alpaca paginates via page_token header
            page_token = r.headers.get("x-next-page-token")
            if not page_token:
                break
        except Exception as e:
            log.error(f"Alpaca fetch error: {e}")
            break

    log.info(f"  Fetched {len(orders)} closed orders from Alpaca ({days}d look-back)")
    return orders


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — FIFO pairing: buy → sell per contract symbol
# ══════════════════════════════════════════════════════════════════════════════

def pair_trades(orders: list[dict]) -> list[dict]:
    """
    FIFO-pair buy/sell orders per symbol.
    Returns list of completed round-trips with real P&L.
    """
    # Group by symbol, sorted by time
    by_sym = defaultdict(list)
    for o in orders:
        sym = o.get("symbol", "")
        if o.get("filled_qty") in (None, "0", 0):
            continue  # not filled
        if not sym:
            continue
        ts = o.get("filled_at") or o.get("submitted_at") or ""
        by_sym[sym].append({
            "symbol":   sym,
            "side":     o.get("side", ""),
            "qty":      int(float(o.get("filled_qty", 1))),
            "price":    float(o.get("filled_avg_price", 0) or 0),
            "ts":       ts,
            "order_id": o.get("id", ""),
            "asset_class": o.get("asset_class", ""),
        })

    trades = []
    for sym, fills in by_sym.items():
        fills.sort(key=lambda x: x["ts"])
        buy_queue = []  # (qty, price, ts)

        for fill in fills:
            if fill["side"] in ("buy",):
                buy_queue.append([fill["qty"], fill["price"], fill["ts"], fill["order_id"]])
            elif fill["side"] == "sell" and buy_queue:
                sell_qty   = fill["qty"]
                sell_price = fill["price"]

                while sell_qty > 0 and buy_queue:
                    b_qty, b_price, b_ts, b_id = buy_queue[0]
                    matched = min(sell_qty, b_qty)

                    pnl_per_contract = (sell_price - b_price) * 100  # options multiplier
                    pnl              = pnl_per_contract * matched

                    is_option = any(c.isdigit() for c in sym[:6]) or len(sym) > 6
                    if not is_option:
                        pnl = (sell_price - b_price) * matched  # equity

                    trades.append({
                        "symbol":     sym,
                        "qty":        matched,
                        "buy_price":  b_price,
                        "sell_price": sell_price,
                        "pnl":        round(pnl, 2),
                        "buy_ts":     b_ts,
                        "sell_ts":    fill["ts"],
                        "buy_id":     b_id,
                        "sell_id":    fill["order_id"],
                        "asset_class": fill["asset_class"],
                    })

                    sell_qty  -= matched
                    b_qty     -= matched
                    if b_qty <= 0:
                        buy_queue.pop(0)
                    else:
                        buy_queue[0][0] = b_qty

    trades.sort(key=lambda x: x["sell_ts"])
    log.info(f"  Paired {len(trades)} round-trip trades")
    return trades


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — Classify each trade: ticker, direction, strategy, outcome
# ══════════════════════════════════════════════════════════════════════════════

def classify_trade(t: dict) -> dict:
    """
    Derive ticker, direction, strategy and outcome from a paired trade.
    Options symbol format: SPY260408C00664000 → SPY, CALL (or PUT)
    """
    sym = t["symbol"]
    pnl = t["pnl"]

    # Parse options symbol
    import re
    m = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d+)$', sym)
    if m:
        ticker    = m.group(1)
        exp_str   = m.group(2)          # YYMMDD
        opt_type  = "CALL" if m.group(3) == "C" else "PUT"
        strike    = float(m.group(4)) / 1000
        exp_date  = datetime.strptime("20" + exp_str, "%Y%m%d").date()
        dte_entry = (exp_date - datetime.fromisoformat(t["buy_ts"][:10]).date()).days
        strategy  = "0DTE" if dte_entry == 0 else ("SWING" if dte_entry >= 7 else "SCALP")
        direction = "LONG" if opt_type == "CALL" else "SHORT"
    else:
        ticker    = sym
        opt_type  = "EQUITY"
        strike    = 0
        dte_entry = 0
        strategy  = "EQUITY"
        direction = "LONG"

    outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "SCRATCH")

    return {
        **t,
        "ticker":     ticker,
        "opt_type":   opt_type,
        "direction":  direction,
        "strike":     strike,
        "strategy":   strategy,
        "outcome":    outcome,
        "pnl_pct":    round((t["sell_price"] - t["buy_price"]) / t["buy_price"] * 100, 1)
                      if t["buy_price"] > 0 else 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 4 — Pattern analysis: what went wrong
# ══════════════════════════════════════════════════════════════════════════════

def analyze_patterns(trades: list[dict]) -> dict:
    """
    Compute win rates, avg P&L, and identify failure patterns.
    Returns a structured findings dict.
    """
    from collections import Counter

    findings = {
        "total_trades":   len(trades),
        "total_pnl":      round(sum(t["pnl"] for t in trades), 2),
        "wins":           sum(1 for t in trades if t["outcome"] == "WIN"),
        "losses":         sum(1 for t in trades if t["outcome"] == "LOSS"),
        "scratches":      sum(1 for t in trades if t["outcome"] == "SCRATCH"),
        "by_ticker":      {},
        "by_direction":   {},
        "by_strategy":    {},
        "worst_trades":   [],
        "best_trades":    [],
        "problems":       [],
        "recommendations": [],
    }

    findings["win_rate"] = round(findings["wins"] / max(len(trades), 1) * 100, 1)

    # ── By ticker ─────────────────────────────────────────────────────────────
    ticker_groups = defaultdict(list)
    for t in trades:
        ticker_groups[t["ticker"]].append(t)

    for ticker, ts in ticker_groups.items():
        wins  = sum(1 for t in ts if t["outcome"] == "WIN")
        total = len(ts)
        pnl   = round(sum(t["pnl"] for t in ts), 2)
        findings["by_ticker"][ticker] = {
            "trades":   total,
            "wins":     wins,
            "losses":   total - wins,
            "win_rate": round(wins / total * 100, 1),
            "total_pnl": pnl,
            "avg_pnl":  round(pnl / total, 2),
        }

    # ── By direction (CALL vs PUT) ────────────────────────────────────────────
    dir_groups = defaultdict(list)
    for t in trades:
        dir_groups[t["direction"]].append(t)

    for direction, ts in dir_groups.items():
        wins  = sum(1 for t in ts if t["outcome"] == "WIN")
        total = len(ts)
        pnl   = round(sum(t["pnl"] for t in ts), 2)
        findings["by_direction"][direction] = {
            "trades":    total,
            "wins":      wins,
            "win_rate":  round(wins / total * 100, 1),
            "total_pnl": pnl,
            "avg_pnl":   round(pnl / total, 2),
        }

    # ── By strategy ──────────────────────────────────────────────────────────
    strat_groups = defaultdict(list)
    for t in trades:
        strat_groups[t["strategy"]].append(t)

    for strat, ts in strat_groups.items():
        wins  = sum(1 for t in ts if t["outcome"] == "WIN")
        total = len(ts)
        pnl   = round(sum(t["pnl"] for t in ts), 2)
        findings["by_strategy"][strat] = {
            "trades":    total,
            "wins":      wins,
            "win_rate":  round(wins / total * 100, 1),
            "total_pnl": pnl,
            "avg_pnl":   round(pnl / total, 2),
        }

    # ── Worst / best trades ───────────────────────────────────────────────────
    sorted_by_pnl = sorted(trades, key=lambda x: x["pnl"])
    findings["worst_trades"] = sorted_by_pnl[:5]
    findings["best_trades"]  = sorted_by_pnl[-5:][::-1]

    # ── Identify specific problems ────────────────────────────────────────────
    problems = []

    # Problem 1: Near-zero premium puts expiring worthless
    tiny_premium_losses = [
        t for t in trades
        if t["opt_type"] == "PUT" and t["buy_price"] < 0.15 and t["outcome"] == "LOSS"
    ]
    if tiny_premium_losses:
        wasted = round(sum(abs(t["pnl"]) for t in tiny_premium_losses), 2)
        problems.append({
            "type":    "TINY_PREMIUM_PUTS",
            "count":   len(tiny_premium_losses),
            "pnl":     -wasted,
            "detail":  f"{len(tiny_premium_losses)} PUT entries with buy price <$0.15 expired worthless. "
                       f"Total wasted: ${wasted}. These are lottery tickets, not trades.",
            "fix":     "Add minimum premium filter: skip options where entry price < $0.20",
        })

    # Problem 2: Same-direction repeated stops (churning)
    qqq_put_losses = [
        t for t in trades
        if t["ticker"] == "QQQ" and t["opt_type"] == "PUT" and t["outcome"] == "LOSS"
    ]
    if len(qqq_put_losses) >= 3:
        churn_loss = round(sum(abs(t["pnl"]) for t in qqq_put_losses), 2)
        problems.append({
            "type":    "QQQ_PUT_CHURN",
            "count":   len(qqq_put_losses),
            "pnl":     -churn_loss,
            "detail":  f"QQQ PUT entered {len(qqq_put_losses)} times and stopped out every time. "
                       f"Total loss: ${churn_loss}. QQQ was trending UP this week (tariff pause rally).",
            "fix":     "After 2 consecutive same-ticker-same-direction stops, add 60-min cooldown before re-entry. "
                       "Macro context: don't fight a +10% gap-up day with PUTs.",
        })

    # Problem 3: Large single-trade loss
    big_losses = [t for t in trades if t["pnl"] < -50]
    for bl in big_losses:
        problems.append({
            "type":    "LARGE_LOSS",
            "count":   1,
            "pnl":     bl["pnl"],
            "detail":  f"{bl['symbol']} bought @ ${bl['buy_price']:.2f}, sold @ ${bl['sell_price']:.2f} "
                       f"→ ${bl['pnl']:.2f}. "
                       f"This is a {bl['strategy']} trade that went wrong direction.",
            "fix":     "For scalp trades >$5 premium, use tighter stop: -20% instead of -30%. "
                       "Also check macro events before entry (tariff news can reverse gaps instantly).",
        })

    # Problem 4: Low win-rate direction
    for direction, stats in findings["by_direction"].items():
        if stats["trades"] >= 3 and stats["win_rate"] < 35:
            problems.append({
                "type":    f"LOW_WIN_RATE_{direction}",
                "count":   stats["trades"],
                "pnl":     stats["total_pnl"],
                "detail":  f"{direction} trades: {stats['trades']} entries, "
                           f"{stats['win_rate']}% win rate, ${stats['total_pnl']} total P&L.",
                "fix":     f"Review conviction threshold for {direction} signals. "
                           f"Consider requiring GEX alignment before entering {direction} positions.",
            })

    findings["problems"] = problems

    # ── Recommendations ───────────────────────────────────────────────────────
    recs = []
    if any(p["type"] == "TINY_PREMIUM_PUTS" for p in problems):
        recs.append("Add minimum entry premium filter: `MIN_OPTION_PREMIUM = 0.20` in arka_engine.py")
    if any(p["type"] == "QQQ_PUT_CHURN" for p in problems):
        recs.append("Implement same-direction cooldown: 2 consecutive stops → 60-min block for that ticker+direction")
    if any(p["type"] == "LARGE_LOSS" for p in problems):
        recs.append("Tighten stop for high-premium scalp entries (>$5): use -20% not -30%")
    if findings["win_rate"] < 45:
        recs.append("Win rate below 45% — self-correct engine should lower conviction threshold by 2pts")

    # SPY vs QQQ comparison
    spy_stats = findings["by_ticker"].get("SPY", {})
    qqq_stats = findings["by_ticker"].get("QQQ", {})
    if spy_stats and qqq_stats:
        if spy_stats.get("win_rate", 0) > qqq_stats.get("win_rate", 0) + 20:
            recs.append(
                f"SPY win rate ({spy_stats['win_rate']}%) far exceeds QQQ ({qqq_stats['win_rate']}%). "
                f"Consider reducing QQQ position size or raising QQQ conviction threshold."
            )

    findings["recommendations"] = recs
    return findings


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 5 — Write outcomes back to ARJUN performance DB
# ══════════════════════════════════════════════════════════════════════════════

def write_outcomes_to_arjun_db(trades: list[dict], dry_run: bool = False) -> int:
    """
    Insert new signal records for this week's trades so ARJUN can retrain.
    Skips duplicates (matches on date + ticker + entry_price).
    Returns count of rows inserted.
    """
    if not ARJUN_DB.exists():
        log.warning(f"ARJUN DB not found at {ARJUN_DB} — skipping write")
        return 0

    conn = sqlite3.connect(ARJUN_DB)
    inserted = 0

    for t in trades:
        buy_date = t["buy_ts"][:10]
        ticker   = t["ticker"]
        price    = t["buy_price"]
        pnl      = t["pnl"]
        outcome  = t["outcome"]
        signal   = "BUY" if t["direction"] == "LONG" else "SELL"

        # Check for duplicate
        existing = conn.execute(
            "SELECT id FROM signals WHERE date=? AND ticker=? AND entry_price=?",
            (buy_date, ticker, price)
        ).fetchone()
        if existing:
            # Update outcome if previously NULL
            if not dry_run:
                conn.execute(
                    "UPDATE signals SET outcome=?, pnl=?, exit_price=? WHERE id=?",
                    (outcome, pnl, t["sell_price"], existing[0])
                )
            continue

        if not dry_run:
            conn.execute(
                """INSERT INTO signals
                   (date, ticker, signal, confidence, entry_price, exit_price,
                    pnl, outcome, risk_decision, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    buy_date, ticker, signal,
                    50.0,          # default confidence — will be updated by retrain
                    price,
                    t["sell_price"],
                    pnl,
                    outcome,
                    t["strategy"],
                    datetime.now().isoformat(),
                )
            )
        inserted += 1

    if not dry_run:
        conn.commit()
    conn.close()
    log.info(f"  {'[DRY RUN] Would insert' if dry_run else 'Inserted'} {inserted} new signal records into ARJUN DB")
    return inserted


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 6 — Retrain ARJUN XGBoost on updated data
# ══════════════════════════════════════════════════════════════════════════════

def retrain_arjun(dry_run: bool = False) -> dict:
    """Run the weekly retrain with fresh labeled outcomes."""
    if dry_run:
        log.info("  [DRY RUN] Skipping retrain")
        return {}
    try:
        from backend.arjun.weekly_retrain import retrain_model, analyze_signal_performance
        log.info("  Running ARJUN retrain...")
        analyze_signal_performance()
        retrain_model()
        log.info("  ARJUN retrain complete")
        return {"status": "ok"}
    except Exception as e:
        log.error(f"  ARJUN retrain failed: {e}")
        return {"status": "error", "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 7 — Post Discord report
# ══════════════════════════════════════════════════════════════════════════════

def _pnl_emoji(pnl: float) -> str:
    if pnl > 50:   return "🟢"
    if pnl > 0:    return "🟡"
    if pnl > -50:  return "🟠"
    return "🔴"


def post_discord_report(findings: dict, retrain: dict, days: int, dry_run: bool = False):
    """Post a full post-mortem embed to #health."""
    try:
        from backend.chakra.discord_router import post_health

        total_pnl  = findings["total_pnl"]
        win_rate   = findings["win_rate"]
        n_trades   = findings["total_trades"]
        n_wins     = findings["wins"]
        n_losses   = findings["losses"]

        # Build per-ticker summary
        ticker_lines = []
        for ticker, stats in sorted(findings["by_ticker"].items(),
                                    key=lambda x: x[1]["total_pnl"], reverse=True):
            wr  = stats["win_rate"]
            pnl = stats["total_pnl"]
            icon = _pnl_emoji(pnl)
            ticker_lines.append(
                f"{icon} **{ticker}** {stats['trades']}t | "
                f"WR:{wr:.0f}% | P&L: **${pnl:.2f}**"
            )

        # Problems summary
        problem_lines = []
        for p in findings["problems"]:
            problem_lines.append(f"⚠️ **{p['type']}** ({p['count']}x, ${p['pnl']:.2f})")
            problem_lines.append(f"  ↳ {p['detail'][:100]}")
            problem_lines.append(f"  🔧 {p['fix'][:100]}")

        # Direction breakdown
        dir_lines = []
        for direction, stats in findings["by_direction"].items():
            icon = "📈" if direction == "LONG" else "📉"
            dir_lines.append(
                f"{icon} **{direction}**: {stats['trades']}t | WR:{stats['win_rate']:.0f}% | ${stats['total_pnl']:.2f}"
            )

        # Best/worst
        best_str  = "\n".join(
            f"  ✅ {t['symbol'][:20]:20} +${t['pnl']:.2f} ({t['pnl_pct']:+.1f}%)"
            for t in findings["best_trades"][:3]
        )
        worst_str = "\n".join(
            f"  ❌ {t['symbol'][:20]:20} ${t['pnl']:.2f} ({t['pnl_pct']:+.1f}%)"
            for t in findings["worst_trades"][:3]
        )

        retrain_status = "✅ Retrained" if retrain.get("status") == "ok" else \
                         ("⏭️ Skipped (dry run)" if dry_run else f"❌ {retrain.get('error','?')}")

        pnl_icon = _pnl_emoji(total_pnl)
        now_et   = datetime.now(ET)
        week_end = now_et.strftime("%A, %B %d")

        fields = []

        # Overall summary
        fields.append({
            "name":  f"{pnl_icon} Week Ending {week_end}",
            "value": (
                f"**{n_trades} trades** | "
                f"**{n_wins}W / {n_losses}L** | "
                f"Win rate: **{win_rate:.1f}%**\n"
                f"Net P&L: **${total_pnl:.2f}**"
            ),
            "inline": False,
        })

        # Per-ticker
        if ticker_lines:
            fields.append({
                "name":  "📊 By Ticker",
                "value": "\n".join(ticker_lines[:8]),
                "inline": False,
            })

        # By direction
        if dir_lines:
            fields.append({
                "name":  "🎯 By Direction",
                "value": "\n".join(dir_lines),
                "inline": False,
            })

        # Best/Worst
        if best_str:
            fields.append({
                "name":  "🏆 Best Trades",
                "value": best_str[:512],
                "inline": True,
            })
        if worst_str:
            fields.append({
                "name":  "💀 Worst Trades",
                "value": worst_str[:512],
                "inline": True,
            })

        # Problems — one field per problem to stay under 1024 char limit
        for i, p in enumerate(findings["problems"][:4], 1):
            val = (
                f"⚠️ **{p['type']}** ({p['count']}x, ${p['pnl']:.2f})\n"
                f"↳ {p['detail'][:150]}\n"
                f"🔧 {p['fix'][:150]}"
            )
            fields.append({
                "name":   f"Problem {i}",
                "value":  val[:1024],
                "inline": False,
            })

        # Recommendations
        if findings["recommendations"]:
            recs = "\n".join(f"• {r[:120]}" for r in findings["recommendations"][:4])
            fields.append({
                "name":  "💡 ARJUN Recommendations",
                "value": recs[:1024],
                "inline": False,
            })

        # Retrain status
        fields.append({
            "name":  "🤖 ARJUN Retrain",
            "value": retrain_status,
            "inline": False,
        })

        embed = {
            "title":       f"📋 ARKA Weekly Post-Mortem — {week_end}",
            "description": f"Analysis of last {days} days. New labeled outcomes written to ARJUN DB.",
            "color":       0x2ECC71 if total_pnl >= 0 else 0xE74C3C,
            "fields":      fields,
            "footer":      {"text": "ARJUN Self-Learning Engine  •  Post-Mortem Analysis"},
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

        if dry_run:
            log.info("  [DRY RUN] Discord payload built — not sending")
            log.info(f"  Title: {embed['title']}")
            return

        ok = post_health({"embeds": [embed]})
        log.info(f"  {'✅ Discord report sent' if ok else '❌ Discord report failed'}")

    except Exception as e:
        log.error(f"  Discord report error: {e}")
        import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run_postmortem(days: int = 7, dry_run: bool = False):
    log.info("=" * 60)
    log.info(f"  ARKA / ARJUN WEEKLY POST-MORTEM  (last {days} days)")
    log.info(f"  {'DRY RUN — no writes' if dry_run else 'LIVE — writing to DB'}")
    log.info("=" * 60)

    # 1. Fetch orders
    orders = fetch_alpaca_orders(days=days)
    if not orders:
        log.warning("No orders found — nothing to analyze")
        return

    # 2. Pair into round-trips
    paired = pair_trades(orders)
    if not paired:
        log.warning("No paired trades found (no completed round-trips)")
        return

    # 3. Classify
    trades = [classify_trade(t) for t in paired]

    # 4. Analyze patterns
    log.info("\n  Analyzing patterns...")
    findings = analyze_patterns(trades)

    # Print to console
    log.info(f"\n  ── RESULTS ──")
    log.info(f"  Trades: {findings['total_trades']}  |  "
             f"W/L: {findings['wins']}/{findings['losses']}  |  "
             f"Win rate: {findings['win_rate']}%  |  "
             f"Net P&L: ${findings['total_pnl']:.2f}")

    log.info(f"\n  ── BY TICKER ──")
    for ticker, stats in sorted(findings["by_ticker"].items(),
                                key=lambda x: x[1]["total_pnl"], reverse=True):
        log.info(f"    {ticker:6} {stats['trades']}t  WR:{stats['win_rate']:4.0f}%  "
                 f"P&L:${stats['total_pnl']:8.2f}")

    log.info(f"\n  ── BY DIRECTION ──")
    for direction, stats in findings["by_direction"].items():
        log.info(f"    {direction:6} {stats['trades']}t  WR:{stats['win_rate']:4.0f}%  "
                 f"P&L:${stats['total_pnl']:8.2f}")

    log.info(f"\n  ── PROBLEMS ({len(findings['problems'])}) ──")
    for p in findings["problems"]:
        log.info(f"    [{p['type']}] {p['detail'][:100]}")
        log.info(f"    FIX: {p['fix'][:100]}")

    log.info(f"\n  ── BEST TRADES ──")
    for t in findings["best_trades"][:3]:
        log.info(f"    {t['symbol']:25} +${t['pnl']:.2f} ({t['pnl_pct']:+.1f}%)")

    log.info(f"\n  ── WORST TRADES ──")
    for t in findings["worst_trades"][:3]:
        log.info(f"    {t['symbol']:25}  ${t['pnl']:.2f} ({t['pnl_pct']:+.1f}%)")

    # 5. Write to ARJUN DB
    inserted = write_outcomes_to_arjun_db(trades, dry_run=dry_run)

    # 6. Retrain ARJUN
    retrain_result = {}
    if inserted > 0 or not dry_run:
        retrain_result = retrain_arjun(dry_run=dry_run)

    # 7. Post Discord
    post_discord_report(findings, retrain_result, days=days, dry_run=dry_run)

    log.info("\n  ✅ Post-mortem complete")
    return findings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARKA/ARJUN Weekly Post-Mortem")
    parser.add_argument("--days",    type=int, default=7, help="Look-back window in days (default 7)")
    parser.add_argument("--dry-run", action="store_true",  help="Analyze only — no DB writes, no Discord")
    args = parser.parse_args()

    run_postmortem(days=args.days, dry_run=args.dry_run)
