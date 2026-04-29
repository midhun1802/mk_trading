"""
CHAKRA — COT Smart Money Index
backend/chakra/modules/cot_smart_money.py

Commitment of Traders (CFTC) data released every Friday at 3:30 PM ET.
"Non-Commercial" (speculators) net position = smart money directional bias.
Extreme positions → contrarian reversal signal.
Trend + positioning alignment → strong continuation signal.

Data source: CFTC public download
  https://www.cftc.gov/dea/newcot/FinFutYY.txt
  ES (S&P): 'E-MINI S&P 500 STOCK INDEX - CHICAGO MERCANTILE EXCHANGE'
  NQ (NDX): 'E-MINI NASDAQ-100 STOCK INDEX - CHICAGO MERCANTILE EXCHANGE'

Signals:
  Net long > +1σ above 52-week mean → Speculators crowded LONG
    If market is trending down → CONTRARIAN BEARISH (they'll need to cover)
    If market is trending up   → TREND CONTINUATION BULLISH
  Net short > -1σ → Speculators crowded SHORT
    If market is trending up   → SHORT SQUEEZE potential → BULLISH
    If market is trending down → TREND CONTINUATION BEARISH

Integration:
  - ARJUN Bull/Bear agents → ±10 pts on extreme positioning
  - Weekly Retrain         → COT net position as XGBoost feature
  - Daily Briefing         → COT alignment/divergence flag
"""

import csv
import io
import json
import logging
import numpy as np
import httpx
import os
from datetime import date, timedelta, datetime
from pathlib import Path
from dotenv import load_dotenv

# COT_ANNUAL_WIRED — 52-week z-score from FinFutYY.xls (Day 15)
import zipfile as _zf, io as _io

