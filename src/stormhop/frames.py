"""Companion frame server client (asyncio). See PROTOCOL.md for sources."""

import asyncio
import struct
import time
from dataclasses import dataclass

from .contacts import Contact, parse_contact

OUT = 0x3C  # client -> server
IN = 0x3E  # server -> client
MAX_FRAME_SIZE = 176
CHANNEL_NAME_SIZE = 32

CMD_APP_START = 1
CMD_SEND_CHANNEL_TXT_MSG = 3
CMD_GET_CONTACTS = 4
CMD_SYNC_NEXT_MESSAGE = 10
CMD_DEVICE_QUERY = 22
CMD_GET_CHANNEL = 31

RESP_OK = 0
RESP_ERR = 1
RESP_END_OF_CONTACTS = 4
RESP_NO_MORE_MESSAGES = 10
RESP_DEVICE_INFO = 13
RESP_CHANNEL_MSG_RECV = 8
RESP_CHANNEL_MSG_RECV_V3 = 17
RESP_CHANNEL_INFO = 18
PUSH_MSG_WAITING = 0x83

APP_VERSION = 3
TXT_TYPE_PLAIN = 0


SELF_INFO_NAME_OFFSET = 58  # code, type, tx, maxtx, pubkey(32), lat, lon, 4 flag bytes, freq, bw, sf, cr


def parse_self_name(payload: bytes) -> str:
    """Our node's advert name from RESP_CODE_SELF_INFO, or '' if it doesn't look right."""
    if len(payload) <= SELF_INFO_NAME_OFFSET or payload[0] != 5:
        return ""
    name = payload[SELF_INFO_NAME_OFFSET:].split(b"\x00", 1)[0].decode("utf-8", "replace")
    return name if name.isprintable() else ""


class FrameError(Exception):
    pass


@dataclass
class ChannelMessage:
    channel_idx: int
    text: str
    timestamp: int
    path_len: int


def encode_frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_SIZE:
        raise FrameError("frame too large")
    return bytes([OUT]) + struct.pack("<H", len(payload)) + payload


def encode_channel_text(channel_idx: int, text: str, timestamp: int | None = None) -> bytes:
    ts = int(time.time()) if timestamp is None else timestamp
    body = text.encode("utf-8")
    return bytes([CMD_SEND_CHANNEL_TXT_MSG, TXT_TYPE_PLAIN, channel_idx]) + struct.pack("<I", ts) + body


def parse_channel_info(payload: bytes) -> tuple[int, str]:
    if len(payload) < 2 + CHANNEL_NAME_SIZE or payload[0] != RESP_CHANNEL_INFO:
        raise FrameError("bad channel info")
    name = payload[2 : 2 + CHANNEL_NAME_SIZE].split(b"\x00", 1)[0].decode("utf-8", "replace")
    return payload[1], name


def parse_channel_message(payload: bytes) -> ChannelMessage | None:
    """Return a ChannelMessage, or None for non-channel-text frames."""
    if not payload:
        return None
    code = payload[0]
    if code == RESP_CHANNEL_MSG_RECV_V3 and len(payload) >= 12:
        chan, path_len = payload[4], payload[5]
        (ts,) = struct.unpack_from("<I", payload, 7)
        text = payload[11:]
    elif code == RESP_CHANNEL_MSG_RECV and len(payload) >= 8:
        chan, path_len = payload[1], payload[2]
        (ts,) = struct.unpack_from("<I", payload, 4)
        text = payload[8:]
    else:
        return None
    return ChannelMessage(chan, text.rstrip(b"\x00").decode("utf-8", "replace"), ts, path_len)


