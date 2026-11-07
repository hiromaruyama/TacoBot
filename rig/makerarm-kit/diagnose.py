"""Read-only health check of one arm: mode, faults, temperature, position and supply voltage per motor.

    python diagnose.py --port candle:0
    python diagnose.py --port COM9
    python diagnose.py --sim

Motors stay OFF. Close every other program using that adapter first (web_app.py ...).
Typical findings:
  * vbus 0.0V on every motor + "enable rejected: mode=0"  -> no motor power: e-stop pressed,
    power supply off/unplugged, or a tripped fuse. CAN still answers, the motors can't energize.
  * a fault word (undervoltage, overtemperature ...)       -> see the fault text; ask the staff.
  * temp > 70 C                                            -> let it cool (power-on is blocked).
  * a position far outside the joint's range               -> lost zero (gripper: zero_gripper.py).
vbus is shown only for firmware with parameter access (params=True); older firmware shows '-'.
"""
from __future__ import annotations

import argparse
import math
import time

from armkit import load_env, open_arm

VBUS = 0x701C  # RobStride "VBUS" parameter (f32, volts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--port", help="the arm's adapter, e.g. candle:0 or COM9")
    target.add_argument("--sim", action="store_true", help="simulated arm, no hardware")
    a = ap.parse_args()
    load_env()
    from maker_arm import protocol as p

    arm = open_arm(port=a.port, sim=a.sim)
    arm.connect()  # never enables torque
    try:
        arm.refresh(wait=True)
        time.sleep(0.1)
        print(f"\n{'motor':>5} {'joint':<14} {'params':>6} {'mode':>4} {'temp':>6} {'pos':>8} {'range':>17} "
              f"{'vbus':>7}  faults")
        vbus_seen = []
        for m, j in zip(arm.motors, arm.config.joints):
            fb = m.feedback
            st = m.query_fault(0.2)
            vbus = "-"
            if m.params_supported:
                try:
                    v = m.read_param(VBUS, "f", 0.2)
                    vbus_seen.append(v)
                    vbus = f"{v:.1f}V"
                except Exception:  # noqa: BLE001
                    vbus = "t/o"
            pos = math.degrees(arm.get_joint_positions()[arm.motors.index(m)])
            rng = f"[{math.degrees(j.lo):+.0f}, {math.degrees(j.hi):+.0f}]"
            flags = ("F" if fb.fault_flag else "") + ("W" if fb.warning_flag else "")
            print(f"{m.motor_id:>5} {j.name:<14} {str(m.params_supported):>6} {fb.mode:>4} "
                  f"{fb.temperature:>5.0f}C {pos:>+8.1f} {rng:>17} {vbus:>7}  "
                  f"{p.fault_text(st.word)}{' [' + flags + ']' if flags else ''}")
        print()
        if vbus_seen and max(vbus_seen) < 5:
            print("=> Every motor reports ~0 V: NO MOTOR POWER. Check the e-stop (release it), the power supply "
                  "(on, plugged, LED) and any fuse. Power-on will be rejected (mode=0) until this is fixed.")
        hot = [m.motor_id for m in arm.motors if m.feedback.temperature > 70]
        if hot:
            print(f"=> Motor(s) {hot} above 70 C: let them cool before powering on.")
    finally:
        arm.disconnect()


if __name__ == "__main__":
    main()
