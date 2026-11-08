"""Astra+Dream: Astra plans the quesadilla from the camera, Dream-1 executes each step.

This is the Dream baseline (tacobot/sequencer.py) with Astra replacing the static tasks.toml:

- **Dream baseline** (`make cook`): a fixed list of prompts (tacobot/tasks.toml) -> Dream-1, human-gated.
- **Astra+Dream** (`make cook-astra`, this file): Astra (NVIDIA NIM) looks at the overhead camera's
  first frame and plans the ordered steps *and* a contact point per step; each step's prompt plus the
  overhead image with that contact marker drawn on it goes to Dream-1 (via `dream.set_markers` +
  `dream.set_task` + `dream.resume`); Astra then verifies the step from a fresh frame before the next.

Run it after you've checked the Dream baseline:

    cd upstream && uv run python ../tacobot/astra_cook.py --goal "make a quesadilla"   # robot + dream-1-tasks running
    cd upstream && uv run python ../tacobot/astra_cook.py --dry-run                    # just plan from the room
    python tacobot/astra_cook.py --dry-run --image scene.jpg                           # plan from a saved image

Like the baseline it is a plain LiveKit participant: it never sends actions, only the `dream.*` RPCs
on `uv run dream-1-tasks`. A new task never starts motion -- each step waits for Enter before resume
(unless --auto). Parking / e-stop is still `p` in the robot or teleop terminal; this is not an e-stop.

The Astra+Dream brain lives in upstream's src/scene_planner.py (the `scene-planner` director) and the
`dream.set_markers` RPC in upstream's dream-1-tasks; this file is the TacoBot entry point for it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM_SRC = HERE.parent / "upstream" / "src"
sys.path.insert(0, str(UPSTREAM_SRC))

DEFAULT_GOAL = "make a quesadilla"


async def _main(args: argparse.Namespace) -> int:
    import scene_planner  # from upstream/src; the Astra+Dream director

    scene_planner.load_env()

    if args.dry_run:
        if args.image:
            frame = scene_planner.load_image(args.image)
        elif args.camera:
            frame = scene_planner.grab_camera(args.camera)
        else:
            director = scene_planner.Director()
            await director.connect()
            try:
                frame = director.frame
            finally:
                await director.close()
        reasoning, steps, present, missing = await asyncio.to_thread(scene_planner.plan, frame, args.goal)
        scene_planner.emit(reasoning, steps, frame, present, missing)
        return 0

    director = scene_planner.Director()
    await director.connect()
    try:
        await director.cook(args.goal, auto=args.auto,
                            verify_every_s=args.verify_every, step_timeout_s=args.step_timeout)
    finally:
        await director.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--goal", default=DEFAULT_GOAL, help=f"the task goal (default: {DEFAULT_GOAL!r})")
    ap.add_argument("--dry-run", action="store_true", help="just plan and save; do not drive Dream-1")
    ap.add_argument("--image", help="plan from a saved image (with --dry-run)")
    ap.add_argument("--camera", help="grab one frame from this camera device (with --dry-run)")
    ap.add_argument("--auto", action="store_true", help="run steps back to back without an Enter per step")
    ap.add_argument("--verify-every", type=float, default=5.0, help="seconds between Astra verifications")
    ap.add_argument("--step-timeout", type=float, default=120.0, help="seconds before a step is given up on")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
