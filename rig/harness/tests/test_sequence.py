import asyncio
from pathlib import Path

import pytest

from harness.sequence import Sequence, Step, load_steps

EXAMPLE = Path(__file__).resolve().parents[1] / "harness" / "example_tasks.toml"


class FakeRig:
    def __init__(self):
        self.calls = []

    async def set_task(self, task):
        self.calls.append(("set_task", task))
        return {}

    async def pause(self):
        self.calls.append(("pause",))
        return {}

    async def resume(self):
        self.calls.append(("resume",))
        return {}

    async def to_teleop(self):
        self.calls.append(("to_teleop",))
        return {}

    async def home(self):
        self.calls.append(("home",))
        return "returning the arms home"

    def event(self, kind, text, level="info"):
        pass


def test_example_loads():
    steps = load_steps(EXAMPLE)
    assert steps[0].id == "tortilla" and steps[-1].home


async def test_arming_never_starts_motion():
    rig = FakeRig()
    seq = Sequence(rig, [Step("a", prompt="do a"), Step("b", prompt="do b")])
    await seq.arm(0)
    assert rig.calls == [("set_task", "do a")] and seq.phase == "armed"
    await seq.start()
    assert rig.calls[-1] == ("resume",) and seq.phase == "running"
    await seq.done()
    # done pauses, then arms the next step (task set), but never resumes on its own
    assert rig.calls[-2:] == [("pause",), ("set_task", "do b")]
    assert seq.phase == "armed" and seq.index == 1


async def test_retry_teleop_and_finish():
    rig = FakeRig()
    seq = Sequence(rig, [Step("a", prompt="do a"), Step("h", home=True)])
    await seq.start()  # from idle: arms then starts
    await seq.retry()
    assert seq.phase == "armed" and rig.calls[-1] == ("pause",)
    await seq.start()
    await seq.teleop()
    assert seq.phase == "teleop"
    await seq.done()
    assert seq.index == 1 and seq.phase == "armed"
    await seq.start()  # home step runs and finishes the sequence
    assert ("home",) in rig.calls and seq.phase == "finished" and seq.completed == ["a", "h"]


async def test_wait_step_counts_down_and_advances():
    rig = FakeRig()
    seq = Sequence(rig, [Step("w", wait_s=0.05), Step("b", prompt="do b")])
    await seq.start()
    assert seq.phase == "waiting"
    await asyncio.sleep(0.15)
    assert seq.index == 1 and seq.phase == "armed" and rig.calls[-1] == ("set_task", "do b")


async def test_cannot_start_twice():
    seq = Sequence(FakeRig(), [Step("a", prompt="do a")])
    await seq.start()
    with pytest.raises(ValueError):
        await seq.start()


def test_bad_steps(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[[step]]\nid = "x"\nprompt = "a"\nwait_s = 1\n')
    with pytest.raises(ValueError):
        load_steps(p)
