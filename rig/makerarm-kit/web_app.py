"""Web app: see the camera and move one or more Maker Arms, locally or with the whole team.

    python web_app.py --sim                         # two simulated arms (left, right)
    python web_app.py                               # real arms from ARM_PORTS (or ARM_PORT) in .env
    python web_app.py --arms left=COM9,right=COM10  # same, from the command line
    python web_app.py --livekit                     # ...and let teammates join from anywhere (remote.py)

Local page: http://127.0.0.1:8765 . It only listens on 127.0.0.1, so powering the motors on is
only possible on this laptop, next to the arms.

With --livekit this process also joins the LiveKit room as "arm-host":
  * it sends every arm's state to the room (topic arm/state, 5 Hz);
  * teammates running remote.py see the camera this page shares and can drive the arms;
  * ONE remote person controls at a time ("take control"); anyone can Hold or Park (stop actions);
  * Power on and Clear fault stay local-only; if the controller leaves or goes idle, arms hold.

Ctrl-C here parks every powered arm (returns to its start pose) and releases torque.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import pathlib
import sys
import time
import webbrowser
from types import SimpleNamespace
from dataclasses import dataclass, field

from aiohttp import web

from arm_server import ArmBridge
from armkit import (WEB_SPEED_DEG_S, JOINT_HELP, MAX_STEP_DEG, _config, is_on, load_env, open_arm,
                    required_env, safe_range_deg, safe_shutdown, usb_serial_ports)
from candle_backend import candle_index, is_candle_port, list_candle_devices
from sim_world import DEMO, World

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"
OFF_MSG = "Motors off. Press Power on when the area is clear."
LOCAL = "local"                    # origin of commands from this laptop's page
REMOTE_ALLOWED = {"jog", "move", "goto", "hold", "park", "reset_world"}
ANYONE_ALLOWED = {"hold", "park"}  # stop actions: any remote viewer may use them
CONTROL_IDLE_S = 60.0              # a controller that sends no commands loses control after this long
HEARTBEAT_S = 5.0                  # ...and one whose page stops sending heartbeats (tab closed, Wi-Fi gone)
INVITE_HOURS = 4                   # default lifetime of an invite link (max 12)
# Public copy of static/remote.html that invite links open (served straight from the GitHub repo).
DEFAULT_REMOTE_PAGE = ("https://htmlpreview.github.io/?https://github.com/hiromaruyama/TacoBot/blob/"
                       "rig/makerarm-web-kit/rig/makerarm-kit/static/remote.html")
STATE_HZ = 5
WATCH_S = 3.0                      # how often the live watcher looks at the USB adapters
RETRY_S = 10.0                     # adapter plugged in but motors silent: retry connecting this often


@dataclass
class ArmSlot:
    id: str
    port: str | None
    bridge: ArmBridge | None = None
    error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connecting: bool = False
    world: World = field(default_factory=World)  # virtual tortilla + plate for pick & place practice
    present: bool | None = None     # adapter seen on USB by the live watcher (None = unknown / sim)
    next_try: float = 0.0

    def info(self) -> dict:
        return {"id": self.id, "port": self.port or "sim", "ok": self.bridge is not None, "error": self.error,
                "connecting": self.connecting, "present": self.present}


def open_connected(port: str | None, sim: bool, speed: float):
    """Open and connect one arm (read-only, torque stays off). Blocking: run it in a thread."""
    arm = open_arm(port=port, sim=sim, speed_deg_s=speed)
    arm.connect()  # closes the adapter again by itself if no motor answers
    return arm


def adapters_present() -> list[str]:
    """USB-CAN adapters plugged in right now: COMx or /dev/... (SLCAN) and candle:N (candleLight)."""
    return [p.device for p in usb_serial_ports()] + list_candle_devices()


def adapter_present(port: str | None, present: list[str]) -> bool | None:
    if not port:
        return None  # auto-detect: unknown, just try
    if is_candle_port(port):
        return f"candle:{candle_index(port)}" in present
    if port.startswith("can") or port.startswith("socketcan:"):
        # Linux SocketCAN (can0, socketcan:can1): a network interface, not a USB serial port
        channel = port.split(":", 1)[1] if ":" in port else port
        if sys.platform.startswith("linux"):
            return os.path.exists(f"/sys/class/net/{channel}")
        return None  # can't tell here: just try
    # macOS lists each adapter once, as /dev/cu.*; accept its /dev/tty.* twin in .env too
    norm = lambda x: x.lower().replace("/dev/tty.", "/dev/cu.")  # noqa: E731
    return norm(port) in (norm(x) for x in present)


def parse_arms(spec: str) -> list[tuple[str, str]]:
    """'left=COM9,right=COM10' -> [('left','COM9'), ('right','COM10')]."""
    arms = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, sep, port = part.partition("=")
        if not sep or not name.strip() or not port.strip():
            raise SystemExit(f"bad arm spec {part!r}; use name=PORT, e.g. left=COM9,right=COM10")
        arms.append((name.strip(), port.strip()))
    if len({n for n, _ in arms}) != len(arms):
        raise SystemExit("arm names must be unique")
    return arms


def open_slots(arms: list[tuple[str, str | None]], sim: bool, speed: float) -> list[ArmSlot]:
    slots = []
    for name, port in arms:
        slot = ArmSlot(name, port)
        try:
            arm = open_connected(port, sim, speed)  # read-only; torque stays off until "Power on"
            slot.bridge = ArmBridge(arm)
            slot.bridge.last_msg = OFF_MSG
            print(f"[web] arm '{name}' ready on {port or 'simulator'}")
        except BaseException as exc:  # SystemExit from port detection too: keep the other arms
            if isinstance(exc, KeyboardInterrupt):
                raise
            slot.error = str(exc) or type(exc).__name__
            print(f"[web] arm '{name}' not connected ({port}): {slot.error}")
            slot.next_try = time.monotonic() + RETRY_S
        slots.append(slot)
    return slots


class Control:
    """Who may drive the arms remotely. The local page can always act and can take control back."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.last_used = 0.0
        self.last_seen = 0.0

    def expired(self) -> bool:
        now = time.monotonic()
        return self.holder is not None and (now - self.last_used > CONTROL_IDLE_S or now - self.last_seen > HEARTBEAT_S)

    def take(self, who: str) -> str:
        if self.holder in (None, who) or self.expired():
            self.holder, self.last_used = who, time.monotonic()
            self.last_seen = self.last_used
            return f"{who} has control"
        return f"{self.holder} has control right now; ask them to release it"

    def release(self, who: str | None = None) -> str:
        if who is None or who == self.holder:
            prev, self.holder = self.holder, None
            return f"control released{f' by {prev}' if prev else ''}"
        return "you don't have control"

    def touch(self, who: str) -> None:
        if who == self.holder:
            self.last_used = self.last_seen = time.monotonic()

    def heartbeat(self, who: str) -> None:
        if who == self.holder:
            self.last_seen = time.monotonic()


