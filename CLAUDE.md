# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- A `SessionStart` hook runs `scripts/claims.py status`: read it, it shows which branches own which files.
- A `PreToolUse` hook blocks Edit/Write on files owned by another branch, shared files, `upstream/**`, and anything on
  `main`. If you're blocked, **don't work around it** (no `sed`/`cat >` through Bash). Pick other work or tell the human.
- Slash commands (`.claude/commands/`):
  - `/start-task <lane> <task> [paths]` — branch, check, claim, push, draft PR
  - `/sync` — refresh claims, rebase on main, report overlaps
  - `/finish` — check, release claim, `make check`, push, mark ready
- Never run hardware commands (`uv run robot`, `dream-1-tasks`, `teleoperator`, `make cook`). Ask the human.
- Several Claude sessions on one machine → one git worktree each.
- Never commit `.env`, API keys (`DREAM_API_KEY`, LiveKit secrets) or recordings.
