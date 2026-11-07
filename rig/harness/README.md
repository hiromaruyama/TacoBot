# Rig harness

Use this to watch and steer the bimanual MakerMods + Dream-1 rig from a browser, from scripts, or from a coding agent.
The rig runs [TacoBot](https://github.com/hiromaruyama/TacoBot/tree/rig/makerarm-web-kit) /
[portal-makermods](https://github.com/livekit-examples/portal-makermods/tree/dc/maker_arm).

```
 browser / harnessctl / scripts ──HTTP+WS──▶ harness ──LiveKit Portal (observer)──▶ room
                                                │                                   ├─ robot            (3 cameras + state, park/home RPCs)
                                                │                                   ├─ policy-dream-1-tasks (dream.* RPCs)
                                                └─ MJPEG video, joints, events      └─ teleoperator / hand-teleop / recorder …
```

The harness joins the server's Portal room as an **observer** called `harness`. From there it can:

- **Monitor.** It shows the three camera streams (`overhead_camera`, `left_wrist_camera`, `right_wrist_camera`), both
  arms' measured joints next to the commanded action, who has control, Dream-1's task and status, the policy
  latency, and an event log.
- **Prompt.** `dream.set_task` changes Dream-1's task. A task change pauses Dream-1 and nothing moves until you
  **Start** (`dream.resume`).
- **Control.** You can pause, resume, hand off to the leader arms or the hand teleop, send the arms **Home**, **Park**
  them (park only), and press **HOLD**. HOLD clears the control gate so nobody drives and the arms hold.
- **Sequence.** It steps through a TacoBot `tasks.toml` (the quesadilla) with buttons. Every step's motion starts only
  when someone presses Start.

**Safety.** An observer cannot send actions, so the harness never moves an arm directly. It never powers the arms
up: power-up stays at the robot terminal (`p`). None of this is an e-stop. Keep a person on the leader arms or the
`p` key, and follow `rig/SAFETY.md`.

## Connect to the server laptop from your laptop

The two laptops never talk directly. Both join the same **LiveKit room**, and LiveKit relays the video and RPCs, so
your laptop needs no ports open to the server laptop.

```
 server laptop                                   your laptop
 uv run robot          ─┐                    ┌─ uv run harness  ─▶ http://127.0.0.1:8770
 uv run dream-1-tasks  ─┼──▶ LiveKit room ◀──┘   (browser, harnessctl)
 (teleoperator, …)     ─┘   (Cloud, or livekit-server on the server laptop)
```

**1. On the server laptop** (a human runs these, from TacoBot's `upstream/`; see `rig/SAFETY.md`):

```bash
uv run robot
uv run dream-1-tasks          # needed for prompts
```

**2. On your laptop.** Copy the server laptop's LiveKit settings into `rig/harness/.env`:

```bash
cd rig/harness
cp .env.example .env          # fill in LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET / LIVEKIT_ROOM
uv sync
```

All four values must equal the server laptop's `upstream/.env`, including `LIVEKIT_ROOM`. Use one of these URLs:

- **LiveKit Cloud:** `LIVEKIT_URL=wss://<project>.livekit.cloud`. This works from any network.
- **livekit-server on the server laptop:** start it there with `livekit-server --dev --bind 0.0.0.0`, then set
  `LIVEKIT_URL=ws://<server-laptop-ip>:7880` on both laptops, with key `devkey` and secret `secret`. Both laptops
  must be on the same network, and the server laptop's firewall must allow ports 7880-7882.

If you don't want to copy the API secret, mint a token on the server laptop for identity `harness` and that room,
then set `LIVEKIT_TOKEN` instead of the key and secret.

**3. Check that you see the server.** This only reads the room list, it doesn't join the room:

```bash
uv run harness --check
#   room 'portal-makermods': 3 participant(s)
#     robot                 robot     overhead_camera(video), left_wrist_camera(video), right_wrist_camera(video)
#     policy-dream-1-tasks  operator
#   ✓ robot   ✓ policy-dream-1-tasks   ✓ cameras published
```

If the output says "room … is not active", the robot isn't connected yet, or the two `.env` files differ (wrong
project or wrong room).

**4. Run the harness, prompt and watch:**

```bash
uv run harness --tasks ../../tacobot/tasks.toml     # --tasks is optional (quesadilla step runner)
open http://127.0.0.1:8770
```

Type a prompt and press **Set task**. Dream-1 pauses and nothing moves. Then press **Start / resume**, or use
**Set & start** to do both. Press **HOLD** (or Space) to stop it driving.

From a terminal: `uv run harnessctl prompt "…" --start`, `uv run harnessctl release`, `uv run harnessctl snapshot
overhead_camera out.jpg`.

`portal.yaml` comes from TacoBot's `upstream/` (`git submodule update --init`), or from `--portal-config`. It must
match the server's, because it defines the cameras and joints. Until the robot is up, the harness keeps retrying and
the page shows why it isn't connected.

**Opening the UI from other machines.** The harness binds to localhost by default. To serve it on a network, set
`HARNESS_TOKEN` and pass `--host 0.0.0.0`. Then open `http://host:8770/?token=…`, or send
`Authorization: Bearer …` with each request.

## UI keys

| Key | Action |
|---|---|
| `Enter` in the prompt box | Set the task (Dream-1 pauses) |
| `⌘/Ctrl+Enter` | Set the task and start |
| `Space` (outside text fields) | HOLD: release control |

## CLI / HTTP API

```bash
uv run harnessctl status
uv run harnessctl prompt "pick up the tortilla and lay it flat in the middle of the griddle"
uv run harnessctl resume            # or: prompt "..." --start
uv run harnessctl release           # HOLD
uv run harnessctl teleop | hand-teleop | stop-hand-teleop | pause | home | park
uv run harnessctl snapshot overhead_camera out.jpg
uv run harnessctl watch             # tail the event log
uv run harnessctl seq arm 0 | start | done | retry | teleop | stop | show
```

| Endpoint | What it does |
|---|---|
| `GET /api/state` | Everything: link, control, Dream-1 status, joints, cameras, sequence |
| `GET /api/events?after=<id>` | The event log |
| `WS /ws` | Pushes state (4 Hz) and events live |
| `GET /video/<camera>.mjpg` | MJPEG stream (use it in an `<img>`) |
| `GET /snapshot/<camera>.jpg` | Newest frame, e.g. for a VLM |
| `POST /api/prompt {"task", "start"}` | Set Dream-1's task; with `"start": true`, also resume it |
| `POST /api/{resume,pause,release,teleop,hand_teleop,stop_hand_teleop,home,park}` | Control commands |
| `POST /api/sequence/{load,arm,start,done,retry,teleop,stop}` | Sequence commands |

Each POST answers `{"ok": true, "result": …}` on success, or `{"ok": false, "error": "…"}` with the server's
message.

## Try it without hardware

```bash
brew install livekit && livekit-server --dev            # local LiveKit, devkey/secret
uv run fake-rig --teleop --task "put the red cube in the box"
uv run harness --env mock/dev.env --tasks harness/example_tasks.toml
```

`mock/fake_rig.py` joins the room as `robot`, `policy-dream-1-tasks` and `teleoperator`, and uses the same RPC
semantics as upstream. It draws stick-figure arms that follow the actions. It refuses to join a `*.livekit.cloud`
room unless you pass `--allow-cloud`, so it can't land in the real rig's room.

## Tests

```bash
uv run pytest            # sequencer + HTTP API against a stub observer (no LiveKit)
```

## Keeping in sync with the server

`harness/protocol.py` mirrors the names in portal-makermods `src/utils/common.py`: identities, `dream.*` RPCs, and
`park`/`home`. The schema (cameras, state fields) comes from the server's `portal.yaml` at runtime. If the server adds
or renames an RPC, update `protocol.py`.
