"""A tiny virtual world per arm for practising pick & place: a tortilla, a plate, and a grasp rule.

Not a full physics engine: a kinematic "digital twin" that works with the simulator AND the real arm.
  * Forward kinematics is the SDK's own chain (maker_arm/gravity.py, the URDF's joint origins,
    raw motor degrees), plus the URDF's fixed grasp_center frame, 17 cm down the gripper.
  * The tortilla is soft and lies flat, so it can't be pinched directly. Press its edge with the
    OPEN gripper and push toward the centre: the edge buckles into a fold. Close the jaws
    (gap < 1.2 cm) on the fold -> it's held and follows the gripper (the 3D view drapes it with
    gravity and lays it on the table where it touches). Pushing past a full fold slides it.
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
BAND = 0.05                                    # the outer band of the tortilla where pressing starts a fold
PRESS_Z = 0.012                                # grasp point this close above the tortilla = pressing it
FOLD_PUSH = 0.03                               # pushing the pressed edge this far inward = full fold
FOLD_MIN = 0.6                                 # fold needed before it can be pinched
FOLD_H = 0.025                                 # height of a full fold
FOLD_REACH = 0.03                              # close the jaws within this distance of the fold

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
    """One arm's tortilla and plate. See the module docstring for the rules."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> str:
        self.pos = (TORTILLA_HOME[0], TORTILLA_HOME[1], TORTILLA_T / 2)
        self.rot = _about_z(0.0)
        self.held = False
        self.rel = None
        self.was_open = True
        self.placed = False
        self.fold = 0.0           # 0 flat .. 1 full fold
        self.fold_at = None       # (x, y) where the edge bunched up
        self.press = None         # while pressing: {"d0": start distance from centre, "xy": last contact}
        self.note = "tortilla on the table: press its edge and push toward the centre to raise a fold"
        return "world reset: tortilla back on the table"

    def _on_plate(self, x, y) -> bool:
        return math.hypot(x - PLATE_POS[0], y - PLATE_POS[1]) <= PLATE_R

    def _top(self) -> float:
        return self.pos[2] + TORTILLA_T / 2

    def _pressing(self, grasp) -> None:
        """The open gripper pressing the edge and pushing it inward -> a fold; past a full fold, a slide."""
        cx, cy = self.pos[0], self.pos[1]
        d = math.hypot(grasp[0] - cx, grasp[1] - cy)
        touching = grasp[2] - self._top() <= PRESS_Z and d <= TORTILLA_R + 0.005
        if not touching:
            self.press = None
            return
        if self.press is None:
            if d < TORTILLA_R - BAND:
                return  # pressing the middle only flattens it
            self.press = {"d0": d, "xy": (grasp[0], grasp[1])}
            if self.fold < 0.05:
                self.note = "pressing the edge: now push toward the centre"
        push = self.press["d0"] - d
        if self.fold >= 1.0 and push > FOLD_PUSH:  # fully bunched: pushing on slides the tortilla
            lx, ly = self.press["xy"]
            ux, uy = (cx - grasp[0]) / max(d, 1e-6), (cy - grasp[1]) / max(d, 1e-6)
            along = (grasp[0] - lx) * ux + (grasp[1] - ly) * uy
            if along > 0:
                self.pos = (cx + ux * along, cy + uy * along, self.pos[2])
                self.note = "fold made; pushing more slides the tortilla"
        elif push > 0:
            amount = min(1.0, push / FOLD_PUSH)
            if amount > self.fold:
                self.fold = amount
                self.note = ("fold ready: close the gripper on it" if amount >= FOLD_MIN
                             else f"fold {amount * 100:.0f}%: keep pushing toward the centre")
        self.fold_at = (grasp[0], grasp[1])
        self.press["xy"] = (grasp[0], grasp[1])

    def _try_grasp(self, grasp, grot) -> None:
        d = math.hypot(grasp[0] - self.pos[0], grasp[1] - self.pos[1])
        if d > TORTILLA_R + 0.02:
            return  # closing in the air: nothing to say
        if self.fold < FOLD_MIN or not self.fold_at:
            self.note = ("too flat to pinch: open, lower onto the edge, press and push ~3 cm toward the "
                         "centre to raise a fold, then close on it")
            return
        df = math.hypot(grasp[0] - self.fold_at[0], grasp[1] - self.fold_at[1])
        dz = grasp[2] - self._top()
        if df > FOLD_REACH or not -0.01 <= dz <= FOLD_H * self.fold + 0.015:
            self.note = f"missed the fold: {df * 100:.0f} cm away, {dz * 100:+.0f} cm above the table"
            return
        gt = _t(grot)
        self.rel = (_g._apply(gt, tuple(p - g for p, g in zip(self.pos, grasp))), _g._mul(gt, self.rot))
        self.held, self.placed, self.press = True, False, None
        self.note = "grasped by the fold!"

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
            self.fold_at = (grasp[0], grasp[1])
            if opened:  # let go: it falls flat onto the table (or the plate) below
                x, y = self.pos[0], self.pos[1]
                self.placed = self._on_plate(x, y)
                z = (PLATE_H if self.placed else 0.0) + TORTILLA_T / 2
                self.pos, self.rot = (x, y, z), _about_z(_yaw(self.rot))
                self.held, self.rel, self.fold, self.fold_at = False, None, 0.0, None
                self.note = "placed on the plate!" if self.placed else "dropped on the table"
        else:
            if not closed:
                self._pressing(grasp)
            if closed and self.was_open:
                self._try_grasp(grasp, grot)
        if closed:
            self.was_open = False
            self.press = None
        elif opened:
            self.was_open = True
        return self.snapshot(grasp, gap)

    def snapshot(self, grasp, gap) -> dict:
        return {
            "tortilla": {"pos": [round(v, 4) for v in self.pos], "rot": [round(v, 4) for r in self.rot for v in r],
                         "r": TORTILLA_R, "t": TORTILLA_T, "held": self.held,
                         "fold": round(self.fold, 3), "fold_h": FOLD_H, "pressing": self.press is not None,
                         "fold_at": [round(v, 4) for v in self.fold_at] if self.fold_at else None},
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
    {"name": "Press the edge (gripper open)", "q": [0, -115, 90, -50, 0, 0, -70]},
    {"name": "Push toward the centre: fold", "q": [0, -125, 105, -55, 0, 0, -70]},
    {"name": "Close on the fold", "q": [0, -125, 105, -55, 0, 0, -3]},
    {"name": "Lift (it hangs)", "q": [0, -95, 100, -45, 0, 0, -3]},
    {"name": "Swing over the plate", "q": [60, -95, 100, -45, 0, 0, -3]},
    {"name": "Lower over the plate", "q": [60, -112, 92, -52, 0, 0, -3]},
    {"name": "Open: let it go", "q": [60, -112, 92, -52, 0, 0, -70]},
    {"name": "Back up", "q": [60, -105, 95, -60, 0, 0, -70]},
]
