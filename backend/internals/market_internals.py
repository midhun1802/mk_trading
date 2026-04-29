"""CHAKRA Market Internals Monitor"""
import asyncio, httpx, pandas as pd, numpy as np
import os, json, logging, sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)
BASE_DIR = Path(__file__).parent.parent.parent
LOG_DIR  = BASE_DIR / "logs/internals"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE_DIR))

ET = ZoneInfo("America/New_York")
POLYGON_KEY  = os.getenv("POLYGON_API_KEY")
POLYGON_BASE = "https://api.polygon.io"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(),
    logging.FileHandler(str(LOG_DIR / f"internals_{date.today()}.log"))])
log = logging.getLogger("CHAKRA.Internals")

INDEX_TICKERS    = ["SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLRE", "XLB", "XLC", "EWU", "EWG", "EWJ", "EWH", "FXI", "EEM"
]
INTERNAL_TICKERS = ["I:VIX", "GLD", "TLT", "UUP"]

async def fetch_with_retry(url, params, retries=3):
    """Fetch with exponential backoff on 429."""
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, params=params)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                log.warning(f"  429 rate limit — waiting {wait}s (attempt {attempt+1}/{retries})")
                await asyncio.sleep(wait)
                continue
            return r.json()
        except Exception as e:
            log.error(f"  Fetch error: {e}")
            await asyncio.sleep(2)
    return {}

async def fetch_prev_close(ticker):
    data = await fetch_with_retry(
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/prev",
        {"apiKey": POLYGON_KEY, "adjusted": "true"}
    )
    res = data.get("results", [])
    if not res: return None
    c = float(res[0].get("c", 0)); o = float(res[0].get("o", c))
    chg = round((c - o) / o * 100, 3) if o else 0
    return {"ticker": ticker, "close": c, "open": o, "change": round(c-o,4),
            "chg_pct": chg, "high": float(res[0].get("h",0)), "low": float(res[0].get("l",0))}

async def fetch_multi_day_closes(ticker, days=5):
    end = date.today(); start = end - timedelta(days=days+5)
    data = await fetch_with_retry(
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
        {"apiKey": POLYGON_KEY, "adjusted": "true", "sort": "asc", "limit": 15}
    )
    return [float(x["c"]) for x in data.get("results", [])[-days:]]

def classify_vix(vix):
    if vix < 15:  return {"regime": "CALM",    "icon": "🟢", "impact": +5}
    if vix < 20:  return {"regime": "NORMAL",  "icon": "🟡", "impact": 0}
    if vix < 25:  return {"regime": "CAUTION", "icon": "🟠", "impact": -5}
    if vix < 30:  return {"regime": "FEAR",    "icon": "🔴", "impact": -10}
    return            {"regime": "PANIC",   "icon": "🚨", "impact": -20}

def calculate_herding(index_closes):
    returns = {}
    for t, closes in index_closes.items():
        if len(closes) >= 3:
            s = pd.Series(closes); returns[t] = s.pct_change().dropna()
    if len(returns) < 2: return {"score": 0.5, "regime": "UNKNOWN", "pairs": []}
    tickers = list(returns.keys()); pairs, total, count = [], 0, 0
    for i in range(len(tickers)):
        for j in range(i+1, len(tickers)):
            t1,t2 = tickers[i],tickers[j]; r1,r2 = returns[t1].values,returns[t2].values
            n = min(len(r1),len(r2))
            if n < 2: continue
            corr = float(np.corrcoef(r1[-n:],r2[-n:])[0,1])
            d1,d2 = r1[-1],r2[-1]
            direction = "▼" if d1<0 and d2<0 else "▲" if d1>0 and d2>0 else "↔"
            pairs.append({"pair": f"{t1} → {t2}", "corr_pct": round(abs(corr)*100), "direction": direction})
            total += abs(corr); count += 1
    score = round(total/count,3) if count else 0.5
    if score >= 0.75:   regime,note = "LOCKSTEP",  "All indices moving together — trade the trend"
    elif score <= 0.40: regime,note = "DIVERGING", "Sector rotation in play — pick winners/losers"
    else:               regime,note = "MIXED",     "Partial correlation — use caution"
    return {"score": score, "score_pct": round(score*100), "regime": regime,
            "regime_note": note, "pairs": sorted(pairs, key=lambda x: x["corr_pct"], reverse=True)}

