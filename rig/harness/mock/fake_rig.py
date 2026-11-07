"""A pretend rig for trying the harness with no arms, cameras or Dream-1 key.

    livekit-server --dev                                   # local LiveKit (devkey / secret)
    uv run fake-rig --portal-config ../portal-makermods/portal.yaml
    uv run harness --env mock/dev.env --portal-config ../portal-makermods/portal.yaml

It joins the room as the same identities the real server uses, speaking the same RPCs:
  * `robot`: a Portal Robot publishing the three cameras (synthetic pictures) and both arms' state. The state
    follows the active operator's actions, and answers `park` / `home` like upstream's robot.
  * `policy-dream-1-tasks`: a Portal Operator answering the `dream.*` RPCs with upstream's semantics (a task change
    pauses first, only resume gives it control) and, while it drives, sending a slow wave as actions.
  * `teleoperator` (with --teleop): an operator that never sends anything, so `dream.to_teleop` has a target.
Nothing here touches hardware.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv

from harness import protocol as P
from harness.link import mint_token

W, H = 480, 360
COLORS = {"overhead_camera": (40, 70, 110), "left_wrist_camera": (90, 50, 50), "right_wrist_camera": (40, 90, 60)}
REST = {f"{side}_{j}.pos": 0.0 for side in P.SIDES for j in P.JOINTS}


def picture(name: str, state: dict[str, float], t: float, parked: bool, driver: str | None) -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = COLORS.get(name, (60, 60, 60))
    cv2.rectangle(img, (0, 0), (W - 1, H - 1), (255, 255, 255), 2)
    for side, cx in (("left", W // 3), ("right", 2 * W // 3)):  # a stick arm per side, drawn from the joints
        x, y, ang = cx, H - 40, -math.pi / 2
        for j, length in (("joint2", 90), ("joint3", 70), ("joint4", 40)):
            ang += state.get(f"{side}_{j}.pos", 0.0)
            nx, ny = int(x + length * math.cos(ang)), int(y + length * math.sin(ang))
            cv2.line(img, (x, y), (nx, ny), (230, 230, 230), 6)
            x, y = nx, ny
        grip = int(4 + state.get(f"{side}_gripper.pos", 0.0) / 5)
        cv2.circle(img, (x, y), grip, (0, 200, 255), -1)
    cv2.putText(img, name, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(img, f"FAKE RIG  {time.strftime('%H:%M:%S')}.{int(t * 10) % 10}", (10, 52),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    cv2.putText(img, "PARKED" if parked else f"driver: {driver or 'nobody'}", (10, H - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 1)
    return img


async def fake_robot(cfg_path: str, url: str, room: str, key: str, secret: str, stop: asyncio.Event) -> None:
    from livekit.portal import Robot, RobotConfig

    cfg = RobotConfig.from_yaml_file(cfg_path, room)
    fps = min(cfg.fps, 15)
    robot = Robot(cfg)
    state = dict(REST)
    target: dict[str, float] = {}
    pending: list[str] = []
    parked = False

    def on_action(action) -> None:
        target.update({k: float(v) for k, v in action.values.items()})

    def on_active(who) -> None:
        target.clear()
        print(f"[fake robot] active operator: {who}")

    def on_park(data) -> str:
        if data.payload not in (P.PARK_ONLY, ""):
            raise ValueError("unknown park request")
        if data.payload == P.PARK_ONLY and parked:
            return "already parked"
        pending.append("park" if data.payload == P.PARK_ONLY or not parked else "unpark")
        return "parking the arms" if pending[-1] == "park" else "powering the arms up"

    def on_home(_data) -> str:
        if parked:
            return "parked: the arms are off at rest; p powers them up"
        pending.append("home")
        return "returning the arms home"

    robot.on_action(on_action)
    robot.on_active_operator_changed(on_active)
    robot.register_rpc_method(P.PARK_RPC, on_park)
    robot.register_rpc_method(P.HOME_RPC, on_home)
    await robot.connect(url, mint_token(P.ROBOT_IDENTITY, room, key, secret))
    print(f"[fake robot] up in room {room!r}, cameras {list(cfg.video_tracks)} at {fps} fps")
    try:
        t0 = time.monotonic()
        while not stop.is_set():
            started = time.monotonic()
            while pending:
                req = pending.pop(0)
                if req in ("park", "home"):
                    await robot.set_active_operator(None)
                    target.clear()
                    for _ in range(fps):  # walk back to rest over a second
                        for k in state:
                            state[k] *= 0.8
                        await asyncio.sleep(1 / fps)
                    parked = req == "park"
                    print(f"[fake robot] {'parked' if parked else 'home, holding'}")
                else:
                    parked = False
                    print("[fake robot] powered up")
            if target and not parked:
                for k, v in target.items():
                    if k in state:
                        state[k] += max(-0.05, min(0.05, v - state[k]))
            ts = robot.now_us()
            robot.send_state(dict(state), timestamp_us=ts)
            for name in cfg.video_tracks:
                robot.send_video_frame(name, picture(name, state, started - t0, parked, robot.active_operator()),
                                       timestamp_us=ts)
            await asyncio.sleep(max(0, 1 / fps - (time.monotonic() - started)))
    finally:
        await robot.disconnect()
        robot.close()


class FakeDream:
    """Upstream TaskControl's semantics, minus the model."""

    def __init__(self, op, presets: list[str]) -> None:
        self.op, self.presets = op, presets
        self.task = presets[0] if presets else None

    def driving(self) -> bool:
        return self.op.active_operator() == self.op.local_identity()

    def status(self) -> dict:
        ops = self.op.operators()
        return {"task": self.task, "presets": self.presets, "driving": self.driving(),
                "active_operator": self.op.active_operator(),
                "teleoperator_connected": P.TELEOPERATOR_IDENTITY in ops,
                "hand_teleop": "hand-teleop", "hand_teleop_connected": "hand-teleop" in ops}

    async def hand_to(self, who: str | None) -> None:
        await self.op.set_active_operator(who)
        for _ in range(40):
            if self.op.active_operator() == who:
                return
            await asyncio.sleep(0.025)

    def register(self) -> None:
        from livekit.portal import RpcError

        def handler(fn):
            async def handle(data) -> str:
                try:
                    return json.dumps(await fn(json.loads(data.payload or "{}")))
                except Exception as exc:  # noqa: BLE001
                    raise RpcError.Error(code=1500, message=str(exc), data=None) from exc
            return handle

        async def status(_):
            return self.status()

        async def set_task(p):
            task = " ".join(str(p.get("task", "")).split())
            if not task or len(task) > P.MAX_TASK_CHARS:
                raise ValueError(f"a task is one instruction of 1-{P.MAX_TASK_CHARS} characters")
            if self.driving():
                await self.hand_to(None)
            self.task = task
            print(f"[fake dream] task {task!r}")
            return self.status()

        async def pause(_):
            if self.driving():
                await self.hand_to(None)
            return self.status()

        async def resume(_):
            if self.task is None:
                raise ValueError("Dream-1 has no task yet; give it one first")
            if not self.driving():
                await self.hand_to(self.op.local_identity())
            return self.status()

        async def to_teleop(_):
            if P.TELEOPERATOR_IDENTITY not in self.op.operators():
                raise ValueError("the teleoperator is not connected; start `uv run teleoperator`")
            await self.hand_to(P.TELEOPERATOR_IDENTITY)
            return self.status()

        async def to_hand(_):
            raise ValueError("the hand teleop is not connected; start `uv run --group hands hand-teleop`")

        async def stop_hand(_):
            return self.status()

        for name, fn in ((P.DREAM_STATUS_RPC, status), (P.DREAM_SET_TASK_RPC, set_task), (P.DREAM_PAUSE_RPC, pause),
                         (P.DREAM_RESUME_RPC, resume), (P.DREAM_TO_TELEOP_RPC, to_teleop),
                         (P.DREAM_TO_HAND_TELEOP_RPC, to_hand), (P.DREAM_STOP_HAND_TELEOP_RPC, stop_hand)):
            self.op.register_rpc_method(name, handler(fn))


