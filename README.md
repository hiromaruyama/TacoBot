# 🌮 TacoBot

Two MakerMods arms, one griddle, one quesadilla, driven by **Dream-1** over **LiveKit Portal**.

```
tortilla → cheese → filling → melt → fold → cook → flip → cook → plate
```

The robot side is [portal-makermods](https://github.com/livekit-examples/portal-makermods/tree/dc/maker_arm), included
as the `upstream/` submodule. TacoBot adds the quesadilla steps (`tacobot/tasks.toml`), a sequencer that walks Dream-1
through them, the rig setup, and team tooling so several people and AI agents can work at once without conflicts.

## Setup
```bash
git clone --recurse-submodules https://github.com/hiromaruyama/TacoBot && cd TacoBot
make setup                     # upstream uv sync + git hooks
cp upstream/.env.example upstream/.env   # fill in CAN adapters, cameras, LiveKit, DREAM_API_KEY
make plan                      # print the quesadilla steps
```
Do the rig setup in `upstream/tutorial/01-first-recording.md` and `rig/SAFETY.md` first.

## Cooking (humans only, someone on the leaders / park key)
In separate terminals, from `upstream/`:
```bash
uv run robot
uv run teleoperator            # on the teleop host, for takeovers
uv run recorder                # optional: record every attempt for ACT training
uv run dream-1-tasks
```
Then from the repo root: `make cook`. Each step waits for you to press Enter before Dream-1 moves.

## Working as a team
Read **[AGENTS.md](AGENTS.md)**. In short: pick a lane, branch `<lane>/<task>`, `scripts/claims.py check` then `claim`,
push right away, draft PR, stay in your claim, `release`, `make check`, mark ready.
