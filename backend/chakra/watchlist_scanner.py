"""
CHAKRA — Watchlist Engine
backend/chakra/watchlist_scanner.py

Dual-schedule S&P 500 swing candidate scanner.
  Post-market (5:00 PM ET)  — primary scan using settled EOD data
  Pre-market  (7:15 AM ET)  — refresh scan catching overnight gaps/news

Three-phase pipeline:
  Phase 1 → Universe Filter  : 500 tickers → ~80-120 survivors
  Phase 2 → Scoring Engine   : 6 dimensions, 0-100 score
  Phase 3 → Rank & Output    : Top 15 → watchlist_latest.json + Discord

Usage:
  python3 backend/chakra/watchlist_scanner.py --mode postmarket
  python3 backend/chakra/watchlist_scanner.py --mode premarket
  python3 backend/chakra/watchlist_scanner.py --test   (dry-run on 20 tickers)
"""

import os
import sys
import json
import time
import argparse
import logging
import requests

from datetime import datetime, timedelta
from typing import Optional

# ── Path setup so we can import from project root ──────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

try:
    from backend.chakra.modules.rsi_divergence import detect_rsi_divergence, score_divergence
except ImportError:
    # Fallback if modules dir not yet created
    from rsi_divergence import detect_rsi_divergence, score_divergence

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [WATCHLIST] %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('watchlist')

# ── Config ─────────────────────────────────────────────────────────────
POLYGON_API_KEY  = os.getenv('POLYGON_API_KEY', '')
DISCORD_WEBHOOK  = os.getenv('DISCORD_TRADES_WEBHOOK') or os.getenv('DISCORD_WEBHOOK_URL', '')
WATCHLIST_DIR    = 'logs/chakra'
WATCHLIST_LATEST = 'logs/chakra/watchlist_latest.json'
MIN_SCORE        = 55      # candidates below this are dropped
TOP_N            = 15      # max candidates to save
BATCH_SIZE       = 50      # tickers per Polygon snapshot batch
REQUEST_DELAY    = 0.15    # seconds between API calls (rate limit safety)

FILTERS = {
    'min_price':      10.0,
    'max_price':      500.0,
    'min_avg_volume': 1_000_000,
    'min_adr_pct':    1.5,
    'max_adr_pct':    8.0,
}

# ── RSI thresholds ──────────────────────────────────────────────────────
RSI_EXTREME_OVERSOLD_STRONG  = 20
RSI_EXTREME_OVERSOLD         = 25
RSI_EXTREME_OVERBOUGHT_STRONG = 80
RSI_EXTREME_OVERBOUGHT       = 75
RSI_SWING_ZONE_HIGH          = 65
RSI_SWING_ZONE_LOW           = 45
RSI_BOUNCE_LOW               = 35


# ═══════════════════════════════════════════════════════════════════════
# PHASE 1 — UNIVERSE FILTER
# ═══════════════════════════════════════════════════════════════════════

def get_sp500_tickers() -> list:
    """Pull S&P 500 tickers from Wikipedia. Free, no API required."""
    try:
        import pandas as pd
        import io
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        resp = requests.get(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
            headers=headers, timeout=15
        )
        table = pd.read_html(io.StringIO(resp.text))[0]
        tickers = table['Symbol'].str.replace('.', '-').tolist()
        log.info(f"Loaded {len(tickers)} S&P500 tickers from Wikipedia")
        return tickers
    except Exception as e:
        log.warning(f"Wikipedia fetch failed: {e} — using hardcoded fallback list")
        return _sp500_fallback()


