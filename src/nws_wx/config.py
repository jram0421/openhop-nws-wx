"""Plugin config: load, validate, and build the NWS User-Agent."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .alerts import SEVERITY_ORDER

EMAIL_RE = re.compile(r"^[^@\s,;()<>]+@[^@\s,;()<>]+\.[^@\s,;()<>]+$")
SAME_RE = re.compile(r"^\d{6}$")
ZONE_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")
STATE_RE = re.compile(r"^[A-Z]{2}$")

DEFAULTS = {
    "contact_email": "",
    "frame_host": "127.0.0.1",
    "frame_port": 5000,
    "wx_channel": "WX",
    "alerts_channel": "WX-Alerts",
    "bot_enabled": True,
    "alerts_enabled": True,
    "states": [],
    "counties": [],
    "zones": [],
    "min_severity": "Moderate",
    "events": [],
    "poll_seconds": 120,
    "alert_cooldown_seconds": 60,
    "alert_hourly_cap": 10,
}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    contact_email: str
    frame_host: str
    frame_port: int
    wx_channel: str
    alerts_channel: str
    bot_enabled: bool
    alerts_enabled: bool
    states: list[str] = field(default_factory=list)
    counties: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    min_severity: str = "Moderate"
    events: list[str] = field(default_factory=list)
    poll_seconds: int = 120
    alert_cooldown_seconds: int = 60
    alert_hourly_cap: int = 10

    @property
    def user_agent(self) -> str:
        return f"openhop-nws-wx/{__version__} ({self.contact_email})"


def _int(raw: dict, key: str, lo: int, hi: int, errors: list[str]) -> int:
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        errors.append(f"{key} must be an integer from {lo} to {hi}")
        return DEFAULTS[key]
    return v


def _list(raw: dict, key: str, pattern: re.Pattern | None, errors: list[str], upper: bool = False) -> list[str]:
    v = raw[key]
    if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
        errors.append(f"{key} must be a list of strings")
        return []
    items = [i.strip().upper() if upper else i.strip() for i in v if i.strip()]
    bad = [i for i in items if pattern and not pattern.match(i)]
    if bad:
        errors.append(f"{key} has invalid entries: {', '.join(bad[:3])}")
    return sorted(set(items))


def parse_config(raw: object) -> Config:
    """Validate a config object. Raises ConfigError listing every problem."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    raw = {**DEFAULTS, **raw}
    errors: list[str] = []

    email = raw["contact_email"]
    if not isinstance(email, str) or not EMAIL_RE.match(email.strip()):
        errors.append("contact_email must be a valid email address (sent to NWS in the User-Agent)")
        email = ""
    for key in ("frame_host", "wx_channel", "alerts_channel"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            errors.append(f"{key} must be a non-empty string")
    for key in ("bot_enabled", "alerts_enabled"):
        if not isinstance(raw[key], bool):
            errors.append(f"{key} must be true or false")
    if raw["min_severity"] not in SEVERITY_ORDER:
        errors.append(f"min_severity must be one of {', '.join(SEVERITY_ORDER)}")

    cfg = Config(
        contact_email=email.strip(),
        frame_host=str(raw["frame_host"]).strip(),
        frame_port=_int(raw, "frame_port", 1, 65535, errors),
        wx_channel=str(raw["wx_channel"]).strip(),
        alerts_channel=str(raw["alerts_channel"]).strip(),
        bot_enabled=raw["bot_enabled"] is True,
        alerts_enabled=raw["alerts_enabled"] is True,
        states=_list(raw, "states", STATE_RE, errors, upper=True),
        counties=_list(raw, "counties", SAME_RE, errors),
        zones=_list(raw, "zones", ZONE_RE, errors, upper=True),
        min_severity=raw["min_severity"] if raw["min_severity"] in SEVERITY_ORDER else "Moderate",
        events=_list(raw, "events", None, errors),
        poll_seconds=_int(raw, "poll_seconds", 60, 3600, errors),
        alert_cooldown_seconds=_int(raw, "alert_cooldown_seconds", 0, 3600, errors),
        alert_hourly_cap=_int(raw, "alert_hourly_cap", 1, 60, errors),
    )
    if cfg.alerts_enabled and not cfg.states:
        errors.append("alerts need at least one state")
    if errors:
        raise ConfigError("; ".join(errors))
    return cfg


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read config.json: {e}") from e
    return parse_config(raw)
