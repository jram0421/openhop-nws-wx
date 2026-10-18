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
from .bot import Bot
from .config import Config, ConfigError, load_config
from .frames import FrameClient
from .weather import WeatherClient

log = logging.getLogger("nws_wx")

MAX_BACKOFF_S = 60
CONTACTS_TTL_S = 60
SYNC_INTERVAL_S = 30  # also keeps the frame server's idle timeout from firing
CONFIG_CHECK_S = 5
SEEN_CAP = 500


class Status:
    """runtime.json snapshot, written atomically and kept small."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "runtime.json"
        self.data: dict = {"state": "starting"}

    def update(self, **fields) -> None:
        self.data.update(fields)
        self.data["updated"] = int(time.time())
        tmp = self.path.with_name(".runtime.json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
        tmp.replace(self.path)


def load_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text("utf-8")).get("seen", []))
    except (OSError, ValueError, AttributeError):
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    tmp = path.with_name(".state.json.tmp")
    tmp.write_text(json.dumps({"seen": sorted(seen)[-SEEN_CAP:]}), "utf-8")
    tmp.replace(path)


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
            reply = await bot.handle(msg.text)
            if reply:
                await client.send_channel(wx_idx, reply)
                status.data["replies"] = status.data.get("replies", 0) + 1


async def alert_loop(client: FrameClient, http: httpx.AsyncClient, cfg: Config, relay: alert_mod.AlertRelay,
                     alerts_idx: int, active: list, status: Status, seen_path: Path) -> None:
    step = max(cfg.alert_cooldown_seconds + 1, 5)
    while True:
        try:
            current = await alert_mod.fetch_active(http, cfg.states)
            active[:] = [a for a in current if relay.filter.matches(a)]
            relay.prune({a.id for a in current})
            status.update(last_poll=int(time.time()), alerts_active=len(active), last_error=None)
        except httpx.HTTPError as e:
            log.warning("NWS poll failed: %s", e)
            status.update(last_error=f"NWS poll failed: {type(e).__name__}")
            await asyncio.sleep(cfg.poll_seconds)
            continue
        deadline = time.monotonic() + cfg.poll_seconds
        while time.monotonic() < deadline:
            for a in relay.select(current):
                if await client.send_channel(alerts_idx, alert_mod.format_alert(a)):
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
        log.info("connected; channels on the companion: %s", sorted(channels))
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

        async with httpx.AsyncClient(headers={"User-Agent": cfg.user_agent}, timeout=15.0,
                                     follow_redirects=True) as http:
            active: list = []
            if wx_idx is None:
                # The bot's message loop doubles as the keepalive; without it we need our own.
                tasks.append(asyncio.create_task(keepalive_loop(client)))
            if wx_idx is not None:
                cache = {"at": 0.0, "items": []}

                async def get_contacts():
                    if time.monotonic() - cache["at"] > CONTACTS_TTL_S:
                        cache["items"], cache["at"] = await client.contacts(), time.monotonic()
                    return cache["items"]

                bot = Bot(WeatherClient(http), get_contacts, lambda: list(active))
                tasks.append(asyncio.create_task(message_loop(client, bot, wx_idx, status)))
            if alerts_idx is not None:
                flt = alert_mod.AlertFilter(states=cfg.states, same_codes=set(cfg.counties), zones=set(cfg.zones),
                                            min_severity=cfg.min_severity, events=set(cfg.events))
                relay = alert_mod.AlertRelay(flt, cfg.alert_cooldown_seconds, cfg.alert_hourly_cap, seen=seen)
                tasks.append(asyncio.create_task(alert_loop(client, http, cfg, relay, alerts_idx, active, status,
                                                            data_dir / "state.json")))
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
