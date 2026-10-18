"""WX channel command handling: wx, wx <zip>, alerts."""

import re
import time
from collections import defaultdict, deque

import httpx

from .alerts import Alert
from .contacts import Contact, resolve
from .weather import WeatherClient, truncate

ZIP_RE = re.compile(r"^\d{5}$")
UNAVAILABLE = "Weather service unavailable, try again later."


class RateLimiter:
    """Per-sender: a minimum gap between commands plus an hourly cap."""

    def __init__(self, min_gap_s: float = 20.0, hourly: int = 12, clock=time.monotonic):
        self.min_gap_s, self.hourly, self._clock = min_gap_s, hourly, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, sender: str) -> bool:
        now = self._clock()
        hits = self._hits[sender]
        while hits and now - hits[0] >= 3600:
            hits.popleft()
        if (hits and now - hits[-1] < self.min_gap_s) or len(hits) >= self.hourly:
            return False
        hits.append(now)
        return True


def split_sender(text: str) -> tuple[str, str] | None:
    """Channel text arrives as 'name: message'."""
    sender, sep, body = text.partition(": ")
    if not sep or not sender:
        return None
    return sender, body.strip()


class Bot:
    def __init__(self, weather: WeatherClient, get_contacts, get_alerts, limiter: RateLimiter | None = None):
        self.weather = weather
        self.get_contacts = get_contacts  # async () -> list[Contact]
        self.get_alerts = get_alerts  # () -> list[Alert] currently matching the filter
        self.limiter = limiter or RateLimiter()

    async def handle(self, text: str) -> str | None:
        """Reply text for a channel message, or None when it isn't a command."""
        parsed = split_sender(text)
        if not parsed:
            return None
        sender, body = parsed
        words = body.lower().split()
        if not words or words[0] not in ("wx", "alerts") or len(words) > 2:
            return None
        if not self.limiter.allow(sender):
            return None  # stay quiet rather than add traffic
        try:
            if words[0] == "alerts":
                return f"@{sender} " + self._alerts_text(self.get_alerts())
            if len(words) == 2:
                return await self._wx_zip(sender, words[1])
            return await self._wx_here(sender)
        except (httpx.HTTPError, KeyError, ValueError):
            return f"@{sender} {UNAVAILABLE}"

    async def _wx_zip(self, sender: str, zipcode: str) -> str:
        if not ZIP_RE.match(zipcode):
            return f"@{sender} Use: wx, wx <5-digit zip>, or alerts"
        coords = await self.weather.zip_to_latlon(zipcode)
        if not coords:
            return f"@{sender} Unknown zip {zipcode}"
        return await self._forecast_reply(sender, *coords)

    async def _wx_here(self, sender: str) -> str:
        contacts: list[Contact] = await self.get_contacts()
        status, contact = resolve(contacts, sender)
        if contact is None:
            reasons = {
                "unknown": "I don't have your node yet",
                "ambiguous": "more than one node uses your name",
                "no_gps": "your node isn't sharing a location",
            }
            return truncate(f"@{sender} {reasons[status]}. Try: wx <zip>")
        return await self._forecast_reply(sender, contact.lat, contact.lon)

    async def _forecast_reply(self, sender: str, lat: float, lon: float) -> str:
        text = await self.weather.forecast(lat, lon)
        if text is None:
            return f"@{sender} No NWS forecast for that location (US only)."
        return truncate(f"@{sender} {text}")

    @staticmethod
    def _alerts_text(alerts: list[Alert]) -> str:
        if not alerts:
            return "No active alerts for the configured area."
        shown = "; ".join(f"{a.event} ({a.area.split(';')[0]})" for a in alerts[:3])
        more = f" +{len(alerts) - 3} more" if len(alerts) > 3 else ""
        return truncate(f"Active: {shown}{more}")
