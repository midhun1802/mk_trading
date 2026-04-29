"""
CHAKRA Signal Memory — ChromaDB vector store
Stores past signals and outcomes so ARJUN can learn from history.
Each signal is stored with its outcome after the trade closes.
ARJUN queries similar past setups before generating new signals.
"""
import json, os, logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

log = logging.getLogger("ARJUN.Memory")

# Lazy import chromadb to avoid startup errors if not installed
_client = None
_collection = None

def _get_collection():
    """Get or create ChromaDB collection."""
    global _client, _collection
    if _collection is not None:
        return _collection
    try:
        import chromadb
        from chromadb.config import Settings

        os.makedirs("logs/arjun/memory", exist_ok=True)
        _client = chromadb.PersistentClient(
            path="logs/arjun/memory",
            settings=Settings(anonymized_telemetry=False)
        )
        _collection = _client.get_or_create_collection(
            name="arjun_signals",
            metadata={"hnsw:space": "cosine"}
        )
        log.info(f"✅ ChromaDB loaded: {_collection.count()} signals in memory")
        return _collection
    except Exception as e:
        log.error(f"ChromaDB init failed: {e}")
        return None


def store_signal(signal: dict, outcome: dict = None) -> bool:
    """
    Store a signal in vector memory.
    Call at signal generation time, then update with outcome after close.

    signal dict keys: ticker, action, direction, confidence,
                      gex_regime, regime_call, rsi, vwap_bias,
                      bull_score, bear_score, rationale
    outcome dict keys: pnl, pnl_pct, result (WIN/LOSS/NEUTRAL),
                       exit_price, hold_minutes
    """
    col = _get_collection()
    if col is None:
        return False

    try:
        ticker      = signal.get("ticker","?")
        regime      = signal.get("gex_regime","UNKNOWN")
        regime_call = signal.get("regime_call","NEUTRAL")
        rsi         = signal.get("rsi", signal.get("rsi_14", 50))
        direction   = signal.get("direction","NEUTRAL")
        conf        = signal.get("confidence", 0.5)
        vwap_bias   = signal.get("vwap_bias","UNKNOWN")
        bull_score  = signal.get("bull_score", 0)
        bear_score  = signal.get("bear_score", 0)

        # Create searchable text document
        doc = (
            f"ticker:{ticker} regime:{regime} call:{regime_call} "
            f"direction:{direction} rsi:{float(rsi):.0f} vwap:{vwap_bias} "
            f"bull:{float(bull_score):.0f} bear:{float(bear_score):.0f} "
            f"conf:{float(conf):.2f} action:{signal.get('action','HOLD')}"
        )

        # Metadata stored alongside
        meta = {
            "ticker":      ticker,
            "date":        str(date.today()),
            "action":      signal.get("action","HOLD"),
            "direction":   direction,
            "confidence":  float(conf),
            "gex_regime":  regime,
            "regime_call": regime_call,
            "rsi":         float(rsi),
            "vwap_bias":   vwap_bias,
            "bull_score":  float(bull_score),
            "bear_score":  float(bear_score),
            "rationale":   str(signal.get("rationale",""))[:200],
        }

        if outcome:
            meta["pnl"]          = float(outcome.get("pnl", 0))
            meta["pnl_pct"]      = float(outcome.get("pnl_pct", 0))
            meta["result"]       = outcome.get("result","NEUTRAL")
            meta["hold_minutes"] = int(outcome.get("hold_minutes", 0))
            meta["has_outcome"]  = True
        else:
            meta["has_outcome"]  = False

        # Use date+ticker+timestamp as ID
        sig_id = f"{date.today()}_{ticker}_{datetime.now().strftime('%H%M%S')}"

        col.upsert(
            ids=[sig_id],
            documents=[doc],
            metadatas=[meta]
        )
        log.info(f"  💾 Stored signal: {sig_id}")
        return True

    except Exception as e:
        log.error(f"  Memory store error: {e}")
        return False


