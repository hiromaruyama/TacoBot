# Data (lane: data)

Recording and training use upstream's flow:
- Record Dream-1 runs and human corrections: `upstream/tutorial/02-dream-1.md`
- Convert archives to a LeRobot dataset and train ACT: `upstream/tutorial/03-train-act.md`

Plan: record one dataset per quesadilla step (same `id` as `tacobot/tasks.toml`), so a weak step can be retrained alone.

Never commit recordings or datasets here (they're gitignored). Keep only notes, configs and a log of runs:

| Date | Step | Policy | Tries | Successes | Notes |
|------|------|--------|-------|-----------|-------|
