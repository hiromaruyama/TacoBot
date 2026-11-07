"""Step through a multi-step job (e.g. TacoBot's quesadilla) with a human starting every motion.

Reads the same `tasks.toml` as TacoBot's `tacobot/sequencer.py`:

    [[step]]  id = "tortilla"  prompt = "pick up the tortilla ..."   # Dream-1 does it
    [[step]]  id = "melt"      wait_s = 20                            # nobody moves; arms hold
    [[step]]  id = "home"      home = true                            # robot walks the arms back to rest

Same rules as the TacoBot sequencer: arming a prompt step only sets the task (which pauses Dream-1); motion
starts only on an explicit `start`. Unlike the terminal sequencer it is driven by buttons / HTTP calls, so the
web UI, the CLI and scripts share one run.
"""
from __future__ import annotations

import asyncio
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .protocol import MAX_TASK_CHARS


@dataclass(frozen=True)
class Step:
    id: str
    prompt: str | None = None
    wait_s: float | None = None
    home: bool = False

    @property
    def kind(self) -> str:
        return "prompt" if self.prompt else "wait" if self.wait_s is not None else "home"


def load_steps(path: Path) -> list[Step]:
    steps = [Step(**s) for s in tomllib.loads(Path(path).read_text())["step"]]
    for s in steps:
        if sum([s.prompt is not None, s.wait_s is not None, s.home]) != 1:
            raise ValueError(f"step {s.id!r} must have exactly one of prompt / wait_s / home")
        if s.prompt is not None and not 0 < len(s.prompt) <= MAX_TASK_CHARS:
            raise ValueError(f"step {s.id!r}: prompt must be 1-{MAX_TASK_CHARS} characters")
    if len({s.id for s in steps}) != len(steps):
        raise ValueError("step ids must be unique")
    return steps


class Rig(Protocol):
    async def set_task(self, task: str) -> dict: ...
    async def pause(self) -> dict: ...
    async def resume(self) -> dict: ...
    async def to_teleop(self) -> dict: ...
    async def home(self) -> str: ...
    def event(self, kind: str, text: str, level: str = "info") -> None: ...


# phases: idle → armed → running / waiting / teleop → (done) armed on the next step … → finished
class Sequence:
    def __init__(self, rig: Rig, steps: list[Step], source: str = "") -> None:
        self.rig, self.steps, self.source = rig, steps, source
        self.index = 0
        self.phase = "idle"
        self.completed: list[str] = []
        self.wait_until: float | None = None
        self._timer: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def step(self) -> Step | None:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    def snapshot(self) -> dict:
        return {"source": self.source, "index": self.index, "phase": self.phase, "completed": self.completed,
                "wait_left_s": round(max(0.0, self.wait_until - time.monotonic()), 1) if self.wait_until else None,
                "steps": [dict(asdict(s), kind=s.kind) for s in self.steps]}

    def _log(self, text: str) -> None:
        self.rig.event("sequence", text)

    async def arm(self, index: int | None = None) -> None:
        """Get a step ready. A prompt step's task is set (Dream-1 pauses); nothing moves until `start`."""
        async with self._lock:
            await self._arm(self.index if index is None else index)

    async def _arm(self, index: int) -> None:
        self._cancel_timer()
        if not 0 <= index < len(self.steps):
            raise ValueError(f"no step {index}")
        self.index = index
        step = self.steps[index]
        if step.kind == "prompt":
            await self.rig.set_task(step.prompt)
        elif step.kind == "wait":
            await self.rig.pause()
        self.phase = "armed"
        self._log(f"[{index + 1}/{len(self.steps)}] {step.id} armed ({step.kind}); start when ready")

    async def start(self) -> None:
        async with self._lock:
            step = self.step
            if step is None or self.phase not in ("armed", "idle"):
                raise ValueError(f"cannot start in phase {self.phase!r}")
            if self.phase == "idle":
                await self._arm(self.index)
            if step.kind == "prompt":
                await self.rig.resume()
                self.phase = "running"
                self._log(f"{step.id}: Dream-1 running")
            elif step.kind == "wait":
                self.phase, self.wait_until = "waiting", time.monotonic() + float(step.wait_s)
                self._timer = asyncio.create_task(self._wait(step.wait_s))
                self._log(f"{step.id}: waiting {step.wait_s:g}s, arms hold")
            else:
                self._log(f"{step.id}: robot → {await self.rig.home()}")
                await self._advance()

    async def _wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        async with self._lock:
            self._timer = None
            await self._advance()

    async def done(self) -> None:
        """The current step is finished (Dream-1 or the leaders did it): pause and arm the next."""
        async with self._lock:
            if self.phase in ("running", "teleop"):
                await self.rig.pause()
            await self._advance()

    async def retry(self) -> None:
        async with self._lock:
            await self.rig.pause()
            self._cancel_timer()
            self.phase = "armed"
            self._log(f"{self.step.id}: retry; start when ready")

    async def teleop(self) -> None:
        async with self._lock:
            if self.phase != "running":
                raise ValueError("hand to the leaders while a step runs")
            await self.rig.to_teleop()
            self.phase = "teleop"
            self._log(f"{self.step.id}: leaders have the arms; press done when the step is finished")

    async def stop(self) -> None:
        async with self._lock:
            self._cancel_timer()
            self.phase = "idle"
            try:
                await self.rig.pause()
            finally:
                self._log("sequence stopped; Dream-1 paused")

    async def _advance(self) -> None:
        self._cancel_timer()
        if self.step is not None:
            self.completed.append(self.step.id)
        if self.index + 1 >= len(self.steps):
            self.index, self.phase = len(self.steps), "finished"
            self._log("sequence finished")
            return
        await self._arm(self.index + 1)

    def _cancel_timer(self) -> None:
        self.wait_until = None
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None
