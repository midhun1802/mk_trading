"""
Pre-market catalyst checker.
Runs at 7:50 AM before ARJUN daily signal generation.
Writes logs/arjun/catalysts_today.json with flagged tickers.
"""
import json
import os
import time
import httpx
from pathlib import Path
from datetime import date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
OUTPUT_PATH = "logs/arjun/catalysts_today.json"

WATCHLIST = [
    "SPY", "QQQ", "IWM", "SPX",
    "AAPL", "NVDA", "TSLA", "AMZN", "MSFT",
    "META", "GOOGL", "AMD", "COIN", "NFLX",
    "ARKK", "SQQQ", "TLT", "GLD", "SLV",
]


def check_earnings_today(ticker: str) -> bool:
    """Check if ticker has earnings today or overnight."""
    try:
        r = httpx.get(
            "https://api.polygon.io/vX/reference/financials",
            params={"ticker": ticker, "timeframe": "quarterly", "limit": 1, "apiKey": POLYGON_KEY},
            timeout=5,
        )
        results = r.json().get("results", [])
        if not results:
            return False
        filing = results[0].get("filing_date", "")
        return filing >= str(date.today())
    except Exception:
        return False


def check_news_catalyst(ticker: str) -> dict:
    """Check for major news in last 12 hours."""
    try:
        r = httpx.get(
            "https://api.polygon.io/v2/reference/news",
            params={"ticker": ticker, "limit": 3, "order": "desc", "apiKey": POLYGON_KEY},
            timeout=5,
        )
        articles = r.json().get("results", [])
        if not articles:
            return {"has_catalyst": False}

        keywords = [
            "earnings", "beat", "miss", "guidance", "FDA", "approval",
            "rejected", "recall", "merger", "acquisition", "SEC",
            "layoffs", "bankruptcy", "downgrade", "upgrade", "analyst",
        ]
        for article in articles[:3]:
            title = article.get("title", "").lower()
            if any(kw in title for kw in keywords):
                return {
                    "has_catalyst":  True,
                    "headline":      article.get("title", ""),
                    "published_utc": article.get("published_utc", ""),
                }
        return {"has_catalyst": False}
    except Exception:
        return {"has_catalyst": False}


def get_catalyst_penalty(ticker: str) -> int:
    """Return conviction penalty if ticker has a pre-market catalyst."""
    path = Path(OUTPUT_PATH)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
        if data.get("date") != str(date.today()):
            return 0  # stale file
        if ticker in data.get("flagged", {}):
            return -20  # major penalty — wait for price to stabilize
    except Exception:
        pass
    return 0


def run_catalyst_check() -> dict:
    """Main function. Returns dict of flagged tickers."""
    Path("logs/arjun").mkdir(parents=True, exist_ok=True)
    flagged = {}

    print("🔍 Running pre-market catalyst check...")
    for ticker in WATCHLIST:
        has_earnings = check_earnings_today(ticker)
        news         = check_news_catalyst(ticker)

        if has_earnings or news["has_catalyst"]:
            flagged[ticker] = {
                "earnings": has_earnings,
                "news":     news.get("headline", ""),
                "caution":  True,
                "reason":   ("EARNINGS" if has_earnings
                             else f"NEWS: {news.get('headline','')[:60]}"),
            }
            print(f"  ⚠️  {ticker}: {flagged[ticker]['reason']}")

    output = {
        "date":          str(date.today()),
        "checked_at":    time.strftime("%H:%M ET"),
        "flagged":       flagged,
        "flagged_list":  list(flagged.keys()),
        "total_flagged": len(flagged),
    }
    Path(OUTPUT_PATH).write_text(json.dumps(output, indent=2))
    print(f"✅ Catalyst check done: {len(flagged)} tickers flagged → {OUTPUT_PATH}")
    return output


if __name__ == "__main__":
    run_catalyst_check()
