"""Plugin runtime: frame client + alert poller + wx bot, supervised in the foreground."""

import asyncio
import json
import logging
import os
import random
import signal
import time
from pathlib import Path

import httpx

from . import alerts as alert_mod
from . import __version__
from .bot import Bot
from .config import Config, ConfigError, load_config
from .frames import FrameClient
from .weather import WeatherClient, truncate

log = logging.getLogger("stormhop")

MAX_BACKOFF_S = 60
CONTACTS_TTL_S = 60
SYNC_INTERVAL_S = 30  # also keeps the frame server's idle timeout from firing
CONFIG_CHECK_S = 5
SEEN_CAP = 500
NWS_PROBE_S = 300
REPLY_GAP_S = 2.0  # spacing between messages of one reply, to be kind to the channel


class Status:
    """runtime.json snapshot, written atomically and kept small."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "runtime.json"
        self.data: dict = {"state": "starting", "version": __version__}

    def update(self, **fields) -> None:
        self.data.update(fields)
        self.data["updated"] = int(time.time())
        tmp = self.path.with_name(".runtime.json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
        tmp.replace(self.path)


def describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def set_nws(status: Status, ok: bool, detail: str, source: str) -> None:
    status.update(nws={"ok": ok, "detail": detail, "source": source, "checked": int(time.time())})


async def probe_nws(http: httpx.AsyncClient, status: Status) -> None:
    """One cheap request so the config page can show whether NWS answers our User-Agent."""
    try:
        resp = await http.get(f"{alert_mod.NWS_BASE}/alerts/active/count", headers={"Accept": "application/json"})
        resp.raise_for_status()
        set_nws(status, True, f"HTTP {resp.status_code}", "probe")
    except httpx.HTTPError as e:
        set_nws(status, False, describe_error(e), "probe")


async def nws_probe_loop(http: httpx.AsyncClient, status: Status) -> None:
    while True:
        await probe_nws(http, status)
        await asyncio.sleep(NWS_PROBE_S)


def load_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text("utf-8")).get("seen", []))
    except (OSError, ValueError, AttributeError):
        return set()


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text("utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(path: Path, **fields) -> None:
    tmp = path.with_name(".state.json.tmp")
    tmp.write_text(json.dumps({**read_state(path), **fields}), "utf-8")
    tmp.replace(path)


def save_seen(path: Path, seen: set[str]) -> None:
    update_state(path, seen=sorted(seen)[-SEEN_CAP:])


async def send_test_if_requested(client: FrameClient, cfg: Config, channels: dict[str, int], data_dir: Path,
                                 status: Status, budget: int) -> None:
    """The config page asks for a test by saving a new test_request number; handle each number once."""
    state_path = data_dir / "state.json"
    if not cfg.test_request or read_state(state_path).get("test_done") == cfg.test_request:
        return
    idx = find_channel(channels, cfg.alerts_channel)
    if idx is None:
        ok, detail = False, f"channel '{cfg.alerts_channel}' not found (have: {', '.join(sorted(channels))})"
    else:
        text = truncate(f"Stormhop test message (v{__version__}): the relay can post here. Ignore.", budget)
        ok = await client.send_channel(idx, text)
        detail = "sent" if ok else "the companion rejected the message"
    update_state(state_path, test_done=cfg.test_request)
    status.update(last_test={"request": cfg.test_request, "ok": ok, "detail": detail, "at": int(time.time())})
    log.info("test message: %s", detail)


async def config_changed(path: Path, baseline: bytes) -> None:
    while True:
        await asyncio.sleep(CONFIG_CHECK_S)
        try:
            if path.read_bytes() != baseline:
                return
        except FileNotFoundError:
            if baseline:
                return


def read_raw(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def message_budget(self_name: str) -> int:
    """Channel text shares 160 bytes with the 'name: ' prefix the sender's node adds."""
    if not self_name:
        return 130  # unknown name: leave room for a long one
    return max(60, alert_mod.MAX_TEXT_BYTES - len(self_name.encode("utf-8")) - 2)


