import asyncio
import json
import struct

import pytest

from nws_wx import config, frames, main
from tests.test_bot import contact_frame


def test_config_defaults_need_email_and_state():
    with pytest.raises(config.ConfigError) as e:
        config.parse_config({})
    assert "contact_email" in str(e.value) and "state" in str(e.value)


def test_config_valid_and_user_agent():
    cfg = config.parse_config({"contact_email": "me@example.com", "states": ["tx", "ok"],
                               "counties": ["048113"], "zones": ["txz104"]})
    assert cfg.states == ["OK", "TX"] and cfg.zones == ["TXZ104"]
    assert cfg.user_agent == "openhop-nws-wx/0.1.0 (me@example.com)"


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a b@c.com", "a@b.com, c@d.com"])
def test_config_rejects_bad_email(bad):
    with pytest.raises(config.ConfigError):
        config.parse_config({"contact_email": bad, "states": ["TX"]})


def test_config_reports_all_problems():
    with pytest.raises(config.ConfigError) as e:
        config.parse_config({"contact_email": "me@example.com", "states": ["Texas"], "counties": ["12"],
                             "poll_seconds": 5, "min_severity": "Bad"})
    msg = str(e.value)
    for key in ("states", "counties", "poll_seconds", "min_severity"):
        assert key in msg


def test_config_alerts_off_needs_no_state():
    cfg = config.parse_config({"contact_email": "me@example.com", "alerts_enabled": False})
    assert cfg.states == []


class FakeFrameServer:
    def __init__(self):
        self.sent: list[bytes] = []
        self.queue = [bytes([17, 8, 0, 0, 1, 0, 0]) + struct.pack("<I", 1) + b"Bob: wx"]

    async def handle(self, reader, writer):
        def reply(payload: bytes):
            writer.write(bytes([frames.IN]) + struct.pack("<H", len(payload)) + payload)

        try:
            while True:
                assert (await reader.readexactly(1))[0] == frames.OUT
                (n,) = struct.unpack("<H", await reader.readexactly(2))
                p = await reader.readexactly(n)
                op = p[0]
                if op == frames.CMD_APP_START:
                    reply(bytes([5]) + bytes(20))
                elif op == frames.CMD_DEVICE_QUERY:
                    reply(bytes([13, 14, 50, 4]) + bytes(78))
                elif op == frames.CMD_GET_CHANNEL:
                    names = {0: b"Public", 1: b"WX", 2: b"WX-Alerts"}
                    reply(bytes([18, p[1]]) + names.get(p[1], b"").ljust(32, b"\0") + bytes(16))
                elif op == frames.CMD_SEND_CHANNEL_TXT_MSG:
                    self.sent.append(p)
                    reply(bytes([0]))
                elif op == frames.CMD_SYNC_NEXT_MESSAGE:
                    reply(self.queue.pop(0) if self.queue else bytes([10]))
                elif op == frames.CMD_GET_CONTACTS:
                    reply(bytes([2]) + struct.pack("<I", 1))
                    reply(contact_frame("Bob", 32.78, -96.8))
                    reply(bytes([4]) + struct.pack("<I", 0))
                    reply(bytes([0x83]))  # unsolicited push must not disturb replies
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


def test_frame_client_against_fake_server():
    async def run():
        fake = FakeFrameServer()
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = frames.FrameClient("127.0.0.1", port)
        await client.connect()
        max_ch = await client.start()
        chans = await client.channels(max_ch)
        assert chans == {"Public": 0, "WX": 1, "WX-Alerts": 2}
        assert await client.send_channel(2, "hello")
        assert fake.sent[0][:3] == bytes([3, 0, 2]) and fake.sent[0].endswith(b"hello")
        msgs = await client.drain_messages()
        assert [(m.channel_idx, m.text) for m in msgs] == [(1, "Bob: wx")]
        contacts = await client.contacts()
        assert [c.name for c in contacts] == ["Bob"]
        await client.close()
        server.close()

    asyncio.run(asyncio.wait_for(run(), 10))


def test_supervise_without_config_reports_needs_config_and_stops(tmp_path):
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(main.supervise(tmp_path, stop))
        await asyncio.sleep(0.2)
        status = json.loads((tmp_path / "runtime.json").read_text())
        assert status["state"] == "needs_config" and "contact_email" in status["message"]
        stop.set()
        await asyncio.wait_for(task, 2)
        assert json.loads((tmp_path / "runtime.json").read_text())["state"] == "stopped"

    asyncio.run(run())


def test_supervise_retries_when_frame_server_down(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"contact_email": "me@example.com", "states": ["TX"], "frame_port": 1}))

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(main.supervise(tmp_path, stop))
        await asyncio.sleep(0.5)
        status = json.loads((tmp_path / "runtime.json").read_text())
        assert status["state"] == "reconnecting" and status["retry_in"] <= main.MAX_BACKOFF_S
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
