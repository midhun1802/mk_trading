"""
ARKA — Standalone Trading Journal
File: backend/arka/trading_journal.py

Posts the daily trading journal to Discord.
Runs independently of the engine (via crontab) so the journal
fires even if the engine had issues during the session.

Crontab:
    05 16 * * 1-5  cd ~/trading-ai && venv/bin/python3 backend/arka/trading_journal.py >> logs/arka/journal.log 2>&1
"""

import os
import json
import logging
from datetime import date, datetime
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parents[2]
load_dotenv(BASE / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Journal] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ARKA.Journal")


def post_journal():
    from backend.arka.arka_discord_notifier import _post

    today = date.today()
    summary_path = BASE / f"logs/arka/summary_{today}.json"

    trade_log  = []
    daily_pnl  = 0.0
    wins       = 0
    losses     = 0

    if summary_path.exists():
        try:
            data      = json.loads(summary_path.read_text())
            trade_log = data.get("trade_log", [])
            daily_pnl = float(data.get("daily_pnl", 0) or 0)
            wins      = data.get("wins", 0)
            losses    = data.get("losses", 0)
        except Exception as e:
            log.warning(f"Could not read summary: {e}")
    else:
        log.warning(f"No summary file found for {today} — posting empty journal")

    # Also pull closed positions from Alpaca to cross-check P&L
    try:
        import httpx
        headers = {
            "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "") or os.getenv("ALPACA_SECRET_KEY", ""),
        }
        acct_r    = httpx.get("https://paper-api.alpaca.markets/v2/account", headers=headers, timeout=8).json()
        equity    = float(acct_r.get("equity", 0))
        last_eq   = float(acct_r.get("last_equity", equity))
        acct_pnl  = round(equity - last_eq, 2)
        positions = httpx.get("https://paper-api.alpaca.markets/v2/positions", headers=headers, timeout=8).json()
        open_pos  = positions if isinstance(positions, list) else []
    except Exception as e:
        log.warning(f"Alpaca fetch failed: {e}")
        acct_pnl = daily_pnl
        open_pos = []

    # Build trade lines
    lines = []
    for t in trade_log:
        p    = t.get("pnl")
        icon = "✅" if (p or 0) > 0 else ("❌" if (p or 0) < 0 else "⏳")
        pstr = f"{'+' if (p or 0) >= 0 else ''}${abs(p or 0):.2f}" if p is not None else "open"
        lines.append(
            f"{icon} **{t.get('ticker','?')}** {t.get('side','?')} "
            f"@${t.get('price', 0):.2f} → `{pstr}`"
        )

    # Still-open positions block
    open_lines = []
    for p in open_pos:
        sym = p.get("symbol", "")
        unr = float(p.get("unrealized_pl", 0))
        mv  = float(p.get("market_value", 0))
        icon = "🟢" if unr >= 0 else "🔴"
        open_lines.append(f"{icon} `{sym}` MV=${mv:.0f} UPL={'+' if unr>=0 else ''}${unr:.2f}")

    total_trades = len(trade_log)
    wr           = round(wins / total_trades * 100) if total_trades else 0
    color        = 0x00E676 if acct_pnl >= 0 else 0xFF4444
    pnl_sign     = "+" if acct_pnl >= 0 else ""

    fields = [
        {"name": "Trades",     "value": str(total_trades), "inline": True},
        {"name": "Win Rate",   "value": f"{wr}%",           "inline": True},
        {"name": "Account P&L","value": f"{pnl_sign}${acct_pnl:,.2f}", "inline": True},
    ]
    if lines:
        fields.append({"name": "Trade Log", "value": "\n".join(lines[:15]), "inline": False})
    else:
        fields.append({"name": "Trade Log", "value": "*No closed trades today*", "inline": False})
    if open_lines:
        fields.append({"name": f"Still Open ({len(open_lines)})", "value": "\n".join(open_lines), "inline": False})

    embed = {
        "title":     f"{'📈' if acct_pnl >= 0 else '📉'}  ARKA TRADING JOURNAL — {today.strftime('%B %d, %Y')}",
        "color":     color,
        "fields":    fields,
        "footer":    {"text": f"ARKA Engine · EOD {today.strftime('%b %d')} · {datetime.now().strftime('%H:%M ET')}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    ok = _post({"embeds": [embed]})
    log.info(f"Trading journal {'posted ✅' if ok else 'failed ❌'} — trades={total_trades} pnl={pnl_sign}${acct_pnl:.2f}")
    return ok


if __name__ == "__main__":
    post_journal()
