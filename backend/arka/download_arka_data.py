"""
ARKA — Minute Data Downloader
Downloads 2 years of 1-minute bars for SPY and QQQ in chunks
to bypass Polygon's 50,000 bar per request limit.

Run from ~/trading-ai:
    python3 backend/arka/download_arka_data.py
"""

import asyncio
import httpx
import pandas as pd
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import os
from dotenv import load_dotenv

load_dotenv(override=True)

POLYGON_KEY = os.getenv("POLYGON_API_KEY")
TICKERS     = ["SPY", "QQQ"]
CHUNK_DAYS  = 30          # 30 days per request — well under 50k bar limit
YEARS_BACK  = 2
OUTPUT_DIR  = "data"

# ── helpers ───────────────────────────────────────────────────────────────────

async def fetch_chunk(client: httpx.AsyncClient, ticker: str, from_date: str, to_date: str) -> list:
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{from_date}/{to_date}"
    params = {
        "adjusted": "true",
        "sort":     "asc",
        "limit":    50000,
        "apiKey":   POLYGON_KEY,
    }
    all_results = []
    while url:
        try:
            r = await client.get(url, params=params, timeout=30)
            data = r.json()
            if data.get("status") not in ("OK", "DELAYED"):
                print(f"      ⚠️  API warning: {data.get('status')} — {data.get('message','')}")
                break
            results = data.get("results", [])
            all_results.extend(results)
            next_url = data.get("next_url")
            if next_url:
                url    = next_url + f"&apiKey={POLYGON_KEY}"
                params = {}
            else:
                break
        except Exception as e:
            print(f"      ❌ Request error: {e}")
            break
    return all_results


def results_to_df(results: list, ticker: str) -> pd.DataFrame:
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    df = df.rename(columns={
        "t": "timestamp", "o": "open", "h": "high",
        "l": "low",       "c": "close","v": "volume",
        "vw": "vwap",     "n": "trades",
    })
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["timestamp"] = df["timestamp"].dt.tz_convert("America/New_York").dt.tz_localize(None)
    df["ticker"]    = ticker

    # keep only regular market hours  09:30 – 16:00
    t = df["timestamp"]
    df = df[
        (t.dt.hour > 9)  | ((t.dt.hour == 9)  & (t.dt.minute >= 30))
    ]
    df = df[
        (t.dt.hour < 16) | ((t.dt.hour == 16) & (t.dt.minute == 0))
    ]
    return df.reset_index(drop=True)


# ── main downloader ───────────────────────────────────────────────────────────

async def download_ticker(ticker: str) -> pd.DataFrame:
    end_date   = datetime.today().date()
    start_date = end_date - relativedelta(years=YEARS_BACK)

    # build list of (chunk_start, chunk_end) date pairs
    chunks = []
    cursor = start_date
    while cursor < end_date:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), end_date)
        chunks.append((cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cursor = chunk_end + timedelta(days=1)

    print(f"\n{'='*55}")
    print(f"  {ticker}  —  {len(chunks)} chunks  ({start_date} → {end_date})")
    print(f"{'='*55}")

    all_dfs = []
    async with httpx.AsyncClient() as client:
        for i, (from_d, to_d) in enumerate(chunks, 1):
            print(f"  [{i:>2}/{len(chunks)}] {from_d} → {to_d}", end="  ", flush=True)
            results = await fetch_chunk(client, ticker, from_d, to_d)
            df_chunk = results_to_df(results, ticker)
            if not df_chunk.empty:
                all_dfs.append(df_chunk)
                print(f"✅  {len(df_chunk):>6,} bars")
            else:
                print("⚠️   0 bars (weekend/holiday block?)")
            # polite rate-limit pause — Polygon free tier = 5 req/min
            # Starter+ tier is much higher but let's be safe
            await asyncio.sleep(0.25)

    if not all_dfs:
        print(f"  ❌ No data collected for {ticker}")
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp", "ticker"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("\n" + "="*55)
    print("  ARKA — MINUTE DATA DOWNLOADER")
    print("  2 years · SPY + QQQ · market hours only")
    print("="*55)

    all_frames = []

    for ticker in TICKERS:
        df = await download_ticker(ticker)
        if df.empty:
            print(f"\n❌ Skipping {ticker} — no data")
            continue

        # save per-ticker file too for easy inspection
        per_ticker_path = os.path.join(OUTPUT_DIR, f"arka_minute_{ticker.lower()}.csv")
        df.to_csv(per_ticker_path, index=False)
        print(f"\n  💾 Saved {ticker} → {per_ticker_path}")
        print(f"     Total bars : {len(df):,}")
        print(f"     Date range : {df['timestamp'].min()}  →  {df['timestamp'].max()}")
        print(f"     Columns    : {list(df.columns)}")
        all_frames.append(df)

    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        combined_path = os.path.join(OUTPUT_DIR, "arka_minute_combined.csv")
        combined.to_csv(combined_path, index=False)

        print("\n" + "="*55)
        print("  ✅  DOWNLOAD COMPLETE")
        print("="*55)
        print(f"  Combined file : {combined_path}")
        print(f"  Total bars    : {len(combined):,}")
        for t in TICKERS:
            n = len(combined[combined["ticker"] == t])
            print(f"    {t:>4}  →  {n:>8,} bars")
        print("\n  ARKA data is ready for feature engineering! 🚀")
    else:
        print("\n❌ No data downloaded — check your Polygon API key and plan.")


if __name__ == "__main__":
    # Install dateutil if missing:  pip install python-dateutil --break-system-packages
    try:
        from dateutil.relativedelta import relativedelta
    except ImportError:
        print("Installing python-dateutil...")
        os.system("pip install python-dateutil --break-system-packages -q")
        from dateutil.relativedelta import relativedelta

    asyncio.run(main())
