"""
arjun_news_monitor.py — ARJUN News Intelligence Monitor
Uses Polygon.io Benzinga News API (real-time, no delay).
Run via cron: */2 8-20 * * 1-5
"""

import httpx
import json
import hashlib
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("arjun_news")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_KEY  = os.getenv("POLYGON_API_KEY", "rrJ5P3S52kvCzQzdQRim8qQZwTjqYhba")
WEBHOOK_URL  = os.getenv("NEWS_WEBHOOK_URL", "https://discord.com/api/webhooks/1481681399432876123/n3wkNK4qCaGyE3lCvNW_xznINFD32lXmdvLBS8J7MY52Q6YDRoL1IG203O6H6I5syBVz")
SEEN_FILE    = Path("logs/chakra/news_seen.json")
LOOKBACK_MIN = 60   # fetch news from last 3 minutes (cron runs every 2min)

# ── Impact Keywords ───────────────────────────────────────────────────────────
CRITICAL_KW = [
    "fed rate", "fomc", "emergency rate", "rate cut", "rate hike", "powell",
    "cpi", "pce", "inflation", "recession", "gdp", "bank failure",
    "circuit breaker", "market halt", "war", "sanctions", "default",
    "flash crash", "systemic", "contagion", "government shutdown", "debt ceiling",
    "executive order", "nuclear", "terror",
]
HIGH_KW = [
    "jobs report", "nfp", "unemployment", "consumer sentiment", "pmi",
    "earnings beat", "earnings miss", "guidance cut", "guidance raise",
    "buyback", "dividend cut", "merger", "acquisition", "bankruptcy",
    "sec charges", "doj", "ceo resign", "layoffs", "strike",
    "tariff", "short squeeze", "margin call", "vix spike",
    "retail sales", "housing", "trade deficit",
]
AMPLIFIERS = ["crash", "collapse", "plunge", "surge", "spike", "halt",
              "freeze", "panic", "explode", "wipe", "fear", "soar"]

WATCHLIST = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "MSFT", "TSLA",
             "META", "AMZN", "GOOGL", "JPM", "GS", "TLT", "GLD", "USO"]


# ── Noise Filter ─────────────────────────────────────────────────────────────
SPAM_PUBLISHERS = [
    "bronstein", "gewirtz", "grossman", "levi & korsinsky", "pomerantz",
    "robbins geller", "rosen law", "glancy prongay", "scott+scott",
    "faruqi", "kessler topaz", "class action", "shareholder alert",
    "investor alert", "lawsuit", "securities fraud alert",
]

def is_spam(article: dict) -> bool:
    title     = article.get("title", "").lower()
    publisher = article.get("publisher", {}).get("name", "").lower()
    return any(s in title or s in publisher for s in SPAM_PUBLISHERS)

# ── Polygon News Fetch ────────────────────────────────────────────────────────
def fetch_polygon_news() -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url   = "https://api.polygon.io/v2/reference/news"
    params = {
        "apiKey":       POLYGON_KEY,
        "published_utc.gte": since,
        "order":        "desc",
        "limit":        50,
        "sort":         "published_utc",
    }
    try:
        r = httpx.get(url, params=params, timeout=10)
        data = r.json()
        results = data.get("results", [])
        log.info(f"[POLYGON] {len(results)} articles since {since}")
        return results
    except Exception as e:
        log.warning(f"[POLYGON] Fetch error: {e}")
        return []

# ── Impact Scoring ────────────────────────────────────────────────────────────
def score_article(article: dict) -> dict:
    title    = article.get("title", "")
    desc     = article.get("description", "") or ""
    raw_kw = article.get("keywords", [])
    keywords = [k if isinstance(k, str) else k.get("value", "") for k in raw_kw]
    raw_t = article.get("tickers", [])
    tickers = [t if isinstance(t, str) else t.get("ticker","") for t in raw_t if (t if isinstance(t, str) else t.get("ticker","")) in WATCHLIST]
    text     = (title + " " + desc + " " + " ".join(keywords)).lower()

    score = 0
    tags  = []

    for kw in CRITICAL_KW:
        if kw in text:
            score += 25; tags.append(kw.upper())
    for kw in HIGH_KW:
        if kw in text:
            score += 10; tags.append(kw.upper())
    for w in AMPLIFIERS:
        if w in text:
            score += 8
    score += len(tickers) * 5

    # Polygon already provides sentiment/keywords
    for kw in keywords:
        if kw.lower() in ["federal reserve", "interest rates", "inflation", "recession"]:
            score += 15; tags.append(kw.upper())

    if score >= 50:   impact = "CRITICAL"
    elif score >= 25: impact = "HIGH"
    elif score >= 10: impact = "MEDIUM"
    else:             impact = "LOW"

    return {"score": score, "impact": impact,
            "tags": list(set(tags))[:6], "tickers": tickers}