async def fake_dream(cfg_path: str, url: str, room: str, key: str, secret: str, stop: asyncio.Event,
                     presets: list[str]) -> None:
    from livekit.portal import Operator, OperatorConfig

    op = Operator(OperatorConfig.from_yaml_file(cfg_path, room))
    dream = FakeDream(op, presets)
    dream.register()
    await op.connect(url, mint_token(P.DREAM_TASKS_IDENTITY, room, key, secret))
    print("[fake dream] up; waiting for a task and resume")
    try:
        t0 = time.monotonic()
        while not stop.is_set():
            obs = op.get_observation()
            if dream.driving() and obs is not None:
                t = time.monotonic() - t0
                action = {}
                for i, side in enumerate(P.SIDES):
                    phase = t * 0.8 + i * math.pi
                    wave = (0.3 * math.sin(phase), 0.4 * math.sin(phase), 0.5 * math.sin(phase * 1.3),
                            0.3 * math.cos(phase), 0.0, 0.0)
                    action.update({f"{side}_joint{n}.pos": v for n, v in enumerate(wave, start=1)})
                    action[f"{side}_gripper.pos"] = 50 + 40 * math.sin(phase * 2)
                op.send_action(action, in_reply_to_ts_us=obs.timestamp_us)
            await asyncio.sleep(1 / 15)
    finally:
        await op.disconnect()
        op.close()


