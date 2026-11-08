"""A tiny virtual world per arm for practising pick & place: a tortilla, a plate, and a grasp rule.

Not physics: a kinematic "digital twin" that works with the simulator AND the real arm.
  * Forward kinematics is the SDK's own chain (maker_arm/gravity.py, the URDF's joint origins,
    raw motor degrees), plus the URDF's fixed grasp_center frame, 17 cm down the gripper.
  * Grasp: close the gripper (jaw gap < 1.2 cm) with the grasp point on the tortilla's outer
    5 cm band, at most 3 cm above it -> it's held and follows the gripper.
  * Release: open the gripper (gap > 3 cm) -> it drops flat onto the table below.
  * Placed: dropped with its centre within the plate -> "placed on the plate".
World frame: z up, origin under the arm's base, x forward (where the arm reaches), metres.
"""
from __future__ import annotations

import math

from maker_arm import gravity as _g

GRASP_CENTER = (0.0, 0.0, -0.169638704618)   # URDF grasp_center_joint, in link_007
JAW_TRAVEL = 0.0524125                         # per jaw, metres (URDF); total gap = 2x
GRIP_OPEN_DEG = -96.0                          # profile: 0 closed, -96 fully open (gap is a preview)
CLOSED_GAP = 0.012
OPEN_GAP = 0.030
BAND = 0.05                                    # graspable outer band of the tortilla
REACH_Z = 0.03                                 # grasp point at most this far above the tortilla

TORTILLA_R = 0.08
TORTILLA_T = 0.003
TORTILLA_HOME = (0.36, 0.0)
PLATE_R = 0.11
PLATE_H = 0.015
PLATE_POS = (0.18, -0.31)                      # radius 0.36 at -60 deg (J1 = +60 swings the arm there)


def fk(q_deg):
    """(grasp point xyz, gripper rotation 3x3 rows) for six motor angles in degrees."""
    rot, origin = _g._ROOT, (0.0, 0.0, 0.0)
    for (xyz, fixed, axis), q in zip(_g._FIXED, q_deg[:6]):
        origin = tuple(o + d for o, d in zip(origin, _g._apply(rot, xyz)))
        rot = _g._mul(rot, fixed)
        rot = _g._mul(rot, _g._about_z(axis[2] * math.radians(q)))
    grasp = tuple(o + d for o, d in zip(origin, _g._apply(rot, GRASP_CENTER)))
    return grasp, rot


def jaw_gap(gripper_deg: float) -> float:
    if not math.isfinite(gripper_deg):
        return math.nan
    return 2 * JAW_TRAVEL * min(1.0, max(0.0, gripper_deg / GRIP_OPEN_DEG))


def _t(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _yaw(r):
    return math.atan2(r[1][0], r[0][0])


def _about_z(a):
    return _g._about_z(a)


class World:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> str:
        self.pos = (TORTILLA_HOME[0], TORTILLA_HOME[1], TORTILLA_T / 2)
        self.rot = _about_z(0.0)
        self.held = False
        self.rel = None
        self.was_open = True
        self.placed = False
        self.note = "tortilla on the table"
        return "world reset: tortilla back on the table"

    def _on_plate(self, x, y) -> bool:
        return math.hypot(x - PLATE_POS[0], y - PLATE_POS[1]) <= PLATE_R

    def update(self, joints_deg) -> dict:
        q = list(joints_deg or [])
        if len(q) < 7 or not all(math.isfinite(v) for v in q[:7]):
            return self.snapshot(None, math.nan)
        grasp, grot = fk(q)
        gap = jaw_gap(q[6])
        closed, opened = gap < CLOSED_GAP, gap > OPEN_GAP
        if self.held:
            rp, rr = self.rel
            self.pos = tuple(g + d for g, d in zip(grasp, _g._apply(grot, rp)))
            self.rot = _g._mul(grot, rr)
            if opened:  # let go: it falls flat onto the table (or the plate) below
                x, y = self.pos[0], self.pos[1]
                self.placed = self._on_plate(x, y)
                z = (PLATE_H if self.placed else 0.0) + TORTILLA_T / 2
                self.pos, self.rot = (x, y, z), _about_z(_yaw(self.rot))
                self.held, self.rel = False, None
                self.note = "placed on the plate!" if self.placed else "dropped on the table"
        elif closed and self.was_open:
            d = math.hypot(grasp[0] - self.pos[0], grasp[1] - self.pos[1])
            dz = grasp[2] - self.pos[2]
            if TORTILLA_R - BAND <= d <= TORTILLA_R + 0.01 and -0.01 <= dz <= REACH_Z:
                gt = _t(grot)
                self.rel = (_g._apply(gt, tuple(p - g for p, g in zip(self.pos, grasp))), _g._mul(gt, self.rot))
                self.held, self.placed = True, False
                self.note = "grasped!"
            else:
                self.note = (f"missed: grasp point {d * 100:.0f} cm from the centre (edge band "
                             f"{(TORTILLA_R - BAND) * 100:.0f}-{(TORTILLA_R + 0.01) * 100:.0f} cm), "
                             f"{dz * 100:+.0f} cm above it (max {REACH_Z * 100:.0f})")
        if closed:
            self.was_open = False
        elif opened:
            self.was_open = True
        return self.snapshot(grasp, gap)

    def snapshot(self, grasp, gap) -> dict:
        return {
            "tortilla": {"pos": [round(v, 4) for v in self.pos], "rot": [round(v, 4) for r in self.rot for v in r],
                         "r": TORTILLA_R, "t": TORTILLA_T, "held": self.held},
            "plate": {"pos": [PLATE_POS[0], PLATE_POS[1], 0.0], "r": PLATE_R, "h": PLATE_H},
            "grasp": [round(v, 4) for v in grasp] if grasp else None,
            "gap_mm": None if not math.isfinite(gap) else round(gap * 1000, 1),
            "placed": self.placed,
            "note": self.note,
        }


# A pick & place sequence that works with the defaults above (checked with World in the simulator).
# J1..J7 in motor degrees; J7 gripper: -70 open, -3 closed.
DEMO = [
    {"name": "Ready above the tortilla edge", "q": [0, -105, 95, -60, 0, 0, -70]},
    {"name": "Down to the edge, gripper open", "q": [0, -115, 90, -50, 0, 0, -70]},
    {"name": "Close the gripper", "q": [0, -115, 90, -50, 0, 0, -3]},
    {"name": "Lift", "q": [0, -95, 100, -45, 0, 0, -3]},
    {"name": "Swing over the plate", "q": [60, -95, 100, -45, 0, 0, -3]},
    {"name": "Lower over the plate", "q": [60, -112, 92, -52, 0, 0, -3]},
    {"name": "Open: let it go", "q": [60, -112, 92, -52, 0, 0, -70]},
    {"name": "Back up", "q": [60, -105, 95, -60, 0, 0, -70]},
]
