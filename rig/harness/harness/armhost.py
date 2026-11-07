"""arm-host mode: the harness in a room run by the Maker Arm kit's web app (`python web_app.py --livekit`).

That app (rig/makerarm-kit on rig/makerarm-web-kit) joins the room as `arm-host`, publishes every arm's state as
JSON on topic `arm/state` (5 Hz), takes commands on `arm/cmd` and answers each on `arm/reply`. The arm laptop's
browser shares its camera as participant `arm-camera`. Its rules, enforced on the arm laptop, are:

  * one remote person drives at a time: `take` control, keep it alive with `ping` heartbeats (< 5 s apart), `release`;
  * anyone may `hold` or `park` (stop actions); `jog` needs control and is at most `max_step` degrees;
  * Power on and Clear fault are only possible on the arm laptop itself. The harness cannot switch motors on.

There is no Dream-1 in that setup, so prompts are not available in this mode.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any

from .link import Camera, LinkBase, RpcFailed, Settings, encode_jpeg, mint_token

ARM_HOST = "arm-host"
CAMERA_IDENTITY = "arm-camera"
CMD_TOPIC, STATE_TOPIC, REPLY_TOPIC = "arm/cmd", "arm/state", "arm/reply"
HEARTBEAT_S = 2.0  # the host drops a controller after 5 s without one
REPLY_TIMEOUT_S = 6.0
RECONNECT_S = 3.0


def camera_name(identity: str, track_name: str) -> str:
    """A URL-safe camera id, e.g. arm-camera_camera."""
    return re.sub(r"[^\w-]+", "_", f"{identity}_{track_name or 'video'}").strip("_")


class ArmHostLink(LinkBase):
    mode = "arm-host"

    def __init__(self, settings: Settings, room_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(settings)
        self._room_factory = room_factory
        self.room: Any = None
        self.host: dict | None = None  # newest arm/state payload
        self.host_seen = 0.0
        self._controller: str | None = None
        self._latest: dict[str, tuple[Any, float]] = {}  # camera -> (newest raw frame, arrival)
        self._encoded_at: dict[str, float] = {}
        self._streams: dict[str, asyncio.Task] = {}
        self._replies: asyncio.Queue = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._took_at: float | None = None  # when the host last confirmed our take (its state lags up to 200 ms)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="arm-host-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.i_control():
            try:  # hand control back so the next person can take it at once
                await self._send({"cmd": "release"})
            except Exception:  # noqa: BLE001
                pass
        await self._drop_room()

    async def _drop_room(self) -> None:
        for task in self._streams.values():
            task.cancel()
        self._streams.clear()
        room, self.room, self.connected = self.room, None, False
        if room is not None:
            try:
                await room.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _make_room(self) -> Any:
        if self._room_factory:
            return self._room_factory()
        from livekit import rtc

        return rtc.Room()

    async def _run(self) -> None:
        while True:
            try:
                await self._connect()
                await self._loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                if msg != self.connect_error:
                    self.event("link", f"not connected ({msg}); retrying every {RECONNECT_S:g}s", level="warn")
                self.connect_error = msg
            await self._drop_room()
            await asyncio.sleep(RECONNECT_S)

    async def _connect(self) -> None:
        room = self._make_room()
        room.on("data_received", self._on_data)
        room.on("track_subscribed", self._on_track)
        room.on("track_unsubscribed", self._on_track_gone)
        room.on("participant_connected", lambda p: self.event("room", f"{p.identity} joined"))
        room.on("participant_disconnected", self._on_left)
        self.room = room
        token = self.s.token or mint_token(self.s.identity, self.s.room, self.s.api_key, self.s.api_secret)
        await room.connect(self.s.url, token)
        self.connected, self.connect_error = True, None
        here = sorted(room.remote_participants)
        self.event("link", f"connected to room {self.s.room!r} as {self.s.identity!r}; in the room: {here or 'nobody'}")
        if ARM_HOST not in here:
            self.event("link", "arm-host is not in the room yet: start `python web_app.py --livekit` on the arm laptop",
                       level="warn")

    async def _loop(self) -> None:
        interval = 1.0 / self.s.video_fps
        last_ping = 0.0
        while self.room is not None and self.room.isconnected():
            started = time.monotonic()
            await self._encode()
            if self.i_control() and started - last_ping >= HEARTBEAT_S:
                last_ping = started
                try:
                    await self._send({"cmd": "ping"})
                except Exception as exc:  # noqa: BLE001
                    self.event("control", f"heartbeat failed: {exc}", level="warn")
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
        raise ConnectionError("disconnected from the room")

    # -- room events ---------------------------------------------------------

    def _on_data(self, packet: Any) -> None:
        who = packet.participant.identity if packet.participant else None
        if who != ARM_HOST or packet.topic not in (STATE_TOPIC, REPLY_TOPIC):
            return
        try:
            msg = json.loads(bytes(packet.data).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        if packet.topic == REPLY_TOPIC:
            self._replies.put_nowait(str(msg.get("msg", "")))
            return
        self.host, self.host_seen = msg, time.monotonic()
        controller = msg.get("controller")
        if controller != self._controller:
            self._controller = controller
            self.event("control", f"remote control: {controller or 'nobody'}"
                                  + (" (this harness)" if controller == self.s.identity else ""))

    def _on_left(self, participant: Any) -> None:
        self.event("room", f"{participant.identity} left", level="warn" if participant.identity == ARM_HOST else "info")
        if participant.identity == ARM_HOST:
            self.host = None

    def _on_track(self, track: Any, publication: Any, participant: Any) -> None:
        from livekit import rtc

        if track.kind != rtc.TrackKind.KIND_VIDEO:
            return
        name = camera_name(participant.identity, getattr(publication, "name", "") or track.sid)
        self.cameras.setdefault(name, Camera())
        if name not in self.tracks:
            self.tracks.append(name)
        if (old := self._streams.pop(name, None)) is not None:
            old.cancel()
        self._streams[name] = asyncio.create_task(self._read(name, track))
        self.event("video", f"camera {name} from {participant.identity}")

    def _on_track_gone(self, track: Any, publication: Any, participant: Any) -> None:
        name = camera_name(participant.identity, getattr(publication, "name", "") or track.sid)
        if (task := self._streams.pop(name, None)) is not None:
            task.cancel()
            self.event("video", f"camera {name} stopped", level="warn")

    async def _read(self, name: str, track: Any) -> None:
        from livekit import rtc

        stream = rtc.VideoStream(track, format=rtc.VideoBufferType.RGB24)
        try:
            async for ev in stream:
                self._latest[name] = (ev.frame, time.monotonic())
        finally:
            await stream.aclose()

    async def _encode(self) -> None:
        todo = [(name, frame, at) for name, (frame, at) in self._latest.items() if self._encoded_at.get(name) != at]
        if not todo:
            return

        def work():
            return [(name, at, *encode_jpeg(bytes(f.data), f.width, f.height, self.s.max_width, self.s.jpeg_quality))
                    for name, f, at in todo]

        now = time.monotonic()
        for name, at, jpeg, w, h in await asyncio.to_thread(work):
            cam = self.cameras.setdefault(name, Camera())
            cam.jpeg, cam.width, cam.height, cam.seen = jpeg, w, h, now
            cam.seq += 1
            cam.arrivals.append(now)
            self._encoded_at[name] = at
        self.new_frames()

    # -- commands ------------------------------------------------------------

    def i_control(self) -> bool:
        if self.host and self.host.get("controller") == self.s.identity:
            return True
        return self._took_at is not None and time.monotonic() - self._took_at < 1.5

    def host_here(self) -> bool:
        return self.connected and self.room is not None and ARM_HOST in self.room.remote_participants

    async def _send(self, msg: dict) -> None:
        await self.room.local_participant.publish_data(json.dumps(msg).encode(), reliable=True,
                                                       destination_identities=[ARM_HOST], topic=CMD_TOPIC)

    async def command(self, msg: dict) -> str:
        """Send one command to the arm host and return its reply text."""
        if not self.host_here():
            raise RpcFailed("arm-host is not in the room: start `python web_app.py --livekit` on the arm laptop")
        async with self._cmd_lock:  # replies are not tagged, so one command in flight at a time
            while not self._replies.empty():
                self._replies.get_nowait()
            await self._send(msg)
            try:
                reply = await asyncio.wait_for(self._replies.get(), REPLY_TIMEOUT_S)
            except TimeoutError as exc:
                self.event("cmd", f"{msg} → no reply", level="error")
                raise RpcFailed("the arm host did not answer") from exc
        self.event("cmd", f"{json.dumps(msg)} → {reply}")
        return reply

    def arm_ids(self) -> list[str]:
        return list((self.host or {}).get("arms", {}))

    def _check_arm(self, arm: str) -> None:
        if arm not in self.arm_ids():
            raise ValueError(f"unknown arm {arm!r}; one of {self.arm_ids()}")

    async def take(self) -> str:
        reply = await self.command({"cmd": "take"})
        if reply.startswith(f"{self.s.identity} has control"):
            self._took_at = time.monotonic()
        return reply

    async def release_control(self) -> str:
        self._took_at = None
        return await self.command({"cmd": "release"})

    async def hold(self, arm: str | None = None) -> str:
        """Hold one arm, or every arm that is on. Anyone may, with or without control."""
        if arm:
            self._check_arm(arm)
            return await self.command({"cmd": "hold", "arm": arm})
        arms = [a for a, s in (self.host or {}).get("arms", {}).items() if s.get("on")]
        if not arms:
            return "no arm is powered: nothing to hold"
        return "; ".join([f"{a}: {await self.command({'cmd': 'hold', 'arm': a})}" for a in arms])

    async def park(self, arm: str) -> str:
        self._check_arm(arm)
        return await self.command({"cmd": "park", "arm": arm})

    async def jog(self, arm: str, joint: int, deg: float) -> str:
        self._check_arm(arm)
        info = (self.host or {}).get("info", {})
        joints = len(info.get("joints") or []) or 7
        max_step = float(info.get("max_step") or 10.0)
        joint, deg = int(joint), float(deg)
        if not 1 <= joint <= joints:
            raise ValueError(f"joint must be 1..{joints}")
        if not math.isfinite(deg) or abs(deg) > max_step or deg == 0:
            raise ValueError(f"step must be within ±{max_step:g}° and not zero")
        # Whether we hold control is the arm host's call: it answers "press Take control first" otherwise.
        return await self.command({"cmd": "jog", "arm": arm, "joint": joint, "deg": deg})

    # -- read side -----------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.monotonic()
        host = self.host or {}
        return {
            "mode": self.mode,
            "connected": self.connected,
            "connect_error": self.connect_error,
            "room": self.s.room,
            "identity": self.s.identity,
            "participants": sorted(self.room.remote_participants) if self.connected and self.room else [],
            "host_present": self.host_here(),
            "host_age_s": round(now - self.host_seen, 2) if self.host_seen else None,
            "arms": host.get("arms"),
            "info": host.get("info"),
            "controller": host.get("controller"),
            "viewers": host.get("viewers"),
            "i_control": self.i_control(),
            "cameras": self.camera_info(now),
        }
