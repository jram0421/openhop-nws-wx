import asyncio
import struct

import httpx

from nws_wx import alerts, bot, contacts, weather


def contact_frame(name: str, lat: float, lon: float) -> bytes:
    body = bytearray(147)
    body[99 : 99 + len(name.encode())] = name.encode()
    struct.pack_into("<ii", body, 135, int(lat * 1e6), int(lon * 1e6))
    return bytes([3]) + bytes(body)


PERIODS = [
    {"name": "Today", "temperature": 78, "temperatureUnit": "F", "shortForecast": "Sunny",
     "windDirection": "W", "windSpeed": "10 mph"},
    {"name": "Tonight", "temperature": 60, "temperatureUnit": "F", "shortForecast": "Clear",
     "windDirection": "N", "windSpeed": "5 mph"},
]


def make_http(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = request.url
        if url.host == "api.zippopotam.us":
            if url.path.endswith("00000"):
                return httpx.Response(404)
            return httpx.Response(200, json={"places": [{"latitude": "32.78", "longitude": "-96.80"}]})
        if url.path.startswith("/points/"):
            if url.path.startswith("/points/51."):
                return httpx.Response(404)
            return httpx.Response(200, json={"properties": {
                "forecast": "https://api.weather.gov/gridpoints/FWD/1,1/forecast",
                "relativeLocation": {"properties": {"city": "Dallas", "state": "TX"}}}})
        if url.path.endswith("/forecast"):
            return httpx.Response(200, json={"properties": {"periods": PERIODS}})
        return httpx.Response(500)

    return handler


def make_bot(cts, active=(), calls=None, limiter=None):
    calls = [] if calls is None else calls
    http = httpx.AsyncClient(transport=httpx.MockTransport(make_http(calls)))

    async def get_contacts():
        return cts

    return bot.Bot(weather.WeatherClient(http), get_contacts, lambda: list(active), limiter), calls


def run(coro):
    return asyncio.run(coro)


def test_parse_contact_and_resolve():
    c = contacts.parse_contact(contact_frame("Bob", 32.78, -96.8))
    assert (c.name, round(c.lat, 2), round(c.lon, 2)) == ("Bob", 32.78, -96.8)
    nogps = contacts.parse_contact(contact_frame("Ann", 0, 0))
    assert contacts.resolve([c, nogps], "Bob")[0] == "ok"
    assert contacts.resolve([c, nogps], "Ann")[0] == "no_gps"
    assert contacts.resolve([c, nogps], "Zed")[0] == "unknown"
    assert contacts.resolve([c, c], "Bob")[0] == "ambiguous"


def test_wx_uses_sender_gps():
    b, calls = make_bot([contacts.parse_contact(contact_frame("Bob", 32.78, -96.8))])
    reply = run(b.handle("Bob: wx"))
    assert reply.startswith("@Bob WX Dallas TX | Today: 78F Sunny, W 10 mph")
    assert any("/points/32.7800,-96.8000" in c for c in calls)


def test_wx_without_gps_asks_for_zip():
    b, _ = make_bot([contacts.parse_contact(contact_frame("Ann", 0, 0))])
    assert "isn't sharing a location" in run(b.handle("Ann: wx"))


def test_wx_unknown_sender():
    b, _ = make_bot([])
    assert "don't have your node" in run(b.handle("Zed: WX"))


def test_wx_zip_and_bad_zip():
    b, _ = make_bot([], limiter=bot.RateLimiter(min_gap_s=0))
    assert "Dallas TX" in run(b.handle("Bob: wx 75201"))
    assert "Unknown zip" in run(b.handle("Bob: wx 00000"))
    assert "Use:" in run(b.handle("Bob: wx abc"))


def test_non_us_point_reports_no_coverage():
    b, _ = make_bot([contacts.parse_contact(contact_frame("Bob", 51.5, -0.1))])
    assert "US only" in run(b.handle("Bob: wx"))


def test_forecast_cached_across_senders():
    b, calls = make_bot([], limiter=bot.RateLimiter(min_gap_s=0))
    run(b.handle("Bob: wx 75201"))
    run(b.handle("Ann: wx 75201"))
    assert sum("/forecast" in c for c in calls) == 1
    assert sum("zippopotam" in c for c in calls) == 1


def test_rate_limit_per_sender():
    t = [0.0]
    b, _ = make_bot([], limiter=bot.RateLimiter(min_gap_s=20, hourly=2, clock=lambda: t[0]))
    assert run(b.handle("Bob: wx 75201"))
    assert run(b.handle("Bob: wx 75201")) is None  # too soon
    assert run(b.handle("Ann: wx 75201"))  # other sender unaffected
    t[0] = 25
    assert run(b.handle("Bob: wx 75201"))
    t[0] = 50
    assert run(b.handle("Bob: wx 75201")) is None  # hourly cap


def test_ignores_non_commands_and_own_replies():
    b, _ = make_bot([])
    assert run(b.handle("Bob: hello")) is None
    assert run(b.handle("no sender prefix")) is None
    assert run(b.handle("Bob: wx 1 2")) is None
    assert run(b.handle("Bob: @Ann wx")) is None


def test_alerts_command():
    a = alerts.Alert("a", "Tornado Warning", "Extreme", "Dallas, TX; Tarrant, TX", "h", None,
                     frozenset(), frozenset())
    b, _ = make_bot([], active=[a])
    assert run(b.handle("Bob: alerts")) == "@Bob Active: Tornado Warning (Dallas, TX)"
    b, _ = make_bot([])
    assert "No active alerts" in run(b.handle("Bob: alerts"))


def test_upstream_failure_reply():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    async def get_contacts():
        return []

    b = bot.Bot(weather.WeatherClient(http), get_contacts, list)
    assert bot.UNAVAILABLE in run(b.handle("Bob: wx 75201"))
