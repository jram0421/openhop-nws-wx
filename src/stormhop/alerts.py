"""NWS active-alert polling, filtering and rate-limited relay."""

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import httpx

NWS_BASE = "https://api.weather.gov"
SEVERITY_ORDER = ["Unknown", "Minor", "Moderate", "Severe", "Extreme"]
URGENT_RANK = SEVERITY_ORDER.index("Severe")  # this severity and above ignore the cooldown
MAX_TEXT_BYTES = 160  # frame payload cap is 176, minus 6 header bytes


@dataclass
class Alert:
    id: str
    event: str
    severity: str
    area: str
    headline: str
    ends: str | None
    same: frozenset[str]
    zones: frozenset[str]


@dataclass
class AlertFilter:
    states: list[str] = field(default_factory=list)
    same_codes: set[str] = field(default_factory=set)  # e.g. "048113"
    zones: set[str] = field(default_factory=set)  # e.g. "TXZ104"
    min_severity: str = "Moderate"
    events: set[str] = field(default_factory=set)  # empty = all events

    def matches(self, alert: Alert) -> bool:
        if _sev(alert.severity) < _sev(self.min_severity):
            return False
        if self.events and alert.event not in self.events:
            return False
        if not self.same_codes and not self.zones:
            return True
        return bool(alert.same & self.same_codes) or bool(alert.zones & self.zones)


def _sev(name: str) -> int:
    try:
        return SEVERITY_ORDER.index(name)
    except ValueError:
        return 0


def parse_alerts(doc: dict) -> list[Alert]:
    out = []
    for feat in doc.get("features", []):
        p = feat.get("properties") or {}
        aid = p.get("id") or feat.get("id")
        if not aid or not p.get("event"):
            continue
        geo = p.get("geocode") or {}
        out.append(
            Alert(
                id=aid,
                event=p["event"],
                severity=p.get("severity") or "Unknown",
                area=p.get("areaDesc") or "",
                headline=p.get("headline") or p["event"],
                ends=p.get("ends") or p.get("expires"),
                same=frozenset(geo.get("SAME") or []),
                zones=frozenset(geo.get("UGC") or []),
            )
        )
    return out


async def fetch_active(client: httpx.AsyncClient, states: list[str]) -> list[Alert]:
    resp = await client.get(
        f"{NWS_BASE}/alerts/active",
        params={"area": ",".join(sorted(states))},
        headers={"Accept": "application/geo+json"},
    )
    resp.raise_for_status()
    return parse_alerts(resp.json())


def until(alert: Alert) -> str:
    """' until 5:30PM Sat' or '' when the end time is missing or unreadable."""
    if not alert.ends:
        return ""
    try:
        return f" until {datetime.fromisoformat(alert.ends).strftime('%-I:%M%p %a')}"
    except ValueError:
        return ""


def format_alert(alert: Alert, limit: int = MAX_TEXT_BYTES) -> str:
    text = f"NWS {alert.event} ({alert.severity}): {alert.area}{until(alert)}"
    raw = text.encode("utf-8")
    if len(raw) > limit:
        text = raw[: limit - 3].decode("utf-8", "ignore") + "..."
    return text


class AlertRelay:
    """Dedupes by alert ID, then enforces a cooldown and hourly caps.

    Severe and Extreme alerts skip the cooldown and have their own, higher hourly cap, so a burst of
    them can neither be held up by nor use up the budget for everything else.

    An alert is only marked seen once it is actually sent, so alerts held
    back by the limits are retried on later polls while still active.
    """

    def __init__(self, flt: AlertFilter, cooldown_s: float = 60.0, hourly_cap: int = 10, clock=time.monotonic,
                 seen: set[str] | None = None, urgent_hourly_cap: int = 30):
        self.filter = flt
        self.cooldown_s = cooldown_s
        self.hourly_cap = hourly_cap
        self.urgent_hourly_cap = urgent_hourly_cap
        self._clock = clock
        self._seen: set[str] = set(seen or ())
        self._sent_at: deque[float] = deque()  # every send; drives the cooldown
        self._normal_at: deque[float] = deque()
        self._urgent_at: deque[float] = deque()

    def _trim(self, now: float) -> None:
        for sent in (self._sent_at, self._normal_at, self._urgent_at):
            while sent and now - sent[0] >= 3600:
                sent.popleft()

    def select(self, alerts: list[Alert]) -> list[Alert]:
        """Alerts to post now, most severe first. Call mark_sent() per success.

        Severe and Extreme alerts go out right away, up to the urgent cap. Anything else waits out
        the cooldown and goes one at a time, up to the normal cap.
        """
        now = self._clock()
        self._trim(now)
        in_cooldown = bool(self._sent_at) and now - self._sent_at[-1] < self.cooldown_s
        fresh = [a for a in alerts if a.id not in self._seen and self.filter.matches(a)]
        fresh.sort(key=lambda a: -_sev(a.severity))
        urgent = [a for a in fresh if _sev(a.severity) >= URGENT_RANK]
        urgent_room = max(0, self.urgent_hourly_cap - len(self._urgent_at))
        if urgent and urgent_room:
            return urgent[:urgent_room]
        normal = [a for a in fresh if _sev(a.severity) < URGENT_RANK]
        normal_room = max(0, self.hourly_cap - len(self._normal_at))
        return [] if in_cooldown else normal[: min(normal_room, 1)]

    def mark_sent(self, alert: Alert) -> None:
        self._seen.add(alert.id)
        now = self._clock()
        self._sent_at.append(now)
        (self._urgent_at if _sev(alert.severity) >= URGENT_RANK else self._normal_at).append(now)

    @property
    def seen(self) -> set[str]:
        return set(self._seen)

    def prune(self, active_ids: set[str]) -> None:
        """Forget IDs no longer active so the set stays bounded."""
        self._seen &= active_ids
