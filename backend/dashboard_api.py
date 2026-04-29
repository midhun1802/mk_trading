import re
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import json
import os
import glob
from datetime import datetime
import httpx
from pydantic import BaseModel

app = FastAPI()


# ── WebSocket Connection Manager ──────────────────────────────────────────────
class _WSManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active = [c for c in self.active if c is not ws]

    async def broadcast(self, data: dict):
        dead = []
        msg  = json.dumps(data)
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = _WSManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive — client can send pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


async def _ws_push_loop():
    """Background task: push live data to all connected clients every 3s."""
    import os as _os, json as _j
    from pathlib import Path
    _base = Path(__file__).resolve().parents[1]

    while True:
        await asyncio.sleep(3)
        if not ws_manager.active:
            continue
        try:
            payload = {"ts": datetime.now().isoformat()}

            # Positions
            pos_file = _base / "logs/arka/open_positions.json"
            if pos_file.exists():
                payload["positions"] = _j.loads(pos_file.read_text())

            # Engine state / daily summary
            from datetime import date as _d
            summary_file = _base / f"logs/arka/summary_{_d.today()}.json"
            if summary_file.exists():
                payload["engine"] = _j.loads(summary_file.read_text())

            # Scan feed (last 20 entries)
            scan_file = _base / "logs/arka/scan_feed.json"
            if scan_file.exists():
                feed = _j.loads(scan_file.read_text())
                payload["scan_feed"] = feed[-20:] if isinstance(feed, list) else feed

            # Flow signals
            flow_file = _base / "logs/chakra/flow_signals_latest.json"
            if flow_file.exists():
                payload["flow"] = _j.loads(flow_file.read_text())

            await ws_manager.broadcast(payload)
        except Exception:
            pass


@app.on_event("startup")
async def _start_ws_push():
    asyncio.create_task(_ws_push_loop())

# Serve frontend
import os
frontend_dir = os.path.join(os.path.dirname(__file__), '..', 'frontend')
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

from fastapi.responses import FileResponse
@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(frontend_dir, 'dashboard.html'))

@app.get("/dashboard")
def serve_dashboard2():
    return FileResponse(os.path.join(frontend_dir, 'dashboard.html'))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.get("/api/premarket")
def get_premarket():
    """Multi-index pre-market: file data + live prices for SPY/QQQ/IWM/DIA/VIX."""
    import json as _j, os as _os, httpx as _hx
    from datetime import date as _date, timedelta as _td, datetime as _dt
    today = _date.today().strftime("%Y-%m-%d")
    base  = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    # Load pre-market file if available
    data = {}
    for day_offset in [0, 1, 2]:
        d = (_date.today() - _td(days=day_offset)).strftime("%Y-%m-%d")
        f = _os.path.join(base, f"logs/premarket/premarket_{d}.json")
        if _os.path.exists(f):
            try:
                data = _j.load(open(f))
                data["file_date"] = d
            except Exception:
                pass
            break

    # Always fetch live index prices
    key = _os.getenv("POLYGON_API_KEY", "")
    index_tickers = ["SPY","QQQ","IWM","DIA","VIX","XLK","XLF","XLE","XLV","XLI","XLP","XLY","UVXY","TLT"]
    live = {}
    try:
        r = _hx.get(
            "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(index_tickers), "apiKey": key}, timeout=10
        ).json()
        for t in r.get("tickers", []):
            sym  = t.get("ticker", "")
            day  = t.get("day", {})
            prev = t.get("prevDay", {})
            pm   = t.get("preMarket", {})   # Polygon preMarket extended hours block
            lp   = float(t.get("lastTrade",{}).get("p",0) or day.get("c",0) or prev.get("c",0) or 0)
            pc   = float(prev.get("c",1) or 1)
            chg  = round(lp - pc, 2)
            pct  = round(chg/pc*100, 3) if pc else 0
            # Pre-market OHLC (Polygon includes this during extended hours)
            pm_high = round(float(pm.get("h", 0) or day.get("h", 0) or 0), 2)
            pm_low  = round(float(pm.get("l", 0) or day.get("l", 0) or 0), 2)
            pm_last = round(float(pm.get("c", 0) or lp or 0), 2)
            live[sym] = {
                "ticker": sym, "price": round(lp,2), "prev_close": round(pc,2),
                "change": chg, "chg_pct": pct,
                "direction": "UP" if pct>0.05 else "DOWN" if pct<-0.05 else "FLAT",
                "high": round(float(day.get("h",0)),2),
                "low":  round(float(day.get("l",0)),2),
                "volume": day.get("v",0),
                "pm_high": pm_high,
                "pm_low":  pm_low,
                "pm_last": pm_last,
            }
    except Exception as e:
        live = {"error": str(e)}

    data["live_indexes"] = live
    data["generated"]    = datetime.now().isoformat()
    if not data.get("tickers"):
        data["message"] = "Pre-market scan runs at 7:15 AM ET. Showing live index data."
    return data


@app.get("/api/engine/status")
def get_engine_status():
    """Check which engines are running."""
    import subprocess as _sp, glob as _gl
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def is_running(pattern):
        r = _sp.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        pids = [p for p in r.stdout.strip().split("\n") if p]
        return {"running": bool(pids), "pid": pids[0] if pids else None}

    def cache_age(fname):
        path = os.path.join(base, "logs/chakra", fname)
        if not os.path.exists(path): return None
        age_min = round((os.time() - os.path.getmtime(path)) / 60, 1)
        return age_min

    # Module cache ages
    modules = {
        "DEX": "dex_latest.json", "Hurst": "hurst_latest.json",
        "VRP": "vrp_latest.json", "VEX": "vex_latest.json",
        "Charm": "charm_latest.json", "Entropy": "entropy_latest.json",
        "HMM": "hmm_latest.json", "IVSkew": "ivskew_latest.json",
        "Iceberg": "iceberg_latest.json", "Lambda": "lambda_latest.json",
        "COT": "cot_latest.json", "ProbDist": "probdist_latest.json",
    }
    import time as _time
    cache_status = {}
    for name, fname in modules.items():
        path = os.path.join(base, "logs/chakra", fname)
        if not os.path.exists(path):
            cache_status[name] = {"status": "MISSING", "age_min": None}
        else:
            age = round((_time.time() - os.path.getmtime(path)) / 60, 1)
            # Modules only run during market hours (9 AM - 4 PM ET)
            # After hours: anything written today is OK; before market: yesterday is OK
            from datetime import datetime
            hour_et = datetime.now().hour  # approximate ET
            market_open = 9 <= hour_et <= 16
            if market_open:
                # During market: must be fresh (< 60 min)
                stale = age > 60
            else:
                # After hours: anything < 8 hours old is fine
                stale = age > 480
            if stale:
                cache_status[name] = {"status": "STALE", "age_min": age}
            else:
                cache_status[name] = {"status": "OK", "age_min": age}

    def is_running_any(*patterns):
        for pat in patterns:
            r = is_running(pat)
            if r["running"]:
                return r
        return {"running": False, "pid": None}

    return {
        "engines": {
            "arka":       is_running_any("backend.arka.arka_engine", "arka_engine"),
            "internals":  is_running_any("start_internals", "market_internals"),
            "dashboard":  is_running_any("uvicorn"),
            "arjun":      is_running_any("arjun_live_engine"),
        },
        "modules": cache_status,
        "checked_at": datetime.now().strftime("%H:%M ET"),
    }


@app.post("/api/engine/start")
def start_engine(engine: str = "all"):
    """Restart a specific engine by name: arka_engine | flow_monitor | market_internals | all"""
    import subprocess as _sp
    _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _venv = os.path.join(_base, "venv/bin/python3")
    _log  = os.path.join(_base, "logs")

    _cmds = {
        "arka_engine": {
            "kill":  ["pkill", "-f", "arka_engine"],
            "start": [_venv, "-m", "backend.arka.arka_engine"],
            "log":   f"{_log}/arka/arka_engine.log",
            "label": "ARKA Engine",
        },
        "flow_monitor": {
            "kill":  ["pkill", "-f", "flow_monitor"],
            "start": [_venv, "backend/chakra/flow_monitor.py", "--watch"],
            "log":   f"{_log}/chakra/flow_monitor.log",
            "label": "Flow Monitor",
        },
        "market_internals": {
            "kill":  ["pkill", "-f", "market_internals"],
            "start": [_venv, "backend/internals/market_internals.py", "--watch"],
            "log":   f"{_log}/internals/market_internals.log",
            "label": "Market Internals",
        },
        # alias used by frontend
        "internals": {
            "kill":  ["pkill", "-f", "market_internals"],
            "start": [_venv, "backend/internals/market_internals.py", "--watch"],
            "log":   f"{_log}/internals/market_internals.log",
            "label": "Market Internals",
        },
        "arka": {
            "kill":  ["pkill", "-f", "arka_engine"],
            "start": [_venv, "-m", "backend.arka.arka_engine"],
            "log":   f"{_log}/arka/arka_engine.log",
            "label": "ARKA Engine",
        },
    }

    # "all" restarts the 3 core modules (exclude aliases)
    _core = ["arka_engine", "flow_monitor", "market_internals"]

    started = []
    errors  = []

    targets = _core if engine == "all" else [engine]
    for _eng in targets:
        if _eng not in _cmds:
            errors.append(f"Unknown engine: {_eng}")
            continue
        cfg = _cmds[_eng]
        _sp.run(cfg["kill"], capture_output=True)
        import time as _t; _t.sleep(0.8)
        try:
            os.makedirs(os.path.dirname(cfg["log"]), exist_ok=True)
            with open(cfg["log"], "a") as _lf:
                _sp.Popen(cfg["start"], stdout=_lf, stderr=_lf, cwd=_base)
            started.append(cfg["label"])
        except Exception as _e:
            errors.append(f"{cfg['label']}: {_e}")

    msg = f"Restarted: {', '.join(started)}" if started else "Nothing started"
    if errors: msg += f" | Errors: {', '.join(errors)}"
    return {"started": started, "errors": errors, "engine": engine, "message": msg}


@app.post("/api/engine/stop")
def stop_engine(engine: str = "all"):
    """Stop a specific engine."""
    import subprocess as _sp
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(base, "stop_engines.sh")
    r = _sp.run(["bash", script, engine], capture_output=True, text=True, cwd=base)
    return {"engine": engine, "output": r.stdout.strip()}

@app.get("/api/signals")
def get_signals():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    # Load only today's most recent signal file (avoid stacking multi-day dupes)
    _today = _dt.now().strftime("%Y%m%d")
    files = sorted(glob.glob(f"logs/signals/signals_{_today}*.json"), reverse=True)
    if not files:  # fallback: latest file regardless of date
        files = sorted(glob.glob("logs/signals/signals_*.json"), reverse=True)[:1]
    all_signals = []
    for f in files[:1]:  # only the most recent file
        try:
            with open(f) as file:
                data = json.load(file)
                if isinstance(data, list):
                    all_signals.extend(data)
        except:
            pass
    # Tag staleness — market hours: stale after 90 min; pre/post: always flag
    _now_et = _dt.now(_ZI("America/New_York"))
    _market_open = (_now_et.weekday() < 5 and
                    ((_now_et.hour == 9 and _now_et.minute >= 30) or _now_et.hour > 9) and
                    _now_et.hour < 16)
    _now_utc = _dt.utcnow()
    for sig in all_signals:
        _ts = sig.get("generated_at") or sig.get("timestamp") or ""
        try:
            _gen = _dt.fromisoformat(_ts.replace("Z", "").split("+")[0])
            _age_min = (_now_utc - _gen).total_seconds() / 60
            _age_h   = _age_min / 60
            sig["age_hours"]   = round(_age_h, 1)
            sig["age_minutes"] = round(_age_min)
            if _market_open:
                sig["stale"]       = _age_min > 90
                sig["stale_label"] = f"{int(_age_min//60)}h {int(_age_min%60)}m ago" if _age_min > 90 else ""
            else:
                sig["stale"]       = False   # after hours — not worth flagging
                sig["stale_label"] = ""
        except Exception:
            sig["age_hours"]   = None
            sig["age_minutes"] = None
            sig["stale"]       = False
            sig["stale_label"] = ""
    return all_signals[:50]


def _update_signal_in_file(ticker: str, updated: dict):
    """Upsert a refreshed signal into today's latest signal file."""
    _today = datetime.now().strftime("%Y%m%d")
    _files = sorted(glob.glob(f"logs/signals/signals_{_today}*.json"), reverse=True)
    if not _files:
        return
    try:
        _sigs = json.loads(open(_files[0]).read())
        if not isinstance(_sigs, list):
            _sigs = _sigs.get("signals", [])
        for i, s in enumerate(_sigs):
            if s.get("ticker") == ticker:
                # Preserve full agents analysis if fast refresh has none
                if not updated.get("agents") and s.get("agents"):
                    updated["agents"] = s["agents"]
                _sigs[i] = updated
                break
        else:
            _sigs.append(updated)
        open(_files[0], "w").write(json.dumps(_sigs, indent=2))
    except Exception:
        pass


@app.post("/api/signals/refresh/{ticker}")
async def refresh_ticker_signal(ticker: str):
    """Fast intraday refresh — hits Polygon directly, no subprocess, returns in <3s."""
    import httpx as _hx
    from zoneinfo import ZoneInfo as _ZI2
    _ticker = ticker.upper()
    _key    = os.getenv("POLYGON_API_KEY", "")
    _ET     = _ZI2("America/New_York")

    try:
        async with _hx.AsyncClient(timeout=12) as _cl:
            # 1. Live snapshot
            _sr  = await _cl.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{_ticker}",
                params={"apiKey": _key}
            )
            _snap      = _sr.json().get("ticker", {})
            _price     = float(_snap.get("day", {}).get("c", 0) or 0)
            _prev      = float(_snap.get("prevDay", {}).get("c", _price) or _price)
            _chg       = ((_price - _prev) / _prev * 100) if _prev else 0
            _high      = float(_snap.get("day", {}).get("h", _price) or _price)
            _low       = float(_snap.get("day", {}).get("l",  _price) or _price)

            if not _price:
                return {"success": False, "error": f"No live price for {_ticker}"}

            # 2. 5-min bars for RSI + VWAP
            _today_s = datetime.now().strftime("%Y-%m-%d")
            _br = await _cl.get(
                f"https://api.polygon.io/v2/aggs/ticker/{_ticker}/range/5/minute/{_today_s}/{_today_s}",
                params={"apiKey": _key, "adjusted": True, "limit": 50, "sort": "asc"}
            )
            _bars = _br.json().get("results", [])

            # 3. RSI(14)
            _rsi = 50.0
            if len(_bars) >= 15:
                _d  = [_bars[i]["c"] - _bars[i-1]["c"] for i in range(1, len(_bars))]
                _g  = [max(0,  x) for x in _d[-14:]]
                _l  = [max(0, -x) for x in _d[-14:]]
                _ag, _al = sum(_g)/14, sum(_l)/14
                if _al > 0:
                    _rsi = round(100 - (100 / (1 + _ag/_al)), 1)

            # 4. VWAP
            _vwap = _price
            if _bars:
                _tpv = sum(b.get("vw", b["c"]) * b["v"] for b in _bars if b.get("v"))
                _tv  = sum(b["v"] for b in _bars if b.get("v"))
                if _tv: _vwap = round(_tpv / _tv, 2)
            _vwap_bias = "ABOVE" if _price > _vwap else "BELOW"

            # 5. Score
            _bull, _bear, _reasons = 0, 0, []
            if _rsi < 30:   _bull += 25; _reasons.append(f"RSI oversold ({_rsi:.0f})")
            elif _rsi < 40: _bull += 15; _reasons.append(f"RSI low ({_rsi:.0f})")
            elif _rsi > 70: _bear += 25; _reasons.append(f"RSI overbought ({_rsi:.0f})")
            elif _rsi > 60: _bear += 15; _reasons.append(f"RSI elevated ({_rsi:.0f})")

            if _vwap_bias == "ABOVE": _bull += 20; _reasons.append(f"Above VWAP ${_vwap:.2f}")
            else:                     _bear += 20; _reasons.append(f"Below VWAP ${_vwap:.2f}")

            if   _chg >  1.5: _bull += 25; _reasons.append(f"Strong momentum +{_chg:.1f}%")
            elif _chg >  0.5: _bull += 15; _reasons.append(f"Positive momentum +{_chg:.1f}%")
            elif _chg < -1.5: _bear += 25; _reasons.append(f"Strong selloff {_chg:.1f}%")
            elif _chg < -0.5: _bear += 15; _reasons.append(f"Negative momentum {_chg:.1f}%")

            _rng = (_price - _low) / (_high - _low) * 100 if _high != _low else 50
            if   _rng > 70: _bear += 10; _reasons.append(f"Near day high ({_rng:.0f}% of range)")
            elif _rng < 30: _bull += 10; _reasons.append(f"Near day low ({_rng:.0f}% of range)")

            if   _bull > _bear + 10: _sig, _dir, _conf = "BUY",  "BULLISH", min(85, 50 + _bull * 0.5)
            elif _bear > _bull + 10: _sig, _dir, _conf = "SELL", "BEARISH", min(85, 50 + _bear * 0.5)
            else:                    _sig, _dir, _conf = "HOLD", "NEUTRAL", 45.0

            _now_s = datetime.now(_ET).isoformat()
            _updated = {
                "ticker":       _ticker,
                "signal":       _sig,
                "confidence":   round(_conf, 1),
                "direction":    _dir,
                "price":        _price,
                "change_pct":   round(_chg, 2),
                "rsi":          _rsi,
                "vwap":         _vwap,
                "vwap_bias":    _vwap_bias,
                "bull_score":   _bull,
                "bear_score":   _bear,
                "reasons":      _reasons,
                "generated_at": _now_s,
                "refreshed_at": _now_s,
                "refreshed":    True,
                "stale":        False,
                "stale_label":  "",
                "age_minutes":  0,
            }
            _update_signal_in_file(_ticker, _updated)
            return {"success": True, "signal": _updated}

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/trades")
def get_trades():
    files = sorted(glob.glob("logs/trades/*.json"), reverse=True)[:30]
    all_trades = []
    for f in files:
        try:
            with open(f) as file:
                data = json.load(file)
                if isinstance(data, list):
                    all_trades.extend(data)
        except:
            pass
    return all_trades

@app.get("/api/backtest")
def get_backtest():
    try:
        with open("logs/backtest_results.json") as f:
            return json.load(f)
    except:
        return []

@app.get("/api/account")
def get_account():
    import httpx as _hx, os as _os
    from dotenv import load_dotenv as _ldenv
    _ldenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'), override=True)
    key    = _os.getenv("ALPACA_API_KEY","")
    secret = _os.getenv("ALPACA_SECRET_KEY","")
    base   = _os.getenv("ALPACA_BASE_URL","https://paper-api.alpaca.markets")
    hdrs   = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    try:
        acct = _hx.get(f"{base}/v2/account", headers=hdrs, timeout=10).json()
        pos  = _hx.get(f"{base}/v2/positions", headers=hdrs, timeout=10).json()
        acct["positions"] = pos if isinstance(pos, list) else []
        return acct
    except Exception as e:
        return {"error": str(e), "portfolio_value": 0, "cash": 0, "buying_power": 0, "positions": []}


@app.get("/api/stats")
def get_stats():
    files = sorted(glob.glob("logs/trades/*.json"), reverse=True)[:60]
    all_trades = []
    for f in files:
        try:
            with open(f) as file:
                data = json.load(file)
                if isinstance(data, list):
                    all_trades.extend(data)
        except:
            pass

    executed = [t for t in all_trades if t.get("action") in ["BUY","SELL"]]
    total    = len(executed)
    wins     = len([t for t in executed if t.get("action") == "SELL"])

    return {
        "total_signals":   len(all_trades),
        "total_executed":  total,
        "days_running":    len(files),
    }

# ── ARKA ENDPOINTS ────────────────────────────────────────────────────────────

@app.get("/api/arka/session")
def get_arka_session():
    """Return today's ARKA log entries as structured JSON."""
    import json as _json
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")
    log_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            f"logs/arka/arka_{today}.log")
    entries = []
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            entries.append(_json.loads(line))
                        except Exception:
                            entries.append({"raw": line})
                    elif line:
                        entries.append({"raw": line})
        except Exception as e:
            return {"error": str(e), "entries": []}
    return {"date": today, "entries": entries, "count": len(entries)}



