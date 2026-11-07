---
description: Refresh claims from all branches and rebase on main
---
1. `python3 scripts/claims.py sync` — show me any overlap.
2. `git pull --rebase origin main && git submodule update --init`. If a conflict is in a file outside my claim,
   `git rebase --abort` and tell me who owns it instead of resolving it.
3. `make check`
4. Summarize: other active branches, overlaps, check result.
