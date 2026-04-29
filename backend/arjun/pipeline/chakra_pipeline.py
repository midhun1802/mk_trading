"""
CHAKRA LangGraph Pipeline
Connects Research → Analyst → Executor → Reporter
as a proper stateful agent graph.
"""
import logging, uuid
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("CHAKRA.Pipeline")
ET  = ZoneInfo("America/New_York")

try:
    from langgraph.graph import StateGraph, END, START
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    log.warning("LangGraph not available — using sequential fallback")

from backend.arjun.pipeline.research_agent import research_node
from backend.arjun.pipeline.analyst_agent  import analyst_node
from backend.arjun.pipeline.executor_agent import executor_node
from backend.arjun.pipeline.reporter_node  import reporter_node


def build_pipeline():
    """Build the CHAKRA agent pipeline graph."""
    if not LANGGRAPH_AVAILABLE:
        return None

    graph = StateGraph(dict)

    graph.add_node("research", research_node)
    graph.add_node("analyst",  analyst_node)
    graph.add_node("executor", executor_node)
    graph.add_node("reporter", reporter_node)

    graph.add_edge(START, "research")
    graph.add_edge("research", "analyst")

    # Only route to executor if there are actionable signals
    graph.add_conditional_edges(
        "analyst",
        lambda s: "executor" if s.get("trade_signals") else "reporter",
        {"executor": "executor", "reporter": "reporter"}
    )

    graph.add_edge("executor", "reporter")
    graph.add_edge("reporter", END)

    compiled = graph.compile()
    log.info("✅ LangGraph pipeline compiled")
    return compiled


async def run_cycle(watchlist: list = None) -> dict:
    """
    Run one full CHAKRA pipeline cycle.
    Research → Analyst → Executor → Reporter
    """
    if watchlist is None:
        watchlist = ["SPY","QQQ","IWM","NVDA","TSLA","AAPL"]

    state = {
        "watchlist":         watchlist,
        "risk_config":       {},
        "vix":               20.0,
        "market_open":       True,
        "market_regime":     "unknown",
        "research_report":   None,
        "trade_signals":     None,
        "all_signals":       None,
        "execution_results": None,
        "messages":          [],
        "cycle_id":          str(uuid.uuid4())[:8],
        "started_at":        datetime.now(ET).isoformat(),
        "error":             None,
    }

    pipeline = build_pipeline()

    try:
        if pipeline and LANGGRAPH_AVAILABLE:
            result = await pipeline.ainvoke(state)
        else:
            # Sequential fallback
            log.info("Running sequential pipeline (LangGraph fallback)")
            state = await research_node(state)
            state = await analyst_node(state)
            if state.get("trade_signals"):
                state = await executor_node(state)
            state = await reporter_node(state)
            result = state

        placed = len([r for r in (result.get("execution_results") or [])
                     if r.get("status")=="placed"])
        sigs   = len(result.get("all_signals") or result.get("trade_signals") or [])

        log.info(f"✅ Cycle complete: {sigs} signals, {placed} placed")
        return result

    except Exception as e:
        log.error(f"❌ Pipeline error: {e}", exc_info=True)
        state["error"] = str(e)
        # Still run reporter to save partial state
        try:
            state = await reporter_node(state)
        except Exception:
            pass
        return state


def is_market_hours() -> bool:
    """Check if market is currently open (ET)."""
    et = datetime.now(ET)
    return (et.weekday() < 5 and
            ((et.hour == 9 and et.minute >= 30) or et.hour > 9) and
            et.hour < 16)
