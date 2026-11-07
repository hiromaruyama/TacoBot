#!/usr/bin/env python3
"""Tortilla dexterity demo: the right Maker Arm runs a safe, bounded choreography while narrating
the Astra-generated tortilla tasks. Shows off coordinated multi-joint motion + the gripper.

This is a *choreography* (relative, soft-limit-clamped joint moves from the pose at enable), not
perception-driven grasping — true task execution needs Dream-1. It reads Astra's task list from
the planner's plan.json if present (../../upstream/plan.json), else uses a default tortilla list.

    ./run.sh tortilla_demo.py --sim                 # no hardware, dry choreography
    ./run.sh tortilla_demo.py --read-only           # motors off: just print the tasks + current pose
    ./run.sh tortilla_demo.py                        # real: right arm on can0 (ARM_PORT), prompts MOVE

Close the web app first (only one program can hold can0). Area clear, hand on the power switch.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

from armkit import (DEFAULT_SPEED_DEG_S, MAX_SPEED_DEG_S, format_joints, load_env, open_arm,
                    positions_deg, safe_range_deg, safe_shutdown, too_hot)
from maker_arm.arm import ArmState

PLAN_PATH = Path(__file__).resolve().parents[2] / "upstream" / "plan.json"
SETTLE_TOL_DEG = 2.5
WARN_C = float(os.environ.get("MAKER_MOTOR_TEMP_WARN_C") or 80.0)
TEMP_NAMES = ["J1", "J2", "J3", "J4", "J5", "J6", "grip"]
DEFAULT_TASKS = [
    "Grasp the tortilla at its edge and lift it off the table.",
    "Move the held tortilla to a clear spot and lay it flat.",
    "Turn the tortilla over.",
    "Fold the tortilla in half and press the crease.",
]

# Choreography: each move is (label, {joint_1based: delta_deg}, gripper) relative to the pose at enable.
# Small, clamped deltas — modest motion that shows dexterity without reaching unknown points.
# gripper: "open" | "close" | None (leave as is).
MOVES = [
    ("approach",      {2: -12.0, 3: +12.0},            None),
    ("grasp",         {},                               "close"),
    ("lift",          {2: +8.0, 3: -8.0},               None),
    ("wrist turn",    {2: +8.0, 3: -8.0, 5: +25.0, 6: +35.0}, None),
    ("wrist back",    {2: +8.0, 3: -8.0, 5: -25.0, 6: -35.0}, None),
    ("place",         {2: -10.0, 3: +10.0},             None),
    ("release",       {},                               "open"),
    ("base sweep",    {1: +20.0},                       None),
    ("base back",     {1: -20.0},                       None),
    ("home",          {},                               "open"),
]


def load_tasks() -> list[str]:
    try:
        data = json.loads(PLAN_PATH.read_text())
        tasks = [s["task"] for s in data.get("steps", []) if s.get("task")]
        if tasks:
            print(f"using Astra tasks from {PLAN_PATH}")
            return tasks
    except (OSError, ValueError, KeyError):
        pass
    print("using default tortilla tasks (run `scene-planner` to generate fresh Astra tasks)")
    return DEFAULT_TASKS


def gripper_rad(arm, which: str) -> float:
    lo, hi = arm.config.joints[6].lo, arm.config.joints[6].hi  # gripper soft limits (rad)
    return (lo if which == "open" else hi)  # maker gripper: open is the lower bound, closed the upper


def settle(arm, targets: list[float], speed_deg_s: float) -> None:
    """Command all-joint targets (rad) and wait for joints 1-6 to arrive (gripper has its own hold)."""
    if not arm.set_joint_targets(targets):
        raise RuntimeError("the arm rejected the target")
    worst0 = max(abs(math.degrees(targets[i]) - positions_deg(arm)[i]) for i in range(6))
    deadline = time.monotonic() + worst0 / max(speed_deg_s, 1.0) + 5.0
    since = None
    while time.monotonic() < deadline:
        if arm.state is ArmState.FAULT:
            raise RuntimeError(f"motor fault: {arm.fault_reason}")
        pos = positions_deg(arm)
        worst = max(abs(math.degrees(targets[i]) - pos[i]) for i in range(6))
        if worst <= SETTLE_TOL_DEG:
            since = since or time.monotonic()
            if time.monotonic() - since >= 0.3:
                return
        else:
            since = None
        time.sleep(0.03)


def run_demo(arm, tasks: list[str], speed_deg_s: float, cycles: int) -> None:
    arm.connect()
    print(f"now: {format_joints(arm)}")
    arm.refresh()
    if reason := too_hot(arm):
        raise SystemExit(f"{reason} Motors were not enabled.")
    print("\nAstra-generated tortilla tasks to demonstrate:")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}. {t}")
    print("\nCHECK: area clear, arm supported/resting, a hand near the power switch.")
    if input("Type MOVE to enable the motors and run the dexterity demo > ").strip().upper() != "MOVE":
        print("cancelled; motors were not enabled")
        return

    arm.enable()
    time.sleep(0.3)
    start = arm.get_commanded_positions()  # rad, pose at enable; all deltas are relative to this
    try:
        for c in range(cycles):
            print(f"\n--- cycle {c + 1}/{cycles} ---")
            for mi, (label, deltas, grip) in enumerate(MOVES):
                task = tasks[mi % len(tasks)]
                print(f"[{label}] Astra task: {task}")
                targets = list(start)
                for j, d in deltas.items():
                    lo, hi = safe_range_deg(arm, j)
                    deg = min(max(math.degrees(start[j - 1]) + d, lo), hi)
                    targets[j - 1] = math.radians(deg)
                if grip:
                    targets[6] = gripper_rad(arm, grip)
                settle(arm, targets, speed_deg_s)
                temps = arm.get_temperatures()
                hot = [f"{TEMP_NAMES[i]}={temps[i]:.0f}C" for i in range(len(temps))
                       if temps[i] == temps[i] and temps[i] >= WARN_C]
                if hot:
                    print(f"  WARNING hot joints: {', '.join(hot)} (auto-stop at the configured limit; "
                          "rest the arm or use cooldown.py to cool)")
                if arm.state is ArmState.FAULT:
                    raise RuntimeError(f"stopped: {arm.fault_reason}")
        print("\ndexterity demo complete.")
    except KeyboardInterrupt:
        print("\ninterrupted; holding position")
        arm.hold_current_position()
    except RuntimeError as exc:
        print(f"\n{exc}; holding position")
        arm.hold_current_position()
    finally:
        safe_shutdown(arm)


def read_only(arm, tasks: list[str]) -> None:
    arm.connect()
    print("connected, torque OFF. Astra tasks for this scene:")
    for i, t in enumerate(tasks, 1):
        print(f"  {i}. {t}")
    print("\nlive pose (move the arm by hand), Ctrl-C to quit:")
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
    ap.add_argument("--port", help="arm port (default ARM_PORT / can0)")
    ap.add_argument("--read-only", action="store_true", help="print tasks + pose, motors off")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED_DEG_S, help=f"deg/s, max {MAX_SPEED_DEG_S:g}")
    ap.add_argument("--cycles", type=int, default=1, help="how many times to run the routine")
    a = ap.parse_args()
    load_env()
    tasks = load_tasks()
    arm = open_arm(port=a.port, sim=a.sim, speed_deg_s=a.speed)
    if a.read_only:
        return read_only(arm, tasks)
    run_demo(arm, tasks, a.speed, max(1, a.cycles))


if __name__ == "__main__":
    main()
