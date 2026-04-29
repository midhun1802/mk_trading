"""
market_briefing.py — Global Market Briefing Engine
100% Polygon.io — uses the same snapshot endpoint as the rest of CHAKRA.
"""

import os
import asyncio
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic

log = logging.getLogger("market.briefing")
ET  = ZoneInfo("America/New_York")

POLYGON_TICKERS = {
    # US Indices (ETF proxies)
    "SPY":  {"label": "SPY — S&P 500",     "region": "US",     "icon": "🇺🇸"},
    "QQQ":  {"label": "QQQ — Nasdaq 100",  "region": "US",     "icon": "🇺🇸"},
    "IWM":  {"label": "IWM — Russell 2000","region": "US",     "icon": "🇺🇸"},
    "DIA":  {"label": "DIA — Dow Jones",   "region": "US",     "icon": "🇺🇸"},

    # Macro
    "GLD":  {"label": "GLD — Gold",        "region": "Macro",  "icon": "🥇"},
    "USO":  {"label": "USO — Oil",         "region": "Macro",  "icon": "🛢️"},
    "TLT":  {"label": "TLT — 10Y Bond",    "region": "Macro",  "icon": "🏛️"},
    "UUP":  {"label": "UUP — US Dollar",   "region": "Macro",  "icon": "💵"},
    "VIXY": {"label": "VIXY — VIX",        "region": "Macro",  "icon": "⚡"},
    # Sentiment / Leverage
    "TQQQ": {"label": "TQQQ — Bull 3x",   "region": "Sentiment","icon": "🚀"},
    "SQQQ": {"label": "SQQQ — Bear 3x",   "region": "Sentiment","icon": "🐻"},
}

CLAUDE_PRE = (
    "You are a professional market analyst writing a pre-market briefing for active US day traders. "
    "You will receive ETF and sector data from Polygon showing pre-market price action.\n\n"
    "Paragraph 1 - OVERNIGHT SETUP: What does the pre-market data show across SPY/QQQ/IWM/DIA? "
    "Which sectors are leading or lagging? What is the macro backdrop (Gold, Oil, Bonds, Dollar, VIX)?\n"
    "Paragraph 2 - BIAS & KEY LEVELS: What is the directional lean for today's session — bullish, bearish, or choppy? "
    "Call out specific price levels on SPY and QQQ that matter. What does the TQQQ/SQQQ ratio say about sentiment?\n"
    "Paragraph 3 - TRADE PLAN: What should a trader focus on today? Which sectors have the best setups? "
    "Any specific risks or catalysts to be aware of?\n\n"
    "Tone: Sharp, direct, like a seasoned trader briefing the desk. Use actual numbers from the data. No fluff.\n"
    "Format: Plain paragraphs only. No bullet points. No markdown headers. No asterisks."
)

CLAUDE_POST = (
    "You are a professional market analyst writing an end-of-day debrief for active US day traders. "
    "You will receive final ETF and sector closing data from Polygon.\n\n"
    "Paragraph 1 - WHAT HAPPENED: How did SPY/QQQ/IWM/DIA close? Which sectors led and lagged today? "
    "What did macro (Gold, Oil, Bonds, Dollar, VIX) tell us about the day?\n"
    "Paragraph 2 - KEY TAKEAWAYS: What were the dominant themes today — risk-on or risk-off? "
    "Was the TQQQ/SQQQ ratio consistent with price action? Any notable divergences?\n"
    "Paragraph 3 - TOMORROW'S SETUP: Based on today's close, what is the early lean for tomorrow? "
    "Key levels to watch overnight on SPY and QQQ. Any sector rotations building?\n\n"
    "Tone: Honest debrief between two traders. Use actual numbers. Be direct about what the market said.\n"
    "Format: Plain paragraphs only. No bullet points. No markdown headers. No asterisks."
)


async def fetch_market_data() -> dict:
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        return {"error": "POLYGON_API_KEY not set in .env"}

    syms = list(POLYGON_TICKERS.keys())
    results = {}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": ",".join(syms), "apiKey": key}
            )
            data = r.json()
            for t in data.get("tickers", []):
                sym  = t.get("ticker", "")
                if sym not in POLYGON_TICKERS:
                    continue
                meta = POLYGON_TICKERS[sym]
                day  = t.get("day", {})
                prev = t.get("prevDay", {})
                lp   = float(t.get("lastTrade", {}).get("p", 0) or day.get("c", 0) or 0)
                pc   = float(prev.get("c", 1) or 1)
                chg  = round((lp - pc) / pc * 100, 2) if pc else 0.0
                results[sym] = {
                    **meta,
                    "price":      round(lp, 2),
                    "prev_close": round(pc, 2),
                    "change_pct": chg,
                    "direction":  "up" if chg > 0 else "down" if chg < 0 else "flat",
                    "status":     "ok",
                }
    except Exception as e:
        log.error(f"Polygon snapshot error: {e}")
        return {"error": str(e)}

    return results


def _build_data_string(market_data: dict) -> str:
    lines = []
    for region in ["US", "Macro", "Sentiment"]:
        lines.append(f"--- {region} ---")
        for sym, d in market_data.items():
            if d.get("region") != region:
                continue
            price = d.get("price")
            chg   = d.get("change_pct")
            arrow = "▲" if chg and chg > 0 else "▼" if chg and chg < 0 else "—"
            if price and chg is not None:
                lines.append(f"  {d['label']}: {price:,.2f}  {arrow} {chg:+.2f}%")
            else:
                lines.append(f"  {d['label']}: N/A")
        lines.append("")
    return "\n".join(lines)


async def generate_briefing(mode: str = "pre") -> dict:
    now    = datetime.now(ET)
    market = await fetch_market_data()

    if "error" in market:
        return {"error": market["error"], "mode": mode}

    data_str = _build_data_string(market)
    system_p = CLAUDE_PRE if mode == "pre" else CLAUDE_POST
    user_msg = (
        f"Today is {now.strftime('%A, %B %d %Y')}. "
        f"Current time: {now.strftime('%I:%M %p ET')}.\n\nMarket Data:\n{data_str}"
    )

    try:
        client    = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        resp      = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 800,
            system     = system_p,
            messages   = [{"role": "user", "content": user_msg}]
        )
        narrative = resp.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude briefing error: {e}")
        narrative = f"Briefing unavailable: {e}"

    grouped = {"US": [], "Macro": [], "Sentiment": []}
    for sym, d in market.items():
        region = d.get("region", "Macro")
        if region in grouped:
            grouped[region].append({"symbol": sym, **d})

    return {
        "mode":      mode,
        "generated": now.strftime("%I:%M %p ET"),
        "date":      now.strftime("%A, %B %d, %Y"),
        "narrative": narrative,
        "markets":   grouped,
    }
