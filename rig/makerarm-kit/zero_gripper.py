"""Re-zero ONLY the gripper (motor 7) of one Maker Arm, e.g. after it lost its zero.

Symptom this fixes: power-on refused with
    "gripper (motor 7) at +82.7 deg is outside its limits [-96.0, -2.5] deg ... stale or missing zero"

    python zero_gripper.py --port candle:0      # the arm whose gripper lost its zero
    python zero_gripper.py --sim                # try the flow without hardware

Same procedure as the SDK's `maker-arm zero --motor 7` (which doesn't support candleLight on
Windows): torque stays OFF, you close the gripper fully by hand, then that position becomes the
gripper's zero. The zero is stored in the motor. Only the gripper is touched; joints 1-6 keep
their calibration. Ask the MakerMods staff first if you're not sure.

Close every other program using this adapter first (web_app.py, MakerModsLab ...).
"""
from __future__ import annotations

import argparse
import math
import time

from armkit import load_env, open_arm

GRIPPER_ID = 7
OK_AFTER_ZERO_DEG = 3.0  # the reading must be within this of 0 after zeroing


def gripper_deg(arm) -> float:
    arm.refresh(wait=True)
    i = next(i for i, j in enumerate(arm.config.joints) if j.motor_id == GRIPPER_ID)
    return math.degrees(arm.get_joint_positions()[i])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--port", help="the arm's adapter, e.g. candle:0 or COM9 (required: no auto-detect)")
    target.add_argument("--sim", action="store_true", help="simulated arm, no hardware")
    a = ap.parse_args()
    load_env()

    arm = open_arm(port=a.port, sim=a.sim)
    gripper = next(m for m in arm.motors if m.motor_id == GRIPPER_ID)
    j = next(j for j in arm.config.joints if j.motor_id == GRIPPER_ID)
    lo, hi = math.degrees(j.lo), math.degrees(j.hi)
    arm.connect()  # read-only: never enables torque
    try:
        gripper.disable()  # make sure the gripper is limp so it can be closed by hand
        time.sleep(0.05)
        print(f"\nArm on {a.port or 'simulator'}: "
              + "  ".join(f"J{i + 1}={math.degrees(p):+.1f}" for i, p in enumerate(arm.get_joint_positions())))
        before = gripper_deg(arm)
        print(f"Gripper now reads {before:+.1f} deg; valid range is [{lo:+.1f}, {hi:+.1f}] deg "
              f"(0 = fully closed).")
        if lo - 5 <= before <= hi + 5:
            print("That is already about right: this gripper probably does NOT need re-zeroing.")
        print("\n1. Motors stay OFF. Close the gripper FULLY by hand (jaws touching, don't force it).")
        print("2. Keep holding it closed, then type ZERO and press Enter. Press Enter alone to refresh.\n")
        while True:
            try:
                answer = input(f"   gripper reads {gripper_deg(arm):+.1f} deg  >  ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                print("\ncancelled; nothing was written")
                return
            if answer == "ZERO":
                break
            if answer in ("Q", "QUIT", "EXIT"):
                print("cancelled; nothing was written")
                return
        pre = gripper_deg(arm)
        gripper.set_zero()
        time.sleep(0.3)
        after = gripper_deg(arm)
        print(f"\nbefore: {pre:+.1f} deg  ->  after: {after:+.1f} deg")
        if abs(after) <= OK_AFTER_ZERO_DEG:
            print("Gripper zero set (stored in the motor). Power the arm on again in the web app and test "
                  "Open/Close gripper with small steps.")
        else:
            print("WARNING: the reading did not come back near 0, so the zero may not have been stored. "
                  "Run this again, power-cycle the arm, or ask the MakerMods staff.")
    finally:
        arm.disconnect()


if __name__ == "__main__":
    main()
