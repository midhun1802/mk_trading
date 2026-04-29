"""
CHAKRA Flow Monitor
Combines Dark Pool + UOA signals → generates real dynamic confidence scores
"""

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("CHAKRA.FlowMonitor")
ET  = ZoneInfo("America/New_York")


# ── Real Confidence Scorer ─────────────────────────────────────────────────

def calc_confidence(
    vol_oi_ratio: float   = 1.0,
    dark_pool_pct: float  = 0.0,
    flow_dom_pct: float   = 0.0,   # % of flow in same direction (e.g. 100% calls)
    iv: float             = 0.0,
    iv_avg: float         = 0.0,
    dte: int              = 30,
    dp_aligned: bool      = False,  # dark pool direction matches options bias
    is_extreme: bool      = False,
    whale_blocks: int     = 0,      # number of large dark pool prints aligned
) -> int:
    """
    Dynamically calculate confidence 0-100 from real signal data.
    Each factor contributes weighted points.

    Factor breakdown (max 100):
      Vol/OI ratio       → up to 30 pts
      Flow dominance %   → up to 20 pts
      Dark pool %        → up to 15 pts
      DP alignment       → up to 15 pts
      IV elevation       → up to 10 pts
      DTE urgency        → up to  5 pts
      Whale blocks       → up to  5 pts
    """
    score = 0.0

    # 1. Vol/OI ratio — core signal strength (0–30 pts)
    if vol_oi_ratio >= 100:  score += 30
    elif vol_oi_ratio >= 50: score += 26
    elif vol_oi_ratio >= 20: score += 22
    elif vol_oi_ratio >= 10: score += 17
    elif vol_oi_ratio >= 5:  score += 12
    elif vol_oi_ratio >= 2:  score += 6
    else:                    score += 2

    # 2. Flow dominance — how one-sided the flow is (0–20 pts)
    if flow_dom_pct >= 95:   score += 20
    elif flow_dom_pct >= 85: score += 16
    elif flow_dom_pct >= 70: score += 11
    elif flow_dom_pct >= 55: score += 6
    else:                    score += 2

    # 3. Dark pool % of volume — smart money participation (0–15 pts)
    if dark_pool_pct >= 50:  score += 15
    elif dark_pool_pct >= 35: score += 11
    elif dark_pool_pct >= 25: score += 7
    elif dark_pool_pct >= 15: score += 3
    else:                    score += 0

    # 4. Dark pool direction aligned with options bias (0–15 pts)
    if dp_aligned:
        if dark_pool_pct >= 35: score += 15
        elif dark_pool_pct >= 20: score += 10
        else:                     score += 5
    else:
        # Contradiction — slight penalty
        score -= 5

    # 5. IV elevation vs average (0–10 pts)
    if iv_avg > 0:
        iv_mult = iv / iv_avg
        if iv_mult >= 2.0:   score += 10
        elif iv_mult >= 1.5: score += 7
        elif iv_mult >= 1.2: score += 4
        else:                score += 1

    # 6. DTE urgency — 0DTE/1DTE sweeps are stronger signals (0–5 pts)
    if dte == 0:   score += 5
    elif dte <= 3: score += 4
    elif dte <= 7: score += 2

    # 7. Whale dark pool blocks aligned (0–5 pts)
    if whale_blocks >= 3:   score += 5
    elif whale_blocks >= 2: score += 3
    elif whale_blocks >= 1: score += 2

    # Extreme flag bonus
    if is_extreme:
        score += 5

    # Clamp to 40–98 (never show 0 or 100 — those are dishonest)
    return max(40, min(98, int(round(score))))


def get_recommendation(bias: str, ticker: str, confidence: int) -> tuple[str, str]:
    """Returns (action, reasoning) based on bias + confidence."""
    is_bull = bias == "BULLISH"
    action  = f"BUY CALL on {ticker}" if is_bull else f"BUY PUT on {ticker}"

    if confidence >= 85:
        reason = "Extreme unusual activity with strong directional alignment"
    elif confidence >= 75:
        reason = "High conviction flow — dark pool + options flow aligned"
    elif confidence >= 65:
        reason = "Elevated unusual activity — monitor for continuation"
    else:
        reason = "Moderate signal — wait for price confirmation before entry"

    return action, reason


# ── Flow Monitor Loop ──────────────────────────────────────────────────────