def query_similar(ticker: str, regime: str, rsi: float,
                  direction: str, n_results: int = 5) -> list:
    """
    Query similar past setups from memory.
    Returns list of past signals with their outcomes.
    Used by ARJUN to inform new signal generation.
    """
    col = _get_collection()
    if col is None or col.count() == 0:
        return []

    try:
        query = (
            f"ticker:{ticker} regime:{regime} "
            f"direction:{direction} rsi:{float(rsi):.0f}"
        )

        # Only filter on has_outcome if there are outcomes stored
        try:
            results = col.query(
                query_texts=[query],
                n_results=min(n_results, col.count()),
                where={"has_outcome": True},
            )
        except Exception:
            # Fallback: no filter (collection may have no outcomes yet)
            results = col.query(
                query_texts=[query],
                n_results=min(n_results, col.count()),
            )

        past = []
        if results and results["metadatas"]:
            for meta in results["metadatas"][0]:
                if meta.get("has_outcome"):
                    past.append({
                        "ticker":     meta.get("ticker"),
                        "date":       meta.get("date"),
                        "action":     meta.get("action"),
                        "direction":  meta.get("direction"),
                        "confidence": meta.get("confidence"),
                        "result":     meta.get("result","?"),
                        "pnl_pct":    meta.get("pnl_pct",0),
                        "gex_regime": meta.get("gex_regime"),
                        "rsi":        meta.get("rsi"),
                    })

        if past:
            wins     = sum(1 for p in past if p.get("result") == "WIN")
            win_rate = round(wins / len(past) * 100, 1)
            log.info(f"  🧠 Found {len(past)} similar past signals, {win_rate}% win rate")

        return past

    except Exception as e:
        log.error(f"  Memory query error: {e}")
        return []


def update_outcome(ticker: str, signal_date: str,
                   pnl: float, pnl_pct: float,
                   hold_minutes: int) -> bool:
    """
    Update a stored signal with its trade outcome.
    Call this when a position closes.
    """
    col = _get_collection()
    if col is None:
        return False

    try:
        result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "NEUTRAL"

        # Find the signal to update
        try:
            existing = col.get(
                where={"$and": [
                    {"ticker":      {"$eq": ticker}},
                    {"date":        {"$eq": signal_date}},
                    {"has_outcome": {"$eq": False}},
                ]}
            )
        except Exception:
            existing = None

        if not existing or not existing["ids"]:
            log.warning(f"  No signal to update for {ticker} on {signal_date}")
            return False

        # Update the most recent one
        sig_id = existing["ids"][-1]
        meta   = existing["metadatas"][-1]

        meta.update({
            "pnl":          float(pnl),
            "pnl_pct":      float(pnl_pct),
            "result":       result,
            "hold_minutes": int(hold_minutes),
            "has_outcome":  True,
        })

        col.update(ids=[sig_id], metadatas=[meta])
        log.info(f"  💾 Updated outcome: {ticker} {result} {pnl_pct:+.1f}%")
        return True

    except Exception as e:
        log.error(f"  Memory update error: {e}")
        return False


def get_ticker_stats(ticker: str) -> dict:
    """Get historical performance stats for a ticker."""
    col = _get_collection()
    if col is None:
        return {}

    try:
        results = col.get(
            where={"$and": [
                {"ticker":      {"$eq": ticker}},
                {"has_outcome": {"$eq": True}},
            ]}
        )

        if not results or not results["metadatas"]:
            return {"ticker": ticker, "signals": 0}

        metas   = results["metadatas"]
        total   = len(metas)
        wins    = sum(1 for m in metas if m.get("result") == "WIN")
        avg_pnl = sum(m.get("pnl_pct",0) for m in metas) / total

        return {
            "ticker":   ticker,
            "signals":  total,
            "wins":     wins,
            "win_rate": round(wins/total*100, 1),
            "avg_pnl":  round(avg_pnl, 2),
            "best":     max(m.get("pnl_pct",0) for m in metas),
            "worst":    min(m.get("pnl_pct",0) for m in metas),
        }
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}


def memory_summary() -> dict:
    """Get overall memory stats."""
    col = _get_collection()
    if col is None:
        return {"total": 0, "with_outcomes": 0}

    try:
        total = col.count()
        if total == 0:
            return {"total": 0, "with_outcomes": 0, "pending": 0}

        try:
            with_outcomes = col.get(where={"has_outcome": {"$eq": True}})
            outcome_count = len(with_outcomes["ids"]) if with_outcomes else 0
        except Exception:
            outcome_count = 0

        return {
            "total":         total,
            "with_outcomes": outcome_count,
            "pending":       total - outcome_count,
        }
    except Exception:
        return {"total": 0}
