"""Forecast lookups: NWS /points -> gridpoint forecast, with caching."""

import time
from dataclasses import dataclass

import httpx

from .alerts import MAX_TEXT_BYTES, NWS_BASE

ZIP_URL = "https://api.zippopotam.us/us/{zip}"
POINTS_TTL = 24 * 3600
FORECAST_TTL = 15 * 60
MAX_CACHE = 512


class TTLCache:
    def __init__(self, ttl: float, clock=time.monotonic):
        self.ttl, self._clock = ttl, clock
        self._data: dict = {}

    def get(self, key):
        hit = self._data.get(key)
        if hit and self._clock() - hit[0] < self.ttl:
            return hit[1]
        self._data.pop(key, None)
        return None

    def put(self, key, value) -> None:
        if len(self._data) >= MAX_CACHE:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (self._clock(), value)


def truncate(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", "ignore") + "..."


DAYS = {"Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed", "Thursday": "Thu", "Friday": "Fri",
        "Saturday": "Sat", "Sunday": "Sun"}
MAX_PERIODS = 4


@dataclass
class Forecast:
    place: str
    parts: list[str]


def forecast_parts(periods: list[dict]) -> list[str]:
    parts = []
    for i, p in enumerate(periods[:MAX_PERIODS]):
        name = p["name"]
        for full, short in DAYS.items():
            name = name.replace(full, short)
        text = f"{name} {p['temperature']}{p.get('temperatureUnit', 'F')} {p['shortForecast']}"
        if i == 0 and p.get("windSpeed"):
            text += f", {p['windDirection']} {p['windSpeed']}".replace(" mph", "mph")
        parts.append(text)
    return parts


def compose(prefix: str, parts: list[str], limit: int = MAX_TEXT_BYTES) -> str:
    """Add whole periods while they fit; only the first may be cut."""
    text = prefix + parts[0]
    for part in parts[1:]:
        candidate = f"{text} | {part}"
        if len(candidate.encode("utf-8")) > limit:
            break
        text = candidate
    return truncate(text, limit)


class WeatherClient:
    def __init__(self, client: httpx.AsyncClient, clock=time.monotonic):
        self._http = client
        self._points = TTLCache(POINTS_TTL, clock)
        self._forecasts = TTLCache(FORECAST_TTL, clock)
        self._zips = TTLCache(POINTS_TTL, clock)

    async def zip_to_latlon(self, zipcode: str) -> tuple[float, float] | None:
        hit = self._zips.get(zipcode)
        if hit:
            return hit
        resp = await self._http.get(ZIP_URL.format(zip=zipcode))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        place = resp.json()["places"][0]
        coords = (float(place["latitude"]), float(place["longitude"]))
        self._zips.put(zipcode, coords)
        return coords

    async def forecast(self, lat: float, lon: float) -> Forecast | None:
        """Forecast periods, or None when NWS has no coverage for the point."""
        key = (round(lat, 2), round(lon, 2))
        cached = self._forecasts.get(key)
        if cached:
            return cached
        point = self._points.get(key)
        if point is None:
            resp = await self._http.get(
                f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}", headers={"Accept": "application/geo+json"}
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            props = resp.json()["properties"]
            rel = props.get("relativeLocation", {}).get("properties", {})
            place = f"{rel.get('city', 'here')} {rel.get('state', '')}".strip()
            point = (props["forecast"], place)
            self._points.put(key, point)
        url, place = point
        resp = await self._http.get(url, headers={"Accept": "application/geo+json"})
        resp.raise_for_status()
        result = Forecast(place, forecast_parts(resp.json()["properties"]["periods"]))
        self._forecasts.put(key, result)
        return result