@app.get("/api/arka/live-feed")
def get_arka_live_feed(n: int = 50):
    """
    Parse ARKA log and return last N scan events with:
    - ticker, conviction score, threshold, direction
    - why trade was skipped (fakeout / below threshold / lunch session)
    - any trades taken
    """
    import glob as _glob
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Find most recent log file
    log_files = sorted(_glob.glob(f"{base}/logs/arka/arka_*.log"), reverse=True)
    if not log_files:
        return {"scans": [], "trades": [], "status": "No ARKA log found", "watching": []}

    log_path = log_files[0]
    scans = []
    trades = []
    current_scan = None

    try:
        lines = open(log_path).readlines()[-500:]  # last 500 lines
        for raw_line in lines:
            line = raw_line.strip()

            # Scan header: "─── Scan HH:MM:SS ET ─"
            if "Scan" in line and "ET" in line and "───" in line:
                if current_scan:
                    scans.append(current_scan)
                time_match = re.search(r'(\d{2}:\d{2}:\d{2})', line)
                current_scan = {
                    "time": time_match.group(1) if time_match else "",
                    "tickers": {},
                    "actions": []
                }
                continue

            if current_scan is None:
                current_scan = {"time": "", "tickers": {}, "actions": []}

            # Conviction score lines: "SPY conviction=72 threshold=55"
            conv_match = re.search(r'(\w+)[:\s]+conviction[=\s]+(\d+).*threshold[=\s]+(\d+)', line, re.I)
            if conv_match:
                sym, conv, thr = conv_match.groups()
                current_scan["tickers"].setdefault(sym, {})
                current_scan["tickers"][sym]["conviction"] = int(conv)
                current_scan["tickers"][sym]["threshold"]  = int(thr)
                continue

            # Skip reasons
            for pattern, reason in [
                (r'(\w+).*fakeout.*block',       "fakeout_blocked"),
                (r'(\w+).*below threshold',       "below_threshold"),
                (r'(\w+).*LUNCH.*skip',           "lunch_session"),
                (r'(\w+).*scan returned no signal', "no_signal"),
                (r'only \d+ market.hours bars',   "insufficient_data"),
            ]:
                m = re.search(pattern, line, re.I)
                if m:
                    sym = m.group(1) if (m.lastindex and m.lastindex >= 1) else "?"
                    current_scan["tickers"].setdefault(sym, {})
                    current_scan["tickers"][sym]["skip_reason"] = reason
                    break

            # Trade taken
            trade_match = re.search(r'(BUY|SELL|LONG|SHORT).*(\w{2,5}).*(\d+)\s+shares', line, re.I)
            if trade_match:
                trades.append({
                    "action":    trade_match.group(1).upper(),
                    "symbol":    trade_match.group(2).upper(),
                    "qty":       trade_match.group(3),
                    "timestamp": current_scan.get("time", ""),
                    "raw":       line[:120]
                })

            # Conviction lines without explicit format
            conv2 = re.search(r'(SPY|QQQ)[^\d]+(\d{2,3})\s*/\s*(\d{2,3})', line)
            if conv2:
                sym, conv, thr = conv2.groups()
                current_scan["tickers"].setdefault(sym, {})
                current_scan["tickers"][sym]["conviction"] = int(conv)
                current_scan["tickers"][sym]["threshold"]  = int(thr)

        if current_scan:
            scans.append(current_scan)

    except Exception as e:
        return {"error": str(e), "scans": [], "trades": []}

    # Get current ARKA status from summary
    status = "RUNNING"
    summary_files = sorted(_glob.glob(f"{base}/logs/arka/summary_*.json"), reverse=True)
    summary = {}
    if summary_files:
        try:
            import json as _j
            summary = _j.load(open(summary_files[0]))
        except Exception:
            pass

    return {
        "status":      status,
        "log_file":    os.path.basename(log_path),
        "scans":       scans[-n:],
        "trades":      trades[-20:],
        "total_scans": len(scans),
        "watching":    ["SPY", "QQQ"],
        "summary":     summary,
    }

_live_pnl_cache: dict = {}   # {"result": ..., "ts": float}
_LIVE_PNL_TTL = 10           # seconds

@app.get("/api/account/live-pnl")
async def get_live_pnl():
    """Pull live unrealized P&L directly from Alpaca positions."""
    import time as _time
    _cached = _live_pnl_cache.get("data")
    if _cached and (_time.time() - _cached["ts"]) < _LIVE_PNL_TTL:
        return _cached["result"]
    try:
        import httpx as _hx, os as _os
        headers = {
            "APCA-API-KEY-ID":     _os.getenv("ALPACA_API_KEY",""),
            "APCA-API-SECRET-KEY": _os.getenv("ALPACA_API_SECRET","") or _os.getenv("ALPACA_SECRET_KEY",""),
        }
        async with _hx.AsyncClient(timeout=8) as _ac:
            r    = await _ac.get("https://paper-api.alpaca.markets/v2/positions", headers=headers)
            acct_r = await _ac.get("https://paper-api.alpaca.markets/v2/account", headers=headers)
        positions = r.json() if r.status_code == 200 else []
        if not isinstance(positions, list): positions = []

        total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)

        acct        = acct_r.json()
        equity      = float(acct.get("equity", 0))
        last_equity = float(acct.get("last_equity", equity))
        daily_pnl   = equity - last_equity

        result = {
            "daily_pnl":          round(daily_pnl, 2),
            "unrealized_pl":      round(total_unrealized, 2),
            "equity":             round(equity, 2),
            "last_equity":        round(last_equity, 2),
            "positions_count":    len(positions),
            "positions":          [{
                "symbol":         p.get("symbol",""),
                "qty":            p.get("qty",""),
                "side":           p.get("side",""),
                "avg_entry_price": p.get("avg_entry_price",""),
                "current_price":  p.get("current_price",""),
                "unrealized_pl":  p.get("unrealized_pl",""),
                "unrealized_plpc": p.get("unrealized_plpc",""),
                "market_value":   p.get("market_value",""),
            } for p in positions],
        }
        _live_pnl_cache["data"] = {"result": result, "ts": _time.time()}
        return result
    except Exception as e:
        return {"error": str(e), "daily_pnl": 0}

@app.get("/api/arka/summary")
def get_arka_summary():
    """Return today's ARKA summary. Falls back to defaults if file not found yet."""
    import json as _json
    from datetime import date as _date
    today = _date.today().strftime("%Y-%m-%d")
    summary_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                f"logs/arka/summary_{today}.json")
    # Default response before market opens
    default = {
        "date": today,
        "trades": 0,
        "daily_pnl": 0.0,
        "losing_streak": 0,
        "win_rate": 0.0,
        "scan_history": [],
        "trade_log": [],
        "config": {}
    }
    if not os.path.exists(summary_file):
        # Try to build partial summary from today's log
        log_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                f"logs/arka/arka_{today}.log")
        if os.path.exists(log_file):
            scans = []
            try:
                with open(log_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                entry = _json.loads(line)
                                scans.append(entry)
                            except Exception:
                                pass
                default["scan_history"] = scans[-20:]  # last 20 scans
                default["_source"] = "live_log"
            except Exception:
                pass
        return default
    try:
        with open(summary_file) as f:
            data = _json.load(f)
        # Ensure all expected keys exist
        for k, v in default.items():
            if k not in data:
                data[k] = v
        return data
    except Exception as e:
        default["error"] = str(e)
        return default



@app.get("/api/options/contracts/picker")
async def options_contracts_picker(ticker: str, direction: str = "call", max_dte: int = 5):
    """Return top contracts for manual buy picker UI."""
    import httpx as _hx
    from datetime import date as _date, timedelta as _td
    ticker = ticker.upper()
    direction = direction.lower()
    try:
        _poly_key = os.getenv("POLYGON_API_KEY", "")
        _alp_headers = {
            "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET","") or os.getenv("ALPACA_SECRET_KEY",""),
        }
        exp_max = (_date.today() + _td(days=max(max_dte, 5))).isoformat()

        # Step 1 — get live price (async, non-blocking)
        async with _hx.AsyncClient(timeout=6) as _ac:
            px_resp = await _ac.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                params={"apiKey": _poly_key},
            )
        snap  = px_resp.json().get("ticker", {})
        price = float(snap.get("lastTrade", {}).get("p", 0) or snap.get("day", {}).get("c", 0) or 0)
        if not price:
            return {"success": False, "error": f"Could not get price for {ticker}"}

        # Strike range: calls → ATM to 5% OTM; puts → 5% OTM to ATM
        # Alpaca returns strikes sorted ascending so we must bound from ATM side
        if direction == "call":
            _strike_lo = str(round(price * 0.995, 0))   # just below ATM
            _strike_hi = str(round(price * 1.05, 0))    # 5% OTM
        else:
            _strike_lo = str(round(price * 0.95, 0))    # 5% OTM
            _strike_hi = str(round(price * 1.005, 0))   # just above ATM

        # Step 2 — Alpaca contracts + Polygon bulk snapshot in parallel (both async)
        async with _hx.AsyncClient(timeout=10) as _ac2:
            contracts_resp, bulk_resp = await asyncio.gather(
                _ac2.get(
                    "https://paper-api.alpaca.markets/v2/options/contracts",
                    headers=_alp_headers,
                    params={
                        "underlying_symbols":  ticker,
                        "type":                direction,
                        "expiration_date_gte": _date.today().isoformat(),
                        "expiration_date_lte": exp_max,
                        "strike_price_gte":    _strike_lo,
                        "strike_price_lte":    _strike_hi,
                        "limit":               20,
                    },
                ),
                _ac2.get(
                    f"https://api.polygon.io/v3/snapshot/options/{ticker}",
                    params={
                        "apiKey": _poly_key,
                        "expiration_date.gte": _date.today().isoformat(),
                        "expiration_date.lte": exp_max,
                        "contract_type": direction,
                        "strike_price.gte": _strike_lo,
                        "strike_price.lte": _strike_hi,
                        "limit": 50,
                    },
                ),
            )

        contracts = contracts_resp.json().get("option_contracts", [])
        if not contracts:
            return {"success": False, "error": f"No {direction} contracts found for {ticker}"}

        # Build bulk price map from Polygon snapshot
        _bulk_prices = {}
        try:
            for _r in bulk_resp.json().get("results", []):
                _sym = (_r.get("details") or {}).get("ticker", "")
                if _sym:
                    _bid = float((_r.get("last_quote") or {}).get("bid", 0) or 0)
                    _ask = float((_r.get("last_quote") or {}).get("ask", 0) or 0)
                    _day = float((_r.get("day") or {}).get("close", 0) or 0)
                    _bulk_prices[_sym] = {"bid": _bid, "ask": _ask, "day": _day}
        except Exception:
            pass

        enriched = []
        for c in contracts:
            sym    = c.get("symbol", "")
            strike = float(c.get("strike_price", 0))
            exp    = c.get("expiration_date", "")
            dte    = (_date.fromisoformat(exp) - _date.today()).days if exp else 0

            _px_data = _bulk_prices.get(sym, {})
            bid  = _px_data.get("bid", 0.0)
            ask  = _px_data.get("ask", 0.0)
            if bid > 0 and ask > 0:
                last_px = round((bid + ask) / 2, 2)
            else:
                last_px = _px_data.get("day", 0.0) or float(c.get("close_price", 0) or 0)

            otm_pct = round((strike - price) / price * 100, 2) if direction == "call" else round((price - strike) / price * 100, 2)
            enriched.append({
                "symbol":    sym,
                "strike":    strike,
                "expiry":    exp,
                "dte":       dte,
                "bid":       round(bid, 2),
                "ask":       round(ask, 2),
                "price":     last_px,
                "cost_1":    round(last_px * 100, 2),
                "otm_pct":   otm_pct,
                "direction": direction,
            })

        # Sort: by DTE asc, then by closeness to ATM
        enriched.sort(key=lambda x: (x["dte"], abs(x["otm_pct"])))
        return {"success": True, "contracts": enriched[:12], "spot": price, "ticker": ticker}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/swings/manual-entry")
async def manual_swing_entry(body: dict):
    """Manual swing entry — place options order with chosen contract + qty."""
    import httpx as _hx
    from datetime import date as _date, timedelta as _td
    ticker       = body.get("ticker","").upper()
    direction    = body.get("direction","call").lower()
    max_dte      = int(body.get("max_dte", 21))
    contract_sym = body.get("contract_sym", "")   # if pre-selected from picker
    qty          = max(1, min(5, int(body.get("qty", 1))))
    if not ticker:
        return {"success": False, "error": "ticker required"}
    try:
        headers = {
            "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET","") or os.getenv("ALPACA_SECRET_KEY",""),
        }

        # If no contract pre-selected, find ATM
        strike, exp_date = "", ""
        if not contract_sym:
            px_r = _hx.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                params={"apiKey": os.getenv("POLYGON_API_KEY","")}, timeout=5
            )
            snap  = px_r.json().get("ticker", {})
            price = float(snap.get("lastTrade", {}).get("p", 0) or snap.get("day", {}).get("c", 0) or 0)
            if not price:
                return {"success": False, "error": f"Could not get price for {ticker}"}

            exp_max = (_date.today() + _td(days=max_dte)).isoformat()
            c_r = _hx.get(
                "https://paper-api.alpaca.markets/v2/options/contracts",
                headers=headers,
                params={
                    "underlying_symbols":  ticker,
                    "type":                direction,
                    "expiration_date_gte": _date.today().isoformat(),
                    "expiration_date_lte": exp_max,
                    "strike_price_gte":    str(round(price * 0.95, 0)),
                    "strike_price_lte":    str(round(price * 1.05, 0)),
                    "limit":               10,
                }, timeout=8
            )
            contracts = c_r.json().get("option_contracts", [])
            if not contracts:
                return {"success": False, "error": f"No {direction} contracts found for {ticker} within {max_dte} DTE"}
            contracts.sort(key=lambda c: (
                c.get("expiration_date",""),
                abs(float(c.get("strike_price",0)) - price)
            ))
            contract_sym = contracts[0].get("symbol","")
            exp_date     = contracts[0].get("expiration_date","")
            strike       = contracts[0].get("strike_price","")

        # ── Options-only guard ────────────────────────────────────────────
        from backend.arka.order_guard import validate_options_order as _voo_api
        _ok, _why = _voo_api(contract_sym, qty, "buy")
        if not _ok:
            return {"success": False, "error": f"ORDER GUARD: {_why}"}

        # Place order (no asset_class field — Alpaca infers from OCC symbol)
        o_r = _hx.post(
            "https://paper-api.alpaca.markets/v2/orders",
            headers=headers,
            json={"symbol": contract_sym, "qty": str(qty), "side": "buy",
                  "type": "market", "time_in_force": "day"},
            timeout=8
        )
        if o_r.status_code in (200, 201):
            return {
                "success":   True,
                "contract":  contract_sym,
                "ticker":    ticker,
                "direction": direction,
                "strike":    strike,
                "expiry":    exp_date,
                "qty":       qty,
            }
        else:
            return {"success": False, "error": o_r.text[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/swings/watchlist")
async def get_swings_watchlist():
    """Return latest swing watchlist candidates with live prices refreshed from Polygon."""
    import httpx as _hx
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wl_path = os.path.join(base, "logs/chakra/watchlist_latest.json")
    try:
        if not os.path.exists(wl_path):
            return {"candidates": [], "count": 0, "message": "No watchlist data yet"}

        data = json.load(open(wl_path))
        candidates = data.get("candidates", data.get("postmarket_candidates", []))

        # Refresh prices live from Polygon
        if candidates:
            _key     = os.getenv("POLYGON_API_KEY", "")
            _tickers = ",".join(c["ticker"] for c in candidates if c.get("ticker"))
            try:
                async with _hx.AsyncClient(timeout=8) as _cl:
                    _r = await _cl.get(
                        "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
                        params={"apiKey": _key, "tickers": _tickers}
                    )
                    _snap = {}
                    for _t in _r.json().get("tickers", []):
                        _sym  = _t.get("ticker", "")
                        _day  = _t.get("day", {})
                        _prev = _t.get("prevDay", {})
                        _lt   = (_t.get("lastTrade") or {}).get("p", 0)
                        _p    = float(_lt or _day.get("c", 0) or _prev.get("c", 0) or 0)
                        _pc   = float(_prev.get("c", _p) or _p or 1)
                        _chg  = round((_p - _pc) / _pc * 100, 2) if _pc else 0
                        if _p > 0:
                            _snap[_sym] = {"price": round(_p, 2), "chg_pct": _chg}

                    for c in candidates:
                        _live = _snap.get(c.get("ticker", ""))
                        if _live:
                            c["price"]    = _live["price"]
                            c["chg_pct"]  = _live["chg_pct"]
                            c["mom5"]     = _live["chg_pct"]  # use today's % as momentum proxy
            except Exception:
                pass  # fall back to cached price if refresh fails

        return {
            "candidates":  candidates,
            "count":       len(candidates),
            "scan_time":   data.get("scan_time", ""),
            "mode":        data.get("mode", ""),
            "top5":        candidates[:5],
            "prices_live": True,
        }
    except Exception as e:
        return {"error": str(e), "candidates": []}


@app.get("/api/swings/positions")
async def get_swings_positions():
    """Return open and recent swing positions from arka_swings table.
    Auto-reconciles stale OPEN positions against live Alpaca data."""
    import sqlite3 as _sq, httpx as _hx, re as _re
    from datetime import date as _date
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(base, "logs/swings/swings_v3.db")
    if not os.path.exists(db_path):
        db_path = os.path.join(base, "logs/swings/swings.db")
    try:
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row

        # Determine which table has data
        _tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        _tbl = "arka_swings" if "arka_swings" in _tables else "swings"

        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({_tbl})")
        cols = [r[1] for r in cur.fetchall()]

        # ── Auto-reconcile: close DB positions not on Alpaca ─────────
        try:
            _headers = {
                "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
                "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET","") or os.getenv("ALPACA_SECRET_KEY",""),
            }
            async with _hx.AsyncClient(timeout=8) as _cl:
                _ar = await _cl.get("https://paper-api.alpaca.markets/v2/positions", headers=_headers)
                _alpaca_syms = set()
                if _ar.status_code == 200 and isinstance(_ar.json(), list):
                    for _ap in _ar.json():
                        _sym = _ap.get("symbol","")
                        # Extract underlying from options contract (e.g. NFLX260417C... → NFLX)
                        _m = _re.match(r'^([A-Z]{1,6})\d', _sym)
                        _alpaca_syms.add(_m.group(1) if _m else _sym)

                # Any DB OPEN position whose ticker isn't on Alpaca → stale, close it
                cur.execute(f"SELECT id, ticker, entry_price, qty FROM {_tbl} WHERE status='OPEN'")
                for _row in cur.fetchall():
                    _rid, _tk, _ep, _qty = _row
                    if _tk not in _alpaca_syms:
                        # Get current stock price for exit
                        _key = os.getenv("POLYGON_API_KEY","")
                        try:
                            _pr = await _cl.get(
                                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{_tk}",
                                params={"apiKey": _key}, timeout=4
                            )
                            _day = _pr.json().get("ticker",{}).get("day",{})
                            _exit = float(_day.get("c",0) or _ep)
                        except Exception:
                            _exit = float(_ep)
                        _pnl_pct = round((_exit - float(_ep)) / float(_ep) * 100, 2) if float(_ep) else 0
                        conn.execute(
                            f"UPDATE {_tbl} SET status='CLOSED', exit_price=?, exit_date=?, pnl_pct=? WHERE id=?",
                            (_exit, str(_date.today()), _pnl_pct, _rid)
                        )
                conn.commit()
        except Exception:
            pass  # reconcile is best-effort, don't fail the endpoint

        # Return positions
        cur.execute(f"SELECT * FROM {_tbl} WHERE status='OPEN' ORDER BY rowid DESC LIMIT 20")
        open_pos = [dict(zip(cols, row)) for row in cur.fetchall()]

        cur.execute(f"SELECT * FROM {_tbl} WHERE status!='OPEN' ORDER BY rowid DESC LIMIT 10")
        closed = [dict(zip(cols, row)) for row in cur.fetchall()]

        conn.close()
        return {
            "open":         open_pos,
            "closed":       closed,
            "open_count":   len(open_pos),
            "closed_count": len(closed),
        }
    except Exception as e:
        return {"error": str(e), "open": [], "closed": []}

@app.get("/api/arka/positions")
def get_arka_positions():
    """
    Return open positions from ARKA state.
    Reads from summary JSON — avoids AlpacaClient instantiation issues.
    Also enriches with live price data for P&L calculation.
    """
    import json, glob
    from datetime import date
    from pathlib import Path as _P

    try:
        # Load ARKA summary for trade log
        summary_path = f"logs/arka/summary_{date.today()}.json"
        summary = {}
        if _P(summary_path).exists():
            with open(summary_path) as f:
                summary = json.load(f)

        trade_log   = summary.get("trade_log", [])
        open_pos    = summary.get("open_positions", {})

        # Build positions from trade log — match BUY with no corresponding STOP/SELL
        positions = []
        for ticker, pos_data in open_pos.items():
            entry  = float(pos_data.get("entry",  0))
            stop   = float(pos_data.get("stop",   0))
            target = float(pos_data.get("target", 0))
            qty    = int(pos_data.get("qty",     0))
            direction = pos_data.get("direction", "LONG")
            trade_sym = pos_data.get("trade_sym", ticker)

            # Try to get live price — always use underlying ticker (not inverse ETF) for P&L
            live_price = entry  # fallback
            try:
                import httpx
                key = "rrJ5P3S52kvCzQzdQRim8qQZwTjqYhba"
                r = httpx.get(
                    f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                    params={"apiKey": key}, timeout=3
                )
                if r.status_code == 200:
                    snap = r.json().get("ticker", {})
                    live_price = float(snap.get("day", {}).get("c", entry) or entry)
            except Exception:
                pass

            # P&L calculation
            if direction in ("SHORT", "STRONG_SHORT"):
                # Short via PUT options — P&L tracked on underlying direction
                pnl = round((entry - live_price) * qty, 2)
            else:
                pnl = round((live_price - entry) * qty, 2)

            pnl_pct = round((live_price - entry) / entry * 100, 2) if entry else 0

            # ── Alpaca live positions ─────────────────────────────────────
        import httpx as _hx, os as _os, re as _re
        headers = {
            "APCA-API-KEY-ID":     _os.getenv("ALPACA_API_KEY",""),
            "APCA-API-SECRET-KEY": _os.getenv("ALPACA_API_SECRET","") or _os.getenv("ALPACA_SECRET_KEY",""),
        }
        r = _hx.get("https://paper-api.alpaca.markets/v2/positions",
                    headers=headers, timeout=8)
        alpaca_pos = r.json() if r.status_code == 200 else []
        if not isinstance(alpaca_pos, list): alpaca_pos = []

        positions = []
        for p in alpaca_pos:
            sym          = p.get("symbol","")
            qty          = int(float(p.get("qty", 1)))
            entry        = float(p.get("avg_entry_price", 0))
            current      = float(p.get("current_price", entry))
            unreal_pl    = float(p.get("unrealized_pl", 0))
            unreal_plpc  = float(p.get("unrealized_plpc", 0)) * 100
            asset_cls    = p.get("asset_class","us_equity")
            is_option    = asset_cls == "us_option"

            # Derive underlying ticker from options symbol
            m = _re.match(r"^([A-Z]+)\d", sym)
            underlying = m.group(1) if m else sym

            # Get ARKA stop/target from summary metadata
            meta   = open_pos.get(underlying, open_pos.get(sym, {}))
            stop   = float(meta.get("stop",      0))
            target = float(meta.get("target",    0))
            direction = meta.get("direction", "LONG")

            if is_option:
                is_call    = "C" in sym[len(underlying):]
                trade_type = "CALL" if is_call else "PUT"
                strategy   = "ARKA-SCALP"
            else:
                trade_type = "EQUITY"
                strategy   = "ARKA"

            positions.append({
                "ticker":        underlying,
                "contract":      sym if is_option else None,
                "trade_sym":     sym,
                "type":          trade_type,
                "action":        "BUY CALL" if (is_option and is_call) else "BUY PUT" if is_option else ("BUY" if direction in ("LONG","STRONG_LONG") else "SHORT"),
                "size":          qty,
                "entry":         round(entry,       2),
                "current_price": round(current,     2),
                "avg_exit":      None,
                "stop":          round(stop,        2),
                "target":        round(target,      2),
                "pnl":           round(unreal_pl,   2),
                "pnl_pct":       round(unreal_plpc, 2),
                "status":        "OPEN",
                "direction":     direction,
                "strategy":      strategy,
                "asset_class":   asset_cls,
            })

        return {"positions": positions, "count": len(positions)}

    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.get("/api/taraka/summary")
def get_taraka_summary():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        path  = f"logs/taraka/summary_{today}.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/taraka/alerts")
def get_taraka_alerts():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        path  = f"logs/taraka/alerts_{today}.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return []
    except Exception as e:
        return {"error": str(e)}


