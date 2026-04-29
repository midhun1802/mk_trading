import os
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv(override=True)
TRADING_ECON_KEY   = os.getenv("TRADING_ECONOMICS_KEY", "")
HIGH_IMPACT_EVENTS = ['FOMC', 'NFP', 'CPI', 'PPI', 'GDP', 'Retail Sales', 'PCE', 'JOLTS']

# Hardcoded 2026 calendar — fallback when no API key
# (month, day, hour_ET, minute, name)
HARDCODED_2026 = [
    (3,  7,  8, 30, "NFP — Non-Farm Payrolls"),
    (3, 12,  8, 30, "CPI — Consumer Price Index"),
    (3, 18,  8, 30, "Retail Sales"),
    (3, 19,  2,  0, "FOMC Rate Decision"),
    (3, 25,  8, 30, "PCE Price Index"),
    (4,  3,  8, 30, "NFP — Non-Farm Payrolls"),
    (4,  9,  8, 30, "CPI — Consumer Price Index"),
    (4, 29,  8, 30, "GDP Q1 Advance"),
    (4, 30,  2,  0, "FOMC Rate Decision"),
    (5,  1,  8, 30, "NFP — Non-Farm Payrolls"),
    (5, 13,  8, 30, "CPI — Consumer Price Index"),
    (6,  5,  8, 30, "NFP — Non-Farm Payrolls"),
    (6, 17,  2,  0, "FOMC Rate Decision"),
]


def _load_hardcoded() -> List[Dict]:
    """Build event list from hardcoded 2026 calendar."""
    try:
        import pytz
        et   = pytz.timezone("America/New_York")
        year = datetime.now().year
        events = []
        for month, day, hour, minute, name in HARDCODED_2026:
            try:
                from datetime import timezone
                dt_et  = et.localize(datetime(year, month, day, hour, minute))
                dt_utc = dt_et.astimezone(timezone.utc)
                events.append({
                    "name":   name,
                    "time":   dt_utc.isoformat(),
                    "impact": "HIGH",
                    "source": "hardcoded",
                })
            except Exception:
                continue
        return sorted(events, key=lambda e: e["time"])
    except ImportError:
        return []


def fetch_upcoming_events(hours_ahead: int = 4, hours_before: int = 2) -> List[Dict]:
    """
    Fetch high-impact macro events in the next N hours.
    Uses Trading Economics API if key is set, otherwise uses hardcoded schedule.
    """
    from datetime import timezone
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)

    # Try Trading Economics API
    if TRADING_ECON_KEY:
        try:
            resp   = requests.get(
                "https://api.tradingeconomics.com/calendar",
                params={"c": TRADING_ECON_KEY, "country": "United States", "importance": 3},
                timeout=5
            )
            events = []
            for e in resp.json():
                try:
                    et = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                    if (now - timedelta(hours=hours_before)) <= et <= cutoff and any(k in e.get("event", "") for k in HIGH_IMPACT_EVENTS):
                        events.append({"name": e["event"], "impact": "HIGH",
                                       "time": et.isoformat(), "hours_away": round((et - now).total_seconds() / 3600, 1)})
                except Exception:
                    continue
            return events
        except Exception:
            pass

    # Fallback: hardcoded calendar
    events   = _load_hardcoded()
    upcoming = []
    for e in events:
        try:
            et = datetime.fromisoformat(e["time"])
            if (now - timedelta(hours=hours_before)) <= et <= cutoff:
                hours_away = (et - now).total_seconds() / 3600
                upcoming.append({**e, "hours_away": round(hours_away, 1)})
        except Exception:
            continue
    return upcoming


def is_blocked(hours_ahead: int = 4) -> bool:
    """Returns True if a high-impact event is within hours_ahead."""
    return len(fetch_upcoming_events(hours_ahead)) > 0


class MacroCalendar:
    """
    MacroCalendar — tracks upcoming high-impact events and blocks trades.

    Usage:
        cal = MacroCalendar()
        await cal.refresh()
        if cal.is_blocked_now():
            return "BLOCK"
    """

    def __init__(self):
        self._events: List[Dict] = []

    async def refresh(self, force: bool = False):
        """Load events from API or hardcoded fallback."""
        # Try API first (sync wrapped in async for compatibility)
        events = fetch_upcoming_events(hours_ahead=24 * 30)  # next 30 days
        if not events:
            self._events = _load_hardcoded()
        else:
            self._events = events

    def get_upcoming_events(self, hours: int = 4) -> List[Dict]:
        """Return all events in the next N hours."""
        from datetime import timezone
        now    = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        result = []
        for e in self._events:
            try:
                et = datetime.fromisoformat(e["time"])
                if now <= et <= cutoff:
                    hours_away = (et - now).total_seconds() / 3600
                    result.append({**e, "hours_away": round(hours_away, 1)})
            except Exception:
                continue
        return sorted(result, key=lambda x: x["time"])

    def is_blocked_now(self, hours_ahead: int = 4) -> bool:
        """Returns True if trading should be blocked right now."""
        return len(self.get_upcoming_events(hours=hours_ahead)) > 0

    def next_event_str(self) -> str:
        """Human-readable string of the next upcoming event."""
        events = self.get_upcoming_events(hours=24)
        if not events:
            return "No high-impact events in next 24h"
        e = events[0]
        return f"{e['name']} in {e['hours_away']:.1f}h"

    def block_reason(self, hours_ahead: int = 4) -> Optional[str]:
        """Returns block reason string if blocked, None if safe to trade."""
        upcoming = self.get_upcoming_events(hours=hours_ahead)
        if upcoming:
            e = upcoming[0]
            return f"HIGH-IMPACT EVENT in {e['hours_away']:.1f}h: {e['name']}"
        return None

    def get_status(self) -> Dict:
        """Full status dict for dashboard / test output."""
        upcoming_4h  = self.get_upcoming_events(hours=4)
        upcoming_24h = self.get_upcoming_events(hours=24)
        blocked      = self.is_blocked_now()
        return {
            "blocked":            blocked,
            "block_reason":       self.block_reason() if blocked else None,
            "upcoming_4h":        upcoming_4h,
            "upcoming_24h":       upcoming_24h,
            "next_event":         self.next_event_str(),
            "event_count_today":  len(upcoming_24h),
        }