def _sp500_fallback() -> list:
    """Hardcoded top-100 S&P500 tickers as fallback if Wikipedia is unavailable."""
    return [
        'AAPL','MSFT','NVDA','AMZN','META','GOOGL','GOOG','TSLA','BRK-B','JPM',
        'LLY','UNH','AVGO','V','XOM','MA','JNJ','PG','HD','COST',
        'ABBV','MRK','CVX','KO','PEP','BAC','NFLX','ADBE','WMT','CRM',
        'TMO','MCD','CSCO','ACN','LIN','ABT','ORCL','NKE','DHR','AMD',
        'INTC','NEE','PM','TXN','AMGN','RTX','BMY','QCOM','MS','SPGI',
        'HON','GE','LOW','CAT','SBUX','IBM','GS','ISRG','INTU','GILD',
        'BLK','ELV','SYK','MDT','C','VRTX','ADI','AXP','REGN','DE',
        'PLD','MDLZ','AMT','CI','CB','SCHW','ZTS','TJX','NOW','MO',
        'SO','BSX','DUK','MMC','USB','ICE','EQIX','MU','CME','HCA',
        'SLB','WM','FI','NOC','CL','GD','EOG','AON','MCO','ITW',
    ]


def fetch_batch_snapshot(tickers: list) -> dict:
    """
    Fetch Polygon snapshot for a batch of tickers.
    Returns dict: { ticker: {close, volume, prev_close, ...} }
    """
    if not POLYGON_API_KEY:
        log.error("POLYGON_API_KEY not set — cannot fetch price data")
        return {}

    results = {}
    # Polygon /v2/snapshot/locale/us/markets/stocks/tickers supports comma-separated
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        ticker_str = ','.join(batch)
        url = (
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
            f"?tickers={ticker_str}&apiKey={POLYGON_API_KEY}"
        )
        try:
            r = requests.get(url, timeout=15)
            if r.status_code != 200:
                log.warning(f"Polygon snapshot HTTP {r.status_code} for batch {i//BATCH_SIZE+1}")
                continue
            data = r.json()
            for item in data.get('tickers', []):
                sym = item.get('ticker', '')
                day = item.get('day', {})
                prev = item.get('prevDay', {})
                results[sym] = {
                    'close':      day.get('c', 0),
                    'open':       day.get('o', 0),
                    'high':       day.get('h', 0),
                    'low':        day.get('l', 0),
                    'volume':     day.get('v', 0),
                    'vwap':       day.get('vw', 0),
                    'prev_close': prev.get('c', 0),
                    'change_pct': item.get('todaysChangePerc', 0),
                }
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            log.warning(f"Batch snapshot error: {e}")

    log.info(f"Snapshot fetched: {len(results)} tickers")
    return results


def fetch_agg_bars(ticker: str, days: int = 30) -> list:
    """
    Fetch daily OHLCV bars for a ticker (last N days).
    Returns list of bar dicts sorted oldest → newest.
    """
    if not POLYGON_API_KEY:
        return []
    end   = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=days + 10)).strftime('%Y-%m-%d')
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        f"?adjusted=true&sort=asc&limit={days+10}&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            results = r.json().get('results', [])
            return results[-days:]   # keep only last N bars
    except Exception as e:
        log.debug(f"Agg bars error for {ticker}: {e}")
    return []


def compute_rsi(closes: list, period: int = 14) -> list:
    """Compute RSI for a list of closing prices. Returns list of RSI values."""
    if len(closes) < period + 1:
        return [50.0] * len(closes)

    rsi_values = [50.0] * period
    gains, losses = [], []

    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain  = max(delta, 0)
        loss  = max(-delta, 0)
        avg_gain = (avg_gain * (period - 1) + gain)  / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(round(100 - (100 / (1 + rs)), 2))

    return rsi_values


def compute_ema(closes: list, period: int) -> float:
    """Compute EMA for a list of closes. Returns last EMA value."""
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def compute_adr(bars: list) -> float:
    """Average Daily Range % over last 14 days."""
    if len(bars) < 2:
        return 0
    recent = bars[-14:]
    ranges = [(b['h'] - b['l']) / b['l'] * 100 for b in recent if b.get('l', 0) > 0]
    return round(sum(ranges) / len(ranges), 2) if ranges else 0


def apply_hard_filters(snapshot: dict) -> list:
    """Apply price/volume/ADR filters. Returns list of tickers that pass."""
    passed = []
    for ticker, d in snapshot.items():
        close  = d.get('close', 0)
        volume = d.get('volume', 0)

        if close < FILTERS['min_price'] or close > FILTERS['max_price']:
            continue
        if volume < FILTERS['min_avg_volume']:
            continue
        passed.append(ticker)

    log.info(f"Hard filter: {len(snapshot)} → {len(passed)} survivors")
    return passed