TARAKA_CHANNELS_PATH = "backend/taraka/taraka_channels.json"

@app.get("/api/taraka/channels")
def get_taraka_channels():
    try:
        with open(TARAKA_CHANNELS_PATH) as f:
            return json.load(f)
    except Exception:
        return {"chChannels": [], "watch_all": False, "active_ids": []}

@app.post("/api/taraka/channels")
def save_taraka_channels(payload: dict):
    try:
        channels   = payload.get("chChannels", [])
        active_ids = [c["id"] for c in channels if c.get("active", True)]
        config = {
            "chChannels":    channels,
            "watch_all":     payload.get("watch_all", False),
            "log_unmatched": payload.get("log_unmatched", True),
            "active_ids":    active_ids,
            "updated_at":    datetime.now().isoformat() + "Z",
        }
        with open(TARAKA_CHANNELS_PATH, "w") as f:
            json.dump(config, f, indent=2)
        return {"ok": True, "active_count": len(active_ids)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.delete("/api/taraka/channels/{channel_id}")
def delete_taraka_channel(channel_id: str):
    try:
        with open(TARAKA_CHANNELS_PATH) as f:
            config = json.load(f)
        config["chChannels"] = [c for c in config.get("chChannels", []) if c["id"] != channel_id]
        config["active_ids"] = [i for i in config.get("active_ids", []) if i != channel_id]
        config["updated_at"] = datetime.now().isoformat() + "Z"
        with open(TARAKA_CHANNELS_PATH, "w") as f:
            json.dump(config, f, indent=2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/taraka/analysts")
def get_taraka_analysts():
    try:
        path = "logs/taraka/analyst_stats.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}
    except Exception as e:
        return {"error": str(e)}

# ── OPTIONS ENDPOINTS ─────────────────────────────────────────────────────────

@app.get("/api/options/ticker-licker")
def get_ticker_licker():
    try:
        path = "logs/options/ticker_licker_latest.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {"plays": [], "calls": [], "puts": [], "total": 0, "time": None}
    except Exception as e:
        return {"error": str(e), "plays": []}

@app.get("/api/options/gex/range-levels")
def get_gex_range_levels(ticker: str = "SPX"):
    """
    Lightweight endpoint — reads gex_latest_{ticker}.json written by gex_calculator.
    Returns call_wall, put_wall, zero_gamma, regime, regime_call, cliff_today,
    bias_ratio, upper_1sd, lower_1sd, spot, accel_up, accel_down.
    TTL: 10 minutes (returns stale=true if file older than 10 min).
    """
    import time as _t, json as _j
    from pathlib import Path as _P
    _ticker = ticker.upper()
    _path   = _P(f"logs/gex/gex_latest_{_ticker}.json")
    if not _path.exists():
        return {"ticker": _ticker, "error": "no_data", "stale": True}
    try:
        d   = _j.loads(_path.read_text())
        age = _t.time() - d.get("ts", 0)
        cliff = d.get("cliff", {})
        return {
            "ticker":       _ticker,
            "spot":         d.get("spot", 0),
            "call_wall":    d.get("call_wall", 0),
            "put_wall":     d.get("put_wall", 0),
            "zero_gamma":   d.get("zero_gamma", 0),
            "net_gex":      d.get("net_gex", 0),
            "regime":       d.get("regime", "UNKNOWN"),
            "regime_call":  d.get("regime_call", "NEUTRAL"),
            "bias_ratio":   d.get("bias_ratio", 1.0),
            "dominant_side":d.get("dominant_side", "NEUTRAL"),
            "cliff_today":  cliff.get("expires_today", False),
            "cliff_strike": cliff.get("strike"),
            "above_zero_gamma": d.get("above_zero_gamma", False),
            "upper_1sd":    d.get("upper_1sd", 0),
            "lower_1sd":    d.get("lower_1sd", 0),
            "expected_move_pts": d.get("expected_move_pts", 0),
            "accel_up":     d.get("accel_up", 0),
            "accel_down":   d.get("accel_down", 0),
            "age_seconds":  round(age),
            "stale":        age > 600,
        }
    except Exception as e:
        return {"ticker": _ticker, "error": str(e), "stale": True}


@app.get("/api/options/gex/heatmap")
def get_gex_heatmap(ticker: str = "SPX"):
    """
    Return GEX heatmap data for a ticker.
    Reads from logs/arka/gex_heatmap_{date}.json (legacy SPX heatmap)
    or logs/gex/gex_latest_{ticker}.json for per-ticker data.
    """
    import time as _t, json as _j
    from pathlib import Path as _P
    from datetime import date as _d
    _ticker = ticker.upper()

    # Try new per-ticker gex_latest first (has full Phase 7 data)
    _latest = _P(f"logs/gex/gex_latest_{_ticker}.json")
    if _latest.exists():
        try:
            d   = _j.loads(_latest.read_text())
            age = _t.time() - d.get("ts", 0)
            cliff = d.get("cliff", {})
            nearby = d.get("nearby_strikes", [])
            return {
                "ticker":          _ticker,
                "spot":            d.get("spot", 0),
                "net_gex":         d.get("net_gex", 0),
                "regime":          d.get("regime", "UNKNOWN"),
                "regime_call":     d.get("regime_call", "NEUTRAL"),
                "call_wall":       d.get("call_wall", 0),
                "put_wall":        d.get("put_wall", 0),
                "zero_gamma":      d.get("zero_gamma", 0),
                "above_zero_gamma":d.get("above_zero_gamma", False),
                "bias_ratio":      d.get("bias_ratio", 1.0),
                "dominant_side":   d.get("dominant_side", "NEUTRAL"),
                "call_gex_dollars":d.get("call_gex_dollars", 0),
                "put_gex_dollars": d.get("put_gex_dollars", 0),
                "iv_skew":         d.get("iv_skew", 0),
                "upper_1sd":       d.get("upper_1sd", 0),
                "lower_1sd":       d.get("lower_1sd", 0),
                "expected_move_pts": d.get("expected_move_pts", 0),
                "accel_up":        d.get("accel_up", 0),
                "accel_down":      d.get("accel_down", 0),
                "cliff_today":     cliff.get("expires_today", False),
                "cliff_strike":    cliff.get("strike"),
                "pin_strikes":     d.get("pin_strikes", []),
                "nearby_strikes":  nearby,
                "age_seconds":     round(age),
                "stale":           age > 600,
                "source":          "gex_latest",
            }
        except Exception as e:
            pass

    # Legacy fallback — today's SPX heatmap file
    _today = _d.today().isoformat()
    _hm    = _P(f"logs/arka/gex_heatmap_{_today}.json")
    if not _hm.exists():
        # Try most recent heatmap
        _files = sorted(_P("logs/arka").glob("gex_heatmap_*.json"), reverse=True)
        _hm = _files[0] if _files else None
    if _hm and _hm.exists():
        try:
            d = _j.loads(_hm.read_text())
            return {**d, "ticker": _ticker, "stale": True, "source": "legacy_heatmap"}
        except Exception:
            pass

    return {"ticker": _ticker, "error": "no_data", "stale": True}


@app.get("/api/weekly/brief")
async def weekly_brief_endpoint():
    """Return ARJUN weekly macro brief — GEX regime, internals, sectors, signals."""
    try:
        from backend.arjun.weekly_brief import generate_brief
        return generate_brief()
    except Exception as e:
        return {"error": str(e), "generated_at": None}


@app.get("/api/oi/delta")
async def oi_delta_endpoint(ticker: str = ""):
    """Return OI delta state for dashboard (intraday OI change per ticker)."""
    try:
        from backend.chakra.oi_tracker import get_oi_state_for_dashboard
        tickers = [ticker.upper()] if ticker else None
        return {"results": get_oi_state_for_dashboard(tickers)}
    except Exception as e:
        return {"results": {}, "error": str(e)}


@app.get("/api/sectors/rotation")
async def sectors_rotation_endpoint():
    """Return sector rotation snapshot with direction and change %."""
    try:
        from backend.chakra.sector_rotation import get_sector_state_for_dashboard
        return {"results": get_sector_state_for_dashboard()}
    except Exception as e:
        return {"results": {}, "error": str(e)}


@app.get("/api/moc/imbalance")
async def moc_imbalance_endpoint(ticker: str = ""):
    """Return MOC imbalance state for index tickers (active 3:00–3:58 PM ET)."""
    try:
        from backend.chakra.moc_imbalance import get_moc_state_for_dashboard, MOC_SUPPORTED_TICKERS
        if ticker:
            tickers = [ticker.upper()]
        else:
            tickers = sorted(MOC_SUPPORTED_TICKERS)
        results = get_moc_state_for_dashboard(tickers)
        return {"results": results, "count": len(results)}
    except Exception as e:
        return {"results": [], "error": str(e)}


@app.get("/api/arka/universe")
def get_arka_universe():
    """Return the current dynamic scan universe — what ARKA is watching right now."""
    try:
        from backend.arka.dynamic_universe import get_universe_summary
        return get_universe_summary()
    except Exception as e:
        return {"error": str(e), "tickers": ["SPY", "QQQ", "SPX"]}


@app.get("/api/options/gex/intraday")
def get_gex_intraday(ticker: str = "SPY"):
    """Return today's GEX intraday timeline for a ticker.
    Written by gex_calculator.snapshot_gex_intraday() after each compute.
    Returns array of {ts, datetime, zero_gamma, call_wall, put_wall, net_gex, regime, spot}.
    """
    from pathlib import Path as _P
    from datetime import date as _date
    import json as _j
    today  = _date.today().isoformat()
    path   = _P(f"logs/gex/gex_intraday_{ticker.upper()}_{today}.json")
    if not path.exists():
        return {"ticker": ticker.upper(), "date": today, "data": [], "count": 0}
    try:
        data = _j.loads(path.read_text())
        return {"ticker": ticker.upper(), "date": today, "data": data, "count": len(data)}
    except Exception as e:
        return {"ticker": ticker.upper(), "date": today, "data": [], "error": str(e)}


@app.get("/api/options/gex")
def get_gex(ticker: str = "SPX"):
    """Return GEX data for a specific ticker. Prioritises gex_latest_{ticker}.json."""
    import glob, json as _gj, time as _time
    from datetime import date as _gd, datetime as _gdt
    from pathlib import Path as _gp
    _today = _gd.today().isoformat()
    _t_up  = ticker.upper()

    # ── PRIORITY 1: gex_latest_{ticker}.json — freshest, written by gex_calculator ──
    _latest_path = _gp(f"logs/gex/gex_latest_{_t_up}.json")
    if _latest_path.exists():
        try:
            _ld = _gj.loads(_latest_path.read_text())
            _ts = float(_ld.get("ts", 0))
            # Accept if written within last 24h (covers after-hours reads of today's data)
            if _time.time() - _ts < 86400:
                _spot = float(_ld.get("spot") or 0)
                _updated_str = _gdt.fromtimestamp(_ts).strftime("%I:%M %p ET") if _ts else _today

                # ── Merge strike ladder from legacy options file when gex_latest has none ──
                # The legacy gex_{date}.json has full top_strikes for SPY/QQQ/IWM/DIA.
                # After gex_calculator runs once with the new code, gex_latest will have its own.
                _top_strikes = _ld.get("top_strikes", [])
                _bsl, _ssl, _eqh, _eql, _max_pain = [], [], [], [], 0
                if not _top_strikes and _t_up in ("SPY","QQQ","IWM","DIA"):
                    try:
                        _leg_path = _gp(f"logs/options/gex_{_today}.json")
                        if not _leg_path.exists():
                            import glob as _gl2
                            _lf = sorted(_gl2.glob("logs/options/gex_*.json"), reverse=True)
                            if _lf: _leg_path = _gp(_lf[0])
                        if _leg_path.exists():
                            _leg = _gj.loads(_leg_path.read_text())
                            _ltd = _leg.get("tickers", {}).get(_t_up, {})
                            _lgex = _ltd.get("gex", {})
                            _lmag = _ltd.get("magnets", {})
                            _raw  = _lgex.get("top_strikes", [])
                            _top_strikes = sorted([{
                                "strike":   float(s.get("strike", 0)),
                                "call_gex": float(s.get("call_gex", 0)),
                                "put_gex":  float(s.get("put_gex", 0)),
                                "net_gex":  float(s.get("net_gex", 0)),
                                "oi":       int(s.get("oi") or 0),
                            } for s in _raw], key=lambda x: x["strike"], reverse=True)
                            _bsl      = _lmag.get("bsl", [])
                            _ssl      = _lmag.get("ssl", [])
                            _eqh      = _lmag.get("eqh", [])
                            _eql      = _lmag.get("eql", [])
                            _max_pain = _lmag.get("max_pain", 0)
                    except Exception:
                        pass

                return {
                    "ticker":            _t_up,
                    "spot":              _spot,
                    "call_wall":         _ld.get("call_wall", 0),
                    "put_wall":          _ld.get("put_wall",  0),
                    "zero_gamma":        _ld.get("zero_gamma", 0),
                    "net_gex":           round(float(_ld.get("net_gex", 0)), 3),
                    "net_total_gex":     round(float(_ld.get("net_gex", 0)) * 1e9, 0),
                    "regime":            _ld.get("regime", "LOW_VOL"),
                    "regime_call":       _ld.get("regime_call"),
                    "above_zero_gamma":  _ld.get("above_zero_gamma"),
                    "bias_ratio":        _ld.get("bias_ratio", 0),
                    "call_gex_dollars":  _ld.get("call_gex_dollars", 0),
                    "put_gex_dollars":   _ld.get("put_gex_dollars",  0),
                    "accel_up":          _ld.get("accel_up",   0),
                    "accel_down":        _ld.get("accel_down", 0),
                    "expected_move_pts": _ld.get("expected_move_pts", 0),
                    "upper_1sd":         _ld.get("upper_1sd", 0),
                    "lower_1sd":         _ld.get("lower_1sd", 0),
                    "pin_strikes":       _ld.get("pin_strikes", []),
                    "cliff":             _ld.get("cliff", {}),
                    "max_pain":          _max_pain,
                    "bsl":               _bsl,
                    "ssl":               _ssl,
                    "eqh":               _eqh,
                    "eql":               _eql,
                    "ladder":            _top_strikes,
                    "top_strikes":       _top_strikes,
                    "nearby_strikes":    _top_strikes,
                    "second_call":       _ld.get("second_call", 0),
                    "second_put":        _ld.get("second_put",  0),
                    "updated":           _updated_str,
                    "source":            "gex_latest",
                }
        except Exception:
            pass

    try:
        # Use today's file or fall back to most recent available
        _path = _gp(f"logs/options/gex_{_today}.json")
        if not _path.exists():
            import glob as _gl
            _files = sorted(_gl.glob("logs/options/gex_*.json"), reverse=True)
            if _files:
                _path = _gp(_files[0])
        if _path.exists() and _t_up in ["SPY","QQQ","IWM","DIA"]:
            _data = _gj.loads(_path.read_text())
            _td   = _data.get("tickers",{}).get(ticker.upper(),{})
            if _td:
                _gex  = _td.get("gex",{})
                _mag  = _td.get("magnets",{})
                _net  = float(_gex.get("net_gex",0))
                _spot = float(_td.get("spot",0) or _gex.get("spot",0))
                _strikes = _gex.get("top_strikes", [])
                # Normalize + sort by price for strike ladder
                _norm_strikes = []
                for _s in _strikes:
                    _norm_strikes.append({
                        "strike":   float(_s.get("strike", 0)),
                        "call_gex": float(_s.get("call_gex", 0)),
                        "put_gex":  float(_s.get("put_gex", 0)),
                        "net_gex":  float(_s.get("net_gex", 0)),
                        "oi":       int(_s.get("oi") or (_s.get("call_oi",0) + _s.get("put_oi",0)) or 0),
                    })
                # Sort by strike descending for ladder (highest at top like the chart)
                _ladder = sorted(_norm_strikes, key=lambda x: x["strike"], reverse=True)
                # Derive regime_call from regime
                _regime = _gex.get("regime","unknown")
                if _regime in ("positive","pinned","positive_gamma"):
                    _regime_call = "SHORT_THE_POPS"
                elif _regime in ("negative","negative_gamma"):
                    _regime_call = "FOLLOW_MOMENTUM"
                else:
                    _regime_call = "NEUTRAL"
                return {
                    "ticker":         ticker.upper(),
                    "spot":           _spot,
                    "call_wall":      _gex.get("call_wall",0),
                    "put_wall":       _gex.get("put_wall",0),
                    "zero_gamma":     _gex.get("zero_gamma",0),
                    "net_gex":        round(_net/1e9,3),
                    "net_total_gex":  _net,
                    "regime":         _regime,
                    "regime_call":    _regime_call,
                    "max_pain":       _mag.get("max_pain",0),
                    "bsl":            _mag.get("bsl",[]),
                    "ssl":            _mag.get("ssl",[]),
                    "eqh":            _mag.get("eqh",[]),
                    "eql":            _mag.get("eql",[]),
                    "ladder":         _ladder,
                    "top_strikes":    _norm_strikes,
                    "nearby_strikes": _norm_strikes,
                    "updated":        _td.get("updated",_today),
                    "source":         "options_gex",
                }
    except Exception: pass
    import glob
    try:
        # SPX — use live heatmap file, normalize field names
        if ticker.upper() == "SPX":
            files = sorted(glob.glob("logs/arka/gex_heatmap_*.json"), reverse=True)
            if files:
                with open(files[0]) as f:
                    d = json.load(f)
                # Normalize: heatmap uses top_call_wall / top_put_wall
                # dashboard expects call_wall / put_wall
                d["ticker"]     = "SPX"
                d["source"]     = "heatmap"
                d["call_wall"]  = d.get("top_call_wall", d.get("call_wall", 0))
                d["put_wall"]   = d.get("top_put_wall",  d.get("put_wall",  0))
                d["second_call"]= d.get("second_call_wall", 0)
                d["second_put"] = d.get("second_put_wall",  0)
                d["zero_gamma"] = d.get("zero_gamma", d.get("spx_price", 0))
                # Staleness check — if heatmap is from a previous day, fetch fresh spot from Polygon
                import os as _os
                from datetime import date as _date
                heatmap_date = files[0].split("gex_heatmap_")[-1].replace(".json","")
                if heatmap_date != _date.today().isoformat():
                    try:
                        import httpx as _httpx
                        _pk = _os.getenv("POLYGON_API_KEY","")
                        _r  = _httpx.get(f"https://api.polygon.io/v2/aggs/ticker/SPY/prev",
                                         params={"apiKey":_pk,"adjusted":"true"}, timeout=5)
                        _res = _r.json().get("results",[])
                        if _res:
                            spy_close = float(_res[0].get("c", 0))
                            if spy_close > 0:
                                d["spx_price"] = round(spy_close * 10.04, 2)
                    except:
                        pass
                d["spx_price"]  = d.get("spx_price", d.get("atm_strike", 0))
                net = float(d.get("net_total_gex", 0))
                d["net_gex"]    = round(net / 1e9, 2)
                cw = d["call_wall"]; pw = d["put_wall"]
                sp = d["spx_price"]
                d["room_to_call"] = round(cw - sp, 2) if cw else 0
                d["room_to_put"]  = round(sp - pw, 2) if pw else 0
                d["regime"]       = d.get("regime", "LOW_VOL")
                d["bearish_bias"] = net < 0
                d["bullish_bias"] = net > 0
                d["contracts_used"] = len(d.get("nearby_strikes", []))
                return d
            return {"ticker": "SPX", "error": "No heatmap yet — runs during market hours"}

        # SPY/QQQ/IWM/XLK/XLF — use options engine gex file
        t = ticker.upper()
        today = datetime.now().strftime("%Y-%m-%d")
        # Try today first, fall back to most recent
        candidates = [f"logs/options/gex_{today}.json"] + sorted(glob.glob("logs/options/gex_*.json"), reverse=True)
        for path in candidates:
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                tickers = data.get("tickers", {})
                if t in tickers:
                    td = tickers[t]
                    gex = td.get("gex", {})
                    # Phase2: fetch live spot price instead of stale cache
                    _live_spot = 0.0
                    try:
                        import httpx as _hx2
                        _sp = _hx2.get(
                            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{t}",
                            params={"apiKey": os.getenv("POLYGON_API_KEY","")}, timeout=4
                        ).json()
                        _day = _sp.get("ticker",{}).get("day",{})
                        _prev = _sp.get("ticker",{}).get("prevDay",{})
                        _live_spot = (float(_sp.get("ticker",{}).get("lastTrade",{}).get("p",0) or
                                           _day.get("c",0) or _prev.get("c",0) or 0))
                    except Exception:
                        pass
                    # Phase2: live spot price
                    try:
                        import httpx as _ghx
                        _gs = _ghx.get(f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{t}',
                            params={'apiKey': os.getenv('POLYGON_API_KEY','')}, timeout=4).json()
                        _gd = _gs.get('ticker',{}).get('day',{})
                        _gp = _gs.get('ticker',{}).get('prevDay',{})
                        _live = float(_gs.get('ticker',{}).get('lastTrade',{}).get('p',0) or _gd.get('c',0) or _gp.get('c',0) or 0)
                        # Phase2: live spot price
                        try:
                            import httpx as _ghx
                            _gs = _ghx.get(f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{t}',
                                params={'apiKey': os.getenv('POLYGON_API_KEY','')}, timeout=4).json()
                            _gd = _gs.get('ticker',{}).get('day',{})
                            _gp = _gs.get('ticker',{}).get('prevDay',{})
                            _live = float(_gs.get('ticker',{}).get('lastTrade',{}).get('p',0) or _gd.get('c',0) or _gp.get('c',0) or 0)
                            spot = _live or _live or _live_spot or td.get("spot", gex.get("spot", 0))
                        except Exception:
                            spot = _live or _live_spot or td.get("spot", gex.get("spot", 0))
                    except Exception:
                        spot = _live_spot or td.get("spot", gex.get("spot", 0))
                    cw = gex.get("call_wall", 0)
                    pw = gex.get("put_wall", 0)
                    zg = gex.get("zero_gamma", spot)
                    # top_strikes lives inside gex{}, not at td level
                    top = gex.get("top_strikes", td.get("top_strikes", []))
                    net = gex.get("net_gex", sum(s.get("net_gex", 0) for s in top))
                    net_b = round(net / 1e9, 2)
                    regime = "NEGATIVE_GAMMA" if net < 0 else "LOW_VOL" if abs(net_b) < 1 else "POSITIVE_GAMMA"
                    # Build enriched nearby_strikes matching heatmap format
                    enriched = []
                    for s in top[:30]:
                        cg = s.get("call_gex", s.get("net_gex", 0) if s.get("net_gex",0) > 0 else 0)
                        pg = s.get("put_gex",  abs(s.get("net_gex", 0)) if s.get("net_gex",0) < 0 else 0)
                        enriched.append({
                            "strike":     s["strike"],
                            "call_gex":   cg,
                            "put_gex":    pg,
                            "net_gex":    s.get("net_gex", cg - pg),
                            "call_oi":    s.get("oi", 0),
                            "put_oi":     0,
                            "call_iv":    s.get("iv", 0),
                            "put_iv":     0,
                            "call_delta": s.get("delta", 0),
                            "put_delta":  0,
                        })
                    # Add magnets from options engine
                    magnets = td.get("magnets", [])
                    return {
                        "ticker":        t,
                        "spx_price":     spot,
                        "atm_strike":    round(spot),
                        "call_wall":     cw,
                        "put_wall":      pw,
                        "zero_gamma":    zg,
                        "top_call_wall": cw,
                        "top_put_wall":  pw,
                        "second_call":   gex.get("second_call", 0),
                        "second_put":    gex.get("second_put",  0),
                        "second_call_wall": gex.get("second_call", 0),
                        "second_put_wall":  gex.get("second_put",  0),
                        "net_total_gex": net,
                        "net_gex":       net_b,
                        "call_gex":      round(gex.get("total_call_gex", 0) / 1e9, 2),
                        "put_gex":       round(gex.get("total_put_gex",  0) / 1e9, 2),
                        "regime":        regime,
                        "room_to_call":  round(cw - spot, 2) if cw else 0,
                        "room_to_put":   round(spot - pw, 2) if pw else 0,
                        "nearby_strikes": enriched,
                        "magnets":       magnets,
                        "iv_skew":       td.get("iv_skew", gex.get("iv_skew", 0)),
                        "bearish_bias":  net < 0,
                        "bullish_bias":  net > 0,
                        "contracts_used": len(top),
                        "source":        "options_engine",
                        "date":          data.get("date", today),
                    }
        # Fallback: compute live GEX from Polygon options snapshot for any stock
        try:
            import httpx as _hx3
            _key = os.getenv("POLYGON_API_KEY","")
            # Get spot price
            _snap = _hx3.get(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{t}",
                params={"apiKey": _key}, timeout=5).json()
            _tk = _snap.get("ticker", {})
            _day = _tk.get("day", {}); _prev = _tk.get("prevDay", {})
            spot = float(_tk.get("lastTrade",{}).get("p",0) or _day.get("c",0) or _prev.get("c",0) or 0)
            chg_pct = float(_tk.get("todaysChangePerc", 0) or 0)

            # Get options snapshot
            _opts = _hx3.get(f"https://api.polygon.io/v3/snapshot/options/{t}",
                params={"apiKey": _key, "limit": 250,
                        "expiration_date.gte": datetime.now().strftime("%Y-%m-%d"),
                        "expiration_date.lte": (datetime.now() + __import__('datetime').timedelta(days=30)).strftime("%Y-%m-%d")},
                timeout=15).json()
            contracts = _opts.get("results", [])

            if not contracts or spot == 0:
                return {"ticker": t, "spot": spot, "error": "No options data available",
                        "chg_pct": chg_pct, "source": "live_fallback"}

            # Calculate basic GEX
            call_gex_by_strike = {}; put_gex_by_strike = {}
            total_call_gex = 0; total_put_gex = 0
            for c in contracts:
                g = c.get("greeks", {}); gamma = g.get("gamma", 0) or 0
                oi = int(c.get("open_interest", 0) or 0)
                det = c.get("details", {})
                strike = float(det.get("strike_price", 0) or 0)
                ct = det.get("contract_type", "").lower()
                if not gamma or not strike: continue
                gex_usd = gamma * oi * 100 * (spot**2) / 100 if spot > 0 else 0
                if ct == "call":
                    call_gex_by_strike[strike] = call_gex_by_strike.get(strike, 0) + gex_usd
                    total_call_gex += gex_usd
                else:
                    put_gex_by_strike[strike] = put_gex_by_strike.get(strike, 0) - gex_usd
                    total_put_gex -= gex_usd

            net_gex = total_call_gex + total_put_gex
            all_strikes = {**call_gex_by_strike}
            for k, v in put_gex_by_strike.items():
                all_strikes[k] = all_strikes.get(k, 0) + v

            above = {k: v for k, v in all_strikes.items() if k > spot}
            below = {k: v for k, v in all_strikes.items() if k < spot}
            call_wall = max(above, key=above.get) if above else 0
            put_wall  = min(below, key=below.get) if below else 0
            regime = "NEGATIVE_GAMMA" if net_gex < 0 else "LOW_VOL" if abs(net_gex) < 1e8 else "pinned"

            # Nearby strikes
            nearby = sorted(all_strikes.items(), key=lambda x: abs(x[0]-spot))[:10]
            nearby_strikes = [{"strike": k, "net_gex": round(v/1e6,2), "call_gex": round(call_gex_by_strike.get(k,0)/1e6,2),
                               "put_gex": round(put_gex_by_strike.get(k,0)/1e6,2)} for k,v in nearby]

            return {
                "ticker": t, "spot": spot, "chg_pct": round(chg_pct,3),
                "call_wall": call_wall, "put_wall": put_wall,
                "zero_gamma": call_wall,
                "net_gex": round(net_gex/1e9, 3),
                "net_total_gex": net_gex,
                "regime": regime,
                "bearish_bias": net_gex < 0,
                "bullish_bias": net_gex > 0,
                "iv_skew": 0,
                "nearby_strikes": nearby_strikes,
                "contracts_used": len(contracts),
                "source": "live_polygon",
                "room_to_call": round(call_wall - spot, 2) if call_wall else 0,
                "room_to_put":  round(spot - put_wall, 2) if put_wall else 0,
            }
        except Exception as _e2:
            return {"ticker": t, "error": f"No GEX data for {t}: {_e2}"}
    except Exception as e:
        return {"error": str(e), "ticker": ticker}

@app.get("/api/options/bell-prep")
def get_bell_prep():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        path  = f"logs/options/bell_prep_{today}.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {"tickers": {}, "bullish": [], "bearish": [], "flips": []}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/internals")
def get_internals():
    try:
        path = "logs/internals/internals_latest.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/internals/pulse-detail")
