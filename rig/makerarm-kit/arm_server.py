"""Robot side: puts the Maker Arm in a LiveKit room so a controller anywhere can move it.

Run this on the laptop the arm's USB-CAN adapter is plugged into:

    python arm_server.py --sim        # simulated arm, real LiveKit room
    python arm_server.py              # real arm (COM port from ARM_PORT or auto-detected)

Safety model
  * Torque is only ever switched ON from THIS terminal (type MOVE). Remote peers cannot.
  * Remote commands are small relative jogs (<= MAX_STEP_DEG) clamped inside the soft limits.
  * A remote "park" returns to the start pose and releases torque; MOVE here powers up again.
  * Controller disconnects -> the arm holds where it is. Ctrl-C here -> park, then exit.

Wire format (JSON over LiveKit data messages)
  topic "arm/cmd"   controller -> arm   {"cmd": "jog", "joint": 1..7, "deg": +-x}
                                        {"cmd": "move", "v": {"1": +-x, "2": ...}}  (several joints)
                                        {"cmd": "hold"} | {"cmd": "park"}
  topic "arm/state" arm -> everyone     {"on": bool, "state": str, "joints_deg": [...7],
                                         "targets_deg": [...7], "fault": str|null, "msg": str}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from livekit import rtc

from armkit import (DEFAULT_SPEED_DEG_S, MAX_STEP_DEG, is_on, load_env, open_arm, positions_deg,
                    required_env, safe_range_deg, safe_shutdown, too_hot)
from lk import ARM_IDENTITY, CMD_TOPIC, STATE_TOPIC, mint_token

STATE_HZ = 10
MOVE_LEAD_DEG = 8.0  # joystick/held-key moves: target at most this far ahead -> stops in ~0.15 s at 60 deg/s


class ArmBridge:
    """Turns validated commands into SDK targets. All arm calls run on one worker thread."""

    def __init__(self, arm):
        self.arm = arm
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arm")
        self.targets_deg: list[float] | None = None
        self.last_msg = "waiting: type MOVE in the arm_server terminal to power the arm"

    async def run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    # --- these run on the arm thread ---
    def power_up(self) -> str:
        if is_on(self.arm):
            return "already on"
        if self.arm.state.name == "FAULT":
            return f"arm is in FAULT ({self.arm.fault_reason}); use Clear fault first"
        self.arm.refresh()
        if reason := too_hot(self.arm):
            return reason
        note = ""
        try:
            self.arm.enable()
        except Exception as exc:
            # Some gripper firmware answers parameter reads but ignores the write of its 500 ms CAN
            # watchdog (reads back 0). The SDK checks this BEFORE enabling any motor, so the arm is
            # still off here. Retry once without that watchdog: the same mode as grippers whose
            # firmware has no parameter access at all (MakerModsLab/SDK run those that way too).
            if "CAN timeout read back" not in str(exc) or not self._drop_gripper_watchdog():
                raise
            self.arm.enable()
            note = " (gripper without its 500 ms motor watchdog: firmware ignores the setting)"
        self.targets_deg = positions_deg(self.arm)
        return "motors ON, holding where they are" + note

    def _drop_gripper_watchdog(self) -> bool:
        from dataclasses import replace
        joints = self.arm.config.joints
        changed = False
        for i, j in enumerate(joints):
            if j.can_timeout_ms is not None:
                joints[i] = replace(j, can_timeout_ms=None)
                changed = True
                print(f"[arm] {j.label}: firmware ignores the CAN watchdog setting; "
                      "running without its motor watchdog (like older grippers)")
        return changed

    def park(self) -> str:
        if not is_on(self.arm):
            return "already off"
        if self.arm.stop():  # walk back to the pose captured at enable, verify, release
            self.targets_deg = None
            return "parked: back at the start pose, torque off."
        return "park could not be verified: arm is HOLDING. Support it, then stop the program."

    def clear_fault(self) -> str:
        """Recover from a latched FAULT (e.g. a transient comms glitch) without dropping the arm.

        The arm has no brakes, so it first walks back to its power-on pose (the SDK's verified
        stop); only then are the motor faults cleared. Power on again afterwards.
        """
        if self.arm.state.name != "FAULT":
            return "no fault to clear"
        if not self.arm.stop():
            return "could not verify the return to rest: arm is HOLDING. Support it before anything else."
        self.arm.clear_faults()
        self.targets_deg = None
        return "fault cleared: arm back at its start pose, torque off. Power on again when ready."

    def hold(self) -> str:
        if not is_on(self.arm):
            return "arm is off"
        self.arm.hold_current_position()
        self.targets_deg = positions_deg(self.arm)
        return "holding"

    def _plan(self, joint: int, deg: float, now: float, lead: float) -> tuple[float | None, str]:
        """Next target for one joint after a relative step, or None (with the reason) if it can't move."""
        lo, hi = safe_range_deg(self.arm, joint)
        current_target = self.targets_deg[joint - 1]
        wanted = current_target + deg
        # Never let the target run more than `lead` degrees ahead of the measured position, so
        # repeated steps (held keys, joysticks) stop soon after they stop arriving.
        if math.isfinite(now):
            wanted = min(max(wanted, now - lead), now + lead)
        clamped = min(max(wanted, lo), hi)
        if (clamped - current_target) * deg <= 0:
            # A joint resting outside the safe band (e.g. folded at zero) must never be pulled
            # the opposite way: only moves back toward the inside are allowed.
            if not lo <= current_target + deg <= hi:
                return None, f"J{joint} is at its safe limit ({current_target:+.1f}°); try the other direction"
            return None, f"J{joint} catching up ({now:+.1f}° -> {current_target:+.1f}°)"
        note = " (at its safe limit)" if clamped != wanted else ""
        return clamped, f"J{joint} -> {clamped:+.1f}°{note}"

    def jog(self, joint: int, deg: float) -> str:
        return self.move({joint: deg}, lead=MAX_STEP_DEG)

    def move(self, deltas: dict[int, float], lead: float = MOVE_LEAD_DEG) -> str:
        """Relative steps for several joints at once (joysticks / held keys), one target update."""
        if not is_on(self.arm) or self.targets_deg is None:
            return "ignored: arm is off (power it on first, next to the arm)"
        n = self.arm.config.n_joints
        for joint, deg in deltas.items():
            if not 1 <= joint <= n:
                return f"ignored: joint must be 1..{n}"
            if not math.isfinite(deg) or abs(deg) > MAX_STEP_DEG:
                return f"ignored: step must be within ±{MAX_STEP_DEG:g}°"
        now = positions_deg(self.arm)
        new = list(self.targets_deg)
        msgs = []
        for joint, deg in deltas.items():
            if deg == 0:
                continue
            target, msg = self._plan(joint, deg, now[joint - 1], lead)
            msgs.append(msg)
            if target is not None:
                new[joint - 1] = target
        if new != self.targets_deg:
            if not self.arm.set_joint_targets([math.radians(x) for x in new]):
                return "the arm rejected the target"
            self.targets_deg = new
        return " · ".join(msgs) or "no movement"

    def snapshot(self) -> dict:
        return {
            "on": is_on(self.arm),
            "state": self.arm.state.name,
            "joints_deg": [round(x, 2) for x in positions_deg(self.arm)],
            "targets_deg": [round(x, 2) for x in self.targets_deg] if self.targets_deg else None,
            "temps_c": [round(t, 1) if math.isfinite(t) else None for t in self.arm.get_temperatures()],
            "fault": self.arm.fault_reason,
            "msg": self.last_msg,
        }

    def refresh_if_off(self) -> None:
        if not is_on(self.arm):  # the control loop refreshes feedback while on
            self.arm.refresh()