async def fake_teleop(cfg_path: str, url: str, room: str, key: str, secret: str, stop: asyncio.Event) -> None:
    from livekit.portal import Operator, OperatorConfig

    op = Operator(OperatorConfig.from_yaml_file(cfg_path, room))
    await op.connect(url, mint_token(P.TELEOPERATOR_IDENTITY, room, key, secret))
    print("[fake teleop] up (sends nothing; the arms hold while it has control)")
    try:
        await stop.wait()
    finally:
        await op.disconnect()
        op.close()


async def main(args) -> None:
    if args.env:
        load_dotenv(args.env, override=True)
    url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    key, secret = os.environ.get("LIVEKIT_API_KEY", "devkey"), os.environ.get("LIVEKIT_API_SECRET", "secret")
    room = args.room or os.environ.get("LIVEKIT_ROOM", "harness-dev")
    if "livekit.cloud" in url and not args.allow_cloud:
        raise SystemExit("refusing to put a fake robot in a cloud room (a real robot may use it); "
                         "pass --allow-cloud with a room nobody else uses")
    stop = asyncio.Event()
    common = (args.portal_config, url, room, key, secret, stop)
    robot = asyncio.create_task(fake_robot(*common))
    await asyncio.sleep(1.0)  # observers and operators sync to the robot's clock
    tasks = [robot, asyncio.create_task(fake_dream(*common, presets=args.task or []))]
    if args.teleop:
        tasks.append(asyncio.create_task(fake_teleop(*common)))
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()


def run() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    configs = [here.parents[2] / "upstream" / "portal.yaml", here.parents[3] / "portal-makermods" / "portal.yaml"]
    ap.add_argument("--portal-config", default=str(next((c for c in configs if c.exists()), configs[0])))
    ap.add_argument("--env", default=str(here / "dev.env"))
    ap.add_argument("--room")
    ap.add_argument("--task", action="append", help="preset task(s), like dream-1-tasks --task")
    ap.add_argument("--teleop", action="store_true", help="also join a do-nothing teleoperator")
    ap.add_argument("--allow-cloud", action="store_true")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