def classify_risk(tdata):
    score = 0
    vix = tdata.get("VIX",{}).get("close",20); tlt = tdata.get("TLT",{}).get("chg_pct",0)
    gld = tdata.get("GLD",{}).get("chg_pct",0); uup = tdata.get("UUP",{}).get("chg_pct",0)
    score += 2 if vix<15 else 1 if vix<20 else -2 if vix>25 else -1 if vix>20 else 0
    score += -2 if tlt>0.3 else -1 if tlt>0.1 else 1 if tlt<-0.3 else 0
    score += -1 if gld>0.3 else 1 if gld<-0.3 else 0
    score += -1 if uup>0.3 else 1 if uup<-0.3 else 0
    if score >= 2:    mode,icon,boost = "RISK ON", "🟢",+8
    elif score >= 0:  mode,icon,boost = "NEUTRAL", "🟡",0
    elif score >= -2: mode,icon,boost = "RISK OFF","🔴",-8
    else:             mode,icon,boost = "RISK OFF","🚨",-15
    return {"mode":mode,"icon":icon,"score":score,"arka_boost":boost,
            "description":f"VIX {vix:.1f} | TLT {tlt:+.2f}% | GLD {gld:+.2f}%"}

def calculate_spy_qqq_ratio(spy_closes: list, qqq_closes: list) -> dict:
    """
    SPY/QQQ ratio — falling = QQQ underperforming = tech weakness = risk-off
    Rising = QQQ leading = growth appetite = risk-on
    """
    if not spy_closes or not qqq_closes or len(spy_closes) < 2:
        return {"ratio": 0, "signal": "NEUTRAL", "modifier": 0, "trend": "FLAT"}
    ratio_now = spy_closes[-1] / qqq_closes[-1] if qqq_closes[-1] else 0
    ratio_avg = sum(s / q for s, q in zip(spy_closes[-5:], qqq_closes[-5:]) if q) / min(5, len(spy_closes))
    if ratio_now < ratio_avg * 0.998:
        signal, modifier, trend = "RISK_ON", +5, "FALLING (QQQ leading ↑)"
    elif ratio_now > ratio_avg * 1.002:
        signal, modifier, trend = "RISK_OFF", -5, "RISING (SPY leading, tech weak ↓)"
    else:
        signal, modifier, trend = "NEUTRAL", 0, "FLAT"
    return {"ratio": round(ratio_now, 4), "signal": signal, "modifier": modifier,
            "trend": trend, "ratio_5d_avg": round(ratio_avg, 4)}


def calculate_bond_stress(tlt_closes: list) -> dict:
    """
    TLT velocity — rapid decline = rising yields = bond stress = equity headwind
    Rapid rise = falling yields = flight to safety = risk-off
    """
    if not tlt_closes or len(tlt_closes) < 2:
        return {"stress": "LOW", "regime": "STABLE", "modifier": 0, "velocity": 0}
    velocity = (tlt_closes[-1] - tlt_closes[0]) / tlt_closes[0] * 100
    if velocity < -1.0:
        stress, regime, modifier = "HIGH", "YIELD_SPIKE", -8
    elif velocity > 1.0:
        stress, regime, modifier = "HIGH", "FLIGHT_TO_SAFETY", -5
    elif velocity < -0.5:
        stress, regime, modifier = "MODERATE", "YIELD_RISING", -4
    elif velocity > 0.5:
        stress, regime, modifier = "MODERATE", "BONDS_RISING", -2
    else:
        stress, regime, modifier = "LOW", "STABLE", 0
    return {"stress": stress, "regime": regime, "modifier": modifier,
            "velocity": round(velocity, 3), "tlt_5d_change_pct": round(velocity, 3)}