class Hub:
    """All arms + the control lock. Both the local page and LiveKit go through run()."""

    def __init__(self, slots: list[ArmSlot], sim: bool, speed: float = WEB_SPEED_DEG_S) -> None:
        self.slots = slots
        self.speed = speed
        self.by_id = {s.id: s for s in slots}
        self.sim = sim
        self.control = Control()
        self.adapters: list[str] = []
        self.viewers: set[str] = set()
        self.livekit_on = False
        # joint names/limits come from the arm profile, so the page works before any arm is connected
        ref = SimpleNamespace(config=_config(speed, rate_hz=None))
        self.joints: list[dict] = []
        if True:
            for i, j in enumerate(ref.config.joints, start=1):
                lo, hi = safe_range_deg(ref, i)
                self.joints.append({"n": i, "name": (j.name or f"J{i}").replace("_", " "),
                                    "help": JOINT_HELP.get(i, "").split("(")[-1].rstrip(")"),
                                    "lo": round(lo, 1), "hi": round(hi, 1)})

    def info(self) -> dict:
        return {"arms": [s.info() for s in self.slots], "adapters": self.adapters, "joints": self.joints, "max_step": MAX_STEP_DEG, "demo": DEMO,
                "speed": self.speed,
                "sim": self.sim, "livekit": self.livekit_on}

    def team(self) -> dict:
        return {"controller": self.control.holder, "viewers": sorted(self.viewers)}

    async def connect(self, slot: ArmSlot, auto: bool = False) -> str:
        """(Re)connect an arm that isn't connected. Motors stay off."""
        if slot.bridge is not None:
            return f"the {slot.id} arm is already connected"
        if slot.connecting:
            return f"already connecting the {slot.id} arm..."
        slot.connecting = True
        try:
            arm = await asyncio.to_thread(open_connected, slot.port, self.sim, self.speed)
        except (Exception, SystemExit) as exc:  # SystemExit: port auto-detection explains itself that way
            err = str(exc) or type(exc).__name__
            if not auto or err != slot.error:
                print(f"[web] arm '{slot.id}' not connected ({slot.port}): {err}")
            slot.error = err
            slot.next_try = time.monotonic() + RETRY_S
            return f"could not connect the {slot.id} arm: {err}"
        finally:
            slot.connecting = False
        slot.bridge = ArmBridge(arm)
        slot.bridge.last_msg = OFF_MSG
        slot.error = None
        print(f"[web] arm '{slot.id}' connected on {slot.port or 'simulator'}")
        return f"{slot.id} arm connected on {slot.port or 'simulator'}. Motors are off."

    async def drop(self, slot: ArmSlot, why: str) -> None:
        """Forget an arm whose adapter was unplugged (only while its motors are off)."""
        bridge, slot.bridge = slot.bridge, None
        slot.error = why
        print(f"[web] arm '{slot.id}': {why}")
        try:
            await bridge.run(bridge.arm.disconnect)
        except Exception:  # noqa: BLE001
            pass
        bridge.pool.shutdown(wait=False)

    async def watch(self) -> None:
        """Live connection check: notices adapters plugged in or out and (re)connects the arms."""
        while True:
            try:
                self.adapters = [] if self.sim else await asyncio.to_thread(adapters_present)
            except Exception:  # noqa: BLE001
                self.adapters = []
            for slot in self.slots:
                present = None if self.sim else adapter_present(slot.port, self.adapters)
                was, slot.present = slot.present, present
                if slot.bridge is not None:
                    if present is False and not is_on(slot.bridge.arm):
                        async with slot.lock:
                            await self.drop(slot, f"adapter {slot.port} unplugged; plug it back in and it reconnects")
                    continue
                if present is False:
                    seen = ", ".join(self.adapters) or "none"
                    slot.error = (f"adapter {slot.port} is not plugged in (or the USB switch points at another "
                                  f"laptop). Adapters seen now: {seen}. On macOS the number in /dev/cu.usbmodem… "
                                  "changes with the USB socket: update ARM_PORTS in .env if so.")
                    continue
                if slot.connecting:
                    continue
                if present and was is False:
                    print(f"[web] adapter {slot.port} plugged in; connecting the {slot.id} arm...")
                    slot.next_try = 0
                if time.monotonic() >= slot.next_try:
                    asyncio.create_task(self.connect(slot, auto=True))
            await asyncio.sleep(WATCH_S)

    async def state(self, slot: ArmSlot) -> dict:
        if slot.bridge is None:
            return {"offline": True, "connected": False, "connecting": slot.connecting,
                    "present": slot.present, "error": slot.error}
        try:
            await slot.bridge.run(slot.bridge.refresh_if_off)
        except Exception as exc:  # adapter/bus trouble: report it, don't spam tracebacks
            reason = (f"no answer from the arm on {slot.port} ({exc}). Check that this arm's "
                      "power supply is on and its e-stop released; the app keeps retrying.")
            if slot.error != reason:
                print(f"[web] arm '{slot.id}': {reason}")
            slot.error = reason
            return {"offline": True, "error": reason}
        if slot.error:
            print(f"[web] arm '{slot.id}' is answering again")
            slot.error = None
        snap = await slot.bridge.run(slot.bridge.snapshot)
        snap["world"] = slot.world.update(snap["joints_deg"])
        return snap

    async def run(self, msg: dict, origin: str) -> str:
        """Execute one command from the local page (origin=LOCAL) or a remote identity."""
        cmd = msg.get("cmd")
        if cmd == "take":
            return self.control.take(origin) if origin != LOCAL else "the local page always has control"
        if cmd == "release":
            return self.control.release(None if origin == LOCAL else origin)
        if origin != LOCAL:
            if cmd not in REMOTE_ALLOWED:
                return f"'{cmd}' is only allowed on the arm laptop"
            if cmd not in ANYONE_ALLOWED:
                if self.control.expired():
                    print(f"[web] {self.control.release()} (idle)")
                if self.control.holder != origin:
                    who = self.control.holder
                    return f"{who} has control" if who else "press Take control first"
            self.control.touch(origin)
        slot = self.by_id.get(str(msg.get("arm") or ""))
        if slot is None:
            return f"unknown arm {msg.get('arm')!r}"
        if cmd == "reset_world":
            return slot.world.reset()
        if cmd == "connect":
            if origin != LOCAL:
                return "connecting arms is done on the arm laptop"
            return await self.connect(slot)
        if slot.bridge is None:
            return f"arm '{slot.id}' is not connected: {slot.error}"
        bridge = slot.bridge
        async with slot.lock:  # one command at a time per arm
            try:
                if cmd == "power":
                    result = await bridge.run(bridge.power_up)
                elif cmd == "park":
                    result = await bridge.run(bridge.park)
                elif cmd == "hold":
                    result = await bridge.run(bridge.hold)
                elif cmd == "clear":
                    result = await bridge.run(bridge.clear_fault)
                elif cmd == "jog":
                    joint, deg = int(msg.get("joint")), float(msg.get("deg"))
                    if not math.isfinite(deg):
                        raise ValueError("deg")
                    result = await bridge.run(bridge.jog, joint, deg)
                elif cmd == "goto":  # step playback: absolute setpoints {"v": {"1": deg, ...}}
                    targets = {int(k): float(v) for k, v in dict(msg.get("v") or {}).items()}
                    if not 0 < len(targets) <= 7 or not all(math.isfinite(v) for v in targets.values()):
                        raise ValueError("v")
                    result = await bridge.run(bridge.goto, targets)
                elif cmd == "move":  # joysticks / held keys: {"v": {"1": deg, "2": deg, ...}}
                    deltas = {int(k): float(v) for k, v in dict(msg.get("v") or {}).items()}
                    if not 0 < len(deltas) <= 7 or not all(math.isfinite(v) for v in deltas.values()):
                        raise ValueError("v")
                    result = await bridge.run(bridge.move, deltas)
                else:
                    return f"unknown cmd {cmd!r}"
            except (TypeError, ValueError):
                return "bad joint/deg"
            except Exception as exc:
                result = f"error: {exc}"
        who = "" if origin == LOCAL else f" ({origin})"
        bridge.last_msg = result + who
        if cmd not in ("move", "goto") and (cmd != "jog" or origin != LOCAL):  # moves arrive 10x/s: don't log them
            print(f"[web] {slot.id} {cmd}{who}: {result}")
        return result

    async def hold_all(self, why: str) -> None:
        for slot in self.slots:
            if slot.bridge is not None:
                async with slot.lock:
                    msg = await slot.bridge.run(slot.bridge.hold)
                if msg == "holding":
                    print(f"[web] {slot.id}: holding ({why})")


