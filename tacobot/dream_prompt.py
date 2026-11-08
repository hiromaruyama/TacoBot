"""Type any instruction; Dream-1 does it. A free-text prompt console for dream-1-tasks.

This is the manual counterpart to the Dream baseline (`make cook`, fixed tasks.toml) and to
Astra+Dream (`make cook-astra`, Astra plans the steps): here *you* type each instruction and
Dream-1 runs it. It drives the running `uv run dream-1-tasks` over the same `dream.*` RPCs the
Astra director and the voice agent use; it is a plain operator peer and never sends actions.

Bring the rig up first (both arms + the 3 cameras), then this:

    # Terminal 1
    cd upstream && uv run robot
    # Terminal 3
    cd upstream && uv run dream-1-tasks
    # Bash
    cd upstream && uv run python ../tacobot/dream_prompt.py     # or: make prompt

Then at the `dream>` prompt:
    <instruction>   prompt Dream-1 with it and start   (e.g. "pick up the tortilla")
    (blank line)    pause: the arms hold where they are
    :status         print what Dream-1 is doing and who drives
    :quit / Ctrl-D  leave the arms held (not driving) and exit

Power-up/park is still `p` in the robot terminal, and that is your e-stop; this console only
ever sets the task and resumes/pauses Dream-1.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM_SRC = HERE.parent / "upstream" / "src"
sys.path.insert(0, str(UPSTREAM_SRC))

from utils.common import (CONFIG_PATH, DREAM_PAUSE_RPC, DREAM_RESUME_RPC, DREAM_SET_TASK_RPC,
                          DREAM_STATUS_RPC, DREAM_TASKS_IDENTITY, load_env, mint_token, required_env)


async def _amain() -> int:
    load_env()
    from livekit.portal import Operator, OperatorConfig

    url, room = required_env("LIVEKIT_URL"), required_env("LIVEKIT_ROOM")
    op = Operator(OperatorConfig.from_yaml_file(CONFIG_PATH, room))
    await op.connect(url, mint_token("dream-prompt", room))
    if DREAM_TASKS_IDENTITY not in op.operators():
        print(f"{DREAM_TASKS_IDENTITY} is not in the room; start `uv run dream-1-tasks` first")
        await op.disconnect()
        op.close()
        return 1

    async def rpc(method: str, payload: dict | None = None) -> dict:
        reply = await op.perform_rpc(method, payload=json.dumps(payload or {}),
                                     destination=DREAM_TASKS_IDENTITY, response_timeout_ms=5000)
        try:
            return json.loads(reply)
        except (TypeError, ValueError):
            return {}

    print("Dream-1 prompt console. Type an instruction and Enter; blank line pauses.")
    print("  :status  show state    :quit  exit (arms stay held)")
    print("  'p' in the robot terminal powers up / parks the arms -- that is your e-stop.\n")
    try:
        while True:
            line = (await asyncio.to_thread(input, "dream> ")).strip()
            if line in (":quit", ":q"):
                break
            if line == ":status":
                print(json.dumps(await rpc(DREAM_STATUS_RPC), indent=2))
                continue
            if not line:
                await rpc(DREAM_PAUSE_RPC)
                print("paused: the arms hold where they are")
                continue
            try:
                await rpc(DREAM_SET_TASK_RPC, {"task": line})
                status = await rpc(DREAM_RESUME_RPC)
            except Exception as exc:  # noqa: BLE001 -- show the RPC error and keep the console alive
                print(f"could not run that: {exc}")
                continue
            if status.get("driving"):
                print(f"Dream-1 running: {line!r}   (blank line to pause)")
            else:
                print(f"set task {line!r} but Dream-1 is not driving "
                      f"(active operator: {status.get('active_operator')}). Is the robot powered up (p)?")
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        try:
            await rpc(DREAM_PAUSE_RPC)  # leave Dream-1 not driving; the robot holds the arms
        except Exception:  # noqa: BLE001
            pass
        await op.disconnect()
        op.close()
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