def _fetch_annual_cot() -> dict:
    """
    Downloads FinFutYY.xls from CFTC and returns 52-week
    net position history for ES/NQ/YM with proper z-scores.
    """
    import urllib.request, numpy as _np, pandas as _pd

    url  = "https://www.cftc.gov/files/dea/history/fut_fin_xls_2026.zip"
    req  = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    with _zf.ZipFile(_io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            df = _pd.read_excel(f, engine="xlrd")

    # Filter S&P 500 Consolidated (ES)
    mask_es = df["Market_and_Exchange_Names"].str.contains(
        "S&P 500 CONSOL", case=False, na=False
    )
    es = df[mask_es].copy()
    es = es.sort_values("As_of_Date_In_Form_YYMMDD").tail(52)

    if len(es) < 4:
        return {}

    # Smart money net = Asset Managers
    am_net = (es["Asset_Mgr_Positions_Long_All"].astype(float) -
              es["Asset_Mgr_Positions_Short_All"].astype(float))

    # Hedge fund net = Leveraged Money
    lm_net = (es["Lev_Money_Positions_Long_All"].astype(float) -
              es["Lev_Money_Positions_Short_All"].astype(float))

    def zscore(series):
        mu, sigma = series.mean(), series.std()
        return round(float((series.iloc[-1] - mu) / sigma), 2) if sigma > 0 else 0.0

    am_z = zscore(am_net)
    lm_z = zscore(lm_net)

    # Combined z-score weighted 60% asset mgr / 40% lev money
    combined_z = round(am_z * 0.6 + lm_z * 0.4, 2)

    signal = (
        "STRONG_BULL" if combined_z >  1.5 else
        "BULL"        if combined_z >  0.5 else
        "STRONG_BEAR" if combined_z < -1.5 else
        "BEAR"        if combined_z < -0.5 else
        "NEUTRAL"
    )

    return {
        "es_am_net":    int(am_net.iloc[-1]),
        "es_lm_net":    int(lm_net.iloc[-1]),
        "am_zscore":    am_z,
        "lm_zscore":    lm_z,
        "combined_z":   combined_z,
        "annual_zscore": _annual.get("combined_z", None),
        "annual_signal": _annual.get("signal", "N/A"),
        "am_zscore": _annual.get("am_zscore", None),
        "lm_zscore": _annual.get("lm_zscore", None),
        "signal":       signal,
        "weeks":        len(es),
        "source":       "FinFutYY.xls (annual)",
    }


BASE = Path(__file__).resolve().parents[3]
load_dotenv(BASE / ".env", override=True)

log       = logging.getLogger("chakra.cot")
COT_CACHE = BASE / "logs" / "chakra" / "cot_latest.json"

# CFTC download URL — financial futures weekly report
CFTC_URL  = "https://www.cftc.gov/dea/newcot/FinFutYY.txt"
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")

# Market names in CFTC file (positional CSV — no header row)
# Col 0 = Market Name, Col 1 = Report Date (YYMMDD)
# Col 7 = NonComm Long (All), Col 8 = NonComm Short (All)
CFTC_MARKETS = {
    "ES": "E-MINI S&P 500",
    "NQ": "NASDAQ MINI",
    "YM": "DJIA x $5",
}
# Also try the historical/legacy URL if primary fails
CFTC_URL_HIST = "https://www.cftc.gov/dea/newcot/f_fin.htm"


# ══════════════════════════════════════════════════════════════════════
# COT PARSING
# ══════════════════════════════════════════════════════════════════════


def _try_fetch_annual_cot() -> list:
    """
    Try to fetch annual COT data from CFTC XLS ZIP.
    Returns list of raw rows, or [] if unavailable.
    """
    from datetime import datetime
    import zipfile, io

    year = datetime.now().year
    url = f"https://www.cftc.gov/files/dea/history/fut_fin_xls_{year}.zip"
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            return []
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            # Look for CSV or TXT files first
            txt_files = [n for n in zf.namelist() if n.lower().endswith(('.txt', '.csv'))]
            if txt_files:
                text = zf.read(txt_files[0]).decode("latin-1", errors="replace")
                return list(csv.reader(text.splitlines()))
            # Try XLS with openpyxl
            xls_files = [n for n in zf.namelist() if n.lower().endswith(('.xls', '.xlsx'))]
            if xls_files:
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(io.BytesIO(zf.read(xls_files[0])), read_only=True, data_only=True)
                    ws = wb.active
                    rows = []
                    for row in ws.iter_rows(values_only=True):
                        rows.append([str(c) if c is not None else "" for c in row])
                    log.info(f"COT: Loaded {len(rows)} rows from XLS ZIP")
                    return rows
                except ImportError:
                    log.warning("COT: openpyxl not installed — run: pip install openpyxl")
                    return []
    except Exception as e:
        log.warning(f"COT: Annual XLS fetch failed: {e}")
    return []

def fetch_and_parse_cot() -> dict:
    """
    Download and parse CFTC COT report.
    Uses annual combined file for 52-week history needed for z-score.
    Falls back to weekly file if annual unavailable.
    """
    import zipfile, io

    YEAR = datetime.now().year
    # Try annual ZIP first (52 weeks), fall back to weekly (1 week)
    # Try annual XLS ZIP first for 52-week history
    annual_rows = _try_fetch_annual_cot()
    if annual_rows and len(annual_rows) > 10:
        log.info(f'COT: Using annual data ({len(annual_rows)} rows)')
        records_by_market = {}
        for code in CFTC_MARKETS:
            records_by_market[code] = []
        for row in annual_rows:
            if len(row) < 10:
                continue
            market_name = str(row[0]).strip()
            for code, match_str in CFTC_MARKETS.items():
                if match_str.upper() in market_name.upper():
                    try:
                        nc_long  = int(str(row[7]).replace(',','').replace(' ','').split('.')[0] or 0)
                        nc_short = int(str(row[8]).replace(',','').replace(' ','').split('.')[0] or 0)
                        records_by_market[code].append({
                            'date': str(row[2]).strip() if len(row) > 2 else 'unknown',
                            'nc_long': nc_long, 'nc_short': nc_short,
                            'net': nc_long - nc_short,
                        })
                    except (ValueError, IndexError):
                        pass
        total = sum(len(v) for v in records_by_market.values())
        markets_found = [k for k, v in records_by_market.items() if v]
        log.info(f'COT: Parsed {total} annual records for: {markets_found}')
        return records_by_market

    urls_to_try = [
        (f"https://www.cftc.gov/files/dea/history/fin_fut_comb_{YEAR}.zip", True),
        ("https://www.cftc.gov/dea/newcot/FinFutWk.txt", False),
    ]

    text = None
    for url, is_zip in urls_to_try:
        try:
            r = httpx.get(url, timeout=30, follow_redirects=True)
            if r.status_code == 200:
                if is_zip:
                    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                        # Find the .txt file inside the zip
                        txt_files = [n for n in zf.namelist() if n.lower().endswith('.txt')]
                        if txt_files:
                            text = zf.read(txt_files[0]).decode("latin-1", errors="replace")
                            log.info(f"COT: Loaded annual file from ZIP ({len(text.splitlines())} rows)")
                        else:
                            continue
                else:
                    text = r.text
                    log.info(f"COT: Loaded weekly file ({len(text.splitlines())} rows)")
                break
        except Exception as e:
            log.warning(f"COT fetch failed for {url}: {e}")
            continue

    if not text:
        log.error("COT: All URL attempts failed")
        return {}

    records_by_market = {}
    for code, match_str in CFTC_MARKETS.items():
        records_by_market[code] = []

    for row in csv.reader(text.splitlines()):
        if len(row) < 10:
            continue
        market_name = row[0].strip()
        matched_code = None
        for code, match_str in CFTC_MARKETS.items():
            if match_str.upper() in market_name.upper():
                matched_code = code
                break
        if not matched_code:
            continue
        try:
            report_date = row[2].strip() if len(row) > 2 else "unknown"
            nc_long  = int(row[7].strip().replace(",", "").replace(" ", "") or 0)
            nc_short = int(row[8].strip().replace(",", "").replace(" ", "") or 0)
            net      = nc_long - nc_short
            records_by_market[matched_code].append({
                "date":     report_date,
                "nc_long":  nc_long,
                "nc_short": nc_short,
                "net":      net,
            })
        except (ValueError, IndexError):
            continue

    total = sum(len(v) for v in records_by_market.values())
    markets_found = [k for k, v in records_by_market.items() if v]
    log.info(f"COT: Parsed {total} records for markets: {markets_found}")
    return records_by_market


def classify_cot_signal(records_list: list, ticker_trend: str) -> dict:
    """
    Classify COT net positioning relative to 52-week distribution.

    records_list: list of {"date", "net"} dicts, newest first
    ticker_trend: "UP" | "DOWN" | "FLAT" from price momentum
    """
    if not records_list:
        return _empty_cot()

    # Use last 52 weeks for z-score
    # Load annual COT data from local CSV (downloaded via curl)
    _annual = {}
    try:
        import pandas as _pd_cot, numpy as _np_cot, os as _os_cot
        _csv_paths = [
            "/tmp/cot_annual_2026.csv",
            "logs/cot/cot_annual_2026.csv",
        ]
        _cot_df = None
        for _cp in _csv_paths:
            if _os_cot.path.exists(_cp):
                _cot_df = _pd_cot.read_csv(_cp)
                break

        if _cot_df is not None:
            _mask = _cot_df["Market_and_Exchange_Names"].str.contains(
                "S&P 500 CONSOL", case=False, na=False
            )
            _es = _cot_df[_mask].tail(52)
            if len(_es) >= 4:
                _am = (_es["Asset_Mgr_Positions_Long_All"].astype(float) -
                       _es["Asset_Mgr_Positions_Short_All"].astype(float))
                _lm = (_es["Lev_Money_Positions_Long_All"].astype(float) -
                       _es["Lev_Money_Positions_Short_All"].astype(float))
                def _z(s):
                    mu, sd = s.mean(), s.std()
                    return round(float((s.iloc[-1]-mu)/sd), 2) if sd > 0 else 0.0
                _am_z = _z(_am); _lm_z = _z(_lm)
                _cz   = round(_am_z*0.6 + _lm_z*0.4, 2)
                _annual = {
                    "combined_z": _cz,
                    "am_zscore":  _am_z,
                    "lm_zscore":  _lm_z,
                    "weeks":      len(_es),
                    "signal": ("STRONG_BULL" if _cz > 1.5 else "BULL" if _cz > 0.5
                               else "STRONG_BEAR" if _cz < -1.5 else "BEAR"
                               if _cz < -0.5 else "NEUTRAL"),
                }
    except Exception as _ae:
        import logging; logging.getLogger(__name__).warning(f"Annual COT z-score failed: {_ae}")

    nets = [
int(r.get('nc_long', 0) or 0) - int(r.get('nc_short', 0) or 0) for r in records_list[-52:]]
    if len(nets) < 1:
        return _empty_cot()

    latest  = float(nets[-1])
    mean    = float(np.mean(nets))
    std     = float(np.std(nets))
    z_score = (latest - mean) / (std + 1e-8) if len(nets) >= 4 else (1.0 if latest > 0 else -1.0 if latest < 0 else 0.0)

    # Crowded long: speculators net long by >1σ
    if z_score > 1.5:
        positioning = "CROWDED_LONG"
        if ticker_trend == "DOWN":
            signal     = "CONTRARIAN_BEARISH"
            label      = "🐻 COT Crowded Long + Down Trend → Covering Risk"
            bear_boost = 10
            bull_boost = 0
        else:
            signal     = "TREND_BULLISH"
            label      = "🐂 COT Crowded Long + Up Trend → Continuation"
            bear_boost = 0
            bull_boost = 8
        color = "FF9500"

    # Crowded short: speculators net short by >1σ
    elif z_score < -1.5:
        positioning = "CROWDED_SHORT"
        if ticker_trend == "UP":
            signal     = "SQUEEZE_BULLISH"
            label      = "🚀 COT Crowded Short + Up Trend → Squeeze Fuel"
            bear_boost = 0
            bull_boost = 12
        else:
            signal     = "TREND_BEARISH"
            label      = "🐻 COT Crowded Short + Down Trend → Continuation"
            bear_boost = 10
            bull_boost = 0
        color = "00D4FF"

    else:
        positioning = "NEUTRAL"
        signal      = "NEUTRAL"
        label       = f"➡️ COT Neutral (z={z_score:+.2f})"
        bear_boost  = 0
        bull_boost  = 0
        color       = "888888"

    # Weekly change direction
    if len(nets) >= 2:
        wk_change = nets[-1] - nets[-2]
        wk_trend  = "INCREASING" if wk_change > 0 else "DECREASING"
    else:
        wk_change = 0
        wk_trend  = "FLAT"

    # Override z_score with 52-week annual if available and market is ES
    _use_z     = round(z_score, 3)
    _use_sig   = signal
    _use_label = label
    _ann_z     = None
    try:
        import pandas as _pd_ann, os as _os_ann
        for _cp in ["/tmp/cot_annual_2026.csv", "logs/cot/cot_annual_2026.csv"]:
            if _os_ann.path.exists(_cp):
                _df_ann = _pd_ann.read_csv(_cp)
                _mask   = _df_ann["Market_and_Exchange_Names"].str.contains(
                              "S&P 500 CONSOL", case=False, na=False)
                _es_ann = _df_ann[_mask].tail(52)
                if len(_es_ann) >= 4:
                    import numpy as _np_ann
                    _am = (_es_ann["Asset_Mgr_Positions_Long_All"].astype(float) -
                           _es_ann["Asset_Mgr_Positions_Short_All"].astype(float))
                    _lm = (_es_ann["Lev_Money_Positions_Long_All"].astype(float) -
                           _es_ann["Lev_Money_Positions_Short_All"].astype(float))
                    def _zs(s):
                        mu,sd = s.mean(),s.std()
                        return round(float((s.iloc[-1]-mu)/sd),2) if sd>0 else 0.0
                    _ann_z   = round(_zs(_am)*0.6 + _zs(_lm)*0.4, 2)
                    _use_z   = _ann_z
                    _use_sig = ("STRONG_BULL" if _ann_z > 1.5 else
                                "BULL"        if _ann_z > 0.5 else
                                "STRONG_BEAR" if _ann_z <-1.5 else
                                "BEAR"        if _ann_z <-0.5 else "NEUTRAL")
                    _use_label = f"COT {_use_sig} (52wk z={_ann_z:+.2f})"
                break
    except Exception:
        pass

    # Recalculate boosts from annual signal
    if _ann_z is not None:
        if _use_sig in ("STRONG_BULL", "BULL"):
            bear_boost, bull_boost = 0, (15 if _use_sig=="STRONG_BULL" else 8)
        elif _use_sig in ("STRONG_BEAR", "BEAR"):
            bear_boost, bull_boost = (15 if _use_sig=="STRONG_BEAR" else 8), 0
        else:
            bear_boost = bull_boost = 0

    return {
        "net":         int(latest),
        "net_mean":    round(mean, 0),
        "net_std":     round(std, 0),
        "z_score":     _use_z,
        "annual_z":    _ann_z,
        "positioning": positioning,
        "signal":      _use_sig,
        "label":       _use_label,
        "color":       color,
        "bear_boost":  bear_boost,
        "bull_boost":  bull_boost,
        "wk_change":   int(wk_change),
        "wk_trend":    wk_trend,
        "weeks_used":  len(_es_ann) if _ann_z is not None else len(nets),
        "trend_input": ticker_trend,
        "report_date": records_list[-1].get("date", ""),
    }


def _empty_cot(note: str = "") -> dict:
    return {
        "net": 0, "z_score": 0, "positioning": "NEUTRAL",
        "signal": "NEUTRAL", "label": f"COT unavailable {note}",
        "color": "888888", "bear_boost": 0, "bull_boost": 0,
        "wk_change": 0, "wk_trend": "FLAT", "weeks_used": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# PRICE TREND (for COT signal alignment)
# ══════════════════════════════════════════════════════════════════════

def get_spy_trend() -> str:
    """Get SPY 20-day trend direction."""
    try:
        end   = date.today().isoformat()
        start = (date.today() - timedelta(days=35)).isoformat()
        r = httpx.get(
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/{start}/{end}",
            params={"apiKey": POLYGON_KEY, "adjusted": "true",
                    "sort": "asc", "limit": 30},
            timeout=10
        )
        bars   = r.json().get("results", [])
        closes = [float(b["c"]) for b in bars if b.get("c")]
        if len(closes) >= 5:
            chg = (closes[-1] - closes[-5]) / closes[-5]
            if chg > 0.01:    return "UP"
            elif chg < -0.01: return "DOWN"
        return "FLAT"
    except Exception:
        return "FLAT"


# ══════════════════════════════════════════════════════════════════════
# COMPUTE + CACHE
# ══════════════════════════════════════════════════════════════════════

def compute_and_cache_cot() -> dict:
    """
    Download COT data and classify signals.
    Run Friday at 4 PM and Monday at 8 AM.
    Cache valid all week.
    """
    log.info("COT: Downloading CFTC weekly report...")
    records_by_market = fetch_and_parse_cot()

    # Phase1 patch: ensure net = nc_long - nc_short
    for _m, _weeks in records_by_market.items():
        for _rec in _weeks:
            if not _rec.get('net'):
                _rec['net'] = _rec.get('nc_long', 0) - _rec.get('nc_short', 0)


    spy_trend = get_spy_trend()
    log.info(f"  SPY trend: {spy_trend}")

    result = {
        "date":      date.today().isoformat(),
        "computed":  datetime.now().strftime("%H:%M ET"),
        "spy_trend": spy_trend,
        "markets":   {},
    }

    # Map COT markets to SPY/QQQ direction
    for code, records_list in records_by_market.items():
        # Sort by date
        records_list.sort(key=lambda x: x.get("date", ""))
        signal = classify_cot_signal(records_list, spy_trend)
        signal["market"] = code
        result["markets"][code] = signal

        log.info(f"  COT {code}: {signal['signal']} "
                 f"net={signal['net']:+,} z={signal['z_score']:+.2f} "
                 f"({signal['weeks_used']} weeks) → "
                 f"bear+{signal['bear_boost']} bull+{signal['bull_boost']}")

    if not result["markets"]:
        log.warning("COT: No data parsed from CFTC — using empty signals")
        result["markets"]["ES"] = _empty_cot("CFTC fetch failed")
        result["markets"]["NQ"] = _empty_cot("CFTC fetch failed")

    COT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(COT_CACHE, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_cot_cache(market: str = "ES") -> dict:
    """Load cached COT. Cache valid all week (updated Fridays)."""
    try:
        if COT_CACHE.exists():
            import time
            age_days = (time.time() - COT_CACHE.stat().st_mtime) / 86400
            if age_days < 7:   # valid for a week
                with open(COT_CACHE) as f:
                    data = json.load(f)
                m = data.get("markets", {}).get(market)
                if m:
                    return m
    except Exception:
        pass
    result = compute_and_cache_cot()
    return result.get("markets", {}).get(market, _empty_cot())


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_cot_agent_boost(ticker: str = "SPY") -> dict:
    """
    ARJUN agent boost from COT positioning.
    SPY/QQQ → ES data. IWM → TF data (fallback to ES).
    """
    market = "ES"   # ES covers SPY/QQQ
    cot    = load_cot_cache(market)
    return {
        "bear_boost": cot.get("bear_boost", 0),
        "bull_boost": cot.get("bull_boost", 0),
        "signal":     cot.get("signal", "NEUTRAL"),
        "label":      cot.get("label", ""),
        "z_score":    cot.get("z_score", 0),
        "cot":        cot,
    }


def get_cot_retrain_features() -> dict:
    """Weekly retrain XGBoost features from COT."""
    es = load_cot_cache("ES")
    nq = load_cot_cache("NQ")
    return {
        "cot_es_net":       es.get("net", 0),
        "cot_es_z":         es.get("z_score", 0),
        "cot_nq_net":       nq.get("net", 0),
        "cot_nq_z":         nq.get("z_score", 0),
        "cot_es_wk_change": es.get("wk_change", 0),
        "cot_signal":       {"CROWDED_LONG": 1, "CROWDED_SHORT": -1, "NEUTRAL": 0}.get(
                            es.get("positioning", "NEUTRAL"), 0),
    }


def get_cot_briefing_line() -> str:
    """One-line COT summary for Daily Briefing."""
    es = load_cot_cache("ES")
    nq = load_cot_cache("NQ")
    return (f"ES: {es.get('label', 'N/A')}  |  "
            f"NQ: {nq.get('positioning', 'N/A')} z={nq.get('z_score', 0):+.2f}  |  "
            f"Wk chg: {es.get('wk_change', 0):+,}")


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    result = compute_and_cache_cot()
    print(f"\n── COT Smart Money ({result['computed']}) ──────────────────────")
    print(f"  SPY 5-day trend: {result['spy_trend']}")
    for code, sig in result.get("markets", {}).items():
        print(f"  {code}:  {sig['signal']:20s}  "
              f"net={sig['net']:+,}  z={sig['z_score']:+.2f}  "
              f"wk_chg={sig['wk_change']:+,}  "
              f"bear+{sig['bear_boost']} bull+{sig['bull_boost']}")
        print(f"         {sig['label']}")
        print(f"         Report date: {sig.get('report_date', 'unknown')}  "
              f"({sig['weeks_used']} weeks of data)")