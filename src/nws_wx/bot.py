"""WX channel command handling: wx, wx <zip>, alerts."""

import re
import time
from collections import defaultdict, deque

import httpx

from .alerts import Alert, until
from .contacts import Contact, resolve
from .alerts import MAX_TEXT_BYTES
from .weather import WeatherClient, compose, truncate

ZIP_RE = re.compile(r"^\d{5}$")
UNAVAILABLE = "Weather service unavailable, try again later."
USAGE = "Use: wx, wx <zip>, alerts, alerts <zip>, help"
HELP_COMMANDS = "wx: forecast+alerts at your GPS | wx <zip> | alerts: our area | alerts <zip> | help"
HELP_NOTE = "NWS data via a convenience relay. Not a replacement for NOAA Weather Radio or WEA."
HELP_WORDS = ("help", "?")


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
    def __init__(self, weather: WeatherClient, get_contacts, get_alerts, limiter: RateLimiter | None = None,
                 max_bytes: int = MAX_TEXT_BYTES, alerts_followup: bool = True):
        self.weather = weather
        self.get_contacts = get_contacts  # async () -> list[Contact]
        self.get_alerts = get_alerts  # () -> list[Alert] currently matching the filter
        self.limiter = limiter or RateLimiter()
        self.max_bytes = max_bytes
        self.alerts_followup = alerts_followup

    async def handle(self, text: str) -> str | None:
        """First reply only; replies() has the full list."""
        out = await self.replies(text)
        return out[0] if out else None

    async def replies(self, text: str) -> list[str]:
        """Messages to send for a channel message; empty when it isn't a command.

        A wx forecast may be followed by a second message listing active alerts there.
        """
        parsed = split_sender(text)
        if not parsed:
            return []
        sender, body = parsed
        words = body.lower().split()
        if not words or words[0] not in ("wx", "alerts", *HELP_WORDS) or len(words) > 2:
            return []
        if not self.limiter.allow(sender):
            return []  # stay quiet rather than add traffic
        if words[0] in HELP_WORDS or (len(words) == 2 and words[1] in HELP_WORDS):
            return self._help(sender)
        try:
            if words[0] == "alerts":
                if len(words) == 2:
                    return [await self._alerts_zip(sender, words[1])]
                return [f"@[{sender}] " + self._alerts_text(self.get_alerts())]
            if len(words) == 2:
                return await self._wx_zip(sender, words[1])
            return await self._wx_here(sender)
        except (httpx.HTTPError, KeyError, ValueError):
            return [f"@[{sender}] {UNAVAILABLE}"]

    def _help(self, sender: str) -> list[str]:
        return [truncate(f"@[{sender}] {HELP_COMMANDS}", self.max_bytes), truncate(HELP_NOTE, self.max_bytes)]

    async def _wx_zip(self, sender: str, zipcode: str) -> list[str]:
        if not ZIP_RE.match(zipcode):
            return [f"@[{sender}] {USAGE}"]
        coords = await self.weather.zip_to_latlon(zipcode)
        if not coords:
            return [f"@[{sender}] Unknown zip {zipcode}"]
        return await self._forecast_reply(sender, *coords, label=f"for {zipcode}")

    async def _alerts_zip(self, sender: str, zipcode: str) -> str:
        if not ZIP_RE.match(zipcode):
            return f"@[{sender}] {USAGE}"
        coords = await self.weather.zip_to_latlon(zipcode)
        if not coords:
            return f"@[{sender}] Unknown zip {zipcode}"
        found = await self.weather.alerts_at(*coords)
        prefix = f"@[{sender}] {zipcode}: "
        return self._point_alerts_text(prefix, found) if found else f"{prefix}no active alerts."

    def _point_alerts_text(self, prefix: str, found: list[Alert]) -> str:
        """As many alerts as fit in one message, then '+N more'."""
        items = [f"{a.event}{until(a)}" for a in found]
        text, shown = prefix + items[0], 1
        for item in items[1:]:
            candidate = f"{text}; {item}"
            if len((candidate + f" +{len(items) - shown - 1} more").encode("utf-8")) > self.max_bytes:
                break
            text, shown = candidate, shown + 1
        if shown < len(items):
            text += f" +{len(items) - shown} more"
        return truncate(text, self.max_bytes)

    async def _wx_here(self, sender: str) -> list[str]:
        contacts: list[Contact] = await self.get_contacts()
        status, contact = resolve(contacts, sender)
        if contact is None:
            reasons = {
                "unknown": "I don't have your node yet",
                "ambiguous": "more than one node uses your name",
                "no_gps": "your node isn't sharing a location",
            }
            return [truncate(f"@[{sender}] {reasons[status]}. Try: wx <zip>", self.max_bytes)]
        return await self._forecast_reply(sender, contact.lat, contact.lon, label="at your location")

    async def _forecast_reply(self, sender: str, lat: float, lon: float, label: str) -> list[str]:
        fc = await self.weather.forecast(lat, lon)
        if fc is None:
            return [f"@[{sender}] No NWS forecast for that location (US only)."]
        out = [compose(f"@[{sender}] {fc.place}: ", fc.parts, self.max_bytes)]
        if self.alerts_followup:
            try:
                found = await self.weather.alerts_at(lat, lon)
            except (httpx.HTTPError, KeyError, ValueError):
                found = []  # the forecast is already good; just skip the extra message
            if found:
                out.append(self._point_alerts_text(f"@[{sender}] Alerts {label}: ", found))
        return out

    def _alerts_text(self, alerts: list[Alert]) -> str:
        if not alerts:
            return "No active alerts for the configured area."
        shown = "; ".join(f"{a.event} ({a.area.split(';')[0]})" for a in alerts[:3])
        more = f" +{len(alerts) - 3} more" if len(alerts) > 3 else ""
        return truncate(f"Active: {shown}{more}", self.max_bytes)
