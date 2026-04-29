"""
CHAKRA — run_all_modules.py
backend/chakra/modules/run_all_modules.py

Master runner for all Power Intelligence modules.
Calls each module's compute_and_cache_* function directly,
then writes a status summary to logs/chakra/modules/.

Schedule:
  */30 9-16 * * 1-5  (market hours, every 30 min)
  0 8 * * 1-5        (pre-market force refresh)

Usage:
  python3 backend/chakra/modules/run_all_modules.py
  python3 backend/chakra/modules/run_all_modules.py --force
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("chakra.modules.runner")

STALE_MINUTES = 35   # re-run if cache older than this
MODULES_DIR   = BASE / "logs" / "chakra" / "modules"


# ── Module registry ────────────────────────────────────────────────────────────
# Maps module name → (cache_file_path, callable_to_run)
# We import lazily inside run_module() so a broken module can't crash others.

MODULE_DEFS = {
    "dex": {
        "cache": BASE / "logs" / "options" / "dex_latest.json",
        "module": "backend.chakra.modules.dex_calculator",
        "fn":     "compute_and_cache_dex",
    },
    "hurst": {
        "cache": BASE / "logs" / "chakra" / "hurst_latest.json",
        "module": "backend.chakra.modules.hurst_engine",
        "fn":     "compute_and_cache_hurst",
    },
    "vrp": {
        "cache": BASE / "logs" / "chakra" / "vrp_latest.json",
        "module": "backend.chakra.modules.vrp_engine",
        "fn":     "compute_and_cache_vrp",
    },
    "vex": {
        "cache": BASE / "logs" / "chakra" / "vex_latest.json",
        "module": "backend.chakra.modules.vex_engine",
        "fn":     "compute_and_cache_vex",
    },
    "charm": {
        "cache": BASE / "logs" / "chakra" / "charm_latest.json",
        "module": "backend.chakra.modules.charm_engine",
        "fn":     "compute_and_cache_charm",
    },
    "entropy": {
        "cache": BASE / "logs" / "chakra" / "entropy_latest.json",
        "module": "backend.chakra.modules.entropy_engine",
        "fn":     "compute_and_cache_entropy",
    },
    "hmm": {
        "cache": BASE / "logs" / "chakra" / "hmm_latest.json",
        "module": "backend.chakra.modules.hmm_regime",
        "fn":     "compute_and_cache_hmm",
    },
    "ivskew": {
        "cache": BASE / "logs" / "chakra" / "ivskew_latest.json",
        "module": "backend.chakra.modules.iv_skew",
        "fn":     "compute_and_cache_skew",
    },
    "iceberg": {
        "cache": BASE / "logs" / "chakra" / "iceberg_latest.json",
        "module": "backend.chakra.modules.iceberg_detector",
        "fn":     "scan_for_icebergs",
    },
    "kyle_lambda": {
        "cache": BASE / "logs" / "chakra" / "lambda_latest.json",
        "module": "backend.chakra.modules.kyle_lambda",
        "fn":     "compute_and_cache_lambda",
    },
    "cot": {
        "cache": BASE / "logs" / "chakra" / "cot_latest.json",
        "module": "backend.chakra.modules.cot_smart_money",
        "fn":     "compute_and_cache_cot",
    },
    "prob_dist": {
        "cache": BASE / "logs" / "chakra" / "probdist_latest.json",
        "module": "backend.chakra.modules.prob_distribution",
        "fn":     "compute_and_cache_probdist",
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_cache_age_minutes(cache_path: Path) -> float | None:
    """Return age of cache file in minutes, or None if missing."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
        ts = data.get("ts") or data.get("timestamp")
        if ts:
            return (time.time() - float(ts)) / 60
        # Fall back to file mtime
        return (time.time() - cache_path.stat().st_mtime) / 60
    except Exception:
        return (time.time() - cache_path.stat().st_mtime) / 60


def is_stale(cache_path: Path) -> bool:
    age = get_cache_age_minutes(cache_path)
    if age is None:
        return True
    return age > STALE_MINUTES


def write_module_status(name: str, result: dict | None, error: str | None,
                        elapsed: float, cache_path: Path):
    """Write a per-module status file to logs/chakra/modules/."""
    MODULES_DIR.mkdir(parents=True, exist_ok=True)
    status_path = MODULES_DIR / f"{name}_latest.json"
    payload = {
        "module":    name,
        "ts":        time.time(),
        "datetime":  datetime.now().isoformat(),
        "elapsed_s": round(elapsed, 2),
        "ok":        error is None,
        "error":     error,
        "cache":     str(cache_path),
        "cache_age_min": get_cache_age_minutes(cache_path),
    }
    if result and isinstance(result, dict):
        # Attach a small summary — avoid storing full chain data
        payload["summary"] = {k: v for k, v in list(result.items())[:8]
                              if not isinstance(v, (list, dict)) or k in ("regime", "ts")}
    status_path.write_text(json.dumps(payload, indent=2, default=str))


# ── Core runner ────────────────────────────────────────────────────────────────

