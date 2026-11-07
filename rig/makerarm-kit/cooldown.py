#!/usr/bin/env python3
"""Cool-down / torque toggle for the Maker Arm: watch motor temperatures and turn torque OFF (limp,
so load joints cool) or ON (hold) on demand, so the shoulder/elbow don't overheat while powered.

    ./run.sh cooldown.py          # motors start OFF; keys: [e]=enable/hold  [r]=release/limp  [q]=quit

Only the load-bearing joints (shoulder J2, elbow J3) heat up holding against gravity. Press 'r' to
drop torque so they cool -- SUPPORT the arm first, it is brakeless and will sag. Press 'e' to
re-enable and hold the current pose. Over-temp protection (MAKER_MOTOR_TEMP_STOP_C, default 85 C)
still faults the arm automatically; this tool is the manual lever and a live thermometer.

Close any other program using the arm first (only one can hold can0).
"""
from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty

from armkit import load_env, open_arm, safe_shutdown
from maker_arm.arm import ArmState

WARN_C = float(os.environ.get("MAKER_MOTOR_TEMP_WARN_C") or 80.0)
NAMES = ["J1", "J2", "J3", "J4", "J5", "J6", "grip"]


def _key() -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.read(1) if ready else None


def _temp_line(arm) -> str:
    temps = arm.get_temperatures()
    parts = []
    for i, t in enumerate(temps):
        if t != t:  # nan
            parts.append(f"{NAMES[i]}=--")
        else:
            parts.append(f"{NAMES[i]}={t:.0f}C" + ("!!" if t >= WARN_C else ""))
    return "  ".join(parts)


def main() -> None:
    load_env()
    arm = open_arm()  # right arm on ARM_PORT (can0)
    arm.connect()
    print("connected, torque OFF.")
    print("keys:  e = enable & hold current pose   r = release torque (limp, SUPPORT it)   q = quit")
    print(f"warn at {WARN_C:.0f} C (shown as !!), auto-stop at {os.environ.get('MAKER_MOTOR_TEMP_STOP_C') or 85} C\n")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            if arm.state is ArmState.CONNECTED:
                arm.refresh()
            print(f"\r[{arm.state.name:>10}] {_temp_line(arm)}        ", end="", flush=True)
            k = _key()
            if k == "q":
                break
            if k == "r" and arm.state in (ArmState.ENABLED, ArmState.FAULT):
                print("\nreleasing torque -- SUPPORT THE ARM (it will go limp) ...")
                arm.disable()
            elif k == "e":
                if arm.state is ArmState.FAULT:
                    print("\nclearing faults ...")
                    arm.clear_faults()
                if arm.state is ArmState.CONNECTED:
                    print("\nCHECK: area clear, hand on the power switch.")
                    try:
                        arm.enable()
                        print("enabled, holding the current pose.")
                    except Exception as exc:  # noqa: BLE001
                        print(f"enable failed: {exc}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()
        safe_shutdown(arm)


if __name__ == "__main__":
    main()
