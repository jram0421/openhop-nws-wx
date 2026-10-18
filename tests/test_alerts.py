import asyncio

import httpx

from nws_wx import alerts

DOC = {
    "features": [
        {"properties": {"id": "a1", "event": "Tornado Warning", "severity": "Extreme",
                        "areaDesc": "Dallas, TX", "ends": "2026-06-01T17:30:00-05:00",
                        "geocode": {"SAME": ["048113"], "UGC": ["TXC113"]}}},
        {"properties": {"id": "a2", "event": "Wind Advisory", "severity": "Minor",
                        "areaDesc": "Travis, TX",
                        "geocode": {"SAME": ["048453"], "UGC": ["TXZ192"]}}},
        {"properties": {"id": "a3", "event": "Flood Watch", "severity": "Moderate",
                        "areaDesc": "Harris, TX",
                        "geocode": {"SAME": ["048201"], "UGC": ["TXZ213"]}}},
        {"properties": {"event": "No id"}},
    ]
}


def test_parse_skips_bad_features():
    assert [a.id for a in alerts.parse_alerts(DOC)] == ["a1", "a2", "a3"]


def test_fetch_sends_user_agent_and_area():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        seen["area"] = request.url.params["area"]
        return httpx.Response(200, json=DOC)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     headers={"User-Agent": "test-ua"}) as c:
            return await alerts.fetch_active(c, ["TX", "OK"])

    assert len(asyncio.run(run())) == 3
    assert seen == {"ua": "test-ua", "area": "OK,TX"}


def test_filter_same_severity_and_event():
    a1, a2, a3 = alerts.parse_alerts(DOC)
    flt = alerts.AlertFilter(same_codes={"048113", "048453"}, min_severity="Moderate")
    assert flt.matches(a1) and not flt.matches(a2) and not flt.matches(a3)
    flt.events = {"Flood Watch"}
    assert not flt.matches(a1)


def test_filter_matches_zone_codes():
    a2 = alerts.parse_alerts(DOC)[1]
    flt = alerts.AlertFilter(zones={"TXZ192"}, min_severity="Minor")
    assert flt.matches(a2)


def test_relay_cooldown_cap_and_dedupe():
    t = [0.0]
    flt = alerts.AlertFilter(min_severity="Minor")
    relay = alerts.AlertRelay(flt, cooldown_s=60, hourly_cap=1, clock=lambda: t[0])
    parsed = alerts.parse_alerts(DOC)

    first = relay.select(parsed)
    assert [a.id for a in first] == ["a1"]  # most severe first
    relay.mark_sent(first[0])
    assert relay.select(parsed) == []  # cooldown

    t[0] = 61
    second = relay.select(parsed)
    assert [a.id for a in second] == ["a3"]  # a1 deduped
    relay.mark_sent(second[0])

    t[0] = 200
    assert relay.select(parsed) == []  # normal hourly cap (1) hit; a1 was urgent and counted separately
    t[0] = 3700
    assert [a.id for a in relay.select(parsed)] == ["a2"]


def test_format_truncates_to_byte_limit():
    long = alerts.Alert("x", "Tornado Warning", "Extreme", "é" * 200, "h", None, frozenset(), frozenset())
    assert len(alerts.format_alert(long).encode()) <= alerts.MAX_TEXT_BYTES


def test_format_includes_end_time():
    a1 = alerts.parse_alerts(DOC)[0]
    assert alerts.format_alert(a1).startswith("NWS Tornado Warning (Extreme): Dallas, TX until 5:30PM")


def _mk(aid, severity):
    return alerts.Alert(aid, "Event " + aid, severity, "Area", "h", None, frozenset(), frozenset())


def test_severe_and_extreme_skip_the_cooldown():
    t = [0.0]
    relay = alerts.AlertRelay(alerts.AlertFilter(min_severity="Minor"), cooldown_s=300, hourly_cap=10, clock=lambda: t[0])
    relay.mark_sent(_mk("first", "Moderate"))
    t[0] = 5  # deep in the cooldown
    pending = [_mk("m", "Moderate"), _mk("s", "Severe"), _mk("e", "Extreme")]
    assert [a.id for a in relay.select(pending)] == ["e", "s"]  # urgent only, most severe first
    for a in relay.select(pending):
        relay.mark_sent(a)
    assert relay.select(pending) == []  # the Moderate one still waits
    t[0] = 400
    assert [a.id for a in relay.select(pending)] == ["m"]


def test_cooldown_still_applies_below_severe():
    t = [0.0]
    relay = alerts.AlertRelay(alerts.AlertFilter(min_severity="Minor"), cooldown_s=60, hourly_cap=10, clock=lambda: t[0])
    relay.mark_sent(_mk("a", "Moderate"))
    t[0] = 10
    assert relay.select([_mk("b", "Moderate"), _mk("c", "Minor")]) == []


def test_urgent_alerts_have_their_own_hourly_cap():
    t = [0.0]
    relay = alerts.AlertRelay(alerts.AlertFilter(min_severity="Minor"), cooldown_s=300, hourly_cap=2,
                              urgent_hourly_cap=3, clock=lambda: t[0])
    relay.mark_sent(_mk("n1", "Minor"))
    t[0] = 400
    relay.mark_sent(_mk("n2", "Moderate"))  # normal budget (2) now spent
    t[0] = 800
    batch = [_mk(f"u{i}", "Extreme") for i in range(5)]
    got = relay.select(batch)
    assert [a.id for a in got] == ["u0", "u1", "u2"]  # urgent room is 3, unaffected by normal sends
    for a in got:
        relay.mark_sent(a)
    assert relay.select(batch[3:]) == []  # urgent budget spent
    assert relay.select([_mk("n3", "Moderate")]) == []  # normal budget still spent too
    t[0] = 800 + 3600
    assert [a.id for a in relay.select(batch[3:])] == ["u3", "u4"]


def test_urgent_burst_does_not_use_the_normal_budget():
    t = [0.0]
    relay = alerts.AlertRelay(alerts.AlertFilter(min_severity="Minor"), cooldown_s=60, hourly_cap=2,
                              urgent_hourly_cap=10, clock=lambda: t[0])
    for i in range(5):
        relay.mark_sent(_mk(f"t{i}", "Severe"))
    t[0] = 100  # cooldown over
    assert [a.id for a in relay.select([_mk("w", "Moderate")])] == ["w"]  # normal budget untouched


def test_normal_alerts_proceed_when_urgent_budget_is_spent():
    t = [0.0]
    relay = alerts.AlertRelay(alerts.AlertFilter(min_severity="Minor"), cooldown_s=60, hourly_cap=5,
                              urgent_hourly_cap=1, clock=lambda: t[0])
    relay.mark_sent(_mk("t0", "Extreme"))
    t[0] = 100
    mixed = [_mk("t1", "Extreme"), _mk("w", "Moderate")]
    assert [a.id for a in relay.select(mixed)] == ["w"]  # Extreme is capped, Moderate still goes


def test_urgent_cap_config():
    from nws_wx import config
    base = {"contact_email": "a@b.co", "states": ["TX"]}
    assert config.parse_config(base).urgent_hourly_cap == 30
    assert config.parse_config({**base, "urgent_hourly_cap": 50}).urgent_hourly_cap == 50
    import pytest
    for bad in (0, 500, "x", True):
        with pytest.raises(config.ConfigError):
            config.parse_config({**base, "urgent_hourly_cap": bad})