async def run_flow_monitor_cycle():
    """
    One full cycle: scan dark pool + UOA, combine signals, post to Discord.
    Called every N minutes by the run loop.
    """
    from flow.darkpoolscanner  import scan_dark_pool
    from flow.uoadetector      import run_uoa_scan
    from arkadiscordnotifier   import send, CHCHAKRAEXTREME, CHCHAKRASIGNALS, nowet

    now_str = datetime.now(ET).strftime("%I:%M %p ET")

    # Run both scans concurrently
    dp_results, uoa_results = await asyncio.gather(
        scan_dark_pool(),
        run_uoa_scan(),
    )

    # Merge by ticker
    all_tickers = set(list(dp_results.keys()) + list(uoa_results.keys()))

    for ticker in all_tickers:
        try:
            dp  = dp_results.get(ticker, {})
            uoa = uoa_results.get(ticker, [])

            if not dp and not uoa:
                continue

            # ── Gather raw inputs for confidence scorer ──────────────────
            dp_pct       = float(dp.get("dark_pool_pct",      0))
            dp_notional  = float(dp.get("total_notional",     0))
            dp_bull_pct  = float(dp.get("cumul_bull_pct",     50))
            dp_bias      = "BULLISH" if dp_bull_pct >= 55 else "BEARISH" if dp_bull_pct <= 45 else "NEUTRAL"
            whale_blocks = int(dp.get("whale_block_count",    0))
            large_prints = dp.get("large_prints", [])

            # Best UOA hit for this ticker
            top_uoa = uoa[0] if uoa else {}
            vol_oi       = float(top_uoa.get("vol_oi_ratio",  1))
            flow_dom     = float(top_uoa.get("flow_dom_pct",  50))
            iv           = float(top_uoa.get("iv",            0))
            iv_avg       = float(top_uoa.get("iv_avg",        0))
            dte          = int(top_uoa.get("dte",             30))
            options_bias = top_uoa.get("bias", "NEUTRAL")
            is_extreme   = bool(top_uoa.get("is_extreme",     False))
            contract     = top_uoa.get("contract_type", "CALL")
            strike       = top_uoa.get("strike", 0)
            expiry       = top_uoa.get("expiry", "")
            premium      = float(top_uoa.get("premium",       0))

            # Determine combined bias
            if options_bias in ("BULLISH", "BEARISH"):
                combined_bias = options_bias
            elif dp_bias in ("BULLISH", "BEARISH"):
                combined_bias = dp_bias
            else:
                continue   # no clear bias — skip

            dp_aligned = (combined_bias == dp_bias) if dp_bias != "NEUTRAL" else False

            # ── REAL CONFIDENCE CALCULATION ──────────────────────────────
            confidence = calc_confidence(
                vol_oi_ratio  = vol_oi,
                dark_pool_pct = dp_pct,
                flow_dom_pct  = flow_dom,
                iv            = iv,
                iv_avg        = iv_avg,
                dte           = dte,
                dp_aligned    = dp_aligned,
                is_extreme    = is_extreme,
                whale_blocks  = whale_blocks,
            )

            action, reasoning = get_recommendation(combined_bias, ticker, confidence)

            # ── Build evidence bullets ────────────────────────────────────
            bullets = []
            if dp_pct > 0:
                side_str = "buy side dominant" if dp_bull_pct >= 55 else "sell side dominant"
                bullets.append(
                    f"Dark pool {dp_pct:.0f}% of volume — {side_str} "
                    f"(${dp_notional/1000:.0f}K)" if dp_notional < 1e6
                    else f"Dark pool {dp_pct:.0f}% of volume — {side_str} "
                         f"(${dp_notional/1e6:.1f}M)"
                )
            if flow_dom > 0:
                bullets.append(
                    f"Options flow {flow_dom:.0f}% {contract.lower()}s — "
                    f"{'bullish' if combined_bias == 'BULLISH' else 'bearish'} sweep"
                )
            if top_uoa:
                contract_id = top_uoa.get("contract_id", f"O:{ticker}{expiry.replace('-','')}{contract[0]}{strike:.0f}000")
                bullets.append(
                    f"Whale call: {contract_id} {vol_oi:.1f}x OI at ${strike:.0f}"
                )
            for w in large_prints[:2]:
                notional = w.get("notional", 0)
                shares   = w.get("shares", 0)
                price    = w.get("price", 0)
                nstr     = f"${notional/1000:.0f}K" if notional < 1e6 else f"${notional/1e6:.1f}M"
                bullets.append(f"Whale block BUY {nstr} at ${price:.3f}")

            evidence_text = "\n".join(f"• {b}" for b in bullets) if bullets else "• Unusual flow detected"

            # ── Confidence label ──────────────────────────────────────────
            if confidence >= 85:
                conf_label = f"🔥 {confidence}%"
            elif confidence >= 75:
                conf_label = f"⚡ {confidence}%"
            elif confidence >= 65:
                conf_label = f"📊 {confidence}%"
            else:
                conf_label = f"📉 {confidence}%"

            color = 0x00FF88 if combined_bias == "BULLISH" else 0xFF4444

            embed = {
                "color": color,
                "author": {"name": f"💰 Elevated Dark Pool Activity — {ticker}"},
                "description": (
                    f"**{combined_bias}** dark pool signal on **{ticker}** at {now_str}"
                ),
                "fields": [
                    {
                        "name": "🪣 Dark Pool %",
                        "value": f"**{dp_pct:.1f}%** of volume",
                        "inline": True,
                    },
                    {
                        "name": "📊 Dark Vol",
                        "value": f"{int(dp.get('dark_shares', 0)):,} shares",
                        "inline": True,
                    },
                    {
                        "name": "📋 Total Scanned",
                        "value": f"{int(dp.get('total_trades', 0)):,} recent trades",
                        "inline": True,
                    },
                ],
                "footer": {
                    "text": f"CHAKRA Flow Monitor • Dark Pool Scanner • {now_str}"
                },
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

            # Large prints block
            if large_prints:
                print_lines = []
                for lp in large_prints[:3]:
                    n   = lp.get("notional", 0)
                    sh  = lp.get("shares", 0)
                    pr  = lp.get("price", 0)
                    nst = f"${n/1000:.0f}K" if n < 1e6 else f"${n/1e6:.1f}M"
                    print_lines.append(f"🟢 {nst} — {sh:,} shares @ ${pr:.2f}")
                embed["fields"].append({
                    "name": "🧱 Large Prints",
                    "value": "\n".join(print_lines),
                    "inline": False,
                })

            # CHAKRA Recommendation
            embed["fields"].append({
                "name": "🎯 CHAKRA Recommendation",
                "value": f"📈 **{action}**\nConfidence: **{conf_label}**",
                "inline": False,
            })

            # Evidence bullets
            embed["fields"].append({
                "name": "\u200b",
                "value": evidence_text,
                "inline": False,
            })

            # Route by confidence

            # ── Feed qualifying signals to Tarak ─────────────────────────────
            if confidence >= 65 and combined_bias in ("BULLISH", "BEARISH"):
                try:
                    from tarak.flowreceiver import emit_flow_signal
                    emit_flow_signal({
                        "ticker":        ticker,
                        "bias":          combined_bias,
                        "contract_type": top_uoa.get("contract_type", "CALL") if top_uoa else "CALL",
                        "strike":        top_uoa.get("strike", 0) if top_uoa else 0,
                        "expiry":        top_uoa.get("expiry", "") if top_uoa else "",
                        "dte":           dte,
                        "vol_oi_ratio":  vol_oi,
                        "flow_dom_pct":  flow_dom,
                        "dark_pool_pct": dp_pct,
                        "mark":          top_uoa.get("mark", 0) if top_uoa else 0,
                        "iv":            iv,
                        "delta":         top_uoa.get("delta", 0) if top_uoa else 0,
                        "is_extreme":    is_extreme,
                        "confidence":    confidence,
                        "whale_blocks":  whale_blocks,
                        "dp_notional":   dp_notional,
                        "source":        "UOA+DP",
                    })
                except Exception as _te:
                    log.warning(f"Tarak feed error for {ticker}: {_te}")
            channel = CHCHAKRAEXTREME if confidence >= 80 or is_extreme else CHCHAKRASIGNALS
            await send(channel, embed, "CHAKRA Flow")
            await asyncio.sleep(0.5)

        except Exception as e:
            log.error(f"Flow monitor error for {ticker}: {e}", exc_info=True)


async def run_loop(interval_seconds: int = 300):
    """Run the flow monitor on a schedule."""
    log.info(f"CHAKRA Flow Monitor started — scanning every {interval_seconds}s")
    while True:
        try:
            await run_flow_monitor_cycle()
        except Exception as e:
            log.error(f"Flow monitor cycle failed: {e}")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(run_loop())