# ── LiveKit bridge: the arms in a room, for the whole team ──
async def livekit_task(hub: Hub) -> None:
    from livekit import rtc

    from lk import CMD_TOPIC, HOST_IDENTITY, REPLY_TOPIC, STATE_TOPIC, mint_token

    url, key, secret = required_env("LIVEKIT_URL"), required_env("LIVEKIT_API_KEY"), required_env("LIVEKIT_API_SECRET")
    room_name = required_env("LIVEKIT_ROOM")
    room = rtc.Room()

    async def reply(to: str, text: str) -> None:
        try:
            await room.local_participant.publish_data(json.dumps({"msg": text}).encode(), topic=REPLY_TOPIC,
                                                      reliable=True, destination_identities=[to])
        except Exception as exc:  # noqa: BLE001
            print(f"[lk] reply to {to} failed: {exc}")

    async def handle(who: str, msg: dict) -> None:
        if msg.get("cmd") == "ping":  # controller heartbeat: no reply, keeps control alive
            hub.control.heartbeat(who)
            return
        await reply(who, await hub.run(msg, origin=who))

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != CMD_TOPIC or packet.participant is None:
            return
        try:
            msg = json.loads(packet.data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(msg, dict):
            asyncio.ensure_future(handle(packet.participant.identity, msg))

    @room.on("participant_connected")
    def on_join(p: rtc.RemoteParticipant) -> None:
        hub.viewers.add(p.identity)
        print(f"[lk] {p.identity} joined")

    @room.on("participant_disconnected")
    def on_leave(p: rtc.RemoteParticipant) -> None:
        hub.viewers.discard(p.identity)
        print(f"[lk] {p.identity} left")
        if p.identity == hub.control.holder:
            hub.control.release()
            asyncio.ensure_future(hub.hold_all(f"controller {p.identity} left"))

    token = mint_token(key, secret, HOST_IDENTITY, room_name, publish_media=False, name="Arm host")
    while True:  # (re)connect loop
        try:
            print(f"[lk] connecting to {url} room '{room_name}' as '{HOST_IDENTITY}' ...")
            await room.connect(url, token)
            hub.viewers = {p.identity for p in room.remote_participants.values()}
            print(f"[lk] in the room; teammates join with: python remote.py   (room '{room_name}')")
            while room.isconnected():
                if hub.control.expired():
                    who = hub.control.holder
                    print(f"[lk] {hub.control.release()} ({who} went silent or idle)")
                    await hub.hold_all(f"controller {who} went silent or idle")
                arms = {s.id: await hub.state(s) for s in hub.slots}
                payload = {"arms": arms, "info": hub.info(), **hub.team(), "t": time.time()}
                await room.local_participant.publish_data(json.dumps(payload).encode(), topic=STATE_TOPIC,
                                                          reliable=False)
                await asyncio.sleep(1 / STATE_HZ)
        except asyncio.CancelledError:
            await room.disconnect()
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[lk] connection problem: {exc}; retrying in 3 s (arms keep holding)")
            if hub.control.holder:
                hub.control.release()
                await hub.hold_all("LiveKit connection lost")
            await asyncio.sleep(3)


# ── HTTP: the local page ──
def make_app(hub: Hub, use_livekit: bool) -> web.Application:
    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def info(_):
        return web.json_response(hub.info())

    async def state(request: web.Request):
        slot = hub.by_id.get(request.query.get("arm") or "")
        if slot is None:
            raise web.HTTPBadRequest(text="unknown arm")
        return web.json_response({**await hub.state(slot), "team": hub.team(),
                                  "arms": [s.info() for s in hub.slots], "adapters": hub.adapters})

    async def command(request: web.Request):
        try:
            msg = await request.json()
        except ValueError:
            return web.json_response({"msg": "bad json"}, status=400)
        return web.json_response({"msg": await hub.run(msg, origin=LOCAL)})

    async def camera_token(_):
        """Token for this laptop's browser to publish its camera into the room."""
        from lk import CAMERA_IDENTITY, mint_token
        if not hub.livekit_on:
            raise web.HTTPNotFound(text="start with --livekit to share the camera")
        tok = mint_token(required_env("LIVEKIT_API_KEY"), required_env("LIVEKIT_API_SECRET"), CAMERA_IDENTITY,
                         required_env("LIVEKIT_ROOM"), publish_media=True, name="Arm camera")
        return web.json_response({"url": required_env("LIVEKIT_URL"), "token": tok})

    async def invite(request: web.Request):
        """An invite link for a teammate: opens the public remote page, nothing to install.

        The token (viewer rights only: data, no video/audio publishing) rides after '#', so it never
        reaches the page host. It expires after `hours`. Anyone holding the link can join until then.
        """
        import re
        import secrets
        from urllib.parse import urlencode

        from lk import HOST_IDENTITY, mint_token
        if not hub.livekit_on:
            raise web.HTTPNotFound(text="start with --livekit to invite teammates")
        name = re.sub(r"[^A-Za-z0-9 _-]", "", request.query.get("name", "")).strip()[:24] or "guest"
        try:
            hours = min(max(float(request.query.get("hours", INVITE_HOURS)), 0.25), 12)
        except ValueError:
            hours = INVITE_HOURS
        identity = f"{name.replace(' ', '-')}-{secrets.token_hex(2)}"
        tok = mint_token(required_env("LIVEKIT_API_KEY"), required_env("LIVEKIT_API_SECRET"), identity,
                         required_env("LIVEKIT_ROOM"), hours=hours, publish_media=False, name=name)
        page = os.environ.get("REMOTE_PAGE_URL", "").strip() or DEFAULT_REMOTE_PAGE
        frag = urlencode({"u": required_env("LIVEKIT_URL"), "t": tok, "n": name, "h": HOST_IDENTITY})
        print(f"[web] invite link created for '{name}' ({identity}), valid {hours:g} h")
        return web.json_response({"link": f"{page}#{frag}", "name": name, "identity": identity, "hours": hours})

    async def lk_ctx(app):
        tasks = [asyncio.create_task(hub.watch())]  # live USB / arm connection check
        if use_livekit:
            hub.livekit_on = True
            tasks.append(asyncio.create_task(livekit_task(hub)))
        yield
        for task in tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def on_cleanup(_):
        for slot in hub.slots:
            if slot.bridge is None:
                continue
            print(f"[web] parking arm '{slot.id}' before exit ...")
            await slot.bridge.run(safe_shutdown, slot.bridge.arm)
            slot.bridge.pool.shutdown(wait=True)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/info", info)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/cmd", command)
    app.router.add_get("/api/camera-token", camera_token)
    app.router.add_get("/api/invite", invite)
    app.router.add_static("/static", STATIC)
    app.cleanup_ctx.append(lk_ctx)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="simulated motors, no hardware")
    ap.add_argument("--arms", help="name=PORT list, e.g. left=COM9,right=COM10 (default: ARM_PORTS / ARM_PORT)")
    ap.add_argument("--livekit", action="store_true", help="also put the arms in the LiveKit room for the team")
    ap.add_argument("--speed", type=float, default=WEB_SPEED_DEG_S, help="deg/s at Fast (Normal = half)")
    ap.add_argument("--http-port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    load_env()
    if a.livekit:
        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_ROOM"):
            required_env(name)  # fail now, not after the arms are open

    spec = a.arms or os.environ.get("ARM_PORTS", "").strip()
    if spec:
        arms: list[tuple[str, str | None]] = list(parse_arms(spec))
    elif a.sim:
        arms = [("left", None), ("right", None)]
    else:
        arms = [("left", os.environ.get("ARM_PORT") or None)]
    if a.sim:
        arms = [(name, None) for name, _ in arms]

    slots = open_slots(arms, a.sim, a.speed)
    if not any(s.bridge for s in slots):
        print("[web] no arm connected yet: starting anyway. Plug in / power the arms and they connect by "
              "themselves (or press Connect in the page).")

    url = f"http://127.0.0.1:{a.http_port}"
    print(f"[web] open {url}   (Ctrl-C to park and quit)")
    if not a.no_browser:
        webbrowser.open(url)
    web.run_app(make_app(Hub(slots, a.sim, a.speed), a.livekit), host="127.0.0.1", port=a.http_port, print=None)


if __name__ == "__main__":
    main()
