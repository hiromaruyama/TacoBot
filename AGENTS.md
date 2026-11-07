# AGENTS.md — TacoBot

For **every** coding agent (Claude Code, Codex, Cursor, Copilot…) and every human on the team.
Read it fully before your first edit. `CLAUDE.md` imports this file.

## 1. Mission

Make a quesadilla with two **MakerMods arms** (Metal, or Maker Arm v1) using **LiveKit Portal** and **Dream-1**:
tortilla on griddle → cheese → filling → melt → fold → cook → flip → cook → plate.

We do **not** write our own arm driver. `upstream/` is
[livekit-examples/portal-makermods @ dc/maker_arm](https://github.com/livekit-examples/portal-makermods/tree/dc/maker_arm)
as a git submodule. It already runs the robot, teleop, recorder, Dream-1, ACT, voice agent and task monitor.
TacoBot adds: the quesadilla steps, a sequencer that walks Dream-1 through them, the rig/griddle setup, and team tooling.

**Read `upstream/AGENTS.md` too.** Its rules apply to anything that touches the arms.

## 2. Repo map

```
upstream/              READ-ONLY submodule — portal-makermods (robot, teleop, Dream-1, ACT, safety)
tacobot/tasks.toml     lane tasks — one Dream-1 prompt per quesadilla step
tacobot/sequencer.py   lane tasks — walks dream-1-tasks through the steps over Portal RPC
rig/                   lane rig   — workcell layout, griddle, cameras, calibration, safety checklist
data/                  lane data  — recordings, dataset conversion, ACT training notes
coordination/          lanes.toml + claims/ — the anti-conflict system (§4)
scripts/claims.py      claim / check / release tool
.claude/  .github/     Claude Code hooks + commands, CI, CODEOWNERS, templates
tests/                 test_sequencer.py (lane tasks), test_claims.py (lane infra)
```

## 3. Lanes (who owns what)

One agent = one lane = one branch at a time. Source of truth: `coordination/lanes.toml`.

| Lane    | Owns                                          | Branch prefix |
|---------|-----------------------------------------------|---------------|
| `tasks` | `tacobot/**`, `tests/test_sequencer*.py`      | `tasks/`      |
| `rig`   | `rig/**`                                      | `rig/`        |
| `data`  | `data/**`                                     | `data/`       |
| `infra` | `.github/**`, `scripts/**`, `.claude/**`, `Makefile`, `pyproject.toml`, `README.md`, `tests/test_claims*.py` | `infra/` |

**READ-ONLY:** `upstream/**`. Never edit it, never commit inside it. If upstream needs a fix, write it up for a human
to send to portal-makermods. Need different behaviour? Wrap it from `tacobot/`.

**SHARED:** `AGENTS.md`, `CLAUDE.md`, `coordination/lanes.toml`, `.gitmodules`, and the `upstream` pin itself.
Change them only on a tiny separate `shared/<what>` branch, merged before feature work rebases on it.
Bumping upstream: `shared/bump-upstream` → `git -C upstream pull origin dc/maker_arm` → run its checks → PR.

## 4. Conflict-prevention protocol (MANDATORY)

Rule: **nobody edits a file another open branch has claimed or already changed.**

### Before you start
```bash
git checkout main && git pull --rebase && git submodule update --init
git checkout -b <lane>/<short-task>                       # e.g. tasks/better-fold-prompt
python3 scripts/claims.py check 'tacobot/tasks.toml'      # who else touches it?
python3 scripts/claims.py claim 'tacobot/tasks.toml' --task "better fold prompt"
git add coordination/claims && git commit -m "claim: better fold prompt"
git push -u origin HEAD                                   # push NOW so others see the claim
```
Then open a **draft PR**.

`check` fetches, then reads every other branch's claim file **and** the files it has actually changed vs `origin/main`.
Any overlap → it names the branch and exits 1. **Stop and coordinate.** Never "quickly" edit someone else's file.

### While working
- Stay inside your claim. Need another file? `check` it, `claim` it, push.
- `python3 scripts/claims.py sync` every ~30 min and before each push. Rebase, don't merge: `git pull --rebase origin main`.
- Branches live < 1 day. Small PRs.
- Claude Code: a `PreToolUse` hook blocks edits to other branches' files, shared files, `upstream/`, and anything on `main`.
  Don't bypass it (no editing through Bash).

### Finishing
```bash
python3 scripts/claims.py release
make check
git commit -am "release claim" && git push
```
Mark the PR ready. CI rejects a ready PR that still has its claim file, overlaps another branch, edits a shared file
from a non-shared branch, or uses a branch name without a lane prefix.

### Conflict anyway?
Rebase. If it's in a file you don't own, don't pick your side — ask the owner. In a shared file, the `shared/` branch wins.

## 5. Commands

```bash
make setup     # submodule + upstream `uv sync` + git hooks
make check     # ruff + pytest + claims check   (before every push)
make plan      # print the quesadilla steps (no hardware)
```
Running the robot is upstream's flow (`upstream/tutorial/01-first-recording.md`, `02-dream-1.md`, `10-voice-tasks.md`):
`uv run robot`, `uv run teleoperator`, `uv run recorder`, `uv run dream-1-tasks`, run from `upstream/`.
Then `make cook` starts the sequencer. Humans only — see §7.

## 6. Conventions
- Python 3.12, type hints, small files. TacoBot code must not import upstream at module top level (tests run without it).
- Talk to the arms **only** through upstream's RPCs/peers. Never open CAN adapters or the `robots` driver from `tacobot/`.
- Dream-1 prompts: one concrete instruction, < 200 chars. Change one prompt per PR and note the run result in the PR.
- Commits: `<lane>: <imperative summary>`.

## 7. Safety (brakeless arms + hot griddle)
1. **Agents never run hardware.** No `uv run robot`/`dream-1-tasks`/`make cook`, no CAN or serial access. Humans run the rig.
2. **The arms have no brakes.** Never disable motors on a fault; the only switch-off is upstream's park (`p`). Don't add code that cuts power.
3. Never change upstream's soft limits, step limiter or park logic (they're read-only anyway).
4. First runs of any new prompt: **griddle OFF and cold**, a person on the leader arms / `p` key.
5. Hot runs: a person watches the whole time, heat-proof gloves and extinguisher nearby. See `rig/SAFETY.md`.
6. The sequencer is not an e-stop. Voice isn't either. Take over with the leaders or park.

## 8. Definition of done
- [ ] `make check` passes
- [ ] Only files in your claim (plus the claim file) changed; nothing in `upstream/`
- [ ] `make plan` still prints a valid plan
- [ ] Claim released, PR filled in (what changed, how tested)
