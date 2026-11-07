"""The harness's seat in the Portal room: a LiveKit Portal observer plus the RPCs that steer the rig.

An observer sees everything the robot publishes (three cameras and both arms' state) and the active operator's
actions, and may hand control between operators, but it never sends actions. So nothing here can move an arm
directly: motion only happens when Dream-1 (or a human on the leader arms) is given control.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import protocol as P

log = logging.getLogger("harness.link")

STATUS_EVERY_S = 1.0  # how often to ask dream-1-tasks for its status
RECONNECT_S = 3.0
RPC_TIMEOUT_MS = 5000


def mint_token(identity: str, room: str, api_key: str, api_secret: str, ttl_hours: int = 12) -> str:
    """Same grants as upstream's `mint_token`: Portal publishes its role as a participant attribute."""
    from livekit import api
    from livekit.protocol.room import RoomConfiguration

    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                             can_update_own_metadata=True)
    return (api.AccessToken(api_key, api_secret).with_identity(identity).with_grants(grants)
            .with_room_config(RoomConfiguration(name=room, min_playout_delay=0, max_playout_delay=1))
            .with_ttl(datetime.timedelta(hours=ttl_hours)).to_jwt())


@dataclass
class Settings:
    portal_config: Path
    url: str
    room: str
    api_key: str
    api_secret: str
    identity: str = P.HARNESS_IDENTITY
    video_fps: float = 12.0
    max_width: int = 640
    jpeg_quality: int = 80
    token: str | None = None  # a ready-made token (its identity/room win); else one is minted from the key/secret


