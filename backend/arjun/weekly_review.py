"""
ARJUN Weekly Performance Review.
Runs Sunday 6pm ET. Reads closed trades, computes accuracy per ticker,
writes weekly_brief.json and updates conviction_adjustments.json.
"""
import json
import glob
import os
import time
import httpx
from pathlib import Path
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
BRIEF_PATH    = "logs/arjun/weekly_brief.json"
ADJ_PATH      = "logs/arjun/conviction_adjustments.json"


def load_week_outcomes() -> list:
    """Load all trade outcomes from the last 7 days."""
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    files  = sorted(glob.glob("logs/arjun/feedback/outcomes_*.json"), reverse=True)[:7]
    outcomes = []
    for f in files:
        day = Path(f).stem.split("outcomes_")[-1]
        if day >= cutoff:
            try:
                outcomes += json.loads(Path(f).read_text())
            except Exception:
                pass
    return outcomes


def load_alpaca_closed_trades() -> list:
    """Pull closed orders from Alpaca as ground truth."""
    headers = {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", ""),
    }
    try:
        r = httpx.get(
            "https://paper-api.alpaca.markets/v2/orders",
            headers=headers,
            params={"status": "closed", "limit": 100, "direction": "desc"},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def compute_ticker_stats(outcomes: list) -> dict:
    """Per-ticker accuracy and P&L summary."""
    stats = {}
    for o in outcomes:
        t = o["ticker"]
        if t not in stats:
            stats[t] = {"trades": 0, "correct": 0, "total_pnl": 0.0}
        stats[t]["trades"]    += 1
        stats[t]["correct"]   += 1 if o.get("correct") else 0
        stats[t]["total_pnl"] += o.get("pnl_pct", 0)

    for t in stats:
        n = stats[t]["trades"]
        stats[t]["accuracy"] = round(stats[t]["correct"] / n * 100, 1) if n else 0
        stats[t]["avg_pnl"]  = round(stats[t]["total_pnl"] / n, 2) if n else 0

    return stats


def generate_brief_with_claude(stats: dict, outcomes: list) -> str:
    """Ask Claude to write the weekly performance brief."""
    if not ANTHROPIC_KEY:
        return "No Anthropic API key — skipping Claude brief."

    summary = json.dumps({
        "week_ending":       str(date.today()),
        "total_trades":      len(outcomes),
        "overall_accuracy":  round(
            sum(1 for o in outcomes if o.get("correct")) / max(len(outcomes), 1) * 100, 1
        ),
        "per_ticker": stats,
    }, indent=2)

    prompt = f"""You are ARJUN, the CHAKRA trading system's analysis agent.
Here is this week's trading performance data:

{summary}

Write a concise weekly performance brief covering:
1. Overall win rate and P&L summary (2 sentences)
2. Top 2 performing tickers this week and why (1 sentence each)
3. Bottom 2 performing tickers and what went wrong (1 sentence each)
4. Specific threshold adjustments for next week:
   - Which tickers need higher conviction threshold (>65) and why
   - Which tickers can stay at standard threshold (55)
   - Any tickers to avoid trading next week
5. One tactical insight about time-of-day patterns if visible in the data

Be direct and specific. No fluff. Total length: under 200 words."""

    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":    "claude-sonnet-4-6",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"Claude brief failed: {e}"


def compute_conviction_adjustments(stats: dict) -> dict:
    """
    Generate Monday conviction threshold adjustments per ticker.
    Tickers with <40% accuracy get +10 threshold (harder to enter).
    Tickers with >65% accuracy get -5 threshold (easier to enter).
    """
    adjustments = {}
    for ticker, s in stats.items():
        if s["trades"] < 3:
            continue
        if s["accuracy"] < 40:
            adjustments[ticker] = {
                "delta":  +10,
                "reason": f"Low accuracy {s['accuracy']:.0f}% — raise bar",
            }
        elif s["accuracy"] > 65:
            adjustments[ticker] = {
                "delta":  -5,
                "reason": f"Strong accuracy {s['accuracy']:.0f}% — lower bar slightly",
            }
    return adjustments


def run_weekly_review():
    """Main entry point."""
    Path("logs/arjun").mkdir(parents=True, exist_ok=True)

    print("📊 ARJUN Weekly Performance Review starting...")
    outcomes = load_week_outcomes()

    if not outcomes:
        print("⚠️  No trade outcomes found for this week.")
        return

    stats       = compute_ticker_stats(outcomes)
    brief       = generate_brief_with_claude(stats, outcomes)
    adjustments = compute_conviction_adjustments(stats)

    Path(BRIEF_PATH).write_text(json.dumps({
        "week_ending":  str(date.today()),
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "brief":        brief,
        "ticker_stats": stats,
        "total_trades": len(outcomes),
    }, indent=2))

    Path(ADJ_PATH).write_text(json.dumps({
        "generated_at": str(date.today()),
        "adjustments":  adjustments,
    }, indent=2))

    print(f"\n{'='*50}")
    print("WEEKLY BRIEF:")
    print(brief)
    print(f"{'='*50}")
    print(f"\nConviction adjustments for Monday: {adjustments}")
    print(f"\n✅ Brief saved to {BRIEF_PATH}")
    print(f"✅ Adjustments saved to {ADJ_PATH}")


if __name__ == "__main__":
    run_weekly_review()