def calculate_neural_pulse(vix: float, tlt_velocity: float, gld_chg: float,
                            uup_chg: float, spy_qqq_signal: str) -> dict:
    """
    Composite market health score 0–100.
    100 = perfect risk-on | <40 = stress, avoid aggressive entries | >70 = green light
    """
    score = 50  # base neutral
    # VIX component
    if vix < 15:   score += 20
    elif vix < 20: score += 10
    elif vix < 25: score -= 10
    else:          score -= 20
    # Bond stress component
    score += tlt_velocity * -5
    # Gold component (rising gold = risk-off)
    if gld_chg > 0.5:  score -= 10
    elif gld_chg < -0.5: score += 5
    # Dollar component (rising dollar = headwind)
    if uup_chg > 0.3:  score -= 10
    elif uup_chg < -0.3: score += 5
    # SPY/QQQ ratio component
    if spy_qqq_signal == "RISK_ON":  score += 5
    if spy_qqq_signal == "RISK_OFF": score -= 5
    score = max(0, min(100, int(score)))
    if score >= 70:   label, color = "BULLISH",  "🟢"
    elif score >= 50: label, color = "NEUTRAL",  "🟡"
    elif score >= 35: label, color = "CAUTION",  "🟠"
    else:             label, color = "STRESSED", "🔴"
    return {"score": score, "label": label, "color": color,
            "trending": "RISING" if score >= 60 else "FALLING" if score <= 40 else "FLAT"}


def get_dynamic_arka_threshold(pulse_score: int) -> dict:
    """Dynamic ARKA entry threshold based on Neural Pulse."""
    if pulse_score >= 70:   threshold, note = 55, "Lowered — strong conditions"
    elif pulse_score >= 50: threshold, note = 60, "Normal threshold"
    elif pulse_score >= 30: threshold, note = 70, "Raised — caution mode"
    else:                   threshold, note = 75, "Raised significantly — stressed market"
    return {"threshold": threshold, "note": note, "pulse_score": pulse_score}


def calc_arka_modifier(risk, vix_data, herding):
    modifier,reasons = 0,[]
    rm = risk.get("arka_boost",0); modifier+=rm; reasons.append(f"Risk {risk['mode']}: {rm:+d}pts")
    vix = vix_data.get("close",20); vc = classify_vix(vix); vi = vc.get("impact",0)
    modifier+=vi; reasons.append(f"VIX {vix:.1f} ({vc['regime']}): {vi:+d}pts")
    hs = herding.get("score",0.5)
    hb = +5 if hs>=0.8 else +2 if hs>=0.6 else -5 if hs<=0.3 else 0
    if hb: reasons.append(f"Herding {hs:.2f} ({herding['regime']}): {hb:+d}pts"); modifier+=hb
    return {"modifier":modifier,"reasons":reasons,
            "label":f"ARKA conviction {'+' if modifier>=0 else ''}{modifier} pts"}