def run_module(name: str, defn: dict, force: bool = False) -> dict:
    """
    Run one module.  Returns status dict.
    If cache is fresh and --force not set, skip.
    """
    cache_path = defn["cache"]
    age = get_cache_age_minutes(cache_path)

    if not force and age is not None and age < STALE_MINUTES:
        log.info(f"  SKIP {name}: cache {age:.0f}min old (< {STALE_MINUTES}min)")
        return {"name": name, "skipped": True, "cache_age_min": age}

    log.info(f"  RUN  {name} ...")
    t0 = time.time()
    try:
        import importlib
        mod = importlib.import_module(defn["module"])
        fn  = getattr(mod, defn["fn"])
        result = fn()
        elapsed = time.time() - t0
        write_module_status(name, result, None, elapsed, cache_path)
        new_age = get_cache_age_minutes(cache_path)
        log.info(f"  OK   {name}: {elapsed:.1f}s → cache {new_age:.1f}min old")
        return {"name": name, "ok": True, "elapsed_s": elapsed, "cache_age_min": new_age}
    except Exception as e:
        elapsed = time.time() - t0
        err_msg = str(e)
        log.error(f"  ERR  {name}: {err_msg}")
        write_module_status(name, None, err_msg, elapsed, cache_path)
        return {"name": name, "ok": False, "error": err_msg, "elapsed_s": elapsed}


def run_all(force: bool = False) -> dict:
    """Run all modules. Returns summary dict."""
    log.info(f"CHAKRA modules runner — {'FORCE' if force else 'stale-only'} mode")
    MODULES_DIR.mkdir(parents=True, exist_ok=True)

    results = {}
    t_start = time.time()

    for name, defn in MODULE_DEFS.items():
        results[name] = run_module(name, defn, force=force)

    total = time.time() - t_start
    ok_count   = sum(1 for r in results.values() if r.get("ok"))
    skip_count = sum(1 for r in results.values() if r.get("skipped"))
    err_count  = sum(1 for r in results.values() if not r.get("ok") and not r.get("skipped"))

    summary = {
        "ts":        time.time(),
        "datetime":  datetime.now().isoformat(),
        "elapsed_s": round(total, 2),
        "total":     len(MODULE_DEFS),
        "ok":        ok_count,
        "skipped":   skip_count,
        "errors":    err_count,
        "modules":   results,
    }

    # Write run summary
    (MODULES_DIR / "run_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )

    log.info(f"Done: {ok_count} ran OK, {skip_count} skipped (fresh), "
             f"{err_count} errors — {total:.1f}s total")
    return summary


# ── ensure_modules_fresh() — callable by ARJUN pipeline ───────────────────────

def ensure_modules_fresh(stale_threshold_min: float = STALE_MINUTES) -> dict:
    """
    Called by ARJUN before signal generation.
    Only re-runs modules whose cache is stale or missing.
    Returns dict of {module_name: "ok" | "skipped" | "error"}.
    """
    report = {}
    for name, defn in MODULE_DEFS.items():
        age = get_cache_age_minutes(defn["cache"])
        if age is None or age > stale_threshold_min:
            r = run_module(name, defn, force=False)
            report[name] = "ok" if r.get("ok") else f"error: {r.get('error', '?')}"
        else:
            report[name] = f"fresh ({age:.0f}min)"
    return report


# ── Status reader — used by /api/modules/status endpoint ──────────────────────

def get_all_module_status() -> dict:
    """
    Read status of all modules from cache files.
    Does NOT run any modules — read-only.
    Returns dict suitable for /api/modules/status.
    """
    now = time.time()
    out = {}
    for name, defn in MODULE_DEFS.items():
        cache = defn["cache"]
        age = get_cache_age_minutes(cache)
        if age is None:
            status = "MISSING"
        elif age < 35:
            status = "OK"
        elif age < 120:
            status = "AGING"
        else:
            status = "STALE"

        out[name] = {
            "status":      status,
            "age_min":     round(age, 1) if age is not None else None,
            "cache_path":  str(cache),
            "cache_exists": cache.exists(),
        }
    return out


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CHAKRA module runner")
    ap.add_argument("--force", action="store_true",
                    help="Force re-run even if cache is fresh")
    ap.add_argument("--module", type=str, default=None,
                    help="Run a single module by name (e.g. hurst)")
    args = ap.parse_args()

    if args.module:
        name = args.module.lower()
        if name not in MODULE_DEFS:
            print(f"Unknown module: {name}. Available: {', '.join(MODULE_DEFS)}")
            sys.exit(1)
        r = run_module(name, MODULE_DEFS[name], force=args.force)
        print(json.dumps(r, indent=2, default=str))
    else:
        summary = run_all(force=args.force)
        print(f"\n{'='*60}")
        print(f"CHAKRA Modules: {summary['ok']} OK | {summary['skipped']} skipped | {summary['errors']} errors")
        print(f"Total time: {summary['elapsed_s']:.1f}s")
        if summary["errors"] > 0:
            for name, r in summary["modules"].items():
                if not r.get("ok") and not r.get("skipped"):
                    print(f"  ERROR {name}: {r.get('error', '?')}")