# ═══════════════════════════════════════════════════════════════════════
# PHASE 2 — SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════

def enrich_ticker(ticker: str, snap: dict) -> dict:
    """
    Fetch historical bars and compute all indicators needed for scoring.
    Returns enriched data dict.
    """
    bars = fetch_agg_bars(ticker, days=30)
    time.sleep(REQUEST_DELAY)

    if len(bars) < 15:
        # Not enough history — use snapshot data only with defaults
        return {
            'ticker':         ticker,
            'close':          snap.get('close', 0),
            'volume':         snap.get('volume', 0),
            'rsi_daily':      50.0,
            'rsi_history':    [50.0] * 15,
            'price_history':  [snap.get('close', 0)] * 15,
            'ema_stack':      'PARTIAL',
            'volume_ratio':   1.0,
            'adr_pct':        2.0,
            'price_structure':'NONE',
            'avg_volume':     snap.get('volume', 0),
        }

    closes  = [b['c'] for b in bars]
    highs   = [b['h'] for b in bars]
    volumes = [b['v'] for b in bars]

    rsi_vals   = compute_rsi(closes)
    rsi_daily  = rsi_vals[-1] if rsi_vals else 50.0
    avg_volume = sum(volumes) / len(volumes) if volumes else 1
    vol_ratio  = volumes[-1] / avg_volume if avg_volume > 0 else 1.0
    adr        = compute_adr(bars)

    # EMA stack
    ema20  = compute_ema(closes, 20)
    ema50  = compute_ema(closes, 50) if len(closes) >= 50 else ema20
    ema200 = compute_ema(closes, 200) if len(closes) >= 200 else ema50
    last   = closes[-1]

    if last > ema20 > ema50 > ema200:
        ema_stack = 'FULL_BULL'
    elif last < ema20 < ema50 < ema200:
        ema_stack = 'BEARISH'
    else:
        ema_stack = 'PARTIAL'

    # Price structure
    high_20d = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    price_structure = 'NONE'
    if last >= high_20d * 0.995:
        price_structure = 'BREAKOUT'
    elif last >= ema20 * 0.99 and last <= ema20 * 1.01:
        price_structure = 'BASE'
    elif last < ema20 and last >= ema50 * 0.97:
        price_structure = 'PULLBACK'

    # ADR filter check
    if adr < FILTERS['min_adr_pct'] or adr > FILTERS['max_adr_pct']:
        return None   # fails ADR gate

    return {
        'ticker':          ticker,
        'close':           last,
        'volume':          volumes[-1],
        'avg_volume':      round(avg_volume),
        'rsi_daily':       rsi_daily,
        'rsi_history':     rsi_vals,
        'price_history':   closes,
        'ema_stack':       ema_stack,
        'ema20':           ema20,
        'ema50':           ema50,
        'ema200':          ema200,
        'volume_ratio':    round(vol_ratio, 2),
        'adr_pct':         adr,
        'price_structure': price_structure,
        'high_20d':        high_20d,
    }


