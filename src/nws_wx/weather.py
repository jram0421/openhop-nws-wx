"""Forecast lookups: NWS /points -> gridpoint forecast, with caching."""

import time

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


def format_forecast(place: str, periods: list[dict]) -> str:
    parts = []
    for p in periods[:3]:
        wind = f", {p['windDirection']} {p['windSpeed']}" if p.get("windSpeed") else ""
        parts.append(f"{p['name']}: {p['temperature']}{p.get('temperatureUnit', 'F')} {p['shortForecast']}{wind}")
    return truncate(f"WX {place} | " + " | ".join(parts))


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

    async def forecast(self, lat: float, lon: float) -> str | None:
        """One-line forecast, or None when NWS has no coverage for the point."""
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
        text = format_forecast(place, resp.json()["properties"]["periods"])
        self._forecasts.put(key, text)
        return text