def get_pulse_detail(ticker: str = "SPY"):
    """
    Return Neural Pulse sub-components for a specific index.
    Shows what's driving the pulse score: VIX, breadth, sectors, bonds, volume.
    """
    import json as _j, os as _os, httpx as _hx
    base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    key  = _os.getenv("POLYGON_API_KEY","")

    # Load internals cache
    internals_file = _os.path.join(base, "logs/internals/internals_latest.json")
    internals = {}
    if _os.path.exists(internals_file):
        try:
            internals = _j.load(open(internals_file))
        except Exception:
            pass

    # Get live price for selected ticker
    live_price = 0.0
    chg_pct    = 0.0
    try:
        r = _hx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": key}, timeout=5
        ).json()
        t_data = r.get("ticker", {})
        day    = t_data.get("day", {})
        prev   = t_data.get("prevDay", {})
        lp     = float(t_data.get("lastTrade",{}).get("p",0) or day.get("c",0) or prev.get("c",0) or 0)
        pc     = float(prev.get("c",1) or 1)
        live_price = lp
        chg_pct    = round((lp - pc) / pc * 100, 3) if pc else 0
    except Exception:
        pass

    # Extract pulse sub-components from internals
    pulse = internals.get("neural_pulse", {})
    risk  = internals.get("risk", {})
    vix   = internals.get("vix", {})
    bond  = internals.get("bond_stress", {})
    herd  = internals.get("herding", {})
    arka  = internals.get("arka_mod", {})

    return {
        "ticker":       ticker,
        "live_price":   live_price,
        "chg_pct":      chg_pct,
        "pulse_score":  pulse.get("score", 0),
        "pulse_label":  pulse.get("label", "UNKNOWN"),
        "pulse_trending": pulse.get("trending", "FLAT"),
        "components": {
            "vix_regime":    vix.get("classification", {}).get("regime", "?"),
            "risk_mode":     risk.get("mode", "?"),
            "risk_score":    risk.get("score", 0),
            "bond_stress":   bond.get("stress", "?"),
            "bond_regime":   bond.get("regime", "?"),
            "herding_regime":herd.get("regime", "?"),
            "herding_score": herd.get("score_pct", 0),
            "arka_modifier": arka.get("modifier", 0),
            "arka_reasons":  arka.get("reasons", []),
        },
        "index_last":   internals.get("index_last", {}),
        "available_tickers": ["SPY", "QQQ", "IWM", "DIA"],
    }

@app.get("/api/options/heatmap")
def get_options_heatmap():
    """Live GEX heatmap — saved by arka_options_engine every scan."""
    import glob
    files = sorted(glob.glob("logs/arka/gex_heatmap_*.json"), reverse=True)
    if not files:
        return {"error": "no heatmap data yet — runs during market hours"}
    try:
        with open(files[0]) as f:
            return json.load(f)
    except:
        return {"error": "could not read heatmap"}

@app.get("/api/options/spx-levels")
def get_spx_levels():
    """Key SPX levels from latest GEX heatmap."""
    import glob
    files = sorted(glob.glob("logs/arka/gex_heatmap_*.json"), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0]) as f:
            h = json.load(f)
        return {
            "spx_price":       h.get("spx_price", 0),
            "call_wall":       h.get("top_call_wall", 0),
            "put_wall":        h.get("top_put_wall", 0),
            "second_call":     h.get("second_call_wall", 0),
            "second_put":      h.get("second_put_wall", 0),
            "regime":          h.get("regime", "UNKNOWN"),
            "bullish_bias":    h.get("bullish_bias", False),
            "bearish_bias":    h.get("bearish_bias", False),
            "iv_skew":         h.get("iv_skew", 0),
            "room_to_call":    h.get("room_to_call_wall", 0),
            "room_to_put":     h.get("room_to_put_wall", 0),
            "updated":         h.get("timestamp", ""),
        }
    except:
        return {}