class FrameClient:
    """One connection. Reader task queues frames; commands wait for a reply.

    Pushes (MSG_WAITING) are surfaced via the `waiting` event.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5000):
        self.host, self.port = host, port
        self.waiting = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_reason = ""
        self.self_name = ""
        self.last_sent = 0
        self._connected_at = 0.0
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._reader = self._writer = self._task = None

    async def connect(self, timeout: float = 10.0) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout
        )
        self._connected_at = time.monotonic()
        self._task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self._writer:
            self._writer.close()
        self.closed.set()

    async def _read_loop(self) -> None:
        try:
            while True:
                if (await self._reader.readexactly(1))[0] != IN:
                    continue
                (n,) = struct.unpack("<H", await self._reader.readexactly(2))
                payload = await self._reader.readexactly(n)
                if not payload:
                    continue
                if payload[0] == PUSH_MSG_WAITING:
                    self.waiting.set()
                elif payload[0] == 9:  # heartbeat (CURR_TIME)
                    continue
                elif payload[0] >= 0x80:
                    continue
                else:
                    self._replies.put_nowait(payload)
        except asyncio.IncompleteReadError:
            self.close_reason = "server closed the connection (EOF)"
        except ConnectionError as e:
            self.close_reason = f"connection error: {type(e).__name__}"
        except asyncio.CancelledError:
            pass
        except Exception as e:  # keep the reason visible instead of a silent task failure
            self.close_reason = f"read loop failed: {type(e).__name__}: {e}"
        finally:
            if self.close_reason:
                alive = time.monotonic() - self._connected_at
                self.close_reason += f" after {alive:.1f}s (last command sent: {self.last_sent})"
            self.closed.set()

    async def request(self, payload: bytes, timeout: float = 10.0) -> bytes:
        async with self._lock:
            while not self._replies.empty():
                self._replies.get_nowait()
            self.last_sent = payload[0]
            self._writer.write(encode_frame(payload))
            await self._writer.drain()
            return await asyncio.wait_for(self._replies.get(), timeout)

    async def request_many(self, payload: bytes, end_code: int, timeout: float = 10.0) -> list[bytes]:
        """Collect reply frames until end_code arrives."""
        async with self._lock:
            while not self._replies.empty():
                self._replies.get_nowait()
            self.last_sent = payload[0]
            self._writer.write(encode_frame(payload))
            await self._writer.drain()
            frames: list[bytes] = []
            while True:
                reply = await asyncio.wait_for(self._replies.get(), timeout)
                if reply[0] == end_code:
                    return frames
                if reply[0] == RESP_ERR:
                    raise FrameError(f"error reply {reply[1:2].hex()}")
                frames.append(reply)

    async def contacts(self) -> list[Contact]:
        frames = await self.request_many(bytes([CMD_GET_CONTACTS]), RESP_END_OF_CONTACTS)
        return [c for c in map(parse_contact, frames) if c]

    async def start(self) -> int:
        """App start + device query. Returns max channels."""
        self.self_name = parse_self_name(await self.request(bytes([CMD_APP_START]) + bytes(7)))
        info = await self.request(bytes([CMD_DEVICE_QUERY, APP_VERSION]))
        if info[0] != RESP_DEVICE_INFO:
            raise FrameError("unexpected device info")
        return info[3]

    async def keepalive(self) -> None:
        """Harmless read-only command so the server's idle timeout never fires."""
        await self.request(bytes([CMD_DEVICE_QUERY, APP_VERSION]))

    async def channels(self, max_channels: int) -> dict[str, int]:
        found: dict[str, int] = {}
        for i in range(max_channels):
            reply = await self.request(bytes([CMD_GET_CHANNEL, i]))
            if reply[0] != RESP_CHANNEL_INFO:
                continue
            idx, name = parse_channel_info(reply)
            if name:
                found.setdefault(name, idx)
        return found

    async def send_channel(self, channel_idx: int, text: str) -> bool:
        reply = await self.request(encode_channel_text(channel_idx, text))
        return reply[0] == RESP_OK

    async def drain_messages(self) -> list[ChannelMessage]:
        self.waiting.clear()
        out: list[ChannelMessage] = []
        while True:
            reply = await self.request(bytes([CMD_SYNC_NEXT_MESSAGE]))
            if reply[0] == RESP_NO_MORE_MESSAGES:
                return out
            msg = parse_channel_message(reply)
            if msg:
                out.append(msg)
