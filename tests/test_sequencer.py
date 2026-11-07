import asyncio
import json
import re
from pathlib import Path

import pytest

from tacobot import sequencer as seq

ROOT = Path(__file__).resolve().parents[1]


class FakeLink:
    def __init__(self):
        self.calls = []
        self.driving = False

    async def call(self, identity, method, payload=""):
        self.calls.append((identity, method, json.loads(payload) if payload.startswith("{") else payload))
        if method == seq.RESUME:
            self.driving = True
        if method in (seq.PAUSE, seq.SET_TASK, seq.TO_TELEOP):
            self.driving = False
        return json.dumps({"driving": self.driving}) if identity == seq.DREAM_TASKS_IDENTITY else "returning home"


def script(*answers):
    it = iter(answers)

    async def ask(_prompt):
        return next(it)

    return ask


async def nosleep(_s):
    return None


def methods(link):
    return [m for _, m, _ in link.calls]


def test_tasks_file_is_valid():
    steps = seq.load_steps()
    assert steps[0].id == "tortilla" and steps[-1].home


def test_full_run_every_step_needs_a_human_start():
    steps = seq.load_steps()
    n_prompts = sum(1 for s in steps if s.prompt)
    link = FakeLink()
    done = asyncio.run(seq.run(steps, link, script(*["", ""] * n_prompts), nosleep))
    assert done == [s.id for s in steps]
    assert methods(link).count(seq.RESUME) == n_prompts
    assert not link.driving
    # a task is always set (which pauses) before it is resumed
    m = methods(link)
    for i, x in enumerate(m):
        if x == seq.RESUME:
            assert seq.SET_TASK in m[:i]


def test_quit_before_start_never_resumes():
    link = FakeLink()
    done = asyncio.run(seq.run(seq.load_steps(), link, script("q"), nosleep))
    assert done == [] and seq.RESUME not in methods(link)
    assert methods(link)[-1] == seq.PAUSE


def test_retry_and_teleop():
    steps = [seq.Step("a", prompt="do a")]
    link = FakeLink()
    asyncio.run(seq.run(steps, link, script("", "r", "", "t", ""), nosleep))
    assert methods(link).count(seq.RESUME) == 2 and seq.TO_TELEOP in methods(link)


def test_bad_step_rejected(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[[step]]\nid = "x"\nprompt = "a"\nwait_s = 3\n')
    with pytest.raises(ValueError):
        seq.load_steps(p)


def test_rpc_names_match_upstream():
    common = ROOT / "upstream/src/utils/common.py"
    if not common.exists():
        pytest.skip("submodule not checked out")
    src = common.read_text()
    for const, value in [("DREAM_TASKS_IDENTITY", seq.DREAM_TASKS_IDENTITY), ("DREAM_SET_TASK_RPC", seq.SET_TASK),
                         ("DREAM_PAUSE_RPC", seq.PAUSE), ("DREAM_RESUME_RPC", seq.RESUME),
                         ("DREAM_TO_TELEOP_RPC", seq.TO_TELEOP), ("HOME_RPC", seq.HOME),
                         ("ROBOT_IDENTITY", seq.ROBOT_IDENTITY)]:
        assert re.search(rf'^{const} = "{re.escape(value)}"', src, re.M), const