async def run_internals():
    log.info(f"\n{'='*50}\n  CHAKRA MARKET INTERNALS\n  {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}\n{'='*50}")
    tdata = {}
    for ticker in INTERNAL_TICKERS:
        d = await fetch_prev_close(ticker)
        if d: key = ticker.replace("I:",""); tdata[key]=d; log.info(f"  {ticker}: ${d['close']:.2f} ({d['chg_pct']:+.2f}%)")
        await asyncio.sleep(2.5)
    index_closes = {}
    for ticker in INDEX_TICKERS:
        closes = await fetch_multi_day_closes(ticker,days=5)
        if closes: index_closes[ticker]=closes
        await asyncio.sleep(2.5)
    vix_data = tdata.get("VIX",{}); vix_cls = classify_vix(vix_data.get("close",20))
    # ── HELX v2.0 — Neural Pulse ──────────────────────────────────────────
    spy_closes  = index_closes.get("SPY", [])
    qqq_closes  = index_closes.get("QQQ", [])
    tlt_closes  = await fetch_multi_day_closes("TLT", days=5)
    spy_qqq     = calculate_spy_qqq_ratio(spy_closes, qqq_closes)
    bond_stress = calculate_bond_stress(tlt_closes)
    pulse       = calculate_neural_pulse(
        vix        = vix_data.get("close", 20),
        tlt_velocity = bond_stress["velocity"],
        gld_chg    = tdata.get("GLD", {}).get("chg_pct", 0),
        uup_chg    = tdata.get("UUP", {}).get("chg_pct", 0),
        spy_qqq_signal = spy_qqq["signal"],
    )
    dynamic_threshold = get_dynamic_arka_threshold(pulse["score"])
    log.info(f"  Neural Pulse: {pulse['color']} {pulse['score']}/100 ({pulse['label']}) | "
             f"SPY/QQQ: {spy_qqq['signal']} | Bond Stress: {bond_stress['stress']} | "
             f"ARKA Threshold: {dynamic_threshold['threshold']}")
    herding  = calculate_herding(index_closes); risk = classify_risk(tdata)
    arka_mod = calc_arka_modifier(risk,vix_data,herding)
    log.info(f"  Risk: {risk['icon']} {risk['mode']} | VIX: {vix_cls['icon']} {vix_cls['regime']} ({vix_data.get('close',0):.1f}) | Herding: {herding['score']:.2f} {herding['regime']} | ARKA: {arka_mod['modifier']:+d}pts")
    output = {"date":date.today().isoformat(),"time":datetime.now(ET).strftime("%I:%M %p ET"),"neural_pulse":pulse,"spy_qqq_ratio":spy_qqq,"bond_stress":bond_stress,"dynamic_arka_threshold":dynamic_threshold,
              "risk":risk,"vix":{**vix_data,"classification":vix_cls},
              "tlt":tdata.get("TLT",{}),"gld":tdata.get("GLD",{}),"uup":tdata.get("UUP",{}),
              "herding":herding,"arka_mod":arka_mod,"raw":tdata,
              "index_last":{k:v[-1] if v else 0 for k,v in index_closes.items()}}
    for p in [LOG_DIR/f"internals_{date.today()}.json", LOG_DIR/"internals_latest.json"]:
        with open(p,"w") as f: json.dump(output,f,indent=2)
    log.info(f"  Saved → {LOG_DIR}")
    try:
        from backend.arka.discord_notifier import post_market_internals
        # Market internals Discord disabled — too noisy, data feeds into ARKA scorer only
        log.info("  Market internals computed (Discord posting disabled)")
    except Exception as e: log.error(f"  Discord failed: {e}")
    return output

class InternalsMonitor:
    def __init__(self): self.last_run=None; self.last_date=None
    async def run(self):
        log.info("  CHAKRA INTERNALS MONITOR — every 30min during market hours")
        while True:
            try:
                now=datetime.now(ET); today=date.today()
                if self.last_date!=today: self.last_date=today; log.info(f"  New day: {today}")
                if now.weekday()>=5: await asyncio.sleep(3600); continue
                h,m=now.hour,now.minute; in_win=(8,0)<=(h,m)<=(16,30)
                ts=now.timestamp(); due=not self.last_run or (ts-self.last_run)>=1800
                if in_win and due: await run_internals(); self.last_run=ts
                else: await asyncio.sleep(60); continue
            except Exception as e: log.error(f"  Error: {e}")
            await asyncio.sleep(60)

if __name__=="__main__":
    if "--watch" in sys.argv: asyncio.run(InternalsMonitor().run())
    else: asyncio.run(run_internals())
