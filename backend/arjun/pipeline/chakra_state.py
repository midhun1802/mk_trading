"""
CHAKRA Pipeline State
Typed state object passed between all agents.
Replaces loose JSON dicts with validated Pydantic models.
"""
from typing import TypedDict, Optional, List, Any


class ChakraState(TypedDict):
    """State passed between CHAKRA pipeline nodes."""
    # Input
    watchlist:          List[str]
    risk_config:        dict

    # Market context
    vix:                float
    market_open:        bool
    market_regime:      str

    # Agent outputs (populated as pipeline runs)
    research_report:    Optional[Any]    # ResearchReport.model_dump()
    trade_signals:      Optional[List[Any]]  # List[TradeSignal.model_dump()]
    all_signals:        Optional[List[Any]]  # all signals including HOLD
    execution_results:  Optional[List[Any]]  # List[ExecutionResult]

    # Messages for LangGraph
    messages:           List[dict]

    # Metadata
    cycle_id:           str
    started_at:         str
    error:              Optional[str]
