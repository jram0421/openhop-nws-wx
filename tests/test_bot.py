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


def _alert(aid, event, sev, ends="2026-06-01T17:30:00-05:00"):
    return {"properties": {"id": aid, "event": event, "severity": sev, "areaDesc": "Dallas, TX",
                           "ends": ends, "geocode": {"SAME": ["048113"], "UGC": ["TXC113"]}}}


POINT_ALERTS = {"features": [_alert("1", "Wind Advisory", "Minor"), _alert("2", "Tornado Warning", "Extreme"),
                             _alert("3", "Flood Watch", "Moderate", None)]}


def make_http(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = request.url
        if url.host == "api.zippopotam.us":
            if url.path.endswith("00000"):
                return httpx.Response(404)
            return httpx.Response(200, json={"places": [{"latitude": "32.78", "longitude": "-96.80"}]})
        if url.path == "/alerts/active":
            if url.params.get("point", "").startswith("32.78"):
                return httpx.Response(200, json=POINT_ALERTS)
            return httpx.Response(200, json={"features": []})
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
    assert reply == "@Bob Dallas TX: Today 78F Sunny, W 10mph | Tonight 60F Clear"
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


LONG_PERIODS = PERIODS + [
    {"name": "Thursday", "temperature": 85, "temperatureUnit": "F", "shortForecast": "Mostly Sunny"},
    {"name": "Thursday Night", "temperature": 66, "temperatureUnit": "F", "shortForecast": "Slight Chance Showers"},
]


def test_forecast_parts_abbreviate_days_and_keep_wind_to_first():
    parts = weather.forecast_parts(LONG_PERIODS)
    assert parts[2] == "Thu 85F Mostly Sunny" and parts[3] == "Thu Night 66F Slight Chance Showers"
    assert "mph" in parts[0] and "mph" not in parts[1]


def test_compose_adds_whole_periods_only():
    parts = weather.forecast_parts(LONG_PERIODS)
    full = weather.compose("@Bob Dallas TX: ", parts, 200)
    assert full.endswith("Thu Night 66F Slight Chance Showers") and "..." not in full
    tight = weather.compose("@Bob Dallas TX: ", parts, 75)
    assert tight == "@Bob Dallas TX: Today 78F Sunny, W 10mph | Tonight 60F Clear"
    assert len(tight.encode()) <= 75
    assert weather.compose("@Bob Dallas TX: ", parts, 30).endswith("...")  # only the first can be cut


def test_message_budget_accounts_for_node_name():
    from nws_wx import main
    assert main.message_budget("Base") == 154
    assert main.message_budget("") == 130
    assert main.message_budget("x" * 200) == 60


def test_parse_self_name():
    from nws_wx import frames
    payload = bytes([5]) + bytes(57) + b"Hilltop\x00"
    assert frames.parse_self_name(payload) == "Hilltop"
    assert frames.parse_self_name(bytes([5]) + bytes(10)) == ""
    assert frames.parse_self_name(bytes([5]) + bytes(57) + b"\x01\x02") == ""


def test_alerts_zip_lists_all_severities_most_severe_first():
    b, calls = make_bot([], limiter=bot.RateLimiter(min_gap_s=0))
    reply = run(b.handle("Bob: alerts 75201"))
    assert reply == ("@Bob 75201: Tornado Warning until 5:30PM Mon; Flood Watch; Wind Advisory until 5:30PM Mon")
    assert any("alerts/active?point=32.7800%2C-96.8000" in c or "point=32.7800,-96.8000" in c for c in calls)


def test_alerts_zip_none_unknown_and_invalid():
    b, _ = make_bot([], limiter=bot.RateLimiter(min_gap_s=0))
    b.weather._zips.put("11111", (40.0, -75.0))
    assert run(b.handle("Bob: alerts 11111")) == "@Bob 11111: no active alerts."
    assert "Unknown zip" in run(b.handle("Bob: alerts 00000"))
    assert "Use:" in run(b.handle("Bob: alerts abc"))


def test_alerts_zip_fits_budget_and_counts_the_rest():
    b, _ = make_bot([], limiter=bot.RateLimiter(min_gap_s=0))
    b.max_bytes = 70
    reply = run(b.handle("Bob: alerts 75201"))
    assert reply.startswith("@Bob 75201: Tornado Warning until 5:30PM Mon") and reply.endswith("more")
    assert len(reply.encode()) <= 70


def test_alerts_zip_cached():
    b, calls = make_bot([], limiter=bot.RateLimiter(min_gap_s=0))
    run(b.handle("Bob: alerts 75201")); run(b.handle("Ann: alerts 75201"))
    assert sum("alerts/active" in c for c in calls) == 1
