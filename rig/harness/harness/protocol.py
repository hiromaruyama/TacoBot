"""Names the server speaks. Mirrors portal-makermods `src/utils/common.py` (dc/maker_arm); keep them in sync.

The harness joins the Portal room as an *observer*: it sees the robot's three cameras, its state and the active
operator's actions, but it can never send an action. It steers the rig only through the RPCs below, the same ones
TacoBot's sequencer and upstream's voice agent use.
"""
from __future__ import annotations

HARNESS_IDENTITY = "harness"

ROBOT_IDENTITY = "robot"
TELEOPERATOR_IDENTITY = "teleoperator"
DREAM_TASKS_IDENTITY = "policy-dream-1-tasks"

# Robot RPCs
PARK_RPC = "park"
PARK_ONLY = "park"  # payload that only parks (never powers up). The harness never sends the toggle "".
HOME_RPC = "home"  # walk powered arms back to rest and hold; releases control; never parks or powers up

# dream-1-tasks RPCs: each takes JSON, answers with the status JSON
DREAM_STATUS_RPC = "dream.status"
DREAM_SET_TASK_RPC = "dream.set_task"  # {"task": "..."}; pauses first if Dream-1 drives; never starts motion
DREAM_PAUSE_RPC = "dream.pause"
DREAM_RESUME_RPC = "dream.resume"  # Dream-1 takes control: new episode on the current task
DREAM_TO_TELEOP_RPC = "dream.to_teleop"
DREAM_TO_HAND_TELEOP_RPC = "dream.to_hand_teleop"
DREAM_STOP_HAND_TELEOP_RPC = "dream.stop_hand_teleop"

MAX_TASK_CHARS = 200

CAMERAS = ("overhead_camera", "left_wrist_camera", "right_wrist_camera")
SIDES = ("left", "right")
JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "gripper")
