"""Tests for the anti-conflict tool, including a real two-agent scenario against a bare git remote."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import claims
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "claims.py"


def test_pattern_overlap():
    files = ["tacobot/tasks.toml", "rig/SAFETY.md"]
    assert claims.patterns_overlap("tacobot/**", "tacobot/tasks.toml", files)
    assert claims.patterns_overlap("tacobot/*.toml", "tacobot/**", files)
    assert not claims.patterns_overlap("rig/**", "tacobot/**", files)
    assert claims.patterns_overlap("tacobot/new.py", "tacobot/**", files)


def test_lane_of():
    lanes = claims.load_lanes(ROOT)
    assert claims.lane_of("tasks/fold", lanes) == "tasks"
    assert claims.lane_of("random-branch", lanes) is None


def sh(cwd, *args, check=True, stdin=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "TACOBOT_AGENT": cwd.name}
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check, env=env, input=stdin)


def cli(cwd, *args, stdin=None):
    return sh(cwd, sys.executable, str(SCRIPT), *args, check=False, stdin=stdin)


def push_claim(repo, branch, *paths):
    sh(repo, "git", "checkout", "-qb", branch)
    r = cli(repo, "claim", *paths, "--task", branch)
    assert r.returncode == 0, r.stdout + r.stderr
    sh(repo, "git", "add", "coordination/claims")
    sh(repo, "git", "commit", "-qm", "claim")
    sh(repo, "git", "push", "-q", "-u", "origin", "HEAD")


@pytest.fixture
def team(tmp_path):
    """A bare origin and two clones (agent_a, agent_b) of this repo's files, without the submodule."""
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    shutil.copytree(ROOT, seed, ignore=shutil.ignore_patterns(".git", "upstream", "__pycache__", ".pytest_cache",
                                                              ".ruff_cache", ".gitmodules"))
    sh(seed, "git", "init", "-b", "main")
    sh(seed, "git", "add", ".")
    sh(seed, "git", "commit", "-qm", "init")
    sh(seed, "git", "remote", "add", "origin", str(origin))
    sh(seed, "git", "push", "-q", "origin", "main")
    a, b = tmp_path / "agent_a", tmp_path / "agent_b"
    for d in (a, b):
        sh(tmp_path, "git", "clone", "-q", str(origin), str(d))
    return a, b


def test_two_agents_cannot_claim_same_file(team):
    a, b = team
    push_claim(a, "tasks/fold", "tacobot/tasks.toml")
    sh(b, "git", "checkout", "-qb", "tasks/flip")
    r = cli(b, "check", "tacobot/tasks.toml")
    assert r.returncode == 1 and "tasks/fold" in r.stdout
    assert cli(b, "claim", "tacobot/tasks.toml").returncode == 1
    sh(b, "git", "checkout", "-qb", "rig/layout")
    assert cli(b, "claim", "rig/**").returncode == 0  # a different lane is fine


def test_unclaimed_edits_on_other_branch_are_detected(team):
    a, b = team
    sh(a, "git", "checkout", "-qb", "rig/griddle")
    p = a / "rig/SAFETY.md"
    p.write_text(p.read_text() + "\nmore\n")
    sh(a, "git", "commit", "-qam", "forgot to claim")
    sh(a, "git", "push", "-q", "-u", "origin", "HEAD")
    sh(b, "git", "checkout", "-qb", "rig/estop")
    r = cli(b, "check", "rig/SAFETY.md")
    assert r.returncode == 1 and "already modified" in r.stdout


def test_cannot_claim_upstream_or_shared(team):
    a, _ = team
    sh(a, "git", "checkout", "-qb", "tasks/x")
    assert cli(a, "claim", "upstream/src/robot.py").returncode == 1
    assert cli(a, "claim", "AGENTS.md").returncode == 1


def test_claude_hook_blocks(team):
    a, b = team
    push_claim(a, "tasks/fold", "tacobot/**")

    def hook(repo, rel):
        return cli(repo, "hook", stdin=json.dumps({"tool_input": {"file_path": str(repo / rel)}}))

    assert hook(b, "tacobot/tasks.toml").returncode == 2             # b is on main
    sh(b, "git", "checkout", "-qb", "tasks/other")
    cli(b, "sync")
    r = hook(b, "tacobot/tasks.toml")
    assert r.returncode == 2 and "tasks/fold" in r.stderr            # claimed by a
    assert hook(b, "AGENTS.md").returncode == 2                      # shared
    assert hook(b, "upstream/src/robot.py").returncode == 2          # read-only
    assert hook(b, "rig/WORKCELL.md").returncode == 0                # free to edit
    assert hook(a, "tacobot/tasks.toml").returncode == 0             # owner may edit


def test_hook_fails_closed_on_bad_input(team):
    a, _ = team
    sh(a, "git", "checkout", "-qb", "tasks/x")
    r = cli(a, "hook", stdin="not json")
    assert r.returncode == 2 and "Failing closed" in r.stderr


def test_hook_fails_closed_on_crash(tmp_path):
    # Not a git repo -> git rev-parse fails inside the hook. Used to exit 0 (edit allowed).
    r = sh(tmp_path, sys.executable, str(SCRIPT), "hook", check=False,
           stdin=json.dumps({"tool_input": {"file_path": str(tmp_path / "x.py")}}))
    assert r.returncode == 2 and "Failing closed" in r.stderr


def _old_python() -> str | None:
    for exe in ("/usr/bin/python3", shutil.which("python3.10"), shutil.which("python3.9")):
        if exe and Path(exe).exists():
            v = subprocess.run([exe, "-c", "import sys; print(sys.version_info < (3, 11))"],
                               capture_output=True, text=True)
            if v.stdout.strip() == "True":
                return exe
    return None


@pytest.mark.skipif(_old_python() is None, reason="no Python < 3.11 available")
def test_hook_fails_closed_on_old_python():
    r = subprocess.run([_old_python(), str(SCRIPT), "hook"], input="{}", capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and "Failing closed" in r.stderr


def test_release(team):
    a, _ = team
    sh(a, "git", "checkout", "-qb", "data/log")
    cli(a, "claim", "data/**")
    f = a / "coordination/claims/data__log.toml"
    assert f.exists()
    cli(a, "release")
    assert not f.exists()