@app.get("/api/arka/performance")
def get_arka_performance():
    """Return paired trade performance from Alpaca — one row per complete round-trip."""
    import httpx as _hx, os as _os, re as _re
    from datetime import datetime as _dt
    from collections import defaultdict
    try:
        headers = {
            "APCA-API-KEY-ID":     _os.getenv("ALPACA_API_KEY",""),
            "APCA-API-SECRET-KEY": _os.getenv("ALPACA_API_SECRET","") or _os.getenv("ALPACA_SECRET_KEY",""),
        }
        r = _hx.get(
            "https://paper-api.alpaca.markets/v2/orders",
            headers=headers,
            params={"status": "closed", "limit": 500, "direction": "desc"},
            timeout=10
        )
        orders = r.json() if r.status_code == 200 else []
        if not isinstance(orders, list): orders = []

        # Sort oldest first for correct FIFO matching
        orders_sorted = sorted(orders, key=lambda o: o.get("filled_at") or o.get("created_at",""))

        _INDICES = {"SPY", "QQQ", "IWM", "DIA", "SPX"}

        def _parse(o):
            filled_at  = o.get("filled_at") or o.get("created_at","")
            filled_qty = float(o.get("filled_qty", 0))
            if not filled_at or filled_qty == 0: return None
            price     = float(o.get("filled_avg_price") or 0)
            sym       = o.get("symbol","")
            side      = o.get("side","buy").upper()
            is_option = o.get("asset_class","") == "us_option"
            try:
                dt_obj   = _dt.fromisoformat(filled_at.replace("Z","+00:00"))
                date_str = dt_obj.strftime("%Y-%m-%d")
                time_str = dt_obj.strftime("%H:%M")
            except Exception:
                date_str = filled_at[:10]; time_str = filled_at[11:16]
            m          = _re.match(r"^([A-Z]+)\d", sym)
            underlying = m.group(1) if m else sym
            if is_option:
                ctype = "CALL" if _re.search(r'\d{6}C\d', sym) else "PUT"
            else:
                ctype = "EQUITY"
            # Determine DTE-based category for options
            if is_option:
                _m2 = _re.match(r'^[A-Z]+(\d{2})(\d{2})(\d{2})[CP]\d+$', sym)
                if _m2:
                    try:
                        from datetime import date as _d2
                        _exp = _d2(2000+int(_m2.group(1)), int(_m2.group(2)), int(_m2.group(3)))
                        _dte = (_exp - _d2.fromisoformat(date_str)).days
                    except Exception:
                        _dte = 0
                else:
                    _dte = 0
                _INDEX_TICKERS = {"SPY","QQQ","SPX","IWM","DIA","RUT"}
                if _dte == 0:
                    category = "INDEX-0DTE" if underlying in _INDEX_TICKERS else "0DTE"
                elif _dte <= 7:
                    category = "INDEX-SCALP" if underlying in _INDEX_TICKERS else "SHORT-SWING"
                elif _dte <= 21:
                    category = "SWING"
                elif underlying in _INDICES:
                    category = "INDEX"
                else:
                    category = "LEAP"
            else:
                category = "EQUITY"
            return dict(sym=sym, underlying=underlying, side=side, ctype=ctype,
                        category=category, qty=int(filled_qty), price=price,
                        date=date_str, time=time_str, is_option=is_option)

        parsed = [_parse(o) for o in orders_sorted]
        parsed = [p for p in parsed if p]

        # FIFO pair BUY → SELL per contract symbol
        buy_queues   = defaultdict(list)
        paired_trades = []

        for p in parsed:
            sym  = p["sym"]
            mult = 100 if p["is_option"] else 1
            if p["side"] == "BUY":
                buy_queues[sym].append({**p, "remaining": p["qty"], "mult": mult})
            elif p["side"] == "SELL":
                rem = p["qty"]
                while rem > 0 and buy_queues[sym]:
                    buy     = buy_queues[sym][0]
                    matched = min(rem, buy["remaining"])
                    pnl     = round((p["price"] - buy["price"]) * matched * mult, 2)
                    paired_trades.append({
                        "entry_date": buy["date"],
                        "entry_time": buy["time"],
                        "exit_date":  p["date"],
                        "exit_time":  p["time"],
                        "ticker":     buy["underlying"],
                        "contract":   sym if buy["is_option"] else None,
                        "type":       buy["ctype"],
                        "category":   buy["category"],
                        "qty":        matched,
                        "entry":      round(buy["price"], 4),
                        "exit":       round(p["price"], 4),
                        "pnl":        pnl,
                        "status":     "CLOSED",
                    })
                    buy["remaining"] -= matched
                    rem -= matched
                    if buy["remaining"] == 0:
                        buy_queues[sym].pop(0)

        # Cross-check with actual Alpaca positions to distinguish LIVE vs EXPIRED
        try:
            _pos_r = _hx.get(
                "https://paper-api.alpaca.markets/v2/positions",
                headers=headers, timeout=8
            )
            _actual = {p["symbol"]: p for p in (_pos_r.json() if _pos_r.status_code == 200 else [])
                       if isinstance(p, dict)}
        except Exception:
            _actual = {}

        # Leftover BUYs with no matching SELL
        for sym, buys in buy_queues.items():
            for buy in buys:
                if buy["remaining"] > 0:
                    # Determine true status: check if Alpaca actually holds this position
                    in_alpaca = sym in _actual
                    if not in_alpaca:
                        # Option that expired worthless, or EOD-closed swing with no sell order
                        is_opt = bool(_re.search(r'\d{6}[CP]\d', sym))
                        status = "EXPIRED" if is_opt else "EOD_CLOSED"
                        pnl    = round(-buy["price"] * buy["remaining"] * buy["mult"], 2) if is_opt else None
                    else:
                        status = "LIVE"
                        pnl    = None
                    paired_trades.append({
                        "entry_date": buy["date"],
                        "entry_time": buy["time"],
                        "exit_date":  None,
                        "exit_time":  None,
                        "ticker":     buy["underlying"],
                        "contract":   sym if buy["is_option"] else None,
                        "type":       buy["ctype"],
                        "category":   buy["category"],
                        "qty":        buy["remaining"],
                        "entry":      round(buy["price"], 4),
                        "exit":       None,
                        "pnl":        pnl,
                        "status":     status,
                    })

        # Sort: LIVE first, CLOSED newest-first, then EOD_CLOSED, then EXPIRED
        live_part = [t for t in paired_trades if t["status"] == "LIVE"]
        _groups: dict = {}
        for t in paired_trades:
            if t["status"] != "LIVE":
                _groups.setdefault(t["status"], []).append(t)
        for st in _groups:
            _groups[st].sort(key=lambda t: t.get("exit_date") or t.get("entry_date") or "", reverse=True)
        closed_part = _groups.get("CLOSED",[]) + _groups.get("EOD_CLOSED",[]) + _groups.get("EXPIRED",[])
        paired_trades = live_part + closed_part

        closed = [t for t in paired_trades if t["status"] in ("CLOSED", "EXPIRED", "EOD_CLOSED")]
        live   = [t for t in paired_trades if t["status"] == "LIVE"]

        total_pnl = sum(t["pnl"] or 0 for t in closed)
        wins      = sum(1 for t in closed if (t["pnl"] or 0) > 0)
        losses    = sum(1 for t in closed if (t["pnl"] or 0) < 0)

        # By category breakdown (only CLOSED trades with real P&L)
        _scored = [t for t in closed if t["pnl"] is not None]
        by_cat: dict = {}
        for t in _scored:
            cat = t["category"]
            if cat not in by_cat:
                by_cat[cat] = {"trades": 0, "pnl": 0.0, "wins": 0}
            by_cat[cat]["trades"] += 1
            by_cat[cat]["pnl"]    = round(by_cat[cat]["pnl"] + t["pnl"], 2)
            if t["pnl"] > 0:
                by_cat[cat]["wins"] += 1

        # ── Daily P&L from Alpaca portfolio history (equity delta per day) ──────
        # This is the authoritative source — matches exactly what Alpaca shows.
        # Falls back to FIFO trade sum if portfolio history is unavailable.
        daily_pnl: list = []
        try:
            _ph = _hx.get(
                "https://paper-api.alpaca.markets/v2/account/portfolio/history",
                headers=headers,
                params={"period": "1M", "timeframe": "1D", "intraday_reporting": "market_hours"},
                timeout=8,
            )
            if _ph.status_code == 200:
                _hist = _ph.json()
                _timestamps = _hist.get("timestamp", [])
                _equity_vals = _hist.get("equity", [])
                _pl_vals     = _hist.get("profit_loss", [])        # Alpaca already computes day delta
                _pl_pct      = _hist.get("profit_loss_pct", [])

                # Build trade count per date from paired trades for the "positions" label
                _trade_cnt: dict = {}
                for t in _scored:
                    _d = t["exit_date"] or t["entry_date"] or "?"
                    _trade_cnt[_d] = _trade_cnt.get(_d, 0) + 1

                for i, ts in enumerate(_timestamps):
                    try:
                        from datetime import datetime as _dtm
                        _date_str = _dtm.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                        _pnl_val  = float(_pl_vals[i]) if i < len(_pl_vals) else 0.0
                        if _pnl_val == 0.0:
                            continue   # skip zero-P&L days (non-trading days)
                        daily_pnl.append({
                            "date":   _date_str,
                            "pnl":    round(_pnl_val, 2),
                            "trades": _trade_cnt.get(_date_str, 0),
                        })
                    except Exception:
                        continue
                daily_pnl.sort(key=lambda x: x["date"], reverse=True)
        except Exception as _phe:
            pass   # fall through to FIFO fallback

        # Fallback: sum FIFO trade P&L per exit date
        if not daily_pnl:
            daily_map: dict = {}
            for t in _scored:
                d = t["exit_date"] or t["entry_date"] or "?"
                if d not in daily_map:
                    daily_map[d] = {"date": d, "pnl": 0.0, "trades": 0}
                daily_map[d]["trades"] += 1
                daily_map[d]["pnl"]    = round(daily_map[d]["pnl"] + (t["pnl"] or 0), 2)
            daily_pnl = sorted(daily_map.values(), key=lambda x: x["date"], reverse=True)

        # ── Override today's entry with ARKA summary (realized-only, accurate count) ──
        # Portfolio history includes unrealized P&L on open positions → inflated number.
        # ARKA summary file tracks only closed/realized P&L and actual ARKA trade count.
        try:
            import json as _js2, os as _os2
            _today_str   = _dt.today().strftime("%Y-%m-%d")
            _base2       = _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__)))
            _sum_path    = _os2.path.join(_base2, "logs", "arka", f"summary_{_today_str}.json")
            if _os2.path.exists(_sum_path):
                _sd2         = _js2.loads(open(_sum_path).read())
                _tlog2       = _sd2.get("trade_log", [])
                _closed_cnt  = sum(1 for _tr in _tlog2 if _tr.get("pnl") is not None)
                _realized    = sum(float(_tr.get("pnl", 0) or 0) for _tr in _tlog2
                                   if _tr.get("pnl") is not None)
                _today_entry = {"date": _today_str, "pnl": round(_realized, 2), "trades": _closed_cnt}
                _today_idx   = next((i for i, d in enumerate(daily_pnl) if d["date"] == _today_str), None)
                if _today_idx is not None:
                    # Only override FIFO result if summary has actual realized data
                    if _realized != 0 or _closed_cnt > 0:
                        daily_pnl[_today_idx] = _today_entry
                elif _realized != 0 or _closed_cnt > 0:
                    daily_pnl.insert(0, _today_entry)
        except Exception:
            pass

        winning_days = sum(1 for d in daily_pnl if d["pnl"] > 0)

        return {
            "trades":       paired_trades[:300],
            "daily_pnl":    daily_pnl,
            "total_pnl":    round(total_pnl, 2),
            "total_trades": len(closed),
            "live_count":   len(live),
            "wins":         wins,
            "losses":       losses,
            "winning_days": winning_days,
            "total_days":   len(daily_pnl),
            "win_rate":     round(wins / max(wins + losses, 1) * 100, 1),
            "by_category":  by_cat,
            "source":       "alpaca_live",
        }
    except Exception as e:
        return {"error": str(e), "trades": [], "daily_pnl": []}

@app.get("/api/arka/options-trades")
def get_arka_options_trades():
    """Today's ARKA 0DTE options trades."""
    import glob
    files = sorted(glob.glob(f"logs/arka/arka_{date.today()}.log"), reverse=True)
    trades = []
    if files:
        try:
            with open(files[0]) as f:
                for line in f:
                    if "0DTE OPTIONS ENTRY" in line or "PROFIT TARGET" in line or "STOP LOSS" in line:
                        trades.append(line.strip())
        except:
            pass
    return {"trades": trades, "count": len(trades)}


@app.get("/api/sectors/snapshot")
def get_sector_snapshot():
    """Fetch live sector ETF prices from Polygon snapshot."""
    import httpx as _hx, os as _os
    key = _os.getenv("POLYGON_API_KEY","")
    tickers = ["XLK","XLF","XLE","XLV","XLI","XLP","XLY","XLU","XLRE","XLB","XLC",
               "EWU","EWG","EWJ","EWH","FXI","EEM","SPY","QQQ","IWM","DIA"]
    syms = ",".join(tickers)
    try:
        r = _hx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": syms, "apiKey": key},
            timeout=15
        ).json()
        out = {}
        for t in r.get("tickers", []):
            sym = t.get("ticker","")
            day = t.get("day", {})
            prev= t.get("prevDay", {})
            lp  = t.get("lastTrade",{}).get("p",0) or day.get("c",0) or prev.get("c",0)
            pc  = prev.get("c",1) or 1
            chg = lp - pc
            chg_pct = (chg/pc*100) if pc else 0
            out[sym] = {
                "ticker":   sym,
                "close":    round(lp,2),
                "prev_close": round(pc,2),
                "change":   round(chg,2),
                "chg_pct":  round(chg_pct,3),
                "direction": "UP" if chg_pct > 0.05 else "DOWN" if chg_pct < -0.05 else "FLAT",
                "direction_5d": "UP" if chg_pct > 0.05 else "DOWN" if chg_pct < -0.05 else "FLAT",
                "high":     day.get("h",0),
                "low":      day.get("l",0),
                "volume":   day.get("v",0),
            }
        return out
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/prices/live")
def get_live_prices():
    import httpx as _hx
    from dotenv import load_dotenv as _ld
    from pathlib import Path as _Pth
    _ld(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'), override=True)
    key = os.getenv("POLYGON_API_KEY", "")
    tickers_set = set(["SPY","QQQ","IWM","DIA","XLF","XLK","XLE","XLV","XLI","XLP","XLY","XLU","XLRE","XLB","XLC",
                        "NVDA","TSLA","AAPL","MSFT","AMZN","GOOGL","META","AMD","NFLX","COIN","SOXX"])
    # Add swing watchlist tickers so cards get live prices
    _wl = _Pth("logs/chakra/watchlist_latest.json")
    if _wl.exists():
        try:
            _wld = json.loads(_wl.read_text())
            for _c in _wld.get("candidates", []):
                if _c.get("ticker"): tickers_set.add(_c["ticker"])
        except Exception:
            pass
    tickers_list = sorted(tickers_set)
    try:
        r = _hx.get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(tickers_list), "apiKey": key}, timeout=12).json()
        out = {}
        for t in r.get("tickers", []):
            sym = t.get("ticker","")
            day = t.get("day",{}); prev = t.get("prevDay",{}); min_ = t.get("min") or {}
            lt = (t.get("lastTrade") or {}).get("p",0)
            price = float(lt or min_.get("c",0) or day.get("c",0) or prev.get("c",0) or 0)
            prevc = float(prev.get("c",0) or price or 1)
            chg_pct = float(t.get("todaysChangePerc",0) or (((price-prevc)/prevc)*100 if prevc else 0))
            out[sym] = {"price":round(price,2),"prev":round(prevc,2),"chg_pct":round(chg_pct,3)}

        # Derive SPX from SPY, RUT from IWM, NDX from QQQ
        if "SPY" in out:
            spy = out["SPY"]["price"]; spy_prev = out["SPY"]["prev"]
            spx = round(spy * 10.04, 2); spx_prev = round(spy_prev * 10.04, 2)
            out["SPX"] = {"price": spx, "prev": spx_prev,
                          "chg_pct": round(((spx - spx_prev) / spx_prev) * 100, 3) if spx_prev else 0}
        if "IWM" in out:
            iwm = out["IWM"]["price"]; iwm_prev = out["IWM"]["prev"]
            rut = round(iwm * 8.35, 2); rut_prev = round(iwm_prev * 8.35, 2)
            out["RUT"] = {"price": rut, "prev": rut_prev,
                          "chg_pct": round(((rut - rut_prev) / rut_prev) * 100, 3) if rut_prev else 0}
        if "QQQ" in out:
            qqq = out["QQQ"]["price"]; qqq_prev = out["QQQ"]["prev"]
            ndx = round(qqq * 34.86, 2); ndx_prev = round(qqq_prev * 34.86, 2)
            out["NDX"] = {"price": ndx, "prev": ndx_prev,
                          "chg_pct": round(((ndx - ndx_prev) / ndx_prev) * 100, 3) if ndx_prev else 0}
        return out
    except Exception as e:
        return {"error": str(e)}



@app.post("/api/arjun/analyze")
async def arjun_analyze(request: dict):
    """Proxy Anthropic API call for Arjun trade analysis."""
    import httpx as _hx
    try:
        body = request
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return {"error": "ANTHROPIC_API_KEY not set"}
        async with _hx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json=body
            )
        return r.json()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/arjun/accuracy")
def get_arjun_accuracy(days: int = 30):
    """Historical ARJUN signal accuracy stats from performance DB."""
    try:
        from backend.arjun.feedback_tracker import get_historical_accuracy
        return get_historical_accuracy(days)
    except Exception as e:
        return {"error": str(e), "period_days": days, "total": 0, "win_rate": 0}


@app.get("/api/arjun/feedback/today")
def get_arjun_feedback_today():
    """Today's feedback file — signals scored against actual price outcomes."""
    from datetime import date as _date
    import json as _json
    from pathlib import Path as _P
    path = _P(f"logs/arjun/feedback_{_date.today().isoformat()}.json")
    if not path.exists():
        return {"date": _date.today().isoformat(), "signals": [],
                "message": "No feedback yet — runs at 4:05 PM ET"}
    try:
        data = _json.loads(path.read_text())
        wins   = sum(1 for s in data if s.get("outcome") == "WIN")
        losses = sum(1 for s in data if s.get("outcome") == "LOSS")
        return {
            "date":     _date.today().isoformat(),
            "wins":     wins,
            "losses":   losses,
            "win_rate": round(wins / max(wins + losses, 1), 3),
            "signals":  data,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/arjun/feedback/run")
async def run_arjun_feedback(days: int = 3):
    """Trigger EOD feedback scoring manually. Returns summary."""
    try:
        import asyncio
        from backend.arjun.feedback_tracker import run_eod_feedback
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, lambda: run_eod_feedback(days))
        return summary
    except Exception as e:
        return {"error": str(e)}


# ── ChromaDB Memory Endpoints ─────────────────────────────────────────────────

@app.get("/api/arjun/memory/stats")
def get_memory_stats():
    try:
        from backend.arjun.memory.signal_memory import (
            memory_summary, get_ticker_stats
        )
        summary = memory_summary()
        ticker_stats = {}
        for t in ["SPY","QQQ","SPX","IWM","NVDA","TSLA"]:
            stats = get_ticker_stats(t)
            if stats.get("signals",0) > 0:
                ticker_stats[t] = stats
        return {
            "summary": summary,
            "tickers": ticker_stats,
            "status":  "active",
        }
    except Exception as e:
        return {"error": str(e), "status": "unavailable"}

@app.get("/api/arjun/memory/query")
async def query_memory(ticker: str = "SPY",
                       regime: str = "UNKNOWN",
                       rsi: float = 50.0,
                       direction: str = "NEUTRAL"):
    try:
        from backend.arjun.memory.signal_memory import query_similar
        past = query_similar(ticker, regime, rsi, direction)
        return {"ticker": ticker, "similar": past, "count": len(past)}
    except Exception as e:
        return {"error": str(e)}


# ── Pipeline Endpoints ────────────────────────────────────────────────────────

@app.get("/api/arjun/pipeline/status")
def get_pipeline_status():
    """Return latest pipeline cycle status."""
    from pathlib import Path as _Pth
    path = _Pth("logs/arjun/pipeline_latest.json")
    if path.exists():
        import json as _j
        return _j.loads(path.read_text())
    return {"status": "no cycles run yet", "signals_count": 0, "placed_count": 0}