def score_swing_candidate(ticker: str, data: dict) -> dict:
    """
    Score a candidate across 6 dimensions. Returns result dict with score,
    direction, reasons, and metadata.
    """
    score   = 0
    reasons = []
    result  = {
        'ticker':           ticker,
        'score':            0,
        'direction':        'NEUTRAL',
        'direction_override': None,
        'trade_type_hint':  'TREND',
        'divergence':       None,
        'rsi':              data.get('rsi_daily', 50),
        'ema_stack':        data.get('ema_stack', 'PARTIAL'),
        'structure':        data.get('price_structure', 'NONE'),
        'vol_ratio':        data.get('volume_ratio', 1.0),
        'close':            data.get('close', 0),
        'reasons':          [],
    }

    rsi            = data.get('rsi_daily', 50)
    extreme_scored = False

    # ── DIM 1: Trend Alignment / EMA Stack (0-25 pts) ──────────────────
    ema_stack = data.get('ema_stack', 'PARTIAL')
    if ema_stack == 'FULL_BULL':
        score += 25
        reasons.append('Full EMA bull stack (price > EMA20 > EMA50 > EMA200)')
    elif ema_stack == 'PARTIAL':
        score += 12
        reasons.append('Partial EMA alignment')
    # BEARISH = 0 pts

    # ── DIM 3: RSI Extreme Levels (0-25 pts) — checked BEFORE swing zone
    # so extremes override standard scoring, no double-counting ─────────
    if rsi < RSI_EXTREME_OVERSOLD:
        extreme_scored = True
        if rsi < RSI_EXTREME_OVERSOLD_STRONG:
            score += 25
            reasons.append(f'EXTREME oversold RSI {rsi:.0f} — strong CALL setup')
        else:
            score += 18
            reasons.append(f'Oversold RSI {rsi:.0f} — CALL setup')
        result['direction_override'] = 'CALL'
        result['trade_type_hint']    = 'MEAN_REVERSION'

    elif rsi > RSI_EXTREME_OVERBOUGHT:
        extreme_scored = True
        if rsi > RSI_EXTREME_OVERBOUGHT_STRONG:
            score += 25
            reasons.append(f'EXTREME overbought RSI {rsi:.0f} — strong PUT setup')
        else:
            score += 18
            reasons.append(f'Overbought RSI {rsi:.0f} — PUT setup')
        result['direction_override'] = 'PUT'
        result['trade_type_hint']    = 'MEAN_REVERSION'

    # ── DIM 2: RSI Swing Zone (0-20 pts) — only if NOT extreme ─────────
    if not extreme_scored:
        if RSI_SWING_ZONE_LOW <= rsi <= RSI_SWING_ZONE_HIGH:
            score += 20
            reasons.append(f'RSI in swing zone ({rsi:.0f})')
        elif RSI_BOUNCE_LOW <= rsi < RSI_SWING_ZONE_LOW:
            score += 10
            reasons.append(f'RSI bounce setup ({rsi:.0f})')

    # ── DIM 4: RSI Divergence (0-20 pts) ───────────────────────────────
    price_hist = data.get('price_history', [])
    rsi_hist   = data.get('rsi_history', [])

    if len(price_hist) >= 14 and len(rsi_hist) >= 14:
        div = detect_rsi_divergence(price_hist, rsi_hist, lookback=14)
        pts, dir_override = score_divergence(div)

        if pts > 0:
            score += pts
            reasons.append(div['description'])
            result['divergence'] = div['type']
            # Divergence direction overrides extreme if stronger
            if dir_override and not result['direction_override']:
                result['direction_override'] = dir_override

    # ── DIM 5: Volume Surge (0-20 pts) ─────────────────────────────────
    vol_ratio = data.get('volume_ratio', 1.0)
    if vol_ratio >= 2.0:
        score += 20
        reasons.append(f'Volume surge {vol_ratio:.1f}x avg — institutional activity')
    elif vol_ratio >= 1.5:
        score += 10
        reasons.append(f'Above avg volume {vol_ratio:.1f}x')

    # ── DIM 6: Price Structure (0-20 pts) ──────────────────────────────
    structure = data.get('price_structure', 'NONE')
    if structure == 'BREAKOUT':
        score += 20
        reasons.append('Breakout above 20-day high')
    elif structure == 'BASE':
        score += 15
        reasons.append('Tight base consolidation')
    elif structure == 'PULLBACK':
        score += 10
        reasons.append('Pullback to support')

    # ── Determine final direction ───────────────────────────────────────
    if result['direction_override']:
        result['direction'] = result['direction_override']
    elif ema_stack == 'FULL_BULL' or structure == 'BREAKOUT':
        result['direction'] = 'CALL'
    elif ema_stack == 'BEARISH':
        result['direction'] = 'PUT'
    else:
        result['direction'] = 'NEUTRAL'

    result['score']   = min(score, 100)
    result['reasons'] = reasons
    return result


# ═══════════════════════════════════════════════════════════════════════
# PHASE 3 — RANK & OUTPUT
# ═══════════════════════════════════════════════════════════════════════

