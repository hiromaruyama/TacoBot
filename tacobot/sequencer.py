"""Walk Dream-1 through the quesadilla, one step at a time, with a human in charge of every start.

    cd upstream && uv run python ../tacobot/sequencer.py            # needs robot + dream-1-tasks running
    python tacobot/sequencer.py --dry-run                           # just print the plan

It is a plain LiveKit participant (like upstream's voice agent): it never sends actions, it only calls the
`dream.*` RPCs on `uv run dream-1-tasks` and the robot's `home` RPC. Upstream's rules hold:
a new task never starts motion — every step waits for you to press Enter before `dream.resume`.

Keys while a step runs: Enter = done, r = retry, t = hand to leader arms, q = stop (Dream-1 releases control).
Parking / emergency: use `p` in the robot or teleop terminal, or take over with the leaders. This tool is not an e-stop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

HERE = Path(__file__).resolve().parent
UPSTREAM_SRC = HERE.parent / "upstream" / "src"
IDENTITY = "tacobot-sequencer"

# Mirrors upstream src/utils/common.py; checked against it by tests when the submodule is present.
DREAM_TASKS_IDENTITY = "policy-dream-1-tasks"
ROBOT_IDENTITY = "robot"
SET_TASK, PAUSE, RESUME, STATUS, TO_TELEOP = "dream.set_task", "dream.pause", "dream.resume", "dream.status", \
    "dream.to_teleop"
HOME = "home"


@dataclass(frozen=True)
class Step:
    id: str
    prompt: str | None = None
    wait_s: float | None = None
    home: bool = False

    def describe(self) -> str:
        if self.prompt:
            return f"Dream-1: {self.prompt}"
        if self.wait_s is not None:
            return f"wait {self.wait_s:g}s (arms hold)"
        return "arms home (powered, holding)"


def load_steps(path: Path = HERE / "tasks.toml") -> list[Step]:
    steps = [Step(**s) for s in tomllib.loads(path.read_text())["step"]]
    for s in steps:
        kinds = sum([s.prompt is not None, s.wait_s is not None, s.home])
        if kinds != 1:
            raise ValueError(f"step {s.id!r} must have exactly one of prompt / wait_s / home")
        if s.prompt is not None and not 0 < len(s.prompt) <= 200:
            raise ValueError(f"step {s.id!r}: prompt must be 1-200 characters")
    if len({s.id for s in steps}) != len(steps):
        raise ValueError("step ids must be unique")
    return steps


class Link(Protocol):
    async def call(self, identity: str, method: str, payload: str = "") -> str: ...


Ask = Callable[[str], Awaitable[str]]


async def run(steps: list[Step], link: Link, ask: Ask, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
              start_at: str | None = None) -> list[str]:
    """Run the steps. Returns the ids completed. Dream-1 is always paused when this returns."""
    done: list[str] = []
    ids = [s.id for s in steps]
    todo = steps[ids.index(start_at):] if start_at else steps

    async def dream(method: str, **payload) -> dict:
        return json.loads(await link.call(DREAM_TASKS_IDENTITY, method, json.dumps(payload)))

    try:
        for n, step in enumerate(todo, start=1):
            print(f"\n[{n}/{len(todo)}] {step.id}: {step.describe()}")
            if step.wait_s is not None:
                await dream(PAUSE)
                await sleep(step.wait_s)
            elif step.home:
                print("  robot:", await link.call(ROBOT_IDENTITY, HOME, ""))
            else:
                await dream(SET_TASK, task=step.prompt)  # pauses Dream-1 first if it drives
                while True:
                    key = (await ask("  Enter = start (q = stop) > ")).strip().lower()
                    if key == "q":
                        return done
                    await dream(RESUME)
                    key = (await ask("  running… Enter = done, r = retry, t = teleop, q = stop > ")).strip().lower()
                    await dream(PAUSE)
                    if key == "r":
                        continue
                    if key == "t":
                        await dream(TO_TELEOP)
                        await ask("  leaders have the arms. Enter when the step is done > ")
                        await dream(PAUSE)
                    if key == "q":
                        return done
                    break
            done.append(step.id)
        print("\n🌮 Quesadilla done.")
        return done
    finally:
        try:
            await dream(PAUSE)
        except Exception as exc:  # noqa: BLE001 — report, the arms already hold
            print(f"  (could not confirm pause: {exc}) — press c in the dream-1-tasks terminal if it still drives")


class RoomLink:
    """Joins the Portal room as a plain participant, using upstream's .env and token helper."""

    def __init__(self) -> None:
        self.room = None

    async def connect(self) -> None:
        sys.path.insert(0, str(UPSTREAM_SRC))
        from livekit import rtc
        from utils.common import load_env, mint_token, required_env

        load_env()
        self.room = rtc.Room()
        token = mint_token(IDENTITY, required_env("LIVEKIT_ROOM"))
        await self.room.connect(required_env("LIVEKIT_URL"), token)

    async def call(self, identity: str, method: str, payload: str = "") -> str:
        if identity not in self.room.remote_participants:
            raise RuntimeError(f"{identity} is not in the room — start it first (see README)")
        return await self.room.local_participant.perform_rpc(
            destination_identity=identity, method=method, payload=payload, response_timeout=5.0)

    async def close(self) -> None:
        if self.room:
            await self.room.disconnect()


async def _main(args) -> int:
    steps = load_steps(Path(args.tasks))
    if args.dry_run:
        for n, s in enumerate(steps, start=1):
            print(f"{n:2}. {s.id:12} {s.describe()}")
        return 0
    link = RoomLink()
    await link.connect()
    try:
        await run(steps, link, ask=lambda p: asyncio.to_thread(input, p), start_at=args.start)
    finally:
        await link.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=str(HERE / "tasks.toml"))
    ap.add_argument("--start", help="step id to start from")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, connect to nothing")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
