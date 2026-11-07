"""Operator side: drive the Maker Arm from the keyboard over LiveKit, from any laptop.

    python controller.py              # keyboard control
    python controller.py --demo       # small scripted wave of the base, then back

Keys (no Enter needed)
  1..7    pick a joint          a / d   jog it - / +
  [ / ]   smaller / bigger step  h       hold here
  p       park (return & release torque)            q   quit (the arm holds)

The arm only moves after someone types MOVE in the arm_server terminal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time

from livekit import rtc

from armkit import JOINT_HELP, MAX_STEP_DEG, load_env, required_env
from lk import ARM_IDENTITY, CMD_TOPIC, STATE_TOPIC, mint_token

STEPS = [1.0, 2.0, 5.0, 10.0]


def start_key_reader(queue: asyncio.Queue) -> None:
    """Single keypresses: msvcrt on Windows, cbreak mode on macOS/Linux; typed lines as a fallback."""
    loop = asyncio.get_running_loop()

    def posix() -> None:
        import atexit
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        atexit.register(termios.tcsetattr, fd, termios.TCSADRAIN, saved)  # always give the terminal back
        tty.setcbreak(fd)  # keys arrive one by one; Ctrl-C still works
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                return
            if ch == "\x1b":  # arrow keys: ESC [ C (right) / ESC [ D (left)
                seq = sys.stdin.read(2)
                ch = {"[C": "d", "[D": "a"}.get(seq, "")
                if not ch:
                    continue
            loop.call_soon_threadsafe(queue.put_nowait, ch.lower())

    def windows() -> None:
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow/function key prefix: swallow the second byte
                msvcrt.getwch()
                continue
            loop.call_soon_threadsafe(queue.put_nowait, ch.lower())

    def lines() -> None:
        for line in sys.stdin:
            for ch in line.strip().lower():
                loop.call_soon_threadsafe(queue.put_nowait, ch)

    if sys.platform == "win32":
        target = windows
    elif sys.stdin.isatty():
        target = posix
    else:
        target = lines
    threading.Thread(target=target, daemon=True, name="keys").start()


class Link:
    def __init__(self, room: rtc.Room):
        self.room = room
        self.state: dict | None = None
        self.state_at = 0.0

    async def send(self, msg: dict) -> None:
        await self.room.local_participant.publish_data(
            json.dumps(msg).encode(), topic=CMD_TOPIC, reliable=True, destination_identities=[ARM_IDENTITY])

    def arm_present(self) -> bool:
        return any(p.identity == ARM_IDENTITY for p in self.room.remote_participants.values())

    def status_line(self) -> str:
        if not self.state or time.monotonic() - self.state_at > 2:
            return "no state from the arm yet (is arm_server.py running in this room?)"
        s = self.state
        joints = "  ".join(f"J{i + 1}={x:+6.1f}" for i, x in enumerate(s["joints_deg"]))
        power = "ON " if s["on"] else "OFF"
        fault = f"  FAULT: {s['fault']}" if s.get("fault") else ""
        return f"[{power}] {joints}{fault}  | {s.get('msg', '')}"


async def wait_for_arm(link: Link, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if link.arm_present() and link.state:
            return
        await asyncio.sleep(0.2)
    raise SystemExit(f"the arm ('{ARM_IDENTITY}') is not in the room. Start arm_server.py first, same room name.")


async def demo(link: Link) -> None:
    """Base +10°, -10° (back), then hold: one gentle wave."""
    if not link.state["on"]:
        print("the arm is OFF: type MOVE in the arm_server terminal, then run --demo again")
        return
    for deg in (5, 5, -5, -5, -5, -5, 5, 5):
        await link.send({"cmd": "jog", "joint": 1, "deg": deg})
        await asyncio.sleep(0.8)
        print(link.status_line())
    await link.send({"cmd": "hold"})
    print("demo done; the arm holds. Run controller.py and press p to park.")


async def keyboard(link: Link) -> None:
    keys: asyncio.Queue = asyncio.Queue()
    start_key_reader(keys)
    joint, step_i = 1, 1
    print(__doc__.split("Keys", 1)[1].split("The arm", 1)[0])
    print(f"joint J{joint}: {JOINT_HELP[joint]}   step {STEPS[step_i]:g}°")
    last_print = 0.0
    while True:
        try:
            key = await asyncio.wait_for(keys.get(), timeout=0.5)
        except asyncio.TimeoutError:
            key = None
        if key == "q":
            print("quitting; the arm holds where it is")
            return
        if key and key in "1234567":
            joint = int(key)
            print(f"joint J{joint}: {JOINT_HELP[joint]}")
        elif key in ("a", "d"):
            deg = STEPS[step_i] * (1 if key == "d" else -1)
            await link.send({"cmd": "jog", "joint": joint, "deg": deg})
        elif key == "[":
            step_i = max(0, step_i - 1)
            print(f"step {STEPS[step_i]:g}°")
        elif key == "]":
            step_i = min(len(STEPS) - 1, step_i + 1)
            print(f"step {STEPS[step_i]:g}°")
        elif key == "h":
            await link.send({"cmd": "hold"})
        elif key == "p":
            await link.send({"cmd": "park"})
        if key or time.monotonic() - last_print > 3:
            await asyncio.sleep(0.15)  # let the arm answer before printing
            print(link.status_line())
            last_print = time.monotonic()


async def main(args) -> None:
    load_env()
    url, key, secret = required_env("LIVEKIT_URL"), required_env("LIVEKIT_API_KEY"), required_env("LIVEKIT_API_SECRET")
    room_name = args.room or required_env("LIVEKIT_ROOM")
    assert max(STEPS) <= MAX_STEP_DEG

    room = rtc.Room()
    link = Link(room)

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket) -> None:
        if packet.topic == STATE_TOPIC:
            try:
                link.state, link.state_at = json.loads(packet.data.decode()), time.monotonic()
            except (ValueError, UnicodeDecodeError):
                pass

    print(f"[ctl] connecting to {url} room '{room_name}' as '{args.identity}' ...")
    await room.connect(url, mint_token(key, secret, args.identity, room_name))
    try:
        await wait_for_arm(link)
        print("[ctl] arm found: " + link.status_line())
        await (demo(link) if args.demo else keyboard(link))
    finally:
        await room.disconnect()


def run() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", help="LiveKit room (default: LIVEKIT_ROOM)")
    ap.add_argument("--identity", default="operator", help="your name in the room")
    ap.add_argument("--demo", action="store_true", help="run a small scripted wave of the base")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
