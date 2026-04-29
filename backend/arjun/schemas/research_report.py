"""
Typed schemas for CHAKRA agent pipeline.
These are the contracts between agents.
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime

class TickerSnapshot(BaseModel):
    ticker:       str
    price:        float
    change_pct:   float
    volume:       int
    rsi_14:       float
    vwap:         float = 0.0
    above_vwap:   bool = False
    gex:          Optional[float] = None
    gex_regime:   str = "UNKNOWN"
    regime_call:  str = "NEUTRAL"
    call_wall:    Optional[float] = None
    put_wall:     Optional[float] = None
    zero_gamma:   Optional[float] = None
    sentiment:    str = "neutral"
    flow_dir:     str = "NEUTRAL"
    flow_conf:    float = 0.0
    dark_pool_pct: float = 0.0
    news_summary: str = ""

class ResearchReport(BaseModel):
    timestamp:     datetime
    tickers:       List[TickerSnapshot]
    vix:           float = 20.0
    market_regime: str = "unknown"
    regime_call:   str = "NEUTRAL"
    neural_pulse:  float = 50.0
    risk_mode:     str = "NORMAL"
    macro_notes:   str = ""
    top_movers:    List[str] = []
    risk_flags:    List[str] = []
