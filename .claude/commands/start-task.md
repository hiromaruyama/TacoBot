---
description: Start a task safely — branch, check, claim, push, draft PR
argument-hint: <lane> <short-task-name> [paths...]
---
Start a new TacoBot task. Arguments: $ARGUMENTS (first = lane, second = task slug, rest = paths to claim).

1. `git checkout main && git pull --rebase && git submodule update --init`
2. `git checkout -b <lane>/<task-slug>`
3. If no paths were given, propose the smallest set inside that lane's paths in `coordination/lanes.toml` and confirm with me.
4. `python3 scripts/claims.py check <paths>` — on any overlap, STOP and tell me who owns what. Don't claim.
5. `python3 scripts/claims.py claim <paths> --task "<task>"`
6. Commit only the claim file (`claim: <task>`), then `git push -u origin HEAD`.
7. Open a draft PR (`gh pr create --draft`) using `.github/PULL_REQUEST_TEMPLATE.md`.
8. Report the branch, the claimed paths and the PR link.
