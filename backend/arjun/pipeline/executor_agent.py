"""
CHAKRA Trade Executor Agent
Validates risk, finds options contracts, places orders via Alpaca.
The last gate before capital is deployed.
"""
import os, logging, re
from datetime import datetime, date, timedelta

log = logging.getLogger("CHAKRA.Executor")

RISK_CONFIG = {
    "max_position_pct":   0.05,
    "max_daily_loss_pct": 0.03,
    "max_open_positions": 3,
    "vix_limit":          35.0,
    "min_conviction":     "MED",
    "max_contracts":      3,
}


async def executor_node(state: dict) -> dict:
    """Executor Agent node. Validates each signal against risk rules and places orders."""
    import httpx

    signals  = state.get("trade_signals",[])
    report   = state.get("research_report",{}) or {}
    vix      = state.get("vix", report.get("vix",20))
    results  = []

    if not signals:
        log.info("  ⏭️  No signals to execute")
        state["execution_results"] = []
        return state

    log.info(f"⚡ Executor Agent: processing {len(signals)} signals")

    headers = {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET","") or os.getenv("ALPACA_SECRET_KEY",""),
    }

    async with httpx.AsyncClient(timeout=12) as client:

        # Account state
        acct_r   = await client.get(
            "https://paper-api.alpaca.markets/v2/account", headers=headers
        )
        account    = acct_r.json() if acct_r.status_code == 200 else {}
        equity     = float(account.get("equity",100000) or 100000)
        last_eq    = float(account.get("last_equity", equity) or equity)
        daily_pl   = equity - last_eq

        pos_r      = await client.get(
            "https://paper-api.alpaca.markets/v2/positions", headers=headers
        )
        open_pos   = pos_r.json() if pos_r.status_code == 200 else []
        if not isinstance(open_pos, list): open_pos = []
        open_count = len(open_pos)

        for sig in signals:
            ticker      = sig.get("ticker","")
            conviction  = sig.get("conviction","LOW")
            confidence  = sig.get("confidence",0)
            contract_t  = sig.get("contract_type","NONE")
            dte_pref    = sig.get("dte_preference",1)
            entry_price = sig.get("entry_price",0)

            result = {
                "ticker":  ticker,
                "signal":  sig,
                "status":  "skipped",
                "reason":  "",
                "order_id": "",
            }

            # ── Risk Gates ──────────────────────────────────────────────────
            if vix > RISK_CONFIG["vix_limit"]:
                result["reason"] = f"VIX {vix:.1f} > limit {RISK_CONFIG['vix_limit']}"
                results.append(result)
                log.info(f"  ⛔ {ticker}: {result['reason']}")
                continue

            if daily_pl < -(equity * RISK_CONFIG["max_daily_loss_pct"]):
                result["reason"] = f"Daily loss limit hit (${daily_pl:.0f})"
                results.append(result)
                log.info(f"  ⛔ HALT: {result['reason']}")
                break

            if open_count >= RISK_CONFIG["max_open_positions"]:
                result["reason"] = f"Max positions ({open_count}/{RISK_CONFIG['max_open_positions']})"
                results.append(result)
                log.info(f"  ⛔ {ticker}: {result['reason']}")
                continue

            if conviction == "LOW":
                result["reason"] = "Conviction too low (LOW)"
                results.append(result)
                continue

            if contract_t not in ("CALL","PUT"):
                result["reason"] = "No valid contract direction"
                results.append(result)
                continue

            # ── Find Options Contract ──────────────────────────────────────
            opt_type = contract_t.lower()
            today    = date.today()
            exp_max  = (today + timedelta(days=dte_pref+1)).isoformat()

            contract_r = await client.get(
                "https://paper-api.alpaca.markets/v2/options/contracts",
                headers=headers,
                params={
                    "underlying_symbols":  ticker,
                    "type":                opt_type,
                    "expiration_date_gte": today.isoformat(),
                    "expiration_date_lte": exp_max,
                    "strike_price_gte":    str(round(entry_price * 0.97, 0)),
                    "strike_price_lte":    str(round(entry_price * 1.03, 0)),
                    "limit": 10,
                }
            )

            contracts = contract_r.json().get("option_contracts",[])
            if not contracts:
                contract_r2 = await client.get(
                    "https://paper-api.alpaca.markets/v2/options/contracts",
                    headers=headers,
                    params={
                        "underlying_symbols":  ticker,
                        "type":                opt_type,
                        "expiration_date_gte": today.isoformat(),
                        "expiration_date_lte": (today+timedelta(days=3)).isoformat(),
                        "limit": 10,
                    }
                )
                contracts = contract_r2.json().get("option_contracts",[])

            if not contracts:
                result["reason"] = f"No {opt_type} contracts found for {ticker}"
                results.append(result)
                log.warning(f"  ⚠️  {ticker}: {result['reason']}")
                continue

            # Pick ATM contract (earliest expiry, closest to ATM)
            contracts.sort(key=lambda c: (
                c.get("expiration_date",""),
                abs(float(c.get("strike_price",0)) - entry_price)
            ))
            contract     = contracts[0]
            contract_sym = contract.get("symbol","")
            strike       = float(contract.get("strike_price",0))
            expiry       = contract.get("expiration_date","")

            # ── Validate with order_guard ────────────────────────────────
            try:
                from backend.arka.order_guard import validate_options_order
                is_valid, guard_reason = validate_options_order(contract_sym, 1)
                if not is_valid:
                    result["reason"] = f"Order guard: {guard_reason}"
                    results.append(result)
                    log.error(f"  ❌ {ticker}: {result['reason']}")
                    continue
            except ImportError:
                pass  # order_guard not available — proceed

            # ── Place Order ──────────────────────────────────────────────
            log.info(f"  📤 Placing {contract_sym} ({contract_t}) for {ticker}")

            order_r = await client.post(
                "https://paper-api.alpaca.markets/v2/orders",
                headers=headers,
                json={
                    "symbol":        contract_sym,
                    "qty":           "1",
                    "side":          "buy",
                    "type":          "market",
                    "time_in_force": "day",
                    "asset_class":   "us_option",
                }
            )

            if order_r.status_code in (200, 201):
                order_data = order_r.json()
                result.update({
                    "status":        "placed",
                    "order_id":      order_data.get("id",""),
                    "contract_sym":  contract_sym,
                    "strike":        strike,
                    "expiry":        expiry,
                    "contract_type": contract_t,
                    "reason":        f"Order placed: {contract_sym}",
                })
                open_count += 1

                log.info(f"  ✅ {ticker}: order placed {contract_sym} "
                         f"strike=${strike} exp={expiry}")

                # Discord notification
                try:
                    from backend.chakra.discord_router import post_scalp_alert
                    from datetime import datetime as _dt
                    embed = {
                        "title":  f"⚡ CHAKRA PIPELINE — {ticker} {contract_t}",
                        "color":  0x00FF88 if contract_t=="CALL" else 0xFF4444,
                        "fields": [
                            {"name":"Contract",   "value":contract_sym,"inline":True},
                            {"name":"Strike",     "value":f"${strike}","inline":True},
                            {"name":"Expiry",     "value":expiry,      "inline":True},
                            {"name":"Conviction", "value":conviction,  "inline":True},
                            {"name":"Confidence", "value":f"{confidence:.0%}","inline":True},
                            {"name":"Rationale",  "value":sig.get("rationale","")[:200],"inline":False},
                        ],
                        "footer": {"text": f"CHAKRA Agentic Pipeline • {_dt.now().strftime('%I:%M %p ET')}"}
                    }
                    post_scalp_alert(ticker, embed)
                except Exception as de:
                    log.debug(f"  Discord notify skipped: {de}")
            else:
                result["reason"] = f"Order failed: {order_r.status_code}"
                log.error(f"  ❌ {ticker}: {result['reason']}")

            results.append(result)

    placed  = [r for r in results if r.get("status")=="placed"]
    skipped = [r for r in results if r.get("status")=="skipped"]

    state["execution_results"] = results
    log.info(f"✅ Executor complete: {len(placed)} placed, {len(skipped)} skipped")
    return state