# ── ARJUN Commentary ──────────────────────────────────────────────────────────
def arjun_take(title: str, scored: dict) -> str:
    text   = title.lower()
    impact = scored["impact"]

    if any(k in text for k in ["fed", "fomc", "rate", "powell", "inflation", "cpi", "pce"]):
        if any(k in text for k in ["cut", "dovish", "pause", "cool", "below"]):
            return "Dovish catalyst — vol compression likely. Vanna tailwind for equities above key GEX levels. Watch for VIX fade toward 22 floor."
        elif any(k in text for k in ["hike", "hawkish", "hot", "above", "beat"]):
            return "Hawkish catalyst — VIX highway opens above 25. Neg-gamma amplification risk. Vanna hedging destabilizes support. Short bias, press on VIX > 25."
        return "Fed-sensitive — wait for price reaction. VIX 25 is the trigger line. Below = range. Above = highway opens."

    if any(k in text for k in ["earnings", "beat", "miss", "guidance", "eps", "revenue"]):
        if any(k in text for k in ["beat", "raise", "above", "record"]):
            return "Earnings beat — potential gamma squeeze above call wall. Fade if IV crush likely post-earnings."
        return "Earnings miss/guide cut — watch put wall activation. Large-cap miss can cascade neg-gamma across index."

    if any(k in text for k in ["war", "sanctions", "nuclear", "attack", "crisis", "terror"]):
        return "Geopolitical shock — safe-haven rotation. VIX spike, oil bid. GEX supports become acceleration zones. Do NOT fade the initial move."

    if any(k in text for k in ["jobs", "nfp", "unemployment", "gdp", "pmi", "retail"]):
        if any(k in text for k in ["weak", "miss", "decline", "drop", "fell", "below"]):
            return "Weak macro — recession narrative reinforced. Bond rally likely. Wait for neg-gamma flush to complete before counter-trend."
        return "Strong macro — stagflation risk if hot. Watch rate reaction first. Equity response depends on whether rates spike with it."

    if any(k in text for k in ["tariff", "trade", "china", "import"]):
        return "Trade/tariff headline — risk-off bias. Supply chain + margin compression narrative. Watch sector rotation out of discretionary/tech."

    if impact == "CRITICAL":
        return "High-impact event — stay flat until price discovers new level. Let vol settle. Monitor VIX + breadth before re-entry."
    return "Monitor for follow-through. Cross-reference GEX flip level and VIX trajectory before acting."

# ── Discord Post ──────────────────────────────────────────────────────────────
def post_discord(article: dict, scored: dict):
    impact  = scored["impact"]
    emoji   = "🚨" if impact == "CRITICAL" else "⚠️"
    color   = 0xFF0000 if impact == "CRITICAL" else 0xFF8C00
    title   = article.get("title", "")[:200]
    url     = article.get("article_url", "")
    source  = article.get("publisher", {}).get("name", "Polygon/Benzinga")
    take    = arjun_take(title, scored)
    tags_str= " ".join([f"`{t}`" for t in scored["tags"]]) or "`MACRO`"
    tick_str= " ".join([f"`{t}`" for t in scored["tickers"]]) or ""

    embed = {
        "title":       f"{emoji} {impact} NEWS — {title}",
        "description": f"**ARJUN's Take:**\n> {take}",
        "url":         url,
        "color":       color,
        "fields": [
            {"name": "Source",       "value": source,             "inline": True},
            {"name": "Impact Score", "value": str(scored["score"]),"inline": True},
            {"name": "Tags",         "value": tags_str,           "inline": False},
        ],
        "footer": {"text": f"CHAKRA News Intel (Polygon/Benzinga) • {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if tick_str:
        embed["fields"].append({"name": "Watchlist Tickers", "value": tick_str, "inline": True})

    try:
        r = httpx.post(WEBHOOK_URL, json={"username": "ARJUN News Intel", "embeds": [embed]}, timeout=10)
        if r.status_code in (200, 204):
            log.info(f"[DISCORD] Posted [{impact}]: {title[:70]}")
        else:
            log.warning(f"[DISCORD] {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log.warning(f"[DISCORD] Error: {e}")

# ── Seen Cache ────────────────────────────────────────────────────────────────
def load_seen() -> set:
    try:
        return set(json.loads(SEEN_FILE.read_text()).get("seen", []))
    except:
        return set()

def save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps({"seen": list(seen)[-1000:]}))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== ARJUN News Scan (Polygon) ===")
    seen = load_seen(); posted = 0

    for article in fetch_polygon_news():
        aid = article.get("id") or hashlib.md5(article.get("title","").encode()).hexdigest()
        if aid in seen:
            continue
        if is_spam(article):
            log.debug(f'[SPAM] {article.get("title","")[:60]}')
            seen.add(aid)
            continue
        seen.add(aid)
        scored = score_article(article)
        if scored["impact"] in ("CRITICAL",):  # Only post score>=50 high-impact news
            post_discord(article, scored)
            posted += 1

    save_seen(seen)
    log.info(f"=== Done — {posted} alert(s) posted ===")

if __name__ == "__main__":
    main()
# This block intentionally left blank - see patch below