def save_watchlist(top_n: list, mode: str):
    """Save watchlist to latest + timestamped archive."""
    os.makedirs(WATCHLIST_DIR, exist_ok=True)

    output = {
        'date':      datetime.now().strftime('%Y-%m-%d'),
        'scan_time': datetime.now().isoformat(),
        'scan_mode': mode,
        'count':     len(top_n),
        'watchlist': top_n,
    }

    # Always overwrite latest — ARJUN reads this at 8 AM
    with open(WATCHLIST_LATEST, 'w') as f:
        json.dump(output, f, indent=2)

    # Also archive with timestamp
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    archive_path = os.path.join(WATCHLIST_DIR, f'watchlist_{ts}.json')
    with open(archive_path, 'w') as f:
        json.dump(output, f, indent=2)

    log.info(f"Saved {len(top_n)} candidates → {WATCHLIST_LATEST}")
    log.info(f"Archived → {archive_path}")


def post_watchlist_to_discord(top_n: list, mode: str, stats: dict):
    """Post watchlist scan summary to Discord as rich embed."""
    if not DISCORD_WEBHOOK:
        log.warning("DISCORD_TRADES_WEBHOOK not set — skipping Discord post")
        return

    calls = [t for t in top_n if t.get('direction_override') == 'CALL'][:4]
    puts  = [t for t in top_n if t.get('direction_override') == 'PUT'][:4]
    other = [t for t in top_n if not t.get('direction_override')][:3]

    def fmt_line(t):
        badge = ''
        if t.get('divergence') in ('BULLISH', 'HIDDEN_BULL'):
            badge = ' 📈div'
        elif t.get('divergence') in ('BEARISH', 'HIDDEN_BEAR'):
            badge = ' 📉div'
        reason = t['reasons'][0] if t.get('reasons') else ''
        return f"**{t['ticker']}** {t['score']}pts{badge} — {reason}"

    call_text  = '\n'.join(fmt_line(t) for t in calls)  or '*None today*'
    put_text   = '\n'.join(fmt_line(t) for t in puts)   or '*None today*'
    other_text = '\n'.join(fmt_line(t) for t in other)  or '*None today*'

    label   = '🌙 Post-Market' if mode == 'postmarket' else '🌅 Pre-Market'
    scanned = stats.get('scanned', 0)
    filtered= stats.get('filtered', 0)
    scored  = stats.get('scored', 0)

    embed = {
        'title':       f'{label} Watchlist Scan Complete — {datetime.now().strftime("%B %d, %Y")}',
        'description': (
            f"🔍 Scanned: **{scanned}** stocks  |  "
            f"Filtered: **{filtered}**  |  "
            f"Scored ≥55: **{scored}**  |  "
            f"Selected: **{len(top_n)}**"
        ),
        'color':  0x00C8AA,
        'fields': [
            {
                'name':   '🟢 Top CALLs (Oversold / Bullish Divergence)',
                'value':  call_text,
                'inline': False,
            },
            {
                'name':   '🔴 Top PUTs (Overbought / Bearish Divergence)',
                'value':  put_text,
                'inline': False,
            },
            {
                'name':   '⚪ Trend Candidates (No Extreme)',
                'value':  other_text,
                'inline': False,
            },
            {
                'name':   '⏰ Next Step',
                'value':  'ARJUN runs full 5-agent debate at **8:00 AM ET**',
                'inline': False,
            },
        ],
        'footer': {
            'text': f'CHAKRA Neural Trading OS  •  {mode.capitalize()} scan  •  {datetime.now().strftime("%H:%M ET")}'
        },
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    }

    try:
        r = requests.post(DISCORD_WEBHOOK, json={'embeds': [embed]}, timeout=10)
        if r.status_code in (200, 204):
            log.info("Discord watchlist embed posted ✅")
        else:
            log.warning(f"Discord post failed: HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"Discord post error: {e}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════

def run_watchlist_scan(mode: str = 'postmarket', test_mode: bool = False) -> list:
    """
    Execute the full 3-phase watchlist scan pipeline.

    Args:
        mode      : 'postmarket' | 'premarket'
        test_mode : if True, only scan first 20 tickers (faster testing)

    Returns:
        list of top candidate dicts
    """
    log.info(f"{'='*60}")
    log.info(f"CHAKRA Watchlist Engine starting — mode={mode}")
    log.info(f"{'='*60}")

    start_time = time.time()

    # ── PHASE 1: Universe Filter ────────────────────────────────────────
    log.info("Phase 1: Fetching S&P 500 universe...")
    all_tickers = get_sp500_tickers()

    if test_mode:
        all_tickers = all_tickers[:20]
        log.info(f"TEST MODE: limiting to {len(all_tickers)} tickers")

    log.info(f"Phase 1: Fetching snapshot for {len(all_tickers)} tickers...")
    snapshot = fetch_batch_snapshot(all_tickers)

    filtered = apply_hard_filters(snapshot)
    stats = {
        'scanned':  len(all_tickers),
        'filtered': len(filtered),
        'scored':   0,
    }

    if not filtered:
        log.error("No tickers passed hard filter — check Polygon API key and data")
        return []

    # ── PHASE 2: Scoring Engine ─────────────────────────────────────────
    log.info(f"Phase 2: Enriching and scoring {len(filtered)} candidates...")
    candidates = []

    for i, ticker in enumerate(filtered):
        try:
            if i % 10 == 0:
                log.info(f"  Scoring {i+1}/{len(filtered)}...")

            enriched = enrich_ticker(ticker, snapshot.get(ticker, {}))
            if enriched is None:
                continue   # failed ADR filter

            result = score_swing_candidate(ticker, enriched)

            if result['score'] >= MIN_SCORE:
                candidates.append(result)

        except Exception as e:
            log.debug(f"  Skipping {ticker}: {e}")
            continue

    stats['scored'] = len(candidates)
    log.info(f"Phase 2 complete: {len(candidates)} candidates scored ≥{MIN_SCORE}")

    # ── PHASE 3: Rank & Output ──────────────────────────────────────────
    log.info("Phase 3: Ranking and saving output...")
    top_n = sorted(candidates, key=lambda x: x['score'], reverse=True)[:TOP_N]

    if not top_n:
        log.warning("No candidates met minimum score threshold today")
        # Save empty watchlist so ARJUN doesn't use stale data
        save_watchlist([], mode)
        return []

    save_watchlist(top_n, mode)
    post_watchlist_to_discord(top_n, mode, stats)

    elapsed = round(time.time() - start_time, 1)
    log.info(f"{'='*60}")
    log.info(f"Scan complete in {elapsed}s — {len(top_n)} candidates saved")
    log.info("Top 5 candidates:")
    for c in top_n[:5]:
        div_tag = f" [{c['divergence']}]" if c.get('divergence') else ''
        log.info(
            f"  {c['ticker']:6} {c['score']:3}pts  {c.get('direction_override') or c['direction']:7}"
            f"  RSI={c['rsi']:.0f}  {c['structure']}{div_tag}"
        )
    log.info(f"{'='*60}")

    return top_n


# ═══════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CHAKRA Watchlist Engine')
    parser.add_argument(
        '--mode',
        choices=['postmarket', 'premarket'],
        default='postmarket',
        help='Scan mode (default: postmarket)'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Test mode — only scan first 20 tickers'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='Show latest watchlist without running scan'
    )
    args = parser.parse_args()

    if args.status:
        if os.path.exists(WATCHLIST_LATEST):
            with open(WATCHLIST_LATEST) as f:
                wl = json.load(f)
            print(f"\nLatest watchlist: {wl.get('date')} ({wl.get('scan_mode')})")
            print(f"  {wl.get('count', 0)} candidates\n")
            for c in wl.get('watchlist', []):
                div = f" [{c['divergence']}]" if c.get('divergence') else ''
                print(
                    f"  {c['ticker']:6} {c['score']:3}pts  "
                    f"{c.get('direction_override') or c.get('direction','?'):7}  "
                    f"RSI={c.get('rsi', 0):.0f}{div}"
                )
        else:
            print("No watchlist found. Run: python3 watchlist_scanner.py --mode postmarket")
        sys.exit(0)

    results = run_watchlist_scan(mode=args.mode, test_mode=args.test)
    sys.exit(0 if results is not None else 1)