@dataclass
class Camera:
    jpeg: bytes | None = None
    width: int = 0
    height: int = 0
    timestamp_us: int = 0
    seen: float = 0.0  # monotonic time the newest frame arrived
    seq: int = 0
    arrivals: collections.deque = field(default_factory=lambda: collections.deque(maxlen=60))

    def fps(self, now: float) -> float:
        recent = [t for t in self.arrivals if now - t <= 2.0]
        return (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 2 and recent[-1] > recent[0] else 0.0


class RpcFailed(RuntimeError):
    pass


def encode_jpeg(data: bytes, width: int, height: int, max_width: int, quality: int) -> tuple[bytes, int, int]:
    rgb = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
    if width > max_width:
        h = max(2, round(height * max_width / width))
        rgb, width, height = cv2.resize(rgb, (max_width, h), interpolation=cv2.INTER_AREA), max_width, h
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return buf.tobytes(), width, height


class RigLink:
    """Keeps an Observer connected (retrying while the robot is not up) and exposes the rig's live picture."""

    def __init__(self, settings: Settings, observer_factory: Callable[[], Any] | None = None) -> None:
        self.s = settings
        self._observer_factory = observer_factory or self._default_observer
        self.obs: Any = None
        self.connected = False
        self.connect_error: str | None = None
        self.tracks: list[str] = list(P.CAMERAS)
        self.cameras: dict[str, Camera] = {name: Camera() for name in self.tracks}
        self.frame_event = asyncio.Event()  # set (then replaced) whenever any camera has a new frame
        self.state: dict[str, float] | None = None
        self.state_seen = 0.0
        self.action: dict[str, float] | None = None
        self.action_seen = 0.0
        self.action_sender: str | None = None
        self.policy_latency_ms: float | None = None  # now - the observation the newest action answered
        self._state_ts = self._action_ts = None
        self.dream: dict | None = None  # last dream.status reply
        self.dream_error: str | None = None
        self.dream_seen = 0.0
        self.events: collections.deque = collections.deque(maxlen=500)
        self._event_seq = 0
        self._listeners: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._status_inflight = False

    # -- lifecycle -----------------------------------------------------------

    def _default_observer(self) -> Any:
        from livekit.portal import Observer, ObserverConfig

        cfg = ObserverConfig.from_yaml_file(str(self.s.portal_config), self.s.room)
        self.tracks = list(cfg.video_tracks)
        for name in self.tracks:
            self.cameras.setdefault(name, Camera())
        return Observer(cfg)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="rig-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._drop_observer()

    async def _drop_observer(self) -> None:
        obs, self.obs, self.connected = self.obs, None, False
        if obs is not None:
            try:
                await obs.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                obs.close()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self._connect()
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep trying; the UI shows why
                msg = f"{type(exc).__name__}: {exc}"
                if msg != self.connect_error:
                    self.event("link", f"not connected ({msg}); retrying every {RECONNECT_S:g}s", level="warn")
                self.connect_error = msg
                await self._drop_observer()
                await asyncio.sleep(RECONNECT_S)

    async def _connect(self) -> None:
        obs = self._observer_factory()
        for name, cb in (("on_active_operator_changed", self._on_active_changed),
                         ("on_operator_joined", lambda who: self.event("room", f"operator joined: {who}")),
                         ("on_operator_left", lambda who: self.event("room", f"operator left: {who}"))):
            if hasattr(obs, name):
                getattr(obs, name)(cb)
        self.obs = obs
        token = self.s.token or mint_token(self.s.identity, self.s.room, self.s.api_key, self.s.api_secret)
        # Returns once synced to the robot's clock; raises if no robot answers within ~10 s.
        await obs.connect(self.s.url, token)
        self.connected, self.connect_error = True, None
        self.event("link", f"connected to room {self.s.room!r} as {self.s.identity!r}; "
                           f"robot={self._safe(obs.robot_identity)} cameras={self.tracks}")

    def _on_active_changed(self, who: str | None) -> None:
        self.event("control", f"active operator: {who or 'nobody (arms hold)'}")

    # -- polling -------------------------------------------------------------

    async def _poll(self) -> None:
        interval = 1.0 / self.s.video_fps
        last_status = 0.0
        while True:
            started = time.monotonic()
            await self._grab_frames()
            obs = self.obs
            if (st := obs.get_state()) is not None and st.timestamp_us != self._state_ts:
                self._state_ts, self.state_seen = st.timestamp_us, started
                self.state = {k: float(v) for k, v in st.values.items()}
            if (act := obs.get_action()) is not None and act.timestamp_us != self._action_ts:
                self._action_ts, self.action_seen = act.timestamp_us, started
                self.action = {k: float(v) for k, v in act.values.items()}
                self.action_sender = getattr(act, "sender", None)
                reply_to = getattr(act, "in_reply_to_ts_us", None)
                if reply_to:
                    self.policy_latency_ms = round((self._safe(obs.now_us, 0) - reply_to) / 1000, 1)
            if started - last_status >= STATUS_EVERY_S and not self._status_inflight:
                last_status = started
                asyncio.create_task(self._refresh_status())
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    async def _grab_frames(self) -> None:
        todo = []
        for name in self.tracks:
            frame = self.obs.get_video_frame(name)
            if frame is not None and frame.timestamp_us != self.cameras[name].timestamp_us:
                todo.append((name, frame))
        if not todo:
            return

        def encode_all():
            return [(name, f.timestamp_us, *encode_jpeg(f.data, f.width, f.height, self.s.max_width,
                                                        self.s.jpeg_quality)) for name, f in todo]

        now = time.monotonic()
        for name, ts, jpeg, w, h in await asyncio.to_thread(encode_all):
            cam = self.cameras[name]
            cam.jpeg, cam.width, cam.height, cam.timestamp_us, cam.seen = jpeg, w, h, ts, now
            cam.seq += 1
            cam.arrivals.append(now)
        self.frame_event.set()
        self.frame_event = asyncio.Event()

    async def _refresh_status(self) -> None:
        self._status_inflight = True
        try:
            if P.DREAM_TASKS_IDENTITY not in self._safe(self.obs.operators, []):
                self.dream_error = "dream-1-tasks is not in the room"
                return
            reply = await self.obs.perform_rpc(P.DREAM_STATUS_RPC, "{}", destination=P.DREAM_TASKS_IDENTITY,
                                               response_timeout_ms=2000)
            self.dream, self.dream_error, self.dream_seen = json.loads(reply), None, time.monotonic()
        except Exception as exc:  # noqa: BLE001
            self.dream_error = str(exc)
        finally:
            self._status_inflight = False

    # -- commands ------------------------------------------------------------

    async def call(self, destination: str, method: str, payload: str = "", quiet: bool = False) -> str:
        if not self.connected or self.obs is None:
            raise RpcFailed("not connected to the room yet")
        try:
            reply = await self.obs.perform_rpc(method, payload, destination=destination,
                                               response_timeout_ms=RPC_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            text = getattr(exc, "message", None) or str(exc)
            self.event("rpc", f"{destination}.{method} failed: {text}", level="error")
            raise RpcFailed(text) from exc
        if not quiet:
            self.event("rpc", f"{destination}.{method}({payload or ''}) → {reply[:160]}")
        return reply

    async def dream_rpc(self, method: str, **payload: Any) -> dict:
        reply = json.loads(await self.call(P.DREAM_TASKS_IDENTITY, method, json.dumps(payload)))
        self.dream, self.dream_error, self.dream_seen = reply, None, time.monotonic()
        return reply

    async def set_task(self, task: str) -> dict:
        task = " ".join(str(task).split())
        if not 0 < len(task) <= P.MAX_TASK_CHARS:
            raise ValueError(f"a task is one instruction of 1-{P.MAX_TASK_CHARS} characters")
        return await self.dream_rpc(P.DREAM_SET_TASK_RPC, task=task)

    async def pause(self) -> dict:
        return await self.dream_rpc(P.DREAM_PAUSE_RPC)

    async def resume(self) -> dict:
        return await self.dream_rpc(P.DREAM_RESUME_RPC)

    async def to_teleop(self) -> dict:
        return await self.dream_rpc(P.DREAM_TO_TELEOP_RPC)

    async def to_hand_teleop(self) -> dict:
        return await self.dream_rpc(P.DREAM_TO_HAND_TELEOP_RPC)

    async def stop_hand_teleop(self) -> dict:
        return await self.dream_rpc(P.DREAM_STOP_HAND_TELEOP_RPC)

    async def home(self) -> str:
        return await self.call(P.ROBOT_IDENTITY, P.HOME_RPC, "")

    async def park(self) -> str:
        """Park only: walk to rest, verify, switch off. Powering up stays at the rig (robot terminal `p`)."""
        return await self.call(P.ROBOT_IDENTITY, P.PARK_RPC, P.PARK_ONLY)

    async def release(self) -> str:
        """Clear the control gate: nobody drives, the robot holds the arms where they are. Works whoever drives."""
        if not self.connected or self.obs is None:
            raise RpcFailed("not connected to the room yet")
        await self.obs.set_active_operator(None)
        self.event("control", "harness released control: nobody drives, the arms hold")
        return "released"

    # -- read side -----------------------------------------------------------

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default

    def event(self, kind: str, text: str, level: str = "info") -> None:
        self._event_seq += 1
        item = {"id": self._event_seq, "t": time.time(), "kind": kind, "level": level, "text": text}
        self.events.append(item)
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(
            "[%s] %s", kind, text)
        for q in list(self._listeners):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def listen(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._listeners.add(q)
        return q

    def unlisten(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    def snapshot(self) -> dict:
        now = time.monotonic()
        obs = self.obs if self.connected else None
        age = lambda t: round(now - t, 2) if t else None  # noqa: E731
        return {
            "connected": self.connected,
            "connect_error": self.connect_error,
            "room": self.s.room,
            "identity": self.s.identity,
            "robot": self._safe(obs.robot_identity) if obs else None,
            "active_operator": self._safe(obs.active_operator) if obs else None,
            "operators": self._safe(obs.operators, []) if obs else [],
            "observers": self._safe(obs.observers, []) if obs else [],
            "dream": self.dream,
            "dream_error": self.dream_error,
            "dream_age_s": age(self.dream_seen),
            "state": self.state,
            "state_age_s": age(self.state_seen),
            "action": self.action,
            "action_age_s": age(self.action_seen),
            "action_sender": self.action_sender,
            "policy_latency_ms": self.policy_latency_ms,
            "cameras": {name: {"width": c.width, "height": c.height, "age_s": age(c.seen), "fps": round(c.fps(now), 1),
                               "seq": c.seq} for name, c in self.cameras.items() if name in self.tracks},
        }
