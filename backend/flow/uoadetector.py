"""
CHAKRA UOA Detector — Expanded
Covers: Index ETFs (SPY/QQQ/IWM/DIA/GLD/TLT)
        Index Options (SPX / RUT)
        Individual Stocks (AAPL/NVDA/TSLA/META/AMZN/MSFT/GOOGL/AMD/PLTR/SMCI + more)
"""

import asyncio
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("CHAKRA.UOA")
ET  = ZoneInfo("America/New_York")

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")

# ── Ticker Universe ────────────────────────────────────────────────────────

INDEX_ETFS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "XLF", "XLE", "XLK"]

# Cash-settled index options — Polygon uses O:SPX* / O:SPXW*
INDEX_OPTIONS = {
    "SPX":  "O:SPX",    # S&P 500 index options (100x multiplier)
    "SPXW": "O:SPXW",   # SPX weeklies
    "RUT":  "O:RUT",    # Russell 2000 index options
}

STOCKS = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    # Semis / AI
    "AMD", "SMCI", "AVGO", "TSM", "INTC", "QCOM", "MU",
    # High-beta / meme flow
    "PLTR", "MSTR", "COIN", "HOOD", "RBLX", "SOFI",
    # Financials
    "JPM", "BAC", "GS", "MS",
    # Energy / commodities
    "XOM", "CVX",
    # Biotech
    "LLY", "MRNA", "BNTX",
]

ALL_EQUITY_TICKERS = INDEX_ETFS + STOCKS

# ── Thresholds (tuned per asset class) ────────────────────────────────────

THRESHOLDS = {
    # (min_vol_oi_ratio, min_contracts, min_premium_usd, extreme_ratio)
    "INDEX_ETF":    (5,   500,   50_000,   50),
    "INDEX_OPTION": (3,   100,  500_000,   20),   # SPX/RUT: high $ per contract
    "MEGA_CAP":     (5,   200,   25_000,   30),   # AAPL MSFT NVDA GOOGL AMZN META TSLA
    "STOCK":        (8,   100,   10_000,   40),   # everything else
}

MEGA_CAPS = {"AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"}

def _get_threshold(ticker: str) -> tuple:
    if ticker in INDEX_ETFS:
        return THRESHOLDS["INDEX_ETF"]
    if ticker in INDEX_OPTIONS:
        return THRESHOLDS["INDEX_OPTION"]
    if ticker in MEGA_CAPS:
        return THRESHOLDS["MEGA_CAP"]
    return THRESHOLDS["STOCK"]

def _asset_class(ticker: str) -> str:
    if ticker in INDEX_ETFS:       return "INDEX ETF"
    if ticker in INDEX_OPTIONS:    return "INDEX OPTION"
    if ticker in MEGA_CAPS:        return "MEGA CAP"
    return "STOCK"

# ── Polygon helpers ────────────────────────────────────────────────────────

