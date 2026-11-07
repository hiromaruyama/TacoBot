"""Simulated Maker Arm motors, so every script can be tried with --sim before the arm is free.

Adapted from the SDK's own test fakes (vendor/maker-arm-sdk/tests/fakes.py): each fake
RobStride motor answers the MIT protocol and jumps to whatever position it is commanded.
"""
from __future__ import annotations

import struct

from maker_arm import protocol as p
from maker_arm.config import ArmConfig
from maker_arm.transport.mock import MockBackend

_PREFIX = bytes([0xFF] * 6)


def _feedback(motor_id: int, pos: float, params: p.MotorParams, enabled: bool, temp: float = 30.0) -> bytes:
    q = p.float_to_uint(pos, params.p_min, params.p_max, 16)
    v = p.float_to_uint(0.0, params.v_min, params.v_max, 12)
    t = p.float_to_uint(0.0, params.t_min, params.t_max, 12)
    temp10 = int(round(temp * 10))
    status = ((temp10 >> 8) & 0x0F)  # legacy firmware: mode bits read 0, no fault/warn
    return bytes([motor_id, q >> 8, q & 0xFF, v >> 4, ((v & 0xF) << 4) | (t >> 8), t & 0xFF,
                  status, temp10 & 0xFF])


class _SimMotor:
    def __init__(self, motor_id: int, params: p.MotorParams, position: float):
        self.motor_id, self.params, self.position, self.enabled = motor_id, params, position, False

    def reply(self):
        return p.HOST_CAN_ID, _feedback(self.motor_id, self.position, self.params, self.enabled)

    def handle(self, can_id: int, data: bytes):
        kind = can_id & 0x700
        if kind in (0x300, 0x400):          # parameter access: act like old firmware (no reply)
            return []
        if data[:6] == _PREFIX:             # special commands
            arg, op = data[6], data[7]
            if op == p.OP_CLEAR_FAULT and arg == 0x00:   # fault query: no fault
                return [(p.HOST_CAN_ID, bytes([self.motor_id]) + struct.pack("<I", 0))]
            if op == p.OP_ENABLE:
                self.enabled = True
            elif op == p.OP_DISABLE:
                self.enabled = False
            elif op == p.OP_SET_ZERO:
                self.position = 0.0
            return [self.reply()]
        # MIT position command: follow it when torque is on and kp > 0
        q = (data[0] << 8) | data[1]
        kp = ((data[3] & 0xF) << 8 | data[4]) * p.KP_MAX / 4095
        if self.enabled and kp > 0:
            self.position = p.uint_to_float(q, self.params.p_min, self.params.p_max, 16)
        return [self.reply()]


class SimBus(MockBackend):
    """A CAN bus with one simulated motor per joint, starting mid-range."""

    def __init__(self, cfg: ArmConfig):
        super().__init__()
        self.motors = {}
        import math
        import os
        lost_zero = os.environ.get("SIM_GRIPPER_DEG")  # e.g. 82.7: simulate a gripper that lost its zero
        for j in cfg.joints:
            mid_joint = (j.lo + j.hi) / 2
            if lost_zero and j.motor_id == 7:
                mid_joint = math.radians(float(lost_zero))
            motor_pos = mid_joint * j.direction + j.offset
            self.motors[j.motor_id] = _SimMotor(j.motor_id, p.MOTOR_PARAMS[j.model], motor_pos)
        self.responder = self._respond

    def _respond(self, can_id, data):
        m = self.motors.get(can_id & 0x7F)
        return m.handle(can_id, data) if m else []
