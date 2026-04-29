#!/usr/bin/env python3
import asyncio, sys
sys.path.insert(0, '/Users/midhunkrothapalli/trading-ai')
from backend.options.options_engine import run_gex_analysis
asyncio.run(run_gex_analysis(["SPY","QQQ","IWM","SPX","RUT"]))