@app.post("/api/arjun/pipeline/run")
async def run_pipeline_now():
    """Manually trigger one pipeline cycle."""
    try:
        from backend.arjun.pipeline.chakra_pipeline import run_cycle
        result = await run_cycle()
        return {
            "success": True,
            "signals": len(result.get("all_signals") or []),
            "placed":  len([r for r in (result.get("execution_results") or [])
                            if r.get("status")=="placed"]),
            "regime":  (result.get("research_report") or {}).get("market_regime","?"),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── HS→ARJUN Intraday Pipeline Endpoints ─────────────────────────────────────

@app.get("/api/arjun/trade-request")
def get_arjun_trade_request():
    """Return the latest ARJUN trade_request from the HS→ARJUN pipeline."""
    import time as _t
    from pathlib import Path as _P
    path = _P("logs/arjun/trade_request.json")
    if not path.exists():
        return {"status": "none", "message": "No pending trade request"}
    try:
        req = json.loads(path.read_text())
        if _t.time() > req.get("expires_at", 0):
            return {"status": "expired", "message": "Trade request has expired"}
        return {"status": "active", "request": req}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/arjun/run-pipeline")
async def run_arjun_intraday_pipeline():
    """Manually trigger HS→ARJUN intraday pipeline. Reads HS cache, deliberates, writes trade_request."""
    import asyncio as _aio
    from concurrent.futures import ThreadPoolExecutor as _TPE
    try:
        from backend.arka.hs_signal_writer import write_pending_signals
        from backend.arjun.arjun_intraday import run_pipeline as _run
        loop = _aio.get_event_loop()
        with _TPE(max_workers=1) as ex:
            signals = await loop.run_in_executor(ex, write_pending_signals)
            result  = await loop.run_in_executor(ex, _run)
        return {
            "success":        True,
            "signals_loaded": len(signals),
            "decision":       result.get("decision", "NONE"),
            "ticker":         result.get("ticker", ""),
            "confidence":     result.get("confidence", 0),
            "direction":      result.get("direction", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/futures/snapshot")
def get_futures_snapshot():
    """Derive ES, NQ, RTY, YM futures prices from ETF proxies via Polygon."""
    import httpx as _hx
    key = os.getenv("POLYGON_API_KEY", "")
    etfs = ["SPY", "QQQ", "IWM", "DIA"]
    try:
        r = _hx.get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(etfs), "apiKey": key}, timeout=10).json()
        prices = {}
        for t in r.get("tickers", []):
            sym  = t.get("ticker","")
            day  = t.get("day",{}); prev = t.get("prevDay",{}); min_ = t.get("min") or {}
            lt   = (t.get("lastTrade") or {}).get("p", 0)
            p    = float(lt or min_.get("c",0) or day.get("c",0) or prev.get("c",0) or 0)
            pc   = float(prev.get("c",0) or p or 1)
            prices[sym] = {"price": p, "prev": pc}

        def _fut(base, mult, name, symbol, desc):
            b = prices.get(base, {})
            p = round(b.get("price",0) * mult, 2)
            pc = round(b.get("prev",0) * mult, 2)
            chg = round(p - pc, 2)
            chg_pct = round(((p - pc) / pc) * 100, 3) if pc else 0
            return {"symbol": symbol, "name": name, "desc": desc,
                    "price": p, "prev": pc, "chg": chg, "chg_pct": chg_pct,
                    "source": f"{base}×{mult}"}

        return {
            "futures": [
                _fut("SPY", 10.04, "S&P 500",  "ES", "E-mini S&P 500"),
                _fut("QQQ", 34.86, "Nasdaq 100","NQ", "E-mini Nasdaq"),
                _fut("IWM",  8.35, "Russell 2000","RTY","E-mini Russell"),
                _fut("DIA", 100.0, "Dow Jones", "YM", "E-mini Dow"),
            ],
            "note": "Derived from ETF proxies — indicative only",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {"error": str(e), "futures": []}

@app.get("/api/lotto/status")
async def get_lotto_status():
    """Power Hour lotto engine — live status."""
    try:
        from backend.arka.lotto_engine import get_lotto_status
        return get_lotto_status()
    except Exception as e:
        return {"enabled": False, "trigger_time": "15:30:00", "active": False,
                "trades_today": 0, "status": "WATCHING", "error": str(e)}

@app.post("/api/lotto/clear")
async def clear_lotto_state():
    """Force-clear a stale active lotto trade from state."""
    try:
        from backend.arka.lotto_engine import clear_lotto_state
        return clear_lotto_state()
    except Exception as e:
        return {"cleared": False, "error": str(e)}

@app.get("/api/command-center")
async def get_command_center():
    """CHAKRA Command Center — aggregated intelligence in one response."""
    import glob
    from pathlib import Path as _Path
    result = {"timestamp": datetime.now().isoformat()}

    # Portfolio
    try:
        result["portfolio"] = (await get_account()).dict() if hasattr((await get_account()), "dict") else await get_account()
    except Exception as e:
        result["portfolio"] = {"error": str(e)}

    # Latest signals
    try:
        files = sorted(glob.glob("logs/signals/signals_*.json"), reverse=True)
        if files:
            with open(files[0]) as f:
                sigs = json.load(f)
            result["signals"] = sigs if isinstance(sigs, list) else [sigs]
        else:
            result["signals"] = []
    except Exception as e:
        result["signals"] = []

    # GEX summary
    try:
        files = sorted(glob.glob("logs/arka/gex_heatmap_*.json"), reverse=True)
        if not files:
            files = sorted(glob.glob("logs/arka/gex-heatmap-*.json"), reverse=True)
        result["gex"] = json.loads(_Path(files[0]).read_text()) if files else {}
    except Exception:
        result["gex"] = {}

    # Internals + neural pulse
    try:
        f = _Path("logs/internals/internals_latest.json")
        result["internals"] = json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        result["internals"] = {}

    # Execution gates
    try:
        from backend.chakra.execution_gates import calculate_execution_gates
        result["execution_gates"] = calculate_execution_gates()
    except Exception as e:
        result["execution_gates"] = {"error": str(e)}

    # ARKA session
    try:
        result["arka"] = await get_arka_session()
    except Exception as e:
        result["arka"] = {"error": str(e)}

    # Lotto status
    try:
        from backend.arka.lotto_engine import get_lotto_status
        result["lotto"] = get_lotto_status()
    except Exception as e:
        result["lotto"] = {"error": str(e)}

    return result


@app.get("/api/prices/sparkline")
async def get_price_sparkline(ticker: str = "SPY", bars: int = 30):
    """
    Return last N 1-minute close prices for a ticker.
    Used to render mini sparklines on the market regime bar.
    Returns: {ticker, closes: [float, ...], open_close_pct: float}
    """
    import httpx as _hx, os as _os
    from datetime import date as _d
    _ticker = ticker.upper()
    _key    = _os.getenv("POLYGON_API_KEY", "")
    _today  = _d.today().isoformat()
    try:
        r = _hx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{_ticker}/range/1/minute/{_today}/{_today}",
            params={"adjusted": "true", "sort": "asc", "limit": bars, "apiKey": _key},
            timeout=5,
        )
        results = r.json().get("results", [])
        if not results:
            return {"ticker": _ticker, "closes": [], "open_close_pct": 0}
        closes = [float(b["c"]) for b in results]
        first, last = closes[0], closes[-1]
        pct = round((last - first) / first * 100, 3) if first else 0
        return {"ticker": _ticker, "closes": closes, "open_close_pct": pct}
    except Exception as e:
        return {"ticker": _ticker, "closes": [], "open_close_pct": 0, "error": str(e)}


@app.get("/api/execution-gates")
async def get_execution_gates():
    """5-gate execution readiness check."""
    try:
        from backend.chakra.execution_gates import calculate_execution_gates
        return calculate_execution_gates()
    except Exception as e:
        return {"error": str(e), "overall": "UNKNOWN"}

@app.get("/api/analysis/correlation-web")
async def get_correlation_web(lookback: int = 20):
    """Sector correlation matrix for D3 force graph."""
    try:
        import sys
        sys.path.insert(0, ".")
        from backend.analysis.correlation_engine import build_correlation_matrix, detect_regime_shift
        data  = build_correlation_matrix(lookback_days=lookback)
        shift = detect_regime_shift(data)
        data["regime_shift"] = shift
        return data
    except Exception as e:
        return {"error": str(e), "nodes": [], "edges": []}

@app.get("/api/analysis/pulse-timeline")
async def get_pulse_timeline():
    """Neural Pulse 30-min rolling timeline for HELX sparkline."""
    try:
        from backend.internals.pulse_timeline import get_timeline
        return get_timeline()
    except Exception as e:
        return {"points": [], "current_score": 50, "trend": "FLAT", "error": str(e)}


@app.get("/api/flow/summary")
async def get_flow_summary():
    """Dark pool + UOA summary for Flow tab."""
    try:
        from backend.flow.dark_pool_scanner import detect_dark_pool_activity
        from backend.flow.uoa_detector import detect_unusual_options
        import httpx as _hx
        key = os.getenv("POLYGON_API_KEY","")
        result = {}
        for ticker in ["SPY","QQQ","IWM"]:
            # Fetch recent trades for dark pool
            r = _hx.get(f"https://api.polygon.io/v3/trades/{ticker}",
                params={"apiKey": key, "limit": 500, "order": "desc"}, timeout=10)
            trades = [{"exchange": t.get("exchange",0),
                       "size": t.get("size",0),
                       "side": "buy" if 14 not in t.get("conditions",[]) else "sell"}
                      for t in r.json().get("results",[])]
            dp  = detect_dark_pool_activity(trades)
            uoa = detect_unusual_options(ticker)
            result[ticker] = {"dark_pool": dp, "uoa": uoa}
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/analysis/liquidity")
async def analysis_liquidity(ticker: str = "SPY"):
    import json
    from pathlib import Path
    p = Path("logs/chakra/lambda_latest.json")
    if not p.exists():
        return {"error": "Kyle Lambda cache missing — run kyle_lambda.py"}
    d = json.loads(p.read_text())
    return {"ticker": ticker, "kyle_lambda": d, "source": "kyle_lambda.py"}

@app.get("/api/analysis/cot")
async def analysis_cot():
    import json
    from pathlib import Path
    p = Path("logs/chakra/cot_latest.json")
    if not p.exists():
        return {"error": "COT cache missing — run cot_smart_money.py"}
    d = json.loads(p.read_text())
    return {"cot": d, "source": "cot_smart_money.py"}

@app.get("/api/analysis/probability-dist")
async def analysis_probability_dist(ticker: str = "SPY"):
    import json
    from pathlib import Path
    p = Path("logs/chakra/probdist_latest.json")
    if not p.exists():
        return {"error": "Prob Dist cache missing — run prob_distribution.py"}
    d = json.loads(p.read_text())
    return {"ticker": ticker, "prob_dist": d, "source": "prob_distribution.py"}

@app.get("/api/system/health")
async def system_health():
    import subprocess, socket
    from datetime import datetime
    from pathlib import Path

    def proc_running(pattern):
        try:
            r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True)
            return bool(r.stdout.strip())
        except:
            return False

    def port_open(port):
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except:
            return False

    def cache_age(filepath):
        p = Path(filepath)
        if not p.exists():
            return {"exists": False, "age_min": None, "status": "MISSING"}
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        age_min = round((datetime.now(timezone.utc) - mtime).total_seconds() / 60, 1)
        status = "FRESH" if age_min < 35 else "STALE" if age_min < 120 else "OLD"
        return {"exists": True, "age_min": age_min, "status": status}

    engine_status = [
        {"name": "ARKA Engine",      "running": proc_running("arka_engine")},
        {"name": "Dashboard API",    "running": port_open(8000)},
        {"name": "TARAKA Engine",    "running": proc_running("taraka_engine")},
        {"name": "Market Internals", "running": Path("logs/internals/internals_latest.json").exists() and ((__import__("datetime").datetime.now(__import__("datetime").timezone.utc) - __import__("datetime").datetime.fromtimestamp(Path("logs/internals/internals_latest.json").stat().st_mtime, tz=__import__("datetime").timezone.utc)).total_seconds() < 3600)},
    ]
    for e in engine_status:
        e["status"] = "RUNNING" if e["running"] else "STOPPED"

    caches = [
        {"name": "DEX / GEX",        "file": "logs/chakra/dex_latest.json"},
        {"name": "Hurst",            "file": "logs/chakra/hurst_latest.json"},
        {"name": "VRP",              "file": "logs/chakra/vrp_latest.json"},
        {"name": "VEX Vanna",        "file": "logs/chakra/vex_latest.json"},
        {"name": "Charm",            "file": "logs/chakra/charm_latest.json"},
        {"name": "Entropy",          "file": "logs/chakra/entropy_latest.json"},
        {"name": "HMM Regime",       "file": "logs/chakra/hmm_latest.json"},
        {"name": "IV Skew",          "file": "logs/chakra/ivskew_latest.json"},
        {"name": "Iceberg",          "file": "logs/chakra/iceberg_latest.json"},
        {"name": "Kyle Lambda",      "file": "logs/chakra/lambda_latest.json"},
        {"name": "COT",              "file": "logs/chakra/cot_latest.json"},
        {"name": "Prob Dist",        "file": "logs/chakra/probdist_latest.json"},
        {"name": "Watchlist",        "file": "logs/chakra/watchlist_latest.json"},
        {"name": "Market Internals", "file": "logs/internals/internals_latest.json"},
    ]
    cache_status = [{"name": c["name"], **cache_age(c["file"])} for c in caches]

    engines_ok = sum(1 for e in engine_status if e["running"])
    caches_ok  = sum(1 for c in cache_status if c.get("status") == "FRESH")
    overall    = "HEALTHY" if engines_ok == 4 and caches_ok >= 8 else                  "DEGRADED" if engines_ok >= 2 else "CRITICAL"

    return {
        "overall":         overall,
        "engines_running": engines_ok,
        "engines_total":   len(engine_status),
        "caches_fresh":    caches_ok,
        "caches_total":    len(cache_status),
        "engines":         engine_status,
        "caches":          cache_status,
        "checked_at":      datetime.now().strftime("%H:%M ET")
    }

# S2_PATCHED — VEX / Entropy / Charm endpoints by patchsession2.py
@app.get("/api/analysis/vex")
def api_vex():
    import pathlib
    f = pathlib.Path("logs/chakra/vex_latest.json")
    return json.loads(f.read_text()) if f.exists() else {"error": "no data"}

@app.get("/api/analysis/entropy")
def api_entropy():
    import pathlib
    f = pathlib.Path("logs/chakra/entropy_latest.json")
    return json.loads(f.read_text()) if f.exists() else {"error": "no data"}

@app.get("/api/analysis/charm")
def api_charm():
    import pathlib
    f = pathlib.Path("logs/chakra/charm_latest.json")
    return json.loads(f.read_text()) if f.exists() else {"error": "no data"}

# S3_PATCHED — HMM / IVSkew / Iceberg endpoints by patchsession3.py
@app.get("/api/analysis/hmm")
def api_hmm():
    import pathlib
    f = pathlib.Path("logs/chakra/hmm_latest.json")
    return json.loads(f.read_text()) if f.exists() else {"error": "no data"}

@app.get("/api/analysis/ivskew")
def api_ivskew():
    import pathlib
    f = pathlib.Path("logs/chakra/ivskew_latest.json")
    return json.loads(f.read_text()) if f.exists() else {"error": "no data"}

@app.get("/api/analysis/iceberg")
def api_iceberg():
    import pathlib
    f = pathlib.Path("logs/chakra/iceberg_latest.json")
    return json.loads(f.read_text()) if f.exists() else {"error": "no data"}


# MANIFOLD_FIX — Ticker-responsive manifold endpoint (Mastermind Session 1)


@app.post("/api/arka/manual-close")
async def manual_close_position(data: dict):
    """Manually close a position via Alpaca, then post Discord exit notification."""
    import httpx as _hx, re as _re_mc
    from datetime import datetime as _dt_mc
    ticker    = data.get("ticker", "")
    trade_sym = data.get("trade_sym", ticker)
    qty       = int(data.get("qty", 0))
    try:
        key    = os.getenv("ALPACA_API_KEY","")
        secret = os.getenv("ALPACA_API_SECRET","") or os.getenv("ALPACA_SECRET_KEY","")
        hdrs   = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

        # ── Fetch current position data (for entry price + P&L) ──────────
        entry_price  = 0.0
        exit_price   = 0.0
        actual_qty   = qty
        unrealized   = 0.0
        side         = "sell"

        pos_r = _hx.get(
            f"https://paper-api.alpaca.markets/v2/positions/{trade_sym}",
            headers=hdrs, timeout=5)
        if pos_r.status_code == 200:
            pd           = pos_r.json()
            actual_qty   = abs(int(float(pd.get("qty", qty))))
            entry_price  = float(pd.get("avg_entry_price", 0) or 0)
            exit_price   = float(pd.get("current_price",   0) or 0)
            unrealized   = float(pd.get("unrealized_pl",   0) or 0)
            side         = "sell" if float(pd.get("qty","0")) > 0 else "buy"

        # ── Options-only guard ────────────────────────────────────────────
        from backend.arka.order_guard import validate_options_order as _voo_close
        _ok, _why = _voo_close(trade_sym, actual_qty, side)
        if not _ok:
            return {"success": False, "error": f"ORDER GUARD: {_why}"}

        # ── Place close order — sell order first (reliable for options) ─────
        _is_opt = len(trade_sym) > 10 and any(c in trade_sym for c in ('C', 'P'))
        r = None
        if _is_opt:
            r = _hx.post(
                "https://paper-api.alpaca.markets/v2/orders",
                headers=hdrs,
                json={"symbol": trade_sym, "qty": str(actual_qty), "side": side,
                      "type": "market", "time_in_force": "day"},
                timeout=10)
        if not _is_opt or r.status_code not in (200, 201):
            # Equity or fallback: DELETE
            r2 = _hx.delete(
                f"https://paper-api.alpaca.markets/v2/positions/{trade_sym}",
                headers=hdrs, timeout=10)
            if r2.status_code not in (200, 204, 207):
                err = r.text[:120] if r else r2.text[:120]
                return {"success": False, "error": err}
            r = r2

        # ── Derive underlying for Discord ─────────────────────────────────
        m_sym = _re_mc.match(r'^([A-Z]{1,6})\d{6}[CP]\d+$', trade_sym)
        underlying = m_sym.group(1) if m_sym else trade_sym

        pnl_dollars = unrealized if unrealized != 0 else (
            (exit_price - entry_price) * actual_qty * 100
            if (entry_price > 0 and exit_price > 0) else 0
        )

        # ── Post Discord exit notification ────────────────────────────────
        try:
            from backend.arka.discord_notifier import post_arka_exit as _disc_exit
            import asyncio as _aio
            _loop = _aio.get_event_loop()
            if _loop.is_running():
                _aio.create_task(
                    _disc_exit(underlying, entry_price, exit_price,
                               actual_qty, "manual_close")
                )
            else:
                _loop.run_until_complete(
                    _disc_exit(underlying, entry_price, exit_price,
                               actual_qty, "manual_close")
                )
        except Exception as _de:
            import logging as _lg
            _lg.getLogger("dashboard_api").warning(f"Discord manual-close notify failed: {_de}")

        return {
            "success":    True,
            "symbol":     trade_sym,
            "underlying": underlying,
            "qty":        actual_qty,
            "entry":      entry_price,
            "exit":       exit_price,
            "pnl":        round(pnl_dollars, 2),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/flow/signals")
def get_flow_signals():
    """Return latest flow signals from flow monitor cache."""
    import json as _fj
    from pathlib import Path as _fp
    try:
        cache = _fp("logs/chakra/flow_signals_latest.json")
        if cache.exists():
            data = _fj.loads(cache.read_text())
            signals = []
            for ticker, sig in data.items():
                signals.append({
                    "ticker":       ticker,
                    "bias":         sig.get("bias", "NEUTRAL"),
                    "confidence":   sig.get("confidence", 0),
                    "vol_oi_ratio": sig.get("vol_oi_ratio", 0),
                    "is_extreme":   sig.get("is_extreme", False),
                    "dark_pool_pct":sig.get("dark_pool_pct", 0),
                    "timestamp":    sig.get("timestamp", ""),
                })
            signals.sort(key=lambda x: (-x["confidence"], -x["vol_oi_ratio"]))
            return {"signals": signals, "count": len(signals)}
    except Exception as e:
        pass
    return {"signals": [], "count": 0}

@app.get("/api/analysis/manifold")
async def api_manifold(ticker: str = "SPY"):
    import json as _j_mf, os as _os_mf, pathlib as _pl_mf
    try:
        import httpx
        import numpy as _np_mf
        from sklearn.manifold import Isomap
        from backend.arjun.modules.manifold_features import (
            compute_ricci_curvature, classify_manifold_regime
        )

        # Fetch 60 daily bars from Polygon
        _api_key = _os_mf.getenv("POLYGON_API_KEY", "")
        from datetime import date, timedelta
        _end   = date.today().isoformat()
        _start = (date.today() - timedelta(days=90)).isoformat()
        _url   = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
                  f"/{_start}/{_end}?apiKey={_api_key}&adjusted=true&sort=asc&limit=60")

        _r    = httpx.get(_url, timeout=10)
        _bars = _r.json().get("results", [])

        if len(_bars) < 10:
            return {"error": "insufficient data", "ticker": ticker, "bars": len(_bars)}

        # Build feature matrix: [close, volume, range, body]
        _features = _np_mf.array([
            [
                float(b.get("c", 0)),
                float(b.get("v", 0)) / 1e6,
                float(b.get("h", 0)) - float(b.get("l", 0)),
                (float(b.get("c", 0)) - float(b.get("o", 0))) / (float(b.get("o", 1)) + 1e-6)
            ]
            for b in _bars
        ], dtype=float)

        # Normalise columns
        _features = (_features - _features.mean(axis=0)) / (_features.std(axis=0) + 1e-6)

        # Isomap 3D embedding
        _n_neighbors = min(8, len(_features) - 1)
        _iso         = Isomap(n_components=3, n_neighbors=_n_neighbors)
        _coords      = _iso.fit_transform(_features)

        # ── Manifold Engine modifier ──────────────────────────────────────
        _phase_label  = 'UNKNOWN'
        _manifold_mod = 0
        try:
            from backend.arka.manifold_engine import ManifoldEngine as _ME
            _mfe        = _ME()
            _ph         = _mfe.phase_engine.get_state()
            _phase_label = getattr(_ph, 'label', str(_ph)) if _ph else 'UNKNOWN'
            _manifold_mod = _mfe.adjust_arka(50, _ph, None).get('modifier', 0)
        except Exception:
            pass


        # Ricci curvature + regime classification
        _ricci  = compute_ricci_curvature(_coords)
        _regime = classify_manifold_regime(_ricci)

        # Cache result
        _cache = _pl_mf.Path(f"logs/chakra/manifold_{ticker.lower()}_latest.json")
        _cache.parent.mkdir(parents=True, exist_ok=True)
        _result = {
            "ticker":       ticker,
            "points":       _coords.tolist(),
            "ricci_flow":   _ricci.tolist(),
            "regime":       _regime,
            "bars_used":    len(_bars),
            "phase_state":   _phase_label,
            "manifold_mod":  _manifold_mod,
            "timestamp":    date.today().isoformat(),
        }
        _cache.write_text(_j_mf.dumps(_result))
        return _result

    except Exception as _e_mf:
        return {"error": str(_e_mf), "ticker": ticker}


_analyze_cache: dict = {}   # ticker → {"result": ..., "ts": float}
_ANALYZE_TTL = 30            # seconds

