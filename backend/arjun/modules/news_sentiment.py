import os
import httpx
import asyncio
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv(override=True)
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

# FinBERT disabled on Intel Mac (torch 2.4+ required)
# Keyword fallback handles all sentiment scoring automatically
_pipeline = None

def _get_pipeline():
    return None  # Keyword fallback always used — safe on all platforms

BULL_KEYWORDS = ['beat','beats','exceeds','strong','surge','rally','gain',
                 'upgrade','buy','positive','record','growth','outperform',
                 'bullish','breakout','recovery','profit','expansion']
BEAR_KEYWORDS = ['miss','misses','disappoints','weak','decline','fall','drop',
                 'downgrade','sell','negative','loss','cut','warning','risk',
                 'bearish','breakdown','recession','contraction','layoffs']

def _keyword_score(text: str) -> float:
    t    = text.lower()
    bull = sum(1 for w in BULL_KEYWORDS if w in t)
    bear = sum(1 for w in BEAR_KEYWORDS if w in t)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 3)

def analyze_news_sentiment(ticker: str, lookback_hours: int = 24) -> Dict:
    """Fetch Polygon news and score with keyword fallback."""
    articles = asyncio.run(_fetch_news(ticker, lookback_hours))
    if not articles:
        return {'sentiment': 'NEUTRAL', 'score': 0.0, 'article_count': 0,
                'top_headlines': [], 'bull_boost': 0.0, 'bear_boost': 0.0}

    scores = [_keyword_score(a.get('title', '') + ' ' + a.get('description', ''))
              for a in articles]
    avg = sum(scores) / len(scores) if scores else 0.0

    return {
        'sentiment':     'POSITIVE' if avg > 0.15 else 'NEGATIVE' if avg < -0.15 else 'NEUTRAL',
        'score':         round(avg, 4),
        'article_count': len(articles),
        'top_headlines': [a['title'] for a in articles[:3]],
        'bull_boost':    round(max(0, avg) * 20, 1),
        'bear_boost':    round(max(0, -avg) * 20, 1),
    }

async def _fetch_news(ticker, hours):
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.polygon.io/v2/reference/news",
                params={"apiKey": POLYGON_API_KEY, "ticker": ticker,
                        "published_utc.gte": cutoff, "limit": 20, "order": "desc"})
        return r.json().get("results", [])
    except Exception:
        return []
