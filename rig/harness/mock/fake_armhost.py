"""A pretend Maker Arm kit host (`web_app.py --livekit`) and its shared camera, for testing arm-host mode.

    livekit-server --dev
    uv run python -m mock.fake_armhost                    # joins room harness-dev as arm-host + arm-camera
    uv run harness --env mock/dev.env                     # auto-detects arm-host mode

Speaks the kit's wire format (rig/makerarm-kit/web_app.py on rig/makerarm-web-kit) with its control rules:
one remote controller at a time (take / release, ping heartbeats < 5 s), anyone may hold or park, jog needs
control and at most 10°, power on is local-only. The two simulated arms start powered (use --off to start them off).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time

import cv2
import numpy as np
from dotenv import load_dotenv

from harness.armhost import ARM_HOST, CAMERA_IDENTITY, CMD_TOPIC, REPLY_TOPIC, STATE_TOPIC
from harness.link import mint_token

MAX_STEP = 10.0
JOINTS = [{"n": i, "name": n, "help": "", "lo": -120.0, "hi": 120.0} for i, n in
          enumerate(["shoulder pan", "shoulder lift", "elbow flex", "wrist flex", "wrist yaw", "wrist roll",
                     "gripper"], start=1)]


class FakeArm:
    def __init__(self, on: bool) -> None:
        self.on = on
        self.pos = [0.0] * 7
        self.targets = [0.0] * 7 if on else None
        self.msg = "motors ON (fake)" if on else "Motors off. Press Power on when the area is clear."

    def step(self) -> None:
        if self.targets:
            for i, t in enumerate(self.targets):
                self.pos[i] += max(-1.5, min(1.5, t - self.pos[i]))

    def snapshot(self) -> dict:
        return {"on": self.on, "state": "ENABLED" if self.on else "DISABLED",
                "joints_deg": [round(p, 2) for p in self.pos],
                "targets_deg": [round(t, 2) for t in self.targets] if self.targets else None,
                "temps_c": [38.0 + i for i in range(7)], "fault": None, "msg": self.msg}


class Host:
    def __init__(self, on: bool) -> None:
        self.arms = {"left": FakeArm(on), "right": FakeArm(on)}
        self.holder: str | None = None
        self.seen = self.used = 0.0
        self.viewers: set[str] = set()

    def expired(self) -> bool:
        now = time.monotonic()
        return self.holder is not None and (now - self.used > 60 or now - self.seen > 5)

    def run(self, msg: dict, who: str) -> str | None:
        cmd = msg.get("cmd")
        if cmd == "ping":
            if who == self.holder:
                self.seen = time.monotonic()
            return None
        if cmd == "take":
            if self.holder in (None, who) or self.expired():
                self.holder, self.used, self.seen = who, time.monotonic(), time.monotonic()
                return f"{who} has control"
            return f"{self.holder} has control right now; ask them to release it"
        if cmd == "release":
            if who == self.holder:
                self.holder = None
                return f"control released by {who}"
            return "you don't have control"
        if cmd not in ("jog", "hold", "park"):
            return f"'{cmd}' is only allowed on the arm laptop"
        if cmd == "jog":
            if self.expired():
                self.holder = None
            if self.holder != who:
                return f"{self.holder} has control" if self.holder else "press Take control first"
            self.used = self.seen = time.monotonic()
        arm = self.arms.get(str(msg.get("arm")))
        if arm is None:
            return f"unknown arm {msg.get('arm')!r}"
        if cmd == "hold":
            if not arm.on:
                return "arm is off"
            arm.targets = list(arm.pos)
            arm.msg = "holding"
        elif cmd == "park":
            if not arm.on:
                return "already off"
            arm.targets = [0.0] * 7
            arm.on, arm.msg = False, "parked: back at the start pose, torque off."
            arm.targets = None
        else:
            if not arm.on or arm.targets is None:
                return "ignored: arm is off (power it on first, next to the arm)"
            joint, deg = int(msg["joint"]), float(msg["deg"])
            if abs(deg) > MAX_STEP:
                return f"ignored: step must be within ±{MAX_STEP:g}°"
            arm.targets[joint - 1] = max(-115.0, min(115.0, arm.targets[joint - 1] + deg))
            arm.msg = f"J{joint} -> {arm.targets[joint - 1]:+.1f}° ({who})"
        return arm.msg if cmd != "hold" else "holding"

    def payload(self) -> dict:
        return {"arms": {k: a.snapshot() for k, a in self.arms.items()},
                "info": {"arms": [{"id": k, "port": "sim", "ok": True, "error": None} for k in self.arms],
                         "joints": JOINTS, "max_step": MAX_STEP, "sim": True, "livekit": True},
                "controller": self.holder, "viewers": sorted(self.viewers), "t": time.time()}


async def run_host(url: str, room_name: str, key: str, secret: str, on: bool) -> None:
    from livekit import rtc

    host = Host(on)
    room = rtc.Room()

    async def handle(who: str, msg: dict) -> None:
        reply = host.run(msg, who)
        if reply is not None:
            print(f"[fake arm-host] {who}: {msg} -> {reply}")
            await room.local_participant.publish_data(json.dumps({"msg": reply}).encode(), topic=REPLY_TOPIC,
                                                      reliable=True, destination_identities=[who])

    @room.on("data_received")
    def on_data(packet) -> None:
        if packet.topic == CMD_TOPIC and packet.participant is not None:
            asyncio.ensure_future(handle(packet.participant.identity, json.loads(bytes(packet.data).decode())))

    room.on("participant_connected", lambda p: host.viewers.add(p.identity))
    room.on("participant_disconnected", lambda p: host.viewers.discard(p.identity))
    await room.connect(url, mint_token(ARM_HOST, room_name, key, secret))
    host.viewers = set(room.remote_participants)
    print(f"[fake arm-host] in room {room_name!r}")
    while True:
        if host.expired():
            print(f"[fake arm-host] {host.holder} went silent; holding")
            host.holder = None
        for arm in host.arms.values():
            arm.step()
        await room.local_participant.publish_data(json.dumps(host.payload()).encode(), topic=STATE_TOPIC,
                                                  reliable=False)
        await asyncio.sleep(0.2)


async def run_camera(url: str, room_name: str, key: str, secret: str) -> None:
    from livekit import rtc

    w, h = 640, 360
    room = rtc.Room()
    await room.connect(url, mint_token(CAMERA_IDENTITY, room_name, key, secret))
    source = rtc.VideoSource(w, h)
    track = rtc.LocalVideoTrack.create_video_track("camera", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA))
    print("[fake arm-camera] sharing a test picture")
    t0 = time.monotonic()
    while True:
        t = time.monotonic() - t0
        img = np.zeros((h, w, 3), np.uint8)
        img[:] = (30, 40, 60)
        cx = int(w / 2 + 200 * math.sin(t))
        cv2.circle(img, (cx, h // 2), 40, (0, 200, 255), -1)
        cv2.putText(img, f"FAKE ARM CAMERA {time.strftime('%H:%M:%S')}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (255, 255, 255), 2)
        rgba = cv2.cvtColor(img, cv2.COLOR_RGB2RGBA)
        source.capture_frame(rtc.VideoFrame(w, h, rtc.VideoBufferType.RGBA, rgba.tobytes()))
        await asyncio.sleep(1 / 15)


async def main(args) -> None:
    load_dotenv(args.env, override=True)
    url = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
    if "livekit.cloud" in url:
        raise SystemExit("refusing to put a fake arm-host in a cloud room (the real one may be there)")
    key, secret = os.environ.get("LIVEKIT_API_KEY", "devkey"), os.environ.get("LIVEKIT_API_SECRET", "secret")
    room = args.room or os.environ.get("LIVEKIT_ROOM", "harness-dev")
    jobs = [run_host(url, room, key, secret, not args.off)]
    if not args.no_camera:
        jobs.append(run_camera(url, room, key, secret))
    await asyncio.gather(*jobs)


def run() -> None:
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(Path(__file__).resolve().parent / "dev.env"))
    ap.add_argument("--room")
    ap.add_argument("--off", action="store_true", help="start with the arms' motors off")
    ap.add_argument("--no-camera", action="store_true")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
