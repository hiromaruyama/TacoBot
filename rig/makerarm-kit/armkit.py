"""Shared helpers: open the Maker Arm (real over USB-CAN, or a simulator), env, LiveKit tokens.

Every script imports from here so the safety numbers live in one place.
"""
from __future__ import annotations

import math
import os
import pathlib
from dataclasses import replace

from dotenv import load_dotenv

from maker_arm.arm import Arm, ArmState
from maker_arm.config import ArmConfig
from maker_arm.profiles import DEFAULT_ARM_CONFIG
from maker_arm.transport.base import create_backend

ROOT = pathlib.Path(__file__).resolve().parent

# ── Safety limits for a first session. Raise them only once you trust the setup. ──
DEFAULT_SPEED_DEG_S = 15.0   # how fast the SDK ramps toward a target
MAX_SPEED_DEG_S = 30.0       # the SDK examples' own ceiling
MAX_STEP_DEG = 10.0          # largest single jog a remote command may ask for
LIMIT_MARGIN_DEG = 5.0       # stay this far inside each joint's soft limits
MAX_POWER_ON_TEMP_C = 70.0   # refuse to switch torque on while any motor is hotter than this

JOINT_HELP = {
    1: "shoulder_pan  (base, turns left/right)",
    2: "shoulder_lift (raises the upper arm)",
    3: "elbow_flex",
    4: "wrist_flex",
    5: "wrist_yaw",
    6: "wrist_roll",
    7: "gripper       (open/close)",
}


def load_env() -> None:
    for name, override in ((".env", False), (".env.local", True)):
        if (path := ROOT / name).exists():
            load_dotenv(path, override=override)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


# ── Finding the USB-CAN adapter on Windows ──
def find_can_port() -> str:
    """Return the COM port of the USB-CAN adapter (CANable/SLCAN), or explain what to pass."""
    from serial.tools import list_ports

    usb = [p for p in list_ports.comports()
           if "BTHENUM" not in (p.hwid or "").upper() and "bluetooth" not in (p.description or "").lower()]
    from candle_backend import list_candle_devices
    candles = list_candle_devices()
    if len(usb) + len(candles) == 1:
        port = usb[0].device if usb else candles[0]
        print(f"using USB-CAN adapter {port}")
        return port
    if not usb and not candles:
        raise SystemExit("no USB-CAN adapter found. Plug the arm's USB-CAN adapter into this laptop, "
                         "or run with --sim to try everything without hardware.")
    listing = "\n".join([f"  {p.device}: {p.description}" for p in usb] + [f"  {c}: candleLight" for c in candles])
    raise SystemExit(f"several USB-CAN adapters found; pick one with --port:\n{listing}")


def list_ports_text() -> str:
    from serial.tools import list_ports
    from candle_backend import list_candle_devices
    rows = [f"  {p.device}: {p.description}  [{p.hwid}]" for p in list_ports.comports()]
    rows += [f"  {c}: candleLight USB-to-CAN adapter (use '{c}' as the port)" for c in list_candle_devices()]
    return "\n".join(rows) or "  (none)"


# ── Opening the arm ──
def _config(speed_deg_s: float, rate_hz: float | None) -> ArmConfig:
    if not 0 < speed_deg_s <= MAX_SPEED_DEG_S:
        raise SystemExit(f"speed must be in (0, {MAX_SPEED_DEG_S:g}] deg/s")
    cfg = ArmConfig.from_yaml(str(DEFAULT_ARM_CONFIG))
    cfg = replace(
        cfg,
        control_rate_hz=rate_hz or cfg.control_rate_hz,
        max_velocity=math.radians(speed_deg_s),
    )
    cfg._validate()
    return cfg


def open_arm(port: str | None = None, sim: bool = False, speed_deg_s: float = DEFAULT_SPEED_DEG_S) -> Arm:
    """Build an Arm (not yet connected). `sim=True` uses simulated motors, no hardware."""
    if sim:
        from sim import SimBus
        cfg = _config(speed_deg_s, rate_hz=None)
        print("SIMULATOR: no hardware is touched")
        return Arm(cfg, SimBus(cfg))
    port = port or os.environ.get("ARM_PORT") or find_can_port()
    from candle_backend import CandleBackend, candle_index, is_candle_port
    if is_candle_port(port):
        # candleLight (gs_usb) over USB: buffered, so a moderate loop rate is safe.
        return Arm(_config(speed_deg_s, rate_hz=50.0), CandleBackend(index=candle_index(port)))
    # Serial SLCAN is verified at 25 Hz in the SDK (see its README).
    backend = create_backend("slcan", port=port, baudrate=115200, rtscts=False, startup_delay=2.5)
    return Arm(_config(speed_deg_s, rate_hz=25.0), backend)


# ── Joint helpers (the SDK works in radians; humans in degrees) ──
def safe_range_deg(arm: Arm, joint: int) -> tuple[float, float]:
    """Soft limits of a 1-based joint, shrunk by LIMIT_MARGIN_DEG, in degrees."""
    j = arm.config.joints[joint - 1]
    margin = math.radians(LIMIT_MARGIN_DEG) + arm.config.limit_margin
    return math.degrees(j.lo + margin), math.degrees(j.hi - margin)


def positions_deg(arm: Arm) -> list[float]:
    return [math.degrees(x) for x in arm.get_joint_positions()]


def format_joints(arm: Arm) -> str:
    return "  ".join(f"J{i + 1}={x:+7.1f}°" for i, x in enumerate(positions_deg(arm)))


def too_hot(arm: Arm) -> str | None:
    """A reason to refuse power-on if any motor is above MAX_POWER_ON_TEMP_C, else None."""
    hot = [f"J{i + 1} {t:.0f}°C" for i, t in enumerate(arm.get_temperatures())
           if math.isfinite(t) and t > MAX_POWER_ON_TEMP_C]
    if hot:
        return (f"too hot to power on ({', '.join(hot)}; limit {MAX_POWER_ON_TEMP_C:.0f}°C). "
                "Let the arm cool down for a few minutes.")
    return None


def is_on(arm: Arm) -> bool:
    return arm.state is ArmState.ENABLED


def safe_shutdown(arm: Arm) -> None:
    """Return to the pose captured at enable, then release torque (the SDK's verified stop).

    If the return cannot be verified the SDK keeps holding and asks you to type RELEASE.
    """
    from maker_arm.cli.safety import release_if_holding
    for step in (lambda: release_if_holding(arm), arm.disconnect):
        try:
            step()
        except Exception as exc:  # keep going: the transport must still close
            print(f"cleanup step failed (continuing): {exc}")
