"""Contact frames and sender location lookup (layout in PROTOCOL.md)."""

import struct
from dataclasses import dataclass

RESP_CONTACT = 3
CONTACT_FRAME_LEN = 1 + 147


@dataclass
class Contact:
    name: str
    lat: float
    lon: float

    @property
    def has_gps(self) -> bool:
        return not (self.lat == 0 and self.lon == 0)


def parse_contact(payload: bytes) -> Contact | None:
    if len(payload) < CONTACT_FRAME_LEN or payload[0] != RESP_CONTACT:
        return None
    body = payload[1:]
    name = body[99:131].split(b"\x00", 1)[0].decode("utf-8", "replace")
    lat, lon = struct.unpack_from("<ii", body, 135)
    return Contact(name, lat / 1e6, lon / 1e6)


def resolve(contacts: list[Contact], name: str) -> tuple[str, Contact | None]:
    """Status is ok, unknown, ambiguous or no_gps. Never guesses between same-name nodes."""
    matches = [c for c in contacts if c.name == name]
    if not matches:
        return "unknown", None
    if len(matches) > 1:
        return "ambiguous", None
    if not matches[0].has_gps:
        return "no_gps", None
    return "ok", matches[0]
