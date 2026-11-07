"""Local web app: see the camera and move one or more Maker Arms from http://127.0.0.1:8765

    python web_app.py --sim                         # two simulated arms (left, right)
    python web_app.py                               # real arms from ARM_PORTS (or ARM_PORT) in .env
    python web_app.py --arms left=COM9,right=COM10  # same, from the command line

Each arm has its own USB-CAN adapter (its own COM port). Pick the arm in the page's dropdown.
An arm that can't be reached (unplugged, busy) shows as offline; the others still work.

The camera is read by the browser itself (no extra install). The server only listens on
127.0.0.1, so only someone on this laptop, next to the arms, can power them up.
Ctrl-C here parks every powered arm (returns to its start pose) and releases torque.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import pathlib
import webbrowser
from dataclasses import dataclass, field

from aiohttp import web

from armkit import (DEFAULT_SPEED_DEG_S, JOINT_HELP, MAX_STEP_DEG, load_env, open_arm, safe_range_deg,
                    safe_shutdown)
from arm_server import ArmBridge

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"
OFF_MSG = "Motors off. Press Power on when the area is clear."


@dataclass
class ArmSlot:
    id: str
    port: str | None
    bridge: ArmBridge | None = None
    error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def info(self) -> dict:
        return {"id": self.id, "port": self.port or "sim", "ok": self.bridge is not None, "error": self.error}


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
            arm = open_arm(port=port, sim=sim, speed_deg_s=speed)
            arm.connect()  # read-only; torque stays off until "Power on" in the page
            slot.bridge = ArmBridge(arm)
            slot.bridge.last_msg = OFF_MSG
            print(f"[web] arm '{name}' ready on {port or 'simulator'}")
        except BaseException as exc:  # SystemExit from port detection too: keep the other arms
            if isinstance(exc, KeyboardInterrupt):
                raise
            slot.error = str(exc) or type(exc).__name__
            print(f"[web] arm '{name}' OFFLINE ({port}): {slot.error}")
        slots.append(slot)
    return slots


def make_app(slots: list[ArmSlot], sim: bool) -> web.Application:
    by_id = {s.id: s for s in slots}
    ref = next((s.bridge.arm for s in slots if s.bridge), None)
    joints_info = []
    if ref is not None:
        for i, j in enumerate(ref.config.joints, start=1):
            lo, hi = safe_range_deg(ref, i)
            joints_info.append({"n": i, "name": (j.name or f"J{i}").replace("_", " "),
                                "help": JOINT_HELP.get(i, "").split("(")[-1].rstrip(")"),
                                "lo": round(lo, 1), "hi": round(hi, 1)})

    def slot_for(arm_id) -> ArmSlot:
        slot = by_id.get(arm_id or "")
        if slot is None:
            raise web.HTTPBadRequest(text=f"unknown arm {arm_id!r}")
        return slot

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def info(_):
        return web.json_response({"arms": [s.info() for s in slots], "joints": joints_info,
                                  "max_step": MAX_STEP_DEG, "sim": sim})

    async def state(request: web.Request):
        slot = slot_for(request.query.get("arm"))
        if slot.bridge is None:
            return web.json_response({"offline": True, "error": slot.error})
        try:
            await slot.bridge.run(slot.bridge.refresh_if_off)
        except Exception as exc:  # adapter/bus trouble: report it, don't spam tracebacks
            reason = (f"no answer from the arm on {slot.port} ({exc}). Check that this arm's "
                      "power supply is on and its e-stop released; the app keeps retrying.")
            if slot.error != reason:
                print(f"[web] arm '{slot.id}': {reason}")
            slot.error = reason
            return web.json_response({"offline": True, "error": reason})
        if slot.error:
            print(f"[web] arm '{slot.id}' is answering again")
            slot.error = None
        return web.json_response(await slot.bridge.run(slot.bridge.snapshot))

    async def command(request: web.Request):
        try:
            msg = await request.json()
        except ValueError:
            return web.json_response({"msg": "bad json"}, status=400)
        slot = slot_for(msg.get("arm"))
        if slot.bridge is None:
            return web.json_response({"msg": f"arm '{slot.id}' is offline: {slot.error}"})
        bridge, cmd = slot.bridge, msg.get("cmd")
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
                else:
                    return web.json_response({"msg": f"unknown cmd {cmd!r}"}, status=400)
            except (TypeError, ValueError):
                return web.json_response({"msg": "bad joint/deg"}, status=400)
            except Exception as exc:
                result = f"error: {exc}"
        bridge.last_msg = result
        print(f"[web] {slot.id} {cmd}: {result}")
        return web.json_response({"msg": result})

    async def on_cleanup(_):
        for slot in slots:
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
    app.router.add_static("/static", STATIC)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="simulated motors, no hardware")
    ap.add_argument("--arms", help="name=PORT list, e.g. left=COM9,right=COM10 (default: ARM_PORTS / ARM_PORT)")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED_DEG_S, help="deg/s ramp speed")
    ap.add_argument("--http-port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    load_env()

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
        raise SystemExit("no arm could be opened; check the USB cables and ports (first_move.py --list-ports)")

    url = f"http://127.0.0.1:{a.http_port}"
    print(f"[web] open {url}   (Ctrl-C to park and quit)")
    if not a.no_browser:
        webbrowser.open(url)
    web.run_app(make_app(slots, a.sim), host="127.0.0.1", port=a.http_port, print=None)


if __name__ == "__main__":
    main()
