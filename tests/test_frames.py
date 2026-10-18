import struct

import pytest

from stormhop import frames


def test_encode_frame_header():
    out = frames.encode_frame(b"\x16\x03")
    assert out == bytes([0x3C, 2, 0]) + b"\x16\x03"


def test_encode_frame_too_large():
    with pytest.raises(frames.FrameError):
        frames.encode_frame(bytes(frames.MAX_FRAME_SIZE + 1))


def test_encode_channel_text():
    out = frames.encode_channel_text(2, "hi", timestamp=5)
    assert out == bytes([3, 0, 2]) + struct.pack("<I", 5) + b"hi"


def test_parse_channel_info():
    name = b"WX".ljust(32, b"\x00")
    payload = bytes([18, 4]) + name + bytes(16)
    assert frames.parse_channel_info(payload) == (4, "WX")


def test_parse_v3_message():
    payload = bytes([17, 8, 0, 0, 1, 2, 0]) + struct.pack("<I", 99) + b"bob: wx 90210\x00"
    msg = frames.parse_channel_message(payload)
    assert (msg.channel_idx, msg.path_len, msg.timestamp, msg.text) == (1, 2, 99, "bob: wx 90210")


def test_parse_legacy_message():
    payload = bytes([8, 3, 1, 0]) + struct.pack("<I", 7) + b"alerts"
    msg = frames.parse_channel_message(payload)
    assert (msg.channel_idx, msg.text) == (3, "alerts")


def test_parse_ignores_other_frames():
    assert frames.parse_channel_message(bytes([16, 0, 0, 0])) is None
    assert frames.parse_channel_message(b"") is None