def find_channel(channels: dict[str, int], name: str) -> int | None:
    """Exact name first, then case-insensitive ignoring a leading '#'."""
    if name in channels:
        return channels[name]
    want = name.lstrip("#").casefold()
    for have, idx in channels.items():
        if have.lstrip("#").casefold() == want:
            return idx
    return None


async def keepalive_loop(client: FrameClient) -> None:
    while True:
        await asyncio.sleep(SYNC_INTERVAL_S)
        await client.keepalive()


async def message_loop(client: FrameClient, bot: Bot, wx_idx: int, status: Status) -> None:
    while True:
        try:
            await asyncio.wait_for(client.waiting.wait(), SYNC_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        for msg in await client.drain_messages():
            if msg.channel_idx != wx_idx:
                continue
            for n, reply in enumerate(await bot.replies(msg.text)):
                if n:
                    await asyncio.sleep(REPLY_GAP_S)
                await client.send_channel(wx_idx, reply)
                status.data["replies"] = status.data.get("replies", 0) + 1


async def alert_loop(client: FrameClient, http: httpx.AsyncClient, cfg: Config, relay: alert_mod.AlertRelay,
                     alerts_idx: int, active: list, status: Status, seen_path: Path, budget: int) -> None:
    step = max(cfg.alert_cooldown_seconds + 1, 5)
    while True:
        try:
            current = await alert_mod.fetch_active(http, cfg.states)
            active[:] = [a for a in current if relay.filter.matches(a)]
            relay.prune({a.id for a in current})
            status.update(last_poll=int(time.time()), alerts_active=len(active), last_error=None)
            set_nws(status, True, f"{len(current)} alerts fetched", "poll")
        except httpx.HTTPError as e:
            log.warning("NWS poll failed: %s", e)
            status.update(last_error=f"NWS poll failed: {describe_error(e)}")
            set_nws(status, False, describe_error(e), "poll")
            await asyncio.sleep(cfg.poll_seconds)
            continue
        deadline = time.monotonic() + cfg.poll_seconds
        while time.monotonic() < deadline:
            for n, a in enumerate(relay.select(current)):
                if n:
                    await asyncio.sleep(REPLY_GAP_S)
                if await client.send_channel(alerts_idx, alert_mod.format_alert(a, budget)):
                    relay.mark_sent(a)
                    save_seen(seen_path, relay.seen)
            await asyncio.sleep(min(step, max(0.0, deadline - time.monotonic())) or 0.1)


async def run_session(cfg: Config, data_dir: Path, status: Status, seen: set[str], baseline: bytes) -> None:
    client = FrameClient(cfg.frame_host, cfg.frame_port)
    status.update(state="connecting", connected=False)
    await client.connect()
    tasks: list[asyncio.Task] = []
    try:
        channels = await client.channels(await client.start())
        budget = message_budget(client.self_name)
        log.info("connected as %r (message budget %d bytes); channels on the companion: %s",
                 client.self_name, budget, sorted(channels))
        wx_idx = find_channel(channels, cfg.wx_channel) if cfg.bot_enabled else None
        alerts_idx = find_channel(channels, cfg.alerts_channel) if cfg.alerts_enabled else None
        problems = []
        if cfg.bot_enabled and wx_idx is None:
            problems.append(f"channel '{cfg.wx_channel}' not found (have: {', '.join(sorted(channels))}); bot disabled")
        if cfg.alerts_enabled and alerts_idx is None:
            problems.append(f"channel '{cfg.alerts_channel}' not found (have: {', '.join(sorted(channels))}); alerts disabled")
        status.update(state="running", connected=True, channels=sorted(channels), warnings=problems)
        for p in problems:
            log.warning(p)
        await send_test_if_requested(client, cfg, channels, data_dir, status, budget)

        async with httpx.AsyncClient(headers={"User-Agent": cfg.user_agent}, timeout=15.0,
                                     follow_redirects=True) as http:
            active: list = []
            tasks.append(asyncio.create_task(nws_probe_loop(http, status)))
            if wx_idx is None:
                # The bot's message loop doubles as the keepalive; without it we need our own.
                tasks.append(asyncio.create_task(keepalive_loop(client)))
            if wx_idx is not None:
                cache = {"at": 0.0, "items": []}

                async def get_contacts():
                    if time.monotonic() - cache["at"] > CONTACTS_TTL_S:
                        cache["items"], cache["at"] = await client.contacts(), time.monotonic()
                    return cache["items"]

                bot = Bot(WeatherClient(http), get_contacts, lambda: list(active), max_bytes=budget,
                          alerts_followup=cfg.wx_alerts_followup)
                tasks.append(asyncio.create_task(message_loop(client, bot, wx_idx, status)))
            if alerts_idx is not None:
                flt = alert_mod.AlertFilter(states=cfg.states, same_codes=set(cfg.counties), zones=set(cfg.zones),
                                            min_severity=cfg.min_severity, events=set(cfg.events))
                relay = alert_mod.AlertRelay(flt, cfg.alert_cooldown_seconds, cfg.alert_hourly_cap, seen=seen,
                                           urgent_hourly_cap=cfg.urgent_hourly_cap)
                tasks.append(asyncio.create_task(alert_loop(client, http, cfg, relay, alerts_idx, active, status,
                                                            data_dir / "state.json", budget)))
            watchers = [asyncio.create_task(client.closed.wait()),
                        asyncio.create_task(config_changed(data_dir / "config.json", baseline))]
            tasks += watchers
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t not in watchers and t.exception():
                    raise t.exception()
            if watchers[0] in done:
                raise ConnectionError(client.close_reason or "frame server closed the connection")
            log.info("config changed, restarting session")
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        status.update(connected=False)


async def supervise(data_dir: Path, stop: asyncio.Event) -> None:
    status = Status(data_dir)
    cfg_path = data_dir / "config.json"
    seen = load_seen(data_dir / "state.json")
    backoff = 1.0

    async def interruptible(coro) -> bool:
        """Run coro until stop is set. Returns True if stop won."""
        task, waiter = asyncio.ensure_future(coro), asyncio.ensure_future(stop.wait())
        await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
        stopped = waiter.done()
        for t in (task, waiter):
            t.cancel()
        results = await asyncio.gather(task, waiter, return_exceptions=True)
        if not stopped and isinstance(results[0], Exception):
            raise results[0]
        return stopped

    while not stop.is_set():
        baseline = read_raw(cfg_path)
        try:
            cfg = load_config(cfg_path)
            if not cfg.contact_email:
                raise ConfigError("contact_email is required")
        except ConfigError as e:
            log.error("config invalid: %s", e)
            status.update(state="needs_config", message=str(e), connected=False)
            if await interruptible(config_changed(cfg_path, baseline)):
                break
            continue
        started = time.monotonic()
        try:
            if await interruptible(run_session(cfg, data_dir, status, seen, baseline)):
                break
            backoff = 1.0
        except Exception as e:  # any session failure is retried with backoff
            log.warning("session ended: %s: %s", type(e).__name__, e)
            if time.monotonic() - started > MAX_BACKOFF_S:
                backoff = 1.0
            delay = min(backoff, MAX_BACKOFF_S) * random.uniform(0.8, 1.2)
            status.update(state="reconnecting", message=f"{type(e).__name__}: {e}"[:200], retry_in=round(delay))
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
    status.update(state="stopped", connected=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry locations
    data_dir = Path(os.environ["OPENHOP_PLUGIN_DATA"])
    data_dir.mkdir(parents=True, exist_ok=True)

    async def runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await supervise(data_dir, stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
