"""
CHAKRA Order Guard — STEP 0
Hard safety layer: every order must pass through here before reaching Alpaca.
CHAKRA trades OPTIONS ONLY — no equity, no inverse ETFs, no shorts on shares.
"""
import re
import logging

log = logging.getLogger(__name__)

# Equity/ETF symbols that must never be traded directly
BLOCKED_EQUITY_SYMBOLS = {
    # Indexes & major ETFs
    "SPY", "QQQ", "IWM", "DIA", "SPX",
    # Inverse / leveraged ETFs
    "SQQQ", "SH", "TZA", "SPXS", "SPXU", "SDOW", "SRTY",
    "UVXY", "VXX", "TQQQ", "SOXS", "LABD", "SOXL",
    # Common underlying stocks (bare symbols — options symbols look different)
    "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "META", "GOOGL",
    "AMD", "NFLX", "AVGO", "COIN", "PLTR", "HOOD", "MSTR",
}

# Options contract pattern: 1-6 uppercase letters, 6 digits (YYMMDD), C or P, 5-8 digits (strike*1000)
_OPTIONS_PATTERN = re.compile(r'^[A-Z]{1,6}\d{6}[CP]\d{5,8}$')


def is_valid_options_symbol(sym: str) -> bool:
    """Return True if sym looks like a valid options contract symbol."""
    if not sym:
        return False
    return bool(_OPTIONS_PATTERN.match(sym.upper().strip()))


def validate_options_order(symbol: str, qty: int, side: str) -> tuple[bool, str]:
    """
    Validate that an order is for an options contract only.
    Returns (is_valid: bool, reason: str).

    Call this BEFORE every Alpaca order placement.
    """
    if not symbol:
        return False, "BLOCKED: empty symbol"

    sym_upper = symbol.upper().strip()

    # Hard block: bare equity/ETF symbols
    if sym_upper in BLOCKED_EQUITY_SYMBOLS:
        return False, f"BLOCKED: {symbol} is an equity/ETF — CHAKRA trades options only"

    # Must match options contract regex
    if not _OPTIONS_PATTERN.match(sym_upper):
        return False, (
            f"BLOCKED: '{symbol}' does not match options contract format "
            f"(expected e.g. SPY260401C00640000)"
        )

    # Qty bounds
    if qty < 1:
        return False, f"BLOCKED: qty={qty} is invalid (minimum 1)"
    if qty > 3:
        return False, f"BLOCKED: qty={qty} exceeds max 3 contracts per trade"

    # Side must be buy or sell
    if side.lower() not in ("buy", "sell"):
        return False, f"BLOCKED: invalid side '{side}' (must be buy or sell)"

    return True, f"✅ Valid options order: {symbol} x{qty} {side.upper()}"


# ── Hard firewall: use this in any NEW order-placement code ──────────────────
def block_equity_order(symbol: str) -> tuple[bool, str]:
    """
    Hard block for any bare equity/ETF order attempt.
    Returns (blocked: bool, reason: str). If blocked=True, DO NOT place the order.
    CHAKRA RULE: options only — 0DTE scalps and ≤28 DTE swings.
    """
    sym = symbol.upper().strip()
    if sym in BLOCKED_EQUITY_SYMBOLS:
        return True, f"HARD BLOCK: {sym} is an equity/ETF — CHAKRA trades OPTIONS ONLY"
    if _OPTIONS_PATTERN.match(sym):
        return False, f"✅ {sym} is a valid options contract"
    # Unknown symbol that's not clearly an option — block to be safe
    return True, f"HARD BLOCK: '{sym}' is not a recognized options contract symbol"


def guard_or_raise(symbol: str, qty: int, side: str) -> None:
    """
    Validate order. Logs and raises ValueError if invalid.
    Use in sync code paths.
    """
    valid, reason = validate_options_order(symbol, qty, side)
    if not valid:
        log.error(f"  🛡️  ORDER GUARD: {reason}")
        raise ValueError(reason)
    log.info(f"  🛡️  ORDER GUARD: {reason}")
