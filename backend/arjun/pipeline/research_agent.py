"""
CHAKRA Market Research Agent
Ingests live market data and produces a typed ResearchReport.
Wraps existing CHAKRA data sources into a clean pipeline node.
"""
import os, json, logging, time
from datetime import datetime, date
from pathlib import Path

log = logging.getLogger("CHAKRA.Research")


async def research_node(state: dict) -> dict:
    """
    Research Agent node for LangGraph pipeline.
    Pulls all market data and produces ResearchReport.
    """
    import httpx
    from backend.arjun.schemas.research_report import ResearchReport, TickerSnapshot

    watchlist = state.get("watchlist", ["SPY","QQQ","IWM"])
    key       = os.getenv("POLYGON_API_KEY","")
    log.info(f"🔍 Research Agent: scanning {watchlist}")

    snapshots = []

    async with httpx.AsyncClient(timeout=12) as client:
        for ticker in watchlist:
            try:
                # 1. Live price snapshot
                r = await client.get(
                    f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                    params={"apiKey": key}
                )
                snap    = r.json().get("ticker",{})
                price   = float(snap.get("day",{}).get("c",0) or
                               snap.get("lastTrade",{}).get("p",0) or 0)
                prev    = float(snap.get("prevDay",{}).get("c",price) or price)
                chg_pct = ((price-prev)/prev*100) if prev else 0
                volume  = int(snap.get("day",{}).get("v",0) or 0)

                # 2. RSI from 5-min bars
                today_str = str(date.today())
                bars_r    = await client.get(
                    f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute/{today_str}/{today_str}",
                    params={"apiKey":key,"limit":20,"sort":"asc"}
                )
                bars = bars_r.json().get("results",[])
                rsi  = 50.0
                vwap = price

                if len(bars) >= 15:
                    deltas = [bars[i]["c"]-bars[i-1]["c"] for i in range(1,len(bars))]
                    gains  = [max(0,d) for d in deltas[-14:]]
                    losses = [max(0,-d) for d in deltas[-14:]]
                    avg_g  = sum(gains)/14
                    avg_l  = sum(losses)/14
                    if avg_l > 0:
                        rsi = round(100-(100/(1+avg_g/avg_l)),1)

                if bars:
                    tv   = sum(b.get("vw",b["c"])*b["v"] for b in bars if b.get("v"))
                    v    = sum(b["v"] for b in bars if b.get("v"))
                    vwap = round(tv/v,2) if v else price

                # 3. GEX context from cached state
                gex_regime  = "UNKNOWN"
                regime_call = "NEUTRAL"
                call_wall   = None
                put_wall    = None
                zero_gamma  = None

                gex_f = Path(f"logs/gex/gex_latest_{ticker}.json")
                if gex_f.exists():
                    try:
                        gex_d = json.loads(gex_f.read_text())
                        if time.time()-gex_d.get("ts",0) < 3600:
                            gex_regime  = gex_d.get("regime","UNKNOWN")
                            regime_call = gex_d.get("regime_call","NEUTRAL")
                            call_wall   = gex_d.get("call_wall")
                            put_wall    = gex_d.get("put_wall")
                            zero_gamma  = gex_d.get("zero_gamma")
                    except Exception:
                        pass

                # 4. Flow signals from chakra
                flow_dir  = "NEUTRAL"
                flow_conf = 0.0
                dp_pct    = 0.0

                flow_f = Path("logs/chakra/flow_signals_latest.json")
                if flow_f.exists():
                    try:
                        flow_d   = json.loads(flow_f.read_text())
                        flow_age = time.time()-flow_d.get("ts",0)
                        if flow_age < 1800:
                            for sig in flow_d.get("signals",[]):
                                if sig.get("ticker","").upper() == ticker.upper():
                                    flow_dir  = sig.get("direction","NEUTRAL")
                                    flow_conf = float(sig.get("confidence",0))
                                    dp_pct    = float(sig.get("dark_pool_pct",0))
                                    break
                    except Exception:
                        pass

                snapshots.append(TickerSnapshot(
                    ticker        = ticker,
                    price         = price,
                    change_pct    = round(chg_pct,2),
                    volume        = volume,
                    rsi_14        = rsi,
                    vwap          = vwap,
                    above_vwap    = price > vwap,
                    gex_regime    = gex_regime,
                    regime_call   = regime_call,
                    call_wall     = call_wall,
                    put_wall      = put_wall,
                    zero_gamma    = zero_gamma,
                    sentiment     = "bullish" if chg_pct>0.5 else "bearish" if chg_pct<-0.5 else "neutral",
                    flow_dir      = flow_dir,
                    flow_conf     = flow_conf,
                    dark_pool_pct = dp_pct,
                ))

                log.info(f"  📊 {ticker}: ${price:.2f} rsi={rsi:.0f} gex={gex_regime} flow={flow_dir}")

            except Exception as e:
                log.error(f"  Research error for {ticker}: {e}")

    # Market internals
    vix          = 20.0
    neural_pulse = 50.0
    risk_mode    = "NORMAL"
    risk_flags   = []

    int_f = Path("logs/internals/internals_latest.json")
    if int_f.exists():
        try:
            int_d        = json.loads(int_f.read_text())
            vix          = float(int_d.get("vix",20))
            neural_pulse = float(int_d.get("neural_pulse",50))
            risk_mode    = int_d.get("risk_mode","NORMAL")
            if vix > 25: risk_flags.append(f"HIGH VIX: {vix:.1f}")
            if risk_mode == "RISK_OFF": risk_flags.append("RISK OFF MODE")
        except Exception:
            pass

    avg_chg = sum(abs(s.change_pct) for s in snapshots) / max(len(snapshots),1)
    regime  = "trending" if avg_chg > 0.5 else "choppy" if avg_chg < 0.2 else "ranging"

    report = ResearchReport(
        timestamp     = datetime.now(),
        tickers       = snapshots,
        vix           = vix,
        market_regime = regime,
        regime_call   = snapshots[0].regime_call if snapshots else "NEUTRAL",
        neural_pulse  = neural_pulse,
        risk_mode     = risk_mode,
        macro_notes   = f"VIX:{vix:.1f} Pulse:{neural_pulse:.0f} Mode:{risk_mode}",
        top_movers    = [s.ticker for s in sorted(snapshots,key=lambda x:abs(x.change_pct),reverse=True)[:3]],
        risk_flags    = risk_flags,
    )

    state["research_report"] = report.model_dump()
    log.info(f"✅ Research complete: {len(snapshots)} tickers, regime={regime}")
    return state
