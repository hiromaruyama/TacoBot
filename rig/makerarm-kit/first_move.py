"""First movement: nudge ONE joint a few degrees, bring it back, then park and release.

No LiveKit involved: the arm's USB-CAN adapter is plugged into this laptop.

    python first_move.py --sim                    # try it with simulated motors
    python first_move.py --read-only              # real arm, motors OFF: move it by hand, watch angles
    python first_move.py --joint 1 --deg 10       # real arm: base turns 10 degrees and back

Defaults are deliberately small and slow. Ctrl-C stops and holds.
"""
from __future__ import annotations

import argparse
import math
import time

from armkit import (DEFAULT_SPEED_DEG_S, JOINT_HELP, MAX_SPEED_DEG_S, format_joints, list_ports_text,
                    load_env, open_arm, positions_deg, safe_range_deg, safe_shutdown, too_hot)
from maker_arm.arm import ArmState

MAX_FIRST_MOVE_DEG = 20.0
TOLERANCE_DEG = 2.0


def move_and_wait(arm, joint: int, target_deg: float, speed_deg_s: float) -> None:
    targets = arm.get_commanded_positions()
    targets[joint - 1] = math.radians(target_deg)
    if not arm.set_joint_targets(targets):
        raise RuntimeError("the arm rejected the target")
    deadline = time.monotonic() + abs(target_deg - positions_deg(arm)[joint - 1]) / speed_deg_s + 5.0
    settled = None
    while time.monotonic() < deadline:
        if arm.state is ArmState.FAULT:
            raise RuntimeError(f"motor fault: {arm.fault_reason}")
        now_deg = positions_deg(arm)[joint - 1]
        print(f"\r  J{joint} = {now_deg:+7.1f}°  (target {target_deg:+.1f}°)   ", end="", flush=True)
        if abs(now_deg - target_deg) <= TOLERANCE_DEG:
            settled = settled or time.monotonic()
            if time.monotonic() - settled >= 0.3:
                print()
                return
        else:
            settled = None
        time.sleep(0.05)
    print()
    raise RuntimeError(f"J{joint} did not reach {target_deg:+.1f}° in time")


def read_only(arm) -> None:
    arm.connect()
    print("connected, torque OFF. Move the arm by hand and watch the angles. Ctrl-C to quit.")
    try:
        while True:
            arm.refresh()
            print("\r" + format_joints(arm), end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        arm.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="simulated motors, no hardware")
    ap.add_argument("--port", help="USB-CAN adapter COM port, e.g. COM9 (default: ARM_PORT or auto)")
    ap.add_argument("--list-ports", action="store_true", help="show serial ports and exit")
    ap.add_argument("--read-only", action="store_true", help="print joint angles with torque off")
    ap.add_argument("--joint", type=int, default=1, help="joint 1-7 (default 1 = base)")
    ap.add_argument("--deg", type=float, default=10.0, help=f"relative move, max ±{MAX_FIRST_MOVE_DEG:g}")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED_DEG_S, help=f"deg/s, max {MAX_SPEED_DEG_S:g}")
    ap.add_argument("--yes", action="store_true", help="skip the MOVE prompt (only with --sim)")
    a = ap.parse_args()
    load_env()

    if a.list_ports:
        print(list_ports_text())
        return
    if not 1 <= a.joint <= 7:
        raise SystemExit("--joint must be 1..7:\n" + "\n".join(f"  {k}: {v}" for k, v in JOINT_HELP.items()))
    if not 0 < abs(a.deg) <= MAX_FIRST_MOVE_DEG:
        raise SystemExit(f"--deg must be non-zero and within ±{MAX_FIRST_MOVE_DEG:g}")
    if a.yes and not a.sim:
        raise SystemExit("--yes is only allowed with --sim")

    arm = open_arm(port=a.port, sim=a.sim, speed_deg_s=a.speed)
    if a.read_only:
        return read_only(arm)

    arm.connect()
    try:
        start = positions_deg(arm)[a.joint - 1]
        target = start + a.deg
        lo, hi = safe_range_deg(arm, a.joint)
        print(f"now:    {format_joints(arm)}")
        print(f"joint:  J{a.joint} {JOINT_HELP[a.joint]}")
        print(f"move:   {start:+.1f}° -> {target:+.1f}° -> back, at {a.speed:g}°/s")
        arm.refresh()
        if reason := too_hot(arm):
            raise SystemExit(f"{reason} Motors were not enabled.")
        if not lo <= target <= hi:
            raise SystemExit(f"target {target:+.1f}° is outside the safe range [{lo:+.1f}, {hi:+.1f}]°; "
                             "try the other direction (negative --deg). Motors were not enabled.")
        print("\nCHECK: area clear, someone's hand near the power switch, arm supported.")
        if not a.yes and input("Type MOVE to enable the motors and go > ").strip().upper() != "MOVE":
            print("cancelled; motors were not enabled")
            return
        arm.enable()
        move_and_wait(arm, a.joint, target, a.speed)
        print("reached. going back ...")
        move_and_wait(arm, a.joint, start, a.speed)
        print("back at the start. First movement done!")
    except KeyboardInterrupt:
        print("\ninterrupted; holding position")
        arm.hold_current_position()
    except RuntimeError as exc:
        print(f"\nstopped: {exc}")
        arm.hold_current_position()
    finally:
        safe_shutdown(arm)


if __name__ == "__main__":
    main()
