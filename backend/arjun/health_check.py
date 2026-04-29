import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY", "")
DISCORD_WEBHOOK  = os.getenv("DISCORD_TRADES_WEBHOOK", "")

def check_polygon_stocks():
    try:
        r = requests.get("https://api.polygon.io/v2/aggs/ticker/SPY/prev",
                         params={"apiKey": POLYGON_API_KEY}, timeout=5)
        return r.status_code == 200
    except: return False

def check_polygon_options():
    try:
        r = requests.get("https://api.polygon.io/v3/snapshot/options/SPY",
                         params={"apiKey": POLYGON_API_KEY, "limit": 1}, timeout=5)
        return r.status_code == 200
    except: return False

def check_alpaca():
    try:
        r = requests.get("https://paper-api.alpaca.markets/v2/account",
                         headers={"APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
                                  "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", os.getenv("ALPACA_SECRET_KEY",""))}, timeout=5)
        return r.status_code == 200
    except: return False

def send_alert(message: str):
    if DISCORD_WEBHOOK:
        requests.post(DISCORD_WEBHOOK, json={"content": f"⚠️ ARJUN HEALTH CHECK: {message}"})

def check_data_sources():
    checks = {
        "polygon_stocks":  check_polygon_stocks(),
        "polygon_options": check_polygon_options(),
        "alpaca":          check_alpaca(),
    }
    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if failed:
        send_alert(f"Data source health check failed: {', '.join(failed)}")
    return len(failed) == 0

if __name__ == "__main__":
    print("Running CHAKRA health checks...")
    ok = check_data_sources()
    print(f"\nOverall: {'✅ HEALTHY' if ok else '❌ DEGRADED'}")
