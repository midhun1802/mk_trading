"""
alert_parser.py — Parses ARKA/CHAKRA Discord embeds into structured trade data.

Priority:
  1. Discord embed fields  -> direct structured parse (free, instant)
  2. Embed title/description fallback -> regex parse
  3. Plain text message    -> Claude API (last resort only)
"""

import anthropic
import json
import re
import logging
from discord import Message, Embed

log = logging.getLogger("taraka.parser")

VALID_TICKERS = {
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "AMD",
    "XLK", "XLF", "XLE", "XLV", "XLU", "GLD", "TLT",
}

# Channels that post ARKA/CHAKRA structured embeds -- skip Claude for these
STRUCTURED_CHANNELS = {
    1477297456545796128,   # arka-signals
    1483969942935044267,   # arka-extreme
    1478124867713765468,   # chakra-signals
    1480690163637026876,   # high_stakes
}

CLAUDE_FALLBACK_SYSTEM = """You are a financial signal parser. Extract trading alert information from Discord messages.
Return ONLY a valid JSON object:
{
  "is_alert": true/false,
  "ticker": "SPY",
  "direction": "CALL" or "PUT",
  "entry": 689.50 or null,
  "target": 695.00 or null,
  "stop": 685.00 or null,
  "strike": 690 or null,
  "expiry": "0DTE" or "date string" or null,
  "premium": 1.50 or null,
  "conviction": 0-100 or null,
  "timeframe": "scalp/intraday/swing" or null,
  "confidence_in_parse": 0-100,
  "notes": "any important context"
}
Rules:
- is_alert=false for general chat, news, commentary
- direction: CALL=bullish/long/calls, PUT=bearish/short/puts
- Return ONLY the JSON, no explanation, no markdown"""