def start_terminal_reader(queue: asyncio.Queue) -> None:
    """Feed typed lines into `queue` from a daemon thread (it never blocks shutdown)."""
    loop = asyncio.get_running_loop()

    def reader() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line.strip().upper())

    threading.Thread(target=reader, daemon=True, name="stdin").start()


async def main(args) -> None:
    load_env()
    url, key, secret = required_env("LIVEKIT_URL"), required_env("LIVEKIT_API_KEY"), required_env("LIVEKIT_API_SECRET")
    room_name = args.room or required_env("LIVEKIT_ROOM")

    arm = open_arm(port=args.port, sim=args.sim, speed_deg_s=args.speed)
    arm.connect()  # read-only: never enables torque
    bridge = ArmBridge(arm)
    room = rtc.Room()
    commands: asyncio.Queue = asyncio.Queue()
    terminal: asyncio.Queue = asyncio.Queue()

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket) -> None:
        if packet.topic != CMD_TOPIC:
            return
        try:
            msg = json.loads(packet.data.decode())
        except (ValueError, UnicodeDecodeError):
            return
        who = packet.participant.identity if packet.participant else "?"
        commands.put_nowait((who, msg))

    @room.on("participant_connected")
    def on_join(p: rtc.RemoteParticipant) -> None:
        print(f"[arm] {p.identity} joined")

    @room.on("participant_disconnected")
    def on_leave(p: rtc.RemoteParticipant) -> None:
        print(f"[arm] {p.identity} left; holding")
        commands.put_nowait((p.identity, {"cmd": "hold"}))

    print(f"[arm] connecting to {url} room '{room_name}' as '{ARM_IDENTITY}' ...")
    await room.connect(url, mint_token(key, secret, ARM_IDENTITY, room_name))
    print("[arm] in the room. Motors are OFF.")
    print("[arm] terminal: MOVE = power up (hold in place)   PARK = return & release   Ctrl-C = park & quit")
    start_terminal_reader(terminal)

    async def act(fn, *a) -> None:
        try:
            bridge.last_msg = await bridge.run(fn, *a)
        except Exception as exc:
            bridge.last_msg = f"error: {exc}"
        print(f"[arm] {bridge.last_msg}")

    try:
        next_state = 0.0
        while True:
            while not terminal.empty():
                line = terminal.get_nowait()
                if line == "MOVE":
                    print("[arm] CHECK: area clear, hand near the power switch. Powering up ...")
                    await act(bridge.power_up)
                elif line == "PARK":
                    await act(bridge.park)
                elif line:
                    print("[arm] type MOVE or PARK (or Ctrl-C)")
            while not commands.empty():
                who, msg = commands.get_nowait()
                cmd = msg.get("cmd")
                if cmd == "jog":
                    try:
                        joint, deg = int(msg.get("joint")), float(msg.get("deg"))
                    except (TypeError, ValueError):
                        continue
                    await act(bridge.jog, joint, deg)
                elif cmd == "move":
                    try:
                        deltas = {int(k): float(v) for k, v in dict(msg.get("v") or {}).items()}
                    except (TypeError, ValueError):
                        continue
                    if 0 < len(deltas) <= 7:
                        bridge.last_msg = await bridge.run(bridge.move, deltas)
                elif cmd == "hold":
                    await act(bridge.hold)
                elif cmd == "clear":
                    await act(bridge.clear_fault)
                elif cmd == "park":
                    print(f"[arm] park requested by {who}")
                    await act(bridge.park)
            now = time.monotonic()
            if now >= next_state:
                next_state = now + 1 / STATE_HZ
                await bridge.run(bridge.refresh_if_off)
                state = await bridge.run(bridge.snapshot)
                await room.local_participant.publish_data(json.dumps(state).encode(), topic=STATE_TOPIC,
                                                          reliable=False)
            await asyncio.sleep(0.01)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[arm] stopping ...")
    finally:
        await bridge.run(safe_shutdown, arm)  # park + release (asks for RELEASE if unverified)
        bridge.pool.shutdown(wait=True)
        await room.disconnect()


def run() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="simulated motors, no hardware")
    ap.add_argument("--port", help="USB-CAN COM port, e.g. COM9 (default: ARM_PORT or auto)")
    ap.add_argument("--room", help="LiveKit room (default: LIVEKIT_ROOM)")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED_DEG_S, help="deg/s ramp speed")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
