---
description: Finish the task — verify, release claim, push, mark PR ready
---
1. `python3 scripts/claims.py check --changed` — on any overlap, STOP and report.
2. `git diff --name-only origin/main...HEAD`: every file must be inside my claim and nothing under `upstream/`. List any that aren't.
3. `make check` and `make plan` must pass.
4. `python3 scripts/claims.py release`, commit `release claim`, push.
5. Fill in the PR description (what changed, how it was tested; for prompt changes, the cold-run result from the human), then mark it ready.