class AlertParser:
    def __init__(self, anthropic_key: str):
        self.client = anthropic.Anthropic(api_key=anthropic_key)

    async def parse(self, message: Message) -> dict | None:
        """
        Parse a Discord Message object.
        Tries embed parse first, falls back to Claude for plain text.
        """
        channel_id = message.channel.id

        # -- Path 1: Structured embed from ARKA/CHAKRA ----------------------
        if message.embeds and channel_id in STRUCTURED_CHANNELS:
            result = self._parse_embed(message.embeds[0], str(message.author))
            if result:
                log.info(f"  Embed parse: {result['ticker']} {result['direction']} conv={result.get('conviction','?')}")
                return result

        # -- Path 2: Plain text via Claude (freeform fallback) --------------
        content = message.content.strip()
        if not content or len(content) < 8:
            return None
        if self._is_obviously_not_alert(content):
            return None

        return await self._claude_parse(content, str(message.author))

    # -- Embed Parser --------------------------------------------------------
    def _parse_embed(self, embed: Embed, author: str) -> dict | None:
        """
        Parse a structured ARKA/CHAKRA embed.
        ARKA entry embed title: "ARKA ENTRY -- SPY CALL"
        CHAKRA title: "CHAKRA SIGNAL -- QQQ PUT"
        """
        title = embed.title or ""
        desc  = embed.description or ""

        # Must be an ENTRY signal (skip EXIT, HEALTH, LOG embeds)
        if not re.search(r"ENTRY|SIGNAL|ALERT", title, re.I):
            return None

        # Extract ticker + direction from title
        m = re.search(
            r"(SPY|QQQ|IWM|DIA|AAPL|MSFT|NVDA|TSLA|AMZN|GOOGL|META|AMD|XL[KFEVIU]|GLD|TLT)\s+(CALL|PUT)",
            title, re.I
        )
        if not m:
            return None

        ticker    = m.group(1).upper()
        direction = m.group(2).upper()

        if ticker not in VALID_TICKERS:
            return None

        # Parse embed fields into a flat dict
        fields = {f.name.lower().strip(): f.value for f in embed.fields}

        def _fv(key_patterns):
            for k in fields:
                for p in key_patterns:
                    if p in k:
                        return fields[k]
            return None

        def _float_field(key_patterns):
            raw = _fv(key_patterns)
            if raw is None:
                return None
            nums = re.findall(r'[\d.]+', str(raw))
            return float(nums[0]) if nums else None

        strike     = _float_field(["strike"])
        expiry     = _fv(["expiry", "exp", "dte"])
        entry      = _float_field(["entry", "price", "spot"])
        target     = _float_field(["target", "tp"])
        stop       = _float_field(["stop", "sl"])
        premium    = _float_field(["premium", "ask", "cost"])
        conviction = _float_field(["conviction"])
        neural     = _float_field(["neural", "pulse"])

        # Fallback: pull strike/entry from description if fields missing
        if not strike:
            sm = re.search(r'(\d{3,4})[Cc]', desc)
            if sm:
                strike = float(sm.group(1))
        if not entry:
            em = re.search(r'entry[:\s~@]*\$?([\d.]+)', desc, re.I)
            if em:
                entry = float(em.group(1))

        source = "ARKA" if "ARKA" in title.upper() else "CHAKRA"

        return {
            "ticker":     ticker,
            "direction":  direction,
            "entry":      entry,
            "target":     target,
            "stop":       stop,
            "strike":     strike,
            "expiry":     expiry or "0DTE",
            "premium":    premium,
            "conviction": int(conviction) if conviction else None,
            "neural":     int(neural) if neural else None,
            "timeframe":  "intraday",
            "parse_conf": 95,
            "notes":      f"From {source} embed",
            "raw":        f"{title} | {desc}",
            "author":     author,
            "source":     source,
        }

    # -- Claude Fallback -----------------------------------------------------
    async def _claude_parse(self, message: str, author: str) -> dict | None:
        try:
            response = self.client.messages.create(
                model      = "claude-haiku-4-5-20251001",
                max_tokens = 400,
                system     = CLAUDE_FALLBACK_SYSTEM,
                messages   = [{"role": "user", "content": f"Parse this Discord message:\n\n{message}"}]
            )
            raw  = response.content[0].text.strip()
            raw  = re.sub(r"```json|```", "", raw).strip()
            data = json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"Claude parse error: {e}")
            return None

        if not data.get("is_alert"):
            return None

        ticker    = data.get("ticker", "").upper().strip("$")
        direction = data.get("direction", "").upper()

        if ticker not in VALID_TICKERS or direction not in ("CALL", "PUT"):
            return None

        return {
            "ticker":     ticker,
            "direction":  direction,
            "entry":      self._safe_float(data.get("entry")),
            "target":     self._safe_float(data.get("target")),
            "stop":       self._safe_float(data.get("stop")),
            "strike":     self._safe_float(data.get("strike")),
            "expiry":     data.get("expiry", "0DTE"),
            "premium":    self._safe_float(data.get("premium")),
            "conviction": data.get("conviction"),
            "timeframe":  data.get("timeframe", "intraday"),
            "parse_conf": int(data.get("confidence_in_parse", 50)),
            "notes":      data.get("notes", ""),
            "raw":        message,
            "author":     author,
            "source":     "CLAUDE",
        }

    # -- Helpers -------------------------------------------------------------
    def _is_obviously_not_alert(self, msg: str) -> bool:
        msg_lower = msg.lower().strip()
        non_alert = [
            r"^(gm|good morning|gn|lol|haha|nice|wow|🔥|💀|😂|👍|🚀)$",
            r"^(what do you think|any thoughts|thoughts\?|opinion\?)$",
        ]
        for p in non_alert:
            if re.match(p, msg_lower):
                return True
        has_ticker   = bool(re.search(r'\b(SPY|QQQ|IWM|AAPL|NVDA|TSLA|MSFT|AMD|calls?|puts?)\b', msg, re.I))
        has_keywords = bool(re.search(r'\b(buy|sell|long|short|entry|target|stop|strike|contract|option)\b', msg_lower))
        return not (has_ticker or has_keywords)

    def _safe_float(self, val) -> float | None:
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None