@app.get("/api/ticker/analyze")
def analyze_ticker(ticker: str = "AAPL"):
    """
    Full ARJUN-style analysis for any ticker.
    Returns: price action, technicals, options flow, module readings, AI verdict.
    """
    import httpx as _hx, os as _os, json as _j, math as _math, time as _ta_time
    from datetime import datetime, date as _date, timedelta as _td
    key = _os.getenv("POLYGON_API_KEY", "")
    ticker = ticker.upper().strip()

    _cached = _analyze_cache.get(ticker)
    if _cached and (_ta_time.time() - _cached["ts"]) < _ANALYZE_TTL:
        return _cached["result"]

    result = {"ticker": ticker, "timestamp": datetime.now().strftime("%H:%M ET"), "sections": {}}

    # ── 1. Snapshot (price + day stats) ──────────────────────
    try:
        r = _hx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": key}, timeout=8
        ).json()
        t = r.get("ticker", {})
        day  = t.get("day", {})
        prev = t.get("prevDay", {})
        lp   = float(t.get("lastTrade", {}).get("p", 0) or day.get("c", 0) or prev.get("c", 0) or 0)
        pc   = float(prev.get("c", 1) or 1)
        chg  = round(lp - pc, 2)
        chg_pct = round(chg / pc * 100, 3) if pc else 0
        vol  = day.get("v", 0)
        vwap = day.get("vw", lp)
        result["sections"]["price"] = {
            "last": round(lp, 2), "prev_close": round(pc, 2),
            "change": chg, "chg_pct": chg_pct,
            "high": day.get("h", 0), "low": day.get("l", 0),
            "volume": vol, "vwap": round(float(vwap), 2),
            "vwap_signal": "ABOVE_VWAP" if lp > vwap else "BELOW_VWAP",
            "direction": "UP" if chg_pct > 0.1 else "DOWN" if chg_pct < -0.1 else "FLAT",
        }
    except Exception as e:
        result["sections"]["price"] = {"error": str(e)}

    # ── 2. Recent bars + technicals ──────────────────────────
    try:
        end = _date.today().isoformat()
        start = (_date.today() - _td(days=60)).isoformat()
        r2 = _hx.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            params={"apiKey": key, "adjusted": "true", "sort": "asc", "limit": 60},
            timeout=8
        ).json()
        bars = r2.get("results", [])
        if len(bars) >= 20:
            closes = [b["c"] for b in bars]
            volumes = [b["v"] for b in bars]
            # EMA
            def ema(prices, period):
                k = 2 / (period + 1)
                e = prices[0]
                for p in prices[1:]: e = p * k + e * (1 - k)
                return round(e, 2)
            ema8  = ema(closes[-8:],  8)
            ema21 = ema(closes[-21:], 21)
            ema55 = ema(closes[-55:], 55) if len(closes) >= 55 else ema(closes, len(closes))
            # RSI
            gains = [max(closes[i]-closes[i-1], 0) for i in range(1, 15)]
            losses= [max(closes[i-1]-closes[i], 0) for i in range(1, 15)]
            avg_g = sum(gains)/14 if gains else 0.001
            avg_l = sum(losses)/14 if losses else 0.001
            rsi   = round(100 - 100/(1 + avg_g/avg_l), 1)
            # Volume ratio
            avg_vol = sum(volumes[-20:-1]) / 19 if len(volumes) >= 20 else sum(volumes)/len(volumes)
            vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol else 1.0
            # MACD
            ema12 = ema(closes[-12:], 12)
            ema26 = ema(closes[-26:], 26) if len(closes) >= 26 else ema(closes, len(closes))
            macd  = round(ema12 - ema26, 3)
            # Trend
            ema_stack = "BULLISH" if ema8 > ema21 > ema55 else "BEARISH" if ema8 < ema21 < ema55 else "MIXED"
            price_vs_200 = "ABOVE" if closes[-1] > ema(closes, min(len(closes),55)) else "BELOW"
            result["sections"]["technicals"] = {
                "ema8": ema8, "ema21": ema21, "ema55": ema55,
                "ema_stack": ema_stack, "rsi": rsi, "macd": macd,
                "vol_ratio": vol_ratio, "price_vs_ema55": price_vs_200,
                "rsi_signal": "OVERBOUGHT" if rsi > 70 else "OVERSOLD" if rsi < 30 else "NEUTRAL",
                "macd_signal": "BULLISH" if macd > 0 else "BEARISH",
                "bars_analyzed": len(bars),
            }
    except Exception as e:
        result["sections"]["technicals"] = {"error": str(e)}

    # ── 3. Options snapshot ───────────────────────────────────
    try:
        spot = result["sections"].get("price", {}).get("last", 0)
        if spot > 0:
            lo = round(spot * 0.85, 0)
            hi = round(spot * 1.15, 0)
            exp_start = _date.today().isoformat()
            exp_end   = (_date.today() + _td(days=45)).isoformat()
            r3 = _hx.get(
                f"https://api.polygon.io/v3/snapshot/options/{ticker}",
                params={"apiKey": key, "limit": 100,
                        "strike_price.gte": lo, "strike_price.lte": hi,
                        "expiration_date.gte": exp_start, "expiration_date.lte": exp_end},
                timeout=8
            ).json()
            options = r3.get("results", [])
            puts  = [o for o in options if o.get("details", {}).get("contract_type") == "put"]
            calls = [o for o in options if o.get("details", {}).get("contract_type") == "call"]
            total_put_oi  = sum(o.get("open_interest", 0) for o in puts)
            total_call_oi = sum(o.get("open_interest", 0) for o in calls)
            pc_ratio = round(total_put_oi / total_call_oi, 3) if total_call_oi else 0
            # Find highest OI strikes
            top_put  = max(puts,  key=lambda o: o.get("open_interest", 0), default={})
            top_call = max(calls, key=lambda o: o.get("open_interest", 0), default={})
            result["sections"]["options"] = {
                "put_call_ratio": pc_ratio,
                "pc_signal": "BEARISH_FEAR" if pc_ratio > 1.3 else "BULLISH_GREED" if pc_ratio < 0.7 else "NEUTRAL",
                "total_put_oi":  total_put_oi,
                "total_call_oi": total_call_oi,
                "top_put_strike":  top_put.get("details",  {}).get("strike_price", 0),
                "top_call_strike": top_call.get("details", {}).get("strike_price", 0),
                "contracts_found": len(options),
            }
    except Exception as e:
        result["sections"]["options"] = {"error": str(e)}

    # ── 4. Load relevant module readings ─────────────────────
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    module_data = {}
    for name, fname in [("hmm", "hmm_latest.json"), ("vrp", "vrp_latest.json"),
                         ("ivskew", "ivskew_latest.json"), ("lambda", "lambda_latest.json"),
                         ("entropy", "entropy_latest.json"), ("cot", "cot_latest.json")]:
        path = os.path.join(base_path, "logs/chakra", fname)
        if os.path.exists(path):
            try:
                module_data[name] = _j.load(open(path))
            except Exception:
                pass
    result["sections"]["modules"] = {
        "hmm_regime":   module_data.get("hmm", {}).get("regime", "UNKNOWN"),
        "vrp_state":    module_data.get("vrp", {}).get("state", "UNKNOWN"),
        "iv_skew":      module_data.get("ivskew", {}).get("signal", "UNKNOWN"),
        "kyle_lambda":  module_data.get("lambda", {}).get("signal", "UNKNOWN"),
        "entropy":      module_data.get("entropy", {}).get("signal", "UNKNOWN"),
        "cot_signal":   module_data.get("cot", {}).get("markets", {}).get("ES", {}).get("signal", "UNKNOWN"),
    }

    # ── 5. ARJUN conviction score ─────────────────────────────
    try:
        tech = result["sections"].get("technicals", {})
        price_s = result["sections"].get("price", {})
        opts  = result["sections"].get("options", {})
        mods  = result["sections"].get("modules", {})

        score = 50  # baseline
        reasons = []

        # EMA stack
        if tech.get("ema_stack") == "BULLISH":
            score += 12; reasons.append("EMA bull stack +12")
        elif tech.get("ema_stack") == "BEARISH":
            score -= 12; reasons.append("EMA bear stack -12")

        # RSI
        rsi = tech.get("rsi", 50)
        if rsi < 30:   score += 5;  reasons.append(f"RSI oversold ({rsi}) +5")
        elif rsi > 70: score -= 5;  reasons.append(f"RSI overbought ({rsi}) -5")

        # VWAP
        if price_s.get("vwap_signal") == "ABOVE_VWAP":
            score += 10; reasons.append("Above VWAP +10")
        else:
            score -= 10; reasons.append("Below VWAP -10")

        # Volume
        vr = tech.get("vol_ratio", 1)
        if vr > 1.5: score += 6; reasons.append(f"Vol surge {vr}x +6")

        # MACD
        if tech.get("macd_signal") == "BULLISH":
            score += 8; reasons.append("MACD bullish +8")
        else:
            score -= 8; reasons.append("MACD bearish -8")

        # PC ratio
        pc = opts.get("pc_ratio", 1)
        if pc > 1.3: score -= 8; reasons.append(f"High P/C {pc} -8")
        elif pc < 0.7: score += 8; reasons.append(f"Low P/C {pc} +8")

        # Module adjustments
        if mods.get("hmm_regime") == "CHOPPY_RANGE":
            score -= 10; reasons.append("HMM: CHOPPY -10")
        elif mods.get("hmm_regime") == "CRISIS":
            score -= 20; reasons.append("HMM: CRISIS -20")
        if mods.get("iv_skew") == "BEARISH_FEAR":
            score -= 10; reasons.append("IV Skew fear -10")
        if mods.get("kyle_lambda") == "EXTREME":
            score -= 8; reasons.append("Lambda illiquid -8")

        score = max(0, min(100, score))
        direction = "BULLISH" if int(score or 0) >= 60 else "BEARISH" if score <= 40 else "NEUTRAL"

        result["sections"]["arjun"] = {
            "conviction_score": score,
            "direction":        direction,
            "signal":           "BUY" if int(score or 0) >= 65 else "SELL" if score <= 35 else "HOLD",
            "reasons":          reasons,
            "threshold":        55,
            "would_trade":      int(score or 0) >= 55,
        }
    except Exception as e:
        result["sections"]["arjun"] = {"error": str(e)}

    _analyze_cache[ticker] = {"result": result, "ts": _ta_time.time()}
    return result

# ── Arjun Chat Proxy ───────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    mode: str = "main"

@app.post("/api/chat")
async def arjun_chat(req: ChatRequest):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        return {"reply": "ANTHROPIC_API_KEY not configured."}
    prompts = {
        "app":   "You are ARJUN, an AI assistant helping improve the CHAKRA trading application. Give concise, actionable suggestions.",
        "trade": "You are ARJUN, an AI trading assistant for CHAKRA. Help with trading strategies and signal generation.",
        "main":  "You are ARJUN, the neural intelligence of CHAKRA algorithmic trading system. Be concise and helpful.",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 600,
                      "system": prompts.get(req.mode, prompts["main"]),
                      "messages": [{"role": "user", "content": req.message}]},
            )
        reply = r.json().get("content", [{}])[0].get("text", "No response.")
        return {"reply": reply}
    except Exception as e:
        return {"reply": f"Error: {str(e)[:120]}"}

# ── Market Briefing ──────────────────────────────────────────────────────────
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', 'backend'))
from market.market_briefing import generate_briefing as _gen_briefing

@app.get("/api/market/briefing")
async def market_briefing(mode: str = "pre"):
    return await _gen_briefing(mode)

# ── Market Scheduler (auto-start) ───────────────────────────────────────────
import asyncio as _asyncio
from market.market_scheduler import run_briefing as _run_briefing, scheduler_loop as _scheduler_loop

@app.on_event("startup")
async def start_market_scheduler():
    _asyncio.create_task(_scheduler_loop())

# ── Options Engine Auto-Runner ───────────────────────────────────────────────
import sys as _sys_opt
_sys_opt.path.insert(0, 'backend')
from options.options_engine import OptionsEngine as _OptionsEngine

@app.on_event("startup")
async def start_options_engine():
    import asyncio as _aio
    _aio.create_task(_OptionsEngine().run())


# ── GEX State Refresh (every 5 min during market hours) ─────────────────────
# Index ETFs (SPY, QQQ) refresh every cycle (~5 min).
# Stock tickers rotate one per cycle so each refreshes ~every 50 min.
# TTL is 10 min so index tickers always stay fresh; stocks are "good enough".
_GEX_INDEX_TICKERS = ["SPY", "QQQ"]
_GEX_STOCK_TICKERS = ["AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL",
                       "TSLA", "AVGO", "NFLX", "AMD"]
_gex_stock_cursor  = 0   # rotates through stock tickers one per cycle


def _fetch_spot_sync(ticker: str, key: str) -> float:
    """Fetch current spot price synchronously (runs in executor, not event loop)."""
    try:
        import httpx as _hx
        _r = _hx.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": key}, timeout=8
        ).json()
        _t = _r.get("ticker", {})
        return float(_t.get("lastTrade", {}).get("p", 0)
                     or _t.get("day", {}).get("c", 0) or 0)
    except Exception:
        return 0.0


async def _gex_refresh_loop():
    """Refresh GEX state files so the ARKA gate always has fresh data.
    - SPY + QQQ: every cycle (~5 min during market hours)
    - Stock tickers: ALL tickers rotated 2 per cycle (~25 min full rotation)
    All HTTP calls run in executor so the event loop is never blocked.
    """
    global _gex_stock_cursor
    import asyncio as _aio
    import logging as _log
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt
    _glog = _log.getLogger("gex_refresh")
    await _aio.sleep(30)   # let API fully start first

    while True:
        try:
            _now = _dt.now(_ZI("America/New_York"))
            _is_mkt = (_now.weekday() < 5 and
                       ((_now.hour == 9 and _now.minute >= 30) or _now.hour > 9) and
                       _now.hour < 16)
            if _is_mkt:
                _key = os.getenv("POLYGON_API_KEY", "")
                from backend.arjun.agents.gex_calculator import get_gex_for_ticker as _gfx
                _loop = _aio.get_running_loop()   # correct for Python 3.10+

                from backend.arka.gex_state import load_gex_state as _lgs2, check_regime_change as _crc
                from backend.arka.arka_discord_notifier import post_gex_regime_change as _pgrc

                def _gfx_and_check(ticker: str, spot: float) -> None:
                    """Run GEX compute then check for regime change (sync, runs in executor)."""
                    _gfx(ticker, spot)
                    try:
                        _gs = _lgs2(ticker)
                        if _gs:
                            _flip = _crc(ticker, _gs.get("regime", ""), _gs)
                            if _flip and _flip.get("changed"):
                                _pgrc(_flip)
                                import logging as _lg3
                                _lg3.getLogger("gex_refresh").warning(
                                    f"🔄 GEX REGIME FLIP {ticker}: "
                                    f"{_flip['old_label']} → {_flip['new_label']} "
                                    f"({_flip['severity']})"
                                )
                    except Exception as _ce:
                        import logging as _lg4
                        _lg4.getLogger("gex_refresh").warning(f"Regime check error {ticker}: {_ce}")

                # ── Index tickers — every cycle ───────────────────────────────
                for _idx in _GEX_INDEX_TICKERS:
                    try:
                        _spot = await _loop.run_in_executor(None, _fetch_spot_sync, _idx, _key)
                        if _spot > 0:
                            await _loop.run_in_executor(None, _gfx_and_check, _idx, _spot)
                            _glog.info(f"GEX ✅ {_idx} @ ${_spot:.2f}")
                    except Exception as _e:
                        _glog.warning(f"GEX ❌ {_idx}: {_e}")
                    await _aio.sleep(2)  # avoid Polygon rate limit

                # ── 2 stock tickers per cycle (full rotation in ~25 min) ──────
                for _ in range(2):
                    _stock = _GEX_STOCK_TICKERS[_gex_stock_cursor % len(_GEX_STOCK_TICKERS)]
                    _gex_stock_cursor += 1
                    try:
                        _spot = await _loop.run_in_executor(None, _fetch_spot_sync, _stock, _key)
                        if _spot > 0:
                            await _loop.run_in_executor(None, _gfx_and_check, _stock, _spot)
                            _glog.info(f"GEX ✅ {_stock} @ ${_spot:.2f}")
                    except Exception as _e:
                        _glog.warning(f"GEX ❌ {_stock}: {_e}")
                    await _aio.sleep(2)

        except Exception as _outer_e:
            import logging as _log2
            _log2.getLogger("gex_refresh").error(f"GEX refresh loop error: {_outer_e}", exc_info=True)
        await _aio.sleep(300)   # 5 minutes


@app.on_event("startup")
async def start_gex_refresh():
    import asyncio as _aio
    _aio.create_task(_gex_refresh_loop())

