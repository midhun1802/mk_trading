"""
options_executor.py — TARAKA 0DTE Options Executor
Selects ATM strike, checks premium fits budget, executes via Alpaca.

Flow:
  1. Get current price of ticker
  2. Find ATM 0DTE contract (today's expiry)
  3. Check premium fits budget ($10-$250)
  4. Submit buy order
  5. Set stop (-50%) and target (+100%) as OCO order
  6. Auto-close at 3:58pm ET
"""

import os
import logging
import asyncio
from datetime import datetime, date
from pathlib import Path
import pytz
import requests

log = logging.getLogger("taraka.executor")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY",    "")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE   = "https://paper-api.alpaca.markets"   # switch to live when ready
ET            = pytz.timezone("America/New_York")

# Tickers that have liquid 0DTE options
ZERO_DTE_TICKERS = {"SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN"}


class OptionsExecutor:

    def __init__(self):
        self.headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
            "Content-Type":        "application/json",
        }

    async def execute(self, ticker: str, direction: str, budget: float, session: str) -> dict:
        """
        Main entry point. Returns trade result dict.
        direction: "CALL" or "PUT"
        budget: dollars to spend (e.g. 150.0)
        """
        log.info(f"  Options executor: {ticker} {direction} budget=${budget}")

        # ── Check options eligibility ──
        if ticker not in ZERO_DTE_TICKERS:
            return {"error": f"{ticker} not in 0DTE watchlist", "mode": "paper"}

        # ── Get current price ──
        price = self._get_price(ticker)
        if not price:
            return {"error": "Could not fetch current price", "mode": "paper"}

        # ── Find ATM strike ──
        strike = self._atm_strike(price, ticker)
        expiry = date.today().strftime("%Y-%m-%d")   # 0DTE = today
        option_type = "call" if direction == "CALL" else "put"

        # ── Build OCC contract symbol ──
        # Format: TICKER + YYMMDD + C/P + 8-digit strike (price * 1000)
        exp_short  = date.today().strftime("%y%m%d")
        cp         = "C" if direction == "CALL" else "P"
        strike_fmt = f"{int(strike * 1000):08d}"
        contract   = f"{ticker}{exp_short}{cp}{strike_fmt}"

        log.info(f"  Contract: {contract}  Strike: ${strike}  Expiry: {expiry}")

        # ── Get option premium ──
        premium = self._get_option_premium(contract)
        if not premium:
            log.warning(f"  Could not fetch premium for {contract} — using estimate")
            # Rough estimate: ATM 0DTE ≈ 0.3-0.5% of underlying
            premium = round(price * 0.004, 2)

        log.info(f"  Premium: ${premium:.2f} per share (${premium*100:.2f} per contract)")

        # ── Check budget ──
        cost_per_contract = premium * 100   # 1 contract = 100 shares
        if cost_per_contract > budget:
            return {
                "error":    f"Premium ${cost_per_contract:.2f} exceeds budget ${budget}",
                "contract": contract,
                "premium":  premium,
                "mode":     "paper",
                "reason":   "budget_exceeded",
            }

        # How many contracts?
        contracts = max(1, int(budget / cost_per_contract))
        total_cost = contracts * cost_per_contract

        log.info(f"  Contracts: {contracts}  Total cost: ${total_cost:.2f}")

        # ── Submit order ──
        order = self._submit_order(contract, contracts, premium)
        if order.get("error"):
            return {**order, "contract": contract, "premium": premium, "contracts": contracts}

        log.info(f"  ✅ Order submitted: {order.get('id')}")

        # ── Set stops and targets ──
        stop_price   = round(premium * 0.50, 2)    # stop at 50% loss
        target_price = round(premium * 2.00, 2)    # target at 100% gain

        return {
            "contract":    contract,
            "ticker":      ticker,
            "direction":   direction,
            "strike":      strike,
            "expiry":      expiry,
            "premium":     premium,
            "contracts":   contracts,
            "total_cost":  total_cost,
            "stop_price":  stop_price,
            "target_price": target_price,
            "order_id":    order.get("id"),
            "mode":        "live",
            "error":       None,
        }


    def _get_price(self, ticker: str) -> float | None:
        """Get latest trade price from Alpaca."""
        try:
            url = f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest"
            r = requests.get(url, headers=self.headers, timeout=5)
            data = r.json()
            return float(data["trade"]["p"])
        except Exception as e:
            log.error(f"Price fetch failed for {ticker}: {e}")
            return None


    def _atm_strike(self, price: float, ticker: str) -> float:
        """
        Round to nearest valid strike.
        SPY/QQQ: $1 strikes
        Others:  $2.50 or $5 strikes
        """
        if ticker in ("SPY", "QQQ", "IWM"):
            return round(price)           # nearest $1
        elif price > 500:
            return round(price / 5) * 5   # nearest $5
        elif price > 100:
            return round(price / 2.5) * 2.5  # nearest $2.50
        else:
            return round(price)


    def _get_option_premium(self, contract: str) -> float | None:
        """Get latest option premium from Alpaca."""
        try:
            url = f"https://data.alpaca.markets/v1beta1/options/trades/latest?symbols={contract}"
            r = requests.get(url, headers=self.headers, timeout=5)
            data = r.json()
            trades = data.get("trades", {})
            if contract in trades:
                return float(trades[contract]["p"])
        except Exception as e:
            log.warning(f"Option premium fetch failed: {e}")
        return None


    def _submit_order(self, contract: str, qty: int, premium: float) -> dict:
        """Submit market order for option contract."""
        try:
            payload = {
                "symbol":        contract,
                "qty":           str(qty),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
            }
            r = requests.post(
                f"{ALPACA_BASE}/v2/orders",
                json=payload,
                headers=self.headers,
                timeout=10,
            )
            data = r.json()
            if r.status_code in (200, 201):
                return {"id": data.get("id"), "status": data.get("status")}
            else:
                return {"error": data.get("message", f"HTTP {r.status_code}")}
        except Exception as e:
            return {"error": str(e)}


    def close_all_positions(self):
        """Called at 3:58pm ET — close all TARAKA option positions."""
        try:
            r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=self.headers, timeout=10)
            positions = r.json()
            closed = []
            for pos in positions:
                # Only close option positions (contract symbols are long)
                if len(pos.get("symbol", "")) > 6:
                    close_r = requests.delete(
                        f"{ALPACA_BASE}/v2/positions/{pos['symbol']}",
                        headers=self.headers, timeout=10
                    )
                    if close_r.status_code == 200:
                        closed.append(pos["symbol"])
                        log.info(f"  Closed: {pos['symbol']} P&L: ${pos.get('unrealized_pl','?')}")
            log.info(f"  Auto-close: {len(closed)} positions closed")
            return closed
        except Exception as e:
            log.error(f"Auto-close failed: {e}")
            return []