async def _fetch(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    try:
        r = await client.get(url, params={**params, "apiKey": POLYGON_KEY}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"Polygon fetch error: {e}")
    return {}

async def _get_snapshot_options(client: httpx.AsyncClient, ticker: str) -> list:
    """Fetch option chain snapshot for a ticker."""
    today_str = date.today().isoformat()
    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {
        "expiration_date.gte": today_str,
        "limit": 250,
        "sort": "expiration_date",
    }
    data = await _fetch(client, url, params)
    return data.get("results", [])

async def _get_iv_avg(client: httpx.AsyncClient, ticker: str) -> float:
    """Rough IV average from ATM options."""
    results = await _get_snapshot_options(client, ticker)
    ivs = [
        r["implied_volatility"]
        for r in results
        if r.get("implied_volatility") and r["implied_volatility"] > 0
    ]
    return sum(ivs) / len(ivs) if ivs else 0.0

# ── Core scanner ──────────────────────────────────────────────────────────

async def scan_ticker(client: httpx.AsyncClient, ticker: str) -> list[dict]:
    """
    Scan one ticker for unusual options activity.
    Returns list of UOA hits (may be empty).
    """
    results = await _get_snapshot_options(client, ticker)
    if not results:
        return []

    min_ratio, min_contracts, min_premium, extreme_ratio = _get_threshold(ticker)
    hits = []

    for r in results:
        try:
            details   = r.get("details", {})
            day       = r.get("day", {})
            greeks    = r.get("greeks", {})
            last_q    = r.get("last_quote", {})

            volume    = int(day.get("volume") or 0)
            oi        = int(r.get("open_interest") or 1)
            iv        = float(r.get("implied_volatility") or 0)
            delta     = float(greeks.get("delta") or 0)
            strike    = float(details.get("strike_price") or 0)
            expiry    = details.get("expiration_date", "")
            ctype     = details.get("contract_type", "").upper()   # CALL / PUT
            mark      = float(last_q.get("midpoint") or r.get("last_trade", {}).get("price") or 0)

            if volume < min_contracts or strike == 0:
                continue

            ratio     = volume / max(oi, 1)
            premium   = mark * volume * 100   # total premium in $

            if ratio < min_ratio or premium < min_premium:
                continue

            # Calculate DTE
            try:
                exp_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
                dte    = (exp_dt - date.today()).days
            except Exception:
                dte = 0

            hits.append({
                "ticker":        ticker,
                "asset_class":   _asset_class(ticker),
                "strike":        strike,
                "expiry":        expiry,
                "contract_type": ctype,
                "volume":        volume,
                "open_interest": oi,
                "vol_oi_ratio":  round(ratio, 1),
                "iv":            round(iv * 100, 1),
                "mark":          round(mark, 2),
                "premium":       round(premium, 0),
                "delta":         round(delta, 3),
                "dte":           dte,
                "is_extreme":    ratio >= extreme_ratio,
                "bias":          "BULLISH" if ctype == "CALL" else "BEARISH",
            })
        except Exception as e:
            log.debug(f"UOA parse error {ticker}: {e}")
            continue

    # Sort by vol/OI ratio descending, cap at top 5 per ticker
    hits.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)
    return hits[:5]


async def run_uoa_scan() -> dict[str, list[dict]]:
    """
    Run full UOA scan across all tickers.
    Returns dict: { ticker: [hits] }
    """
    results = {}
    async with httpx.AsyncClient() as client:
        # Scan in batches of 10 to avoid rate limits
        all_tickers = ALL_EQUITY_TICKERS + list(INDEX_OPTIONS.keys())
        for i in range(0, len(all_tickers), 10):
            batch = all_tickers[i:i+10]
            tasks = [scan_ticker(client, t) for t in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for ticker, res in zip(batch, batch_results):
                if isinstance(res, list) and res:
                    results[ticker] = res
            await asyncio.sleep(0.5)   # Polygon rate limit buffer
    return results


async def run_uoa_scan_and_notify():
    """
    Full scan + Discord notification.
    Called by chakra flow monitor loop.
    """
    from chakra.arkadiscordnotifier import (
        notifyindexuoa,
        notifystockuoa,
    )

    log.info("UOA scan starting...")
    all_hits = await run_uoa_scan()

    total_alerts = 0
    for ticker, hits in all_hits.items():
        for hit in hits:
            try:
                if hit["asset_class"] in ("INDEX ETF", "INDEX OPTION"):
                    await notifyindexuoa(
                        ticker       = hit["ticker"],
                        strike       = hit["strike"],
                        expiry       = hit["expiry"],
                        contracttype = hit["contract_type"],
                        voloiratio   = hit["vol_oi_ratio"],
                        premium      = hit["premium"],
                        iv           = hit["iv"],
                        ivavg        = 0,
                        dte          = hit["dte"],
                        delta        = hit["delta"],
                    )
                else:
                    await notifystockuoa(
                        ticker       = hit["ticker"],
                        asset_class  = hit["asset_class"],
                        strike       = hit["strike"],
                        expiry       = hit["expiry"],
                        contracttype = hit["contract_type"],
                        voloiratio   = hit["vol_oi_ratio"],
                        premium      = hit["premium"],
                        iv           = hit["iv"],
                        dte          = hit["dte"],
                        delta        = hit["delta"],
                        is_extreme   = hit["is_extreme"],
                    )
                total_alerts += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                log.error(f"UOA notify error {ticker}: {e}")

    log.info(f"UOA scan complete — {total_alerts} alerts sent across {len(all_hits)} tickers")
    return all_hits


if __name__ == "__main__":
    import json
    results = asyncio.run(run_uoa_scan())
    for t, hits in results.items():
        print(f"\n{t}:")
        for h in hits:
            print(f"  {h['contract_type']} ${h['strike']} {h['expiry']} "
                  f"Vol/OI={h['vol_oi_ratio']}x  ${h['premium']:,.0f}  IV={h['iv']}%  "
                  f"{'🔥 EXTREME' if h['is_extreme'] else ''}")