@app.get("/api/options/gex/expiry-breakdown")
async def get_gex_expiry_breakdown(ticker: str = "SPY"):
    """Per-expiry GEX breakdown for Expiration Breakdown + Term Structure panels."""
    import glob, httpx as _hx
    from datetime import date as _date, timedelta as _td
    key = os.getenv("POLYGON_API_KEY", "")
    ticker = ticker.upper()
    try:
        # Fetch options with multiple expirations
        today = _date.today()
        exp_end = (today + _td(days=90)).isoformat()
        all_contracts = []
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
        # After market close: use tomorrow as start to skip expired 0DTE contracts
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _now_et = _dt.now(_ZI("America/New_York"))
        _market_closed = _now_et.hour >= 16 or _now_et.hour < 9
        exp_start = (today + _td(days=1)).isoformat() if _market_closed else today.isoformat()
        params = {"apiKey": key, "limit": 250, "expiration_date.gte": exp_start,
                  "expiration_date.lte": exp_end}
        async with _hx.AsyncClient(timeout=30) as client:
            for _page in range(12):   # max 12 pages × 250 = 3,000 contracts
                r = await client.get(url, params=params)
                data = r.json()
                results = data.get("results", [])
                all_contracts.extend(results)
                next_url = data.get("next_url", "")
                if not next_url or not results: break
                params = {"apiKey": key, "cursor": next_url.split("cursor=")[-1]}

        # Get spot price
        snap = await _hx.AsyncClient(timeout=8).get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            params={"apiKey": key})
        t = snap.json().get("ticker", {})
        spot = float(t.get("day", {}).get("c") or t.get("prevDay", {}).get("c") or 0)

        # Group by expiry
        from collections import defaultdict
        by_expiry = defaultdict(lambda: {"call_gex": 0, "put_gex": 0, "oi": 0,
                                          "contracts": 0, "strikes": set(), "pin": 0})
        strike_gex = defaultdict(lambda: defaultdict(float))

        for c in all_contracts:
            greeks  = c.get("greeks", {})
            gamma   = greeks.get("gamma", 0) or 0
            oi      = int(c.get("open_interest", 0) or 0)
            details = c.get("details", {})
            strike  = float(details.get("strike_price", 0) or 0)
            exp     = details.get("expiration_date", "")
            ct      = details.get("contract_type", "").lower()
            if not exp or not strike: continue

            gex_usd = abs(gamma) * oi * 100 * (spot ** 2) / 100 if spot > 0 else 0
            if ct == "call":
                by_expiry[exp]["call_gex"] += gex_usd
                strike_gex[exp][strike] += gex_usd
            else:
                by_expiry[exp]["put_gex"] -= gex_usd
                strike_gex[exp][strike] -= gex_usd
            by_expiry[exp]["oi"] += oi
            by_expiry[exp]["contracts"] += 1
            by_expiry[exp]["strikes"].add(strike)

        # Build breakdown list
        result = []
        for exp in sorted(by_expiry.keys()):
            d = by_expiry[exp]
            net = d["call_gex"] + d["put_gex"]
            dte = (_date.fromisoformat(exp) - today).days
            # Find pin (max abs GEX strike)
            s_gex = strike_gex[exp]
            pin = max(s_gex.keys(), key=lambda k: abs(s_gex[k])) if s_gex else 0
            call_wall = max((k for k,v in s_gex.items() if v > 0 and k > (spot or pin)), default=0)
            put_wall  = min((k for k,v in s_gex.items() if v < 0 and k < (spot or pin)), default=0)
            # Top strikes by abs GEX
            top_strikes = sorted(s_gex.items(), key=lambda x: abs(x[1]), reverse=True)[:6]
            result.append({
                "expiry":      exp,
                "dte":         dte,
                "call_gex_b":  round(d["call_gex"] / 1e9, 3),
                "put_gex_b":   round(d["put_gex"] / 1e9, 3),
                "net_gex_b":   round(net / 1e9, 3),
                "oi":          d["oi"],
                "contracts":   d["contracts"],
                "strike_count":len(d["strikes"]),
                "pin":         pin,
                "call_wall":   call_wall,
                "put_wall":    put_wall,
                "top_strikes": [{"strike": k, "gex_m": round(v/1e6, 1)} for k,v in top_strikes],
                "pct_of_total": 0,  # filled below
            })

        # Calc % of total
        total_abs = sum(abs(r["net_gex_b"]) for r in result) or 1
        for r in result:
            r["pct_of_total"] = round(abs(r["net_gex_b"]) / total_abs * 100, 1)

        return {"ticker": ticker, "spot": spot, "expirations": result,
                "total_contracts": len(all_contracts), "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e), "ticker": ticker, "expirations": []}


# ══════════════════════════════════════════════════════════════════════════════
# HEAT SEEKER — Unusual Options Flow Scanner
# ══════════════════════════════════════════════════════════════════════════════
import json as _hs_json

_HS_WATCHLIST_FILE = "logs/heatseeker_watchlist.json"
_HS_DEFAULT        = ["SPY", "QQQ", "IWM", "NVDA", "TSLA", "AAPL", "MSFT"]


def _load_hs_watchlist() -> list:
    try:
        parsed = _hs_json.loads(open(_HS_WATCHLIST_FILE).read())
        if isinstance(parsed, list) and parsed:
            return parsed
    except Exception:
        pass
    _save_hs_watchlist(_HS_DEFAULT)
    return list(_HS_DEFAULT)


def _save_hs_watchlist(tickers: list) -> None:
    import os as _os
    _os.makedirs("logs", exist_ok=True)
    open(_HS_WATCHLIST_FILE, "w").write(_hs_json.dumps(tickers))


# Load once at startup into module-level list
_hs_watchlist: list = _load_hs_watchlist()


@app.get("/api/heatseeker/watchlist")
async def hs_get_watchlist():
    """Return current Heat Seeker watchlist."""
    return {"tickers": _hs_watchlist}


@app.post("/api/heatseeker/watchlist")
async def hs_add_ticker(payload: dict):
    """Add a ticker to the watchlist."""
    ticker = (payload.get("ticker") or "").upper().strip()
    if not ticker:
        return {"error": "No ticker provided"}
    if ticker not in _hs_watchlist:
        _hs_watchlist.append(ticker)
        _save_hs_watchlist(_hs_watchlist)
    return {"tickers": _hs_watchlist}


@app.delete("/api/heatseeker/watchlist/{ticker}")
async def hs_remove_ticker(ticker: str):
    """Remove a ticker from the watchlist."""
    t = ticker.upper().strip()
    if t in _hs_watchlist:
        _hs_watchlist.remove(t)
        _save_hs_watchlist(_hs_watchlist)
    return {"tickers": _hs_watchlist}



@app.get("/api/heatseeker/summary")
async def hs_summary(mode: str = "scalp"):
    """
    Aggregate Heat Seeker signals per ticker into a directional bias summary.
    Returns top bullish strike, top bearish strike, and overall bias per ticker.
    """
    from backend.arka.heat_seeker import scan_ticker, load_watchlist
    import math

    watchlist = load_watchlist()
    summary = []

    for ticker in watchlist:
        try:
            signals = await scan_ticker(ticker, mode=mode)
        except Exception as e:
            print(f"[HS Summary] Error scanning {ticker}: {e}")
            continue

        if not signals:
            continue

        bull = [s for s in signals if s["bias"] == "🟢 BULLISH"]
        bear = [s for s in signals if s["bias"] == "🔴 BEARISH"]

        bull_count = len(bull)
        bear_count = len(bear)
        total      = bull_count + bear_count
        if total == 0:
            continue

        # Top strike for each direction (highest score)
        top_bull = max(bull, key=lambda x: x["score"]) if bull else None
        top_bear = max(bear, key=lambda x: x["score"]) if bear else None

        # Overall bias — need 60% threshold to call direction
        bear_pct = bear_count / total
        bull_pct = bull_count / total

        if bear_pct >= 0.60:
            bias       = "BEARISH"
            bias_emoji = "🔴"
            top_signal = top_bear
        elif bull_pct >= 0.60:
            bias       = "BULLISH"
            bias_emoji = "🟢"
            top_signal = top_bull
        else:
            bias       = "MIXED"
            bias_emoji = "⚪"
            top_signal = top_bear if (top_bear and top_bull and top_bear["score"] >= top_bull["score"]) else top_bull

        summary.append({
            "ticker":       ticker,
            "bias":         bias,
            "bias_emoji":   bias_emoji,
            "bull_count":   bull_count,
            "bear_count":   bear_count,
            "total":        total,
            "bull_pct":     round(bull_pct * 100),
            "bear_pct":     round(bear_pct * 100),
            "top_strike":   top_signal["strike"] if top_signal else None,
            "top_type":     top_signal["type"]   if top_signal else None,
            "top_score":    top_signal["score"]  if top_signal else None,
            "top_expiry":   top_signal["expiry"] if top_signal else None,
            "top_dte":      top_signal.get("dte") if top_signal else None,
            "top_bias":     top_signal["bias"]   if top_signal else None,
            "top_bull_strike":  top_bull["strike"] if top_bull else None,
            "top_bull_score":   top_bull["score"]  if top_bull else None,
            "top_bear_strike":  top_bear["strike"] if top_bear else None,
            "top_bear_score":   top_bear["score"]  if top_bear else None,
        })

    # Sort: bearish first, then mixed, then bullish
    order = {"BEARISH": 0, "MIXED": 1, "BULLISH": 2}
    summary.sort(key=lambda x: (order.get(x["bias"], 1), -x.get("top_score", 0)))

    return {
        "summary":    summary,
        "mode":       mode,
        "scanned_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    }

def _detect_expiry_clustering(signals: list) -> dict:
    """
    Detect when multiple signals target the same expiry date.
    3+ signals at the same DTE = institutions targeting a specific event.
    """
    from collections import Counter as _Counter
    expiry_counts    = _Counter()
    expiry_premium   = {}
    expiry_direction = {}

    for s in signals:
        exp = s.get("expiry", "")
        if not exp:
            continue
        expiry_counts[exp]  += 1
        expiry_premium[exp]  = expiry_premium.get(exp, 0) + s.get("premium", 0)
        if exp not in expiry_direction:
            expiry_direction[exp] = {"bull": 0, "bear": 0}
        if "BULL" in (s.get("bias") or "").upper():
            expiry_direction[exp]["bull"] += 1
        else:
            expiry_direction[exp]["bear"] += 1

    clusters = []
    for exp, count in expiry_counts.most_common(3):
        if count >= 3:
            bull = expiry_direction[exp]["bull"]
            bear = expiry_direction[exp]["bear"]
            direction = "BULLISH" if bull >= bear else "BEARISH"
            clusters.append({
                "expiry":     exp,
                "count":      count,
                "premium":    round(expiry_premium[exp], 2),
                "direction":  direction,
                "confidence": round(max(bull, bear) / count * 100, 1),
            })
    return {"clusters": clusters}


_hs_scan_cache: dict = {}   # mode → {"result": ..., "ts": float}
_HS_CACHE_TTL = 60          # seconds — scan is expensive (Polygon pagination)

@app.get("/api/heatseeker/scan")
async def hs_scan(mode: str = "swing"):
    """
    Scan all watchlist tickers. mode=scalp|swing.
    Scalp: 0DTE/1DTE ATM sweeps → DISCORD_ARKA_SCALP_EXTREME
    Swing: any DTE OTM flow   → DISCORD_FLOW_SIGNALS
    """
    import os as _os, httpx as _hx, time as _time
    from datetime import datetime as _dt2, timezone as _tz2
    from backend.arka.heat_seeker import scan_ticker as _hs_scan_ticker

    _mode = mode if mode in ("scalp", "swing") else "swing"

    # Return cached result if fresh enough — scan fetches 500-1500 contracts from Polygon
    _cached = _hs_scan_cache.get(_mode)
    if _cached and (_time.time() - _cached["ts"]) < _HS_CACHE_TTL:
        return _cached["result"]

    all_signals: list = []
    for _t in list(_hs_watchlist):
        try:
            sigs = await _hs_scan_ticker(_t, mode=_mode)
            all_signals.extend(sigs)
        except Exception as _e:
            print(f"  [HeatSeeker] ⚠️  {_t}: {_e}")
    all_signals.sort(key=lambda x: x["score"], reverse=True)

    # ── Enrich each signal with GEX alignment label + walls ──────────────
    # GEX score adjustment is already baked in by heat_seeker.py v2.
    # Here we just add display labels and wall levels for the UI.
    try:
        from backend.arka.gex_state import load_gex_state as _lgs
        for _sig in all_signals:
            try:
                _gex = _lgs(_sig.get("ticker", "SPY"))
                if _gex:
                    _rc  = _gex.get("regime_call", "NEUTRAL")
                    _adj = _sig.get("gex_adj", 0)
                    if _adj <= -25:
                        _sig["gex_alignment"] = "BLOCKED ❌"
                    elif _adj < -5:
                        _sig["gex_alignment"] = "AGAINST GEX ⚠️"
                    elif _adj > 5:
                        _sig["gex_alignment"] = "WITH GEX 🎯"
                    elif _rc == "FOLLOW_MOMENTUM":
                        _sig["gex_alignment"] = "MOMENTUM ⚡"
                    else:
                        _sig["gex_alignment"] = "NEUTRAL —"
                    _sig["gex_regime"]    = _rc
                    _sig["gex_call_wall"] = _gex.get("call_wall", 0)
                    _sig["gex_put_wall"]  = _gex.get("put_wall",  0)
                else:
                    _sig["gex_alignment"] = "—"
                    _sig["gex_regime"]    = "—"
            except Exception:
                _sig["gex_alignment"] = "—"
                _sig["gex_regime"]    = "—"
    except ImportError:
        pass

    # ── Expiry clustering ─────────────────────────────────────────────────
    clustering = _detect_expiry_clustering(all_signals)
    # Boost signals that match the top cluster's direction
    if clustering["clusters"]:
        _top = clustering["clusters"][0]
        for _sig in all_signals:
            if (_sig.get("expiry") == _top["expiry"] and
                    _top["direction"] in (_sig.get("bias") or "").upper()):
                _sig["score"]         = min(100, _sig["score"] + 10)
                _sig["cluster_boost"] = True

    # ── Scan quality metrics ──────────────────────────────────────────────
    scan_stats = {
        "total_signals":   len(all_signals),
        "high_conviction": len([s for s in all_signals if s["score"] >= 75]),
        "sweeps_detected": len([s for s in all_signals if s.get("is_sweep")]),
        "bullish_count":   len([s for s in all_signals if "BULL" in (s.get("bias") or "").upper()]),
        "bearish_count":   len([s for s in all_signals if "BEAR" in (s.get("bias") or "").upper()]),
        "top_ticker":      all_signals[0]["ticker"] if all_signals else None,
        "top_score":       all_signals[0]["score"]  if all_signals else 0,
        "cluster_detected": bool(clustering.get("clusters")),
    }

    # ── Write to file cache (for ARKA bridge fallback) ────────────────────
    try:
        from backend.arka.heat_seeker_bridge import update_cache_from_scan as _hs_update
        _hs_update(_mode, all_signals[:20])
    except Exception:
        pass

    result = {
        "signals":    all_signals,
        "watchlist":  list(_hs_watchlist),
        "mode":       _mode,
        "scanned_at": _dt2.now(_tz2.utc).isoformat(),
        "count":      len(all_signals),
        "clustering": clustering,
        "scan_stats": scan_stats,
    }

    # Cache result for 60s — prevents Polygon rate hits on rapid frontend polls
    _hs_scan_cache[_mode] = {"result": result, "ts": _time.time()}

    # ── Discord alerts by mode ─────────────────────────────────────────────
    if _mode == "scalp":
        _wh = _os.getenv("DISCORD_ARKA_SCALP_EXTREME", _os.getenv("DISCORD_FLOW_EXTREME", ""))
    else:
        _wh = _os.getenv("DISCORD_FLOW_SIGNALS", _os.getenv("DISCORD_ALERTS", ""))

    if _wh:
        for sig in all_signals:
            if sig["score"] < 70:
                break
            _prem = (f"${sig['premium']/1_000_000:.2f}M"
                     if sig['premium'] >= 1_000_000
                     else f"${sig['premium']/1_000:.1f}K")
            if _mode == "scalp":
                _msg = (f"⚡ SCALP FLOW | {sig['ticker']} {sig['type']} ${sig['strike']} "
                        f"{'0DTE' if sig.get('dte',999)==0 else str(sig.get('dte',1))+'DTE'} | "
                        f"{sig['vol_mult']}x | {_prem} | {sig['bias']} | Score: {sig['score']}")
            else:
                _msg = (f"🌊 SWING FLOW | {sig['ticker']} {sig['type']} ${sig['strike']} "
                        f"{sig['expiry']} | {sig['vol_mult']}x | {_prem} | "
                        f"{sig['bias']} | Score: {sig['score']}")
            try:
                await asyncio.to_thread(
                    lambda m=_msg: _hx.post(_wh, json={"content": m}, timeout=5)
                )
            except Exception:
                pass

            # ── Institutional flow check on high-score Heat Seeker signals ──
            if sig.get("score", 0) >= 75:
                try:
                    from backend.chakra.flow_monitor import (
                        is_institutional_flow as _iif,
                        institutional_cooldown_ok as _ico,
                    )
                    from backend.arka.arka_discord_notifier import post_institutional_flow as _pif_hs
                    _hs_sig = {
                        "ticker":        sig.get("ticker", ""),
                        "direction":     "BULLISH" if "BULL" in (sig.get("bias") or "").upper() else "BEARISH",
                        "strike":        sig.get("strike", 0),
                        "dte":           sig.get("dte", 0),
                        "premium":       sig.get("premium", 0),
                        "dark_pool_pct": 75,  # heat seeker = high institutional by definition
                        "vol_ratio":     sig.get("vol_mult", 0),
                        "score":         sig.get("score", 0),
                        "execution":     "SWEEP",
                    }
                    _is_i, _t_i, _b_i = _iif(_hs_sig)
                    if _is_i and _ico(sig.get("ticker",""), sig.get("strike",0), _hs_sig["direction"]):
                        await asyncio.to_thread(lambda s=_hs_sig: _pif_hs(s))
                except Exception:
                    pass

    return result


# In-memory cache for last scan results per ticker (for ARKA boost)
_hs_latest_cache: dict = {}   # ticker → list[signal]
_hs_latest_ts:    dict = {}   # ticker → epoch float


@app.get("/api/heatseeker/latest")
async def hs_latest(ticker: str = "SPY"):
    """
    Return the most recent Heat Seeker signals for a specific ticker.
    Used by ARKA engine for conviction boost (+15 if direction confirmed).
    Results are cached per scan; stale after 10 minutes.
    """
    from datetime import datetime as _dt2, timezone as _tz2
    from backend.arka.heat_seeker import scan_ticker as _hs_scan_ticker

    _t   = ticker.upper().strip()
    _now = _dt2.now(_tz2.utc).timestamp()

    # Return cached if fresh (< 10 min)
    if _t in _hs_latest_ts and (_now - _hs_latest_ts[_t]) < 600:
        return {"ticker": _t, "signals": _hs_latest_cache.get(_t, []),
                "cached": True, "age_seconds": int(_now - _hs_latest_ts[_t])}

    # Fresh scan (swing mode — captures more signals)
    try:
        sigs = await _hs_scan_ticker(_t, mode="swing")
        _hs_latest_cache[_t] = sigs
        _hs_latest_ts[_t]    = _now
        return {"ticker": _t, "signals": sigs, "cached": False, "age_seconds": 0}
    except Exception as e:
        return {"ticker": _t, "signals": [], "error": str(e)}


# ── /api/health — simple health check for Step 8b ───────────────────────────
@app.get("/api/health")
async def api_health():
    """Health check: arka_running, flow_monitor_running, last_signal_time,
    open_positions, daily_pnl, uptime_seconds."""
    import subprocess, time as _time
    from pathlib import Path as _P

    def _proc(pattern: str) -> bool:
        try:
            r = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True)
            return bool(r.stdout.strip())
        except Exception:
            return False

    _arka_running   = _proc("arka_engine")
    _flow_running   = _proc("flow_monitor")

    # Last signal time from ARKA summary
    _last_signal    = None
    _open_positions = 0
    _daily_pnl      = 0.0
    try:
        _sf = _P("logs/arka/arka_summary.json")
        if _sf.exists():
            _sd = json.loads(_sf.read_text())
            _last_signal    = _sd.get("last_scan_time")
            _open_positions = len(_sd.get("open_positions", {}))
            _daily_pnl      = float(_sd.get("daily_pnl", 0))
    except Exception:
        pass

    # Uptime from process start (approximate via log file mtime)
    _uptime = None
    try:
        import pathlib
        for _lf in ["logs/dashboard_api.log", "logs/arka/arka_engine.log"]:
            _p = pathlib.Path(_lf)
            if _p.exists():
                _uptime = round(_time.time() - _p.stat().st_mtime)
                break
    except Exception:
        pass

    return {
        "arka_running":        _arka_running,
        "flow_monitor_running": _flow_running,
        "last_signal_time":    _last_signal,
        "open_positions":      _open_positions,
        "daily_pnl":           _daily_pnl,
        "uptime_seconds":      _uptime,
        "status":              "ok",
        "timestamp":           datetime.now().isoformat(),
    }


# ── Ensure logs/gex directory exists on startup ──────────────────────────────
@app.on_event("startup")
async def ensure_log_dirs():
    for _d in ["logs/gex", "logs/arka", "logs/chakra", "logs/signals",
               "logs/arjun", "logs/arjun/memory"]:
        os.makedirs(_d, exist_ok=True)

# ── Trade ARJUN Analysis ─────────────────────────────────────────────────────

@app.post("/api/trades/analyze")
async def analyze_trade(body: dict):
    """Get ARJUN analysis for an open options position."""
    ticker     = body.get("ticker", "SPY")
    entry      = float(body.get("entry", 0) or 0)
    direction  = body.get("direction", "CALL")
    contract   = body.get("contract", "")
    pnl_pct    = float(body.get("pnl_pct", 0) or 0)

    import time as _time
    from pathlib import Path as _Path

    # GEX context
    gex_context = ""
    _gex_f = _Path(f"logs/gex/gex_latest_{ticker}.json")
    if _gex_f.exists():
        try:
            _gd = json.loads(_gex_f.read_text())
            if _time.time() - _gd.get("ts", 0) < 3600:
                gex_context = (
                    f"GEX Regime: {_gd.get('regime','?')} | "
                    f"Regime Call: {_gd.get('regime_call','?')} | "
                    f"Call Wall: ${_gd.get('call_wall',0):.2f} | "
                    f"Put Wall: ${_gd.get('put_wall',0):.2f} | "
                    f"Zero Gamma: ${_gd.get('zero_gamma',0):.2f}"
                )
        except Exception:
            pass

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"success": False, "error": "ANTHROPIC_API_KEY not set",
                "analysis": "Analysis unavailable — API key not configured."}
    try:
        import httpx as _hx
        async with _hx.AsyncClient(timeout=30) as _cl:
            _r = await _cl.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"You are ARJUN, CHAKRA's trading analyst. Analyze this open options position:\n\n"
                            f"TICKER: {ticker}\nDIRECTION: {direction}\nCONTRACT: {contract}\n"
                            f"ENTRY: ${entry:.2f}\nCURRENT P&L: {pnl_pct:+.1f}%\n"
                            f"{gex_context}\n\n"
                            f"Provide: 1. HOLD, EXIT, or ADD? 2. Key risk. 3. Target/stop.\n"
                            f"Keep under 80 words. Direct trader language."
                        ),
                    }],
                },
            )
        _data = _r.json()
        _text = _data.get("content", [{}])[0].get("text", "No analysis returned.")
        return {"success": True, "analysis": _text, "ticker": ticker}
    except Exception as _e:
        return {"success": False, "error": str(_e),
                "analysis": f"Analysis error: {str(_e)[:100]}"}


# ── Module Status + Refresh Endpoints ────────────────────────────────────────

@app.get("/api/modules/status")
def api_modules_status():
    """Return live status for all 12 power intelligence modules."""
    try:
        from backend.chakra.modules.run_all_modules import get_all_module_status
        return {"modules": get_all_module_status(), "timestamp": __import__("time").time()}
    except Exception as _e:
        return {"error": str(_e), "modules": {}}


@app.post("/api/modules/refresh")
async def api_modules_refresh(force: bool = False):
    """Trigger module refresh run via run_all_modules.py subprocess."""
    import asyncio, sys as _sys
    try:
        _cmd = [_sys.executable, "backend/chakra/modules/run_all_modules.py"]
        if force:
            _cmd.append("--force")
        proc = await asyncio.create_subprocess_exec(
            *_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        return {
            "success": proc.returncode == 0,
            "output":  stdout.decode()[-800:] if stdout else "",
            "error":   stderr.decode()[-300:] if stderr else "",
        }
    except asyncio.TimeoutError:
        return {"success": False, "error": "Timeout after 120s"}
    except Exception as _e:
        return {"success": False, "error": str(_e)}


# ── CHAKRA Agentic Pipeline Scheduler ────────────────────────────────────────
_chakra_scheduler = None

@app.on_event("startup")
async def start_chakra_scheduler():
    global _chakra_scheduler
    try:
        from backend.arjun.pipeline.scheduler import start_scheduler
        _chakra_scheduler = start_scheduler()
        if _chakra_scheduler:
            print("✅ CHAKRA agentic pipeline scheduler started (15min cycles)")
    except Exception as _e:
        print(f"⚠️  CHAKRA scheduler start failed (non-fatal): {_e}")
