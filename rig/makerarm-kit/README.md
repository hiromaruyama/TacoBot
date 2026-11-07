# Maker Arm control kit (web app + LiveKit)

A small, safety-first kit for the MakerMods **Maker Arm v1** (6 joints + gripper), built at the
MakerMods Physical AI Hackathon. Control one or two arms from a local web page with a live
camera view, or remotely over LiveKit. Built on the official
[maker-arm-sdk](https://github.com/makermods-robotics/maker-arm-sdk) (MIT).

> **Where this fits in TacoBot (rig lane).** This is a standalone bring-up / manual-jog tool
> for checking the arms, cameras and temperatures before a run. It talks to the CAN adapters
> **directly**, unlike `tacobot/`, which only goes through upstream's Portal peers:
> - **Never run it at the same time as upstream's `uv run robot`** (or MakerModsLab). Only one
>   program can hold an adapter. Stop this app (Ctrl-C parks the arms first) before starting the robot.
> - Humans only, like any hardware command (AGENTS.md §7). Agents must not run it on the rig.
> - It has its own venv and requirements; it doesn't touch the repo's `pyproject.toml` or `upstream/`.

Tested on **Windows 11** with two arms through one USB hub: one SLCAN adapter (shows up as a
COM port) and one candleLight adapter (no COM port, uses gs_usb). macOS and Linux are supported
(port detection, setup and launch scripts below) but have not been tried on real arms yet.

| File | What it does |
|---|---|
| `web_app.py` + `static/` | Local web page at http://127.0.0.1:8765: live camera, arm dropdown, buttons for every joint |
| `first_move.py` | Simplest test: nudge one joint a few degrees and back. Also lists ports |
| `arm_server.py` | Runs next to an arm: joins a LiveKit room and accepts small jog commands |
| `controller.py` | Runs anywhere: keyboard (or `--demo`) control over LiveKit |
| `sim.py` | Simulated motors so everything works with `--sim`, no hardware |
| `setup.sh` / `setup.ps1` | One-time setup (macOS/Linux / Windows) |
| `run.sh` / `run.ps1` | Run any script with the kit's venv; no arguments = web app |
| `armkit.py`, `candle_backend.py`, `lk.py` | Shared helpers, safety limits, candleLight support, LiveKit tokens |

## 1. Setup (once)

Requires Python 3.10+ and git.

**Windows (PowerShell)**
```powershell
git clone https://github.com/hiromaruyama/TacoBot.git
cd TacoBot\rig\makerarm-kit
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**
```bash
git clone https://github.com/hiromaruyama/TacoBot.git
cd TacoBot/rig/makerarm-kit
bash setup.sh
source .venv/bin/activate
```

All commands below run from `rig/makerarm-kit/`, with the venv activated. Without activating,
use the launchers instead of `python`: `./run.sh <script> [args]` (macOS/Linux) or
`.\run.ps1 <script> [args]` (Windows). With no arguments they start the web app.

Setup creates `.env` from `.env.example`. Never commit `.env`.

### macOS quick start

```bash
# 1. Python 3.10+ (macOS's built-in python3 is usually 3.9)
brew install python@3.12            # needs Homebrew: https://brew.sh

# 2. Get the kit and set it up
git clone https://github.com/hiromaruyama/TacoBot.git
cd TacoBot/rig/makerarm-kit
bash setup.sh

# 3. Try it without hardware
./run.sh web_app.py --sim           # opens http://127.0.0.1:8765

# 4. Plug in the arms' USB hub, find the ports, put them in .env
./run.sh first_move.py --list-ports # e.g. /dev/cu.usbmodem2101 and candle:0
open -e .env                        # ARM_PORTS=left=/dev/cu.usbmodem2101,right=candle:0

# 5. Go
./run.sh first_move.py --read-only  # motors OFF: move an arm by hand, angles should change
./run.sh                            # web app with the real arms
```

macOS notes:
- **Ports:** SLCAN adapters are `/dev/cu.usbmodem…` or `/dev/cu.usbserial…`. Always use the
  `cu.` name, not the `tty.` twin. The listing marks the likely ones with `*`.
- **candleLight** (`candle:0`) needs no driver on macOS: `libusb` ships inside the
  `libusb-package` dependency. If it isn't found, `brew install libusb` and try again.
- **Camera:** the first time you click *Start camera*, allow it in the browser. If it stays
  black, enable your browser under System Settings → Privacy & Security → Camera, then restart
  the browser. USB cameras on the hub appear in the camera dropdown.
- **Keyboard controller** (`controller.py`) reads single keys in Terminal/iTerm, arrows included.
- If `./run.sh` says *permission denied*: `chmod +x run.sh setup.sh`, or use `bash run.sh`.

## 2. Try it without hardware

```bash
python web_app.py --sim          # two simulated arms in the browser
python first_move.py --sim --yes # simulated single-joint move
```

## 3. Find your arms' ports

Plug the arms' USB cable (or hub) into the laptop and run:

```bash
python first_move.py --list-ports
```

- An SLCAN adapter shows as a serial port, e.g. `COM9` (Windows), `/dev/cu.usbmodem…` (macOS)
  or `/dev/ttyACM0` (Linux). Ignore "Bluetooth" ports; likely adapters are marked with `*`.
- A **candleLight** adapter shows as `candle:0` (`candle:1` for a second one).
  On Linux you can also bring it up as SocketCAN instead.

Put them in `.env`, naming each arm the way you want it to appear in the dropdown:

```ini
# Windows
ARM_PORTS=left=COM9,right=candle:0
ARM_PORT=COM9   # used by the single-arm scripts
# macOS
ARM_PORTS=left=/dev/cu.usbmodem2101,right=candle:0
```

Not sure which arm is which? Use `python first_move.py --read-only --port COM9` (motors stay
OFF) and move an arm by hand: the one whose angles change is on that port.

## 4. Before powering anything

- Arm clamped to the table, workspace clear, someone's hand near the power switch.
- The arms must already be **zeroed** (the MakerMods stations are). Don't run `maker-arm zero`
  unless an organizer tells you to.
- Close MakerModsLab or any other program using the adapters. Only one program at a time
  can talk to an adapter, so don't run two copies of the web app either.
- **The arms have no brakes**: support an arm before its torque is released.

## 5. First movement

```bash
python first_move.py --read-only          # motors OFF: move by hand, watch the angles
python first_move.py --joint 1 --deg 10   # base turns 10° and back, then parks
```

Type `MOVE` (and press Enter) when asked. If the target is outside the safe range it says so;
try `--deg -10`. Use `--port candle:0` to pick a specific arm.

## 6. Web app (camera + controls)

```bash
python web_app.py                              # arms from ARM_PORTS in .env
python web_app.py --arms left=COM9,right=candle:0
```

The browser opens at **http://127.0.0.1:8765** (also http://localhost:8765).

1. **Start camera** and allow access. With several cameras, choose one in the dropdown.
2. Pick the arm in the **Arm** dropdown, or switch with `Q` / `E`.
3. **Power on**, which asks you to confirm. The arm holds where it is.
4. Move with **−** / **+** (keep pressed to keep moving), **Open/Close gripper**, or the keys:
   `1`–`7` pick a joint, `←`/`→` or `A`/`D` move it, `Space` holds.
5. **Park & power off** returns the arm to where it was when you powered it on and releases torque.
   Ctrl-C in the terminal parks every arm and quits.

Start with 1–2° steps on J1 (base) and J4–J6 (wrist); J2/J3 carry the arm's weight.
If an arm can't be reached, it shows as *offline* and the other one still works.

## 7. Remote control over LiveKit (optional)

1. LiveKit Cloud → your project → **Settings → API Keys** → create a key. Put `LIVEKIT_URL`,
   `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` in `.env`.
2. Next to the arm: `python arm_server.py` (or `--sim`), then type `MOVE` to power it up.
3. On any laptop with the same `.env`: `python controller.py`
   (`1`–`7` pick joint, `a`/`d` jog, `[`/`]` step size, `h` hold, `p` park, `q` quit),
   or `python controller.py --demo` for a small wave of the base.

Messages are JSON over LiveKit data channels: `arm/cmd` (controller → arm) and `arm/state`
(arm → everyone, 10 Hz). See the docstring of `arm_server.py`.

## Safety built in

- Torque is switched on only next to the arm (the web page on 127.0.0.1, or `MOVE` in the
  terminal), never by a remote peer.
- A motor above **70 °C** blocks power-on. The web app shows joint temperatures in orange from
  60 °C and red above 70 °C.
- Each step is at most 10° and ramps at 15°/s. Targets stay 5° inside each joint's soft limits,
  never run more than 10° ahead of the real position, and a joint resting outside its safe band
  can only be moved back toward the inside.
- If the remote operator leaves, the arm holds. Parking returns to the power-on pose before
  releasing torque. If that can't be verified, the SDK keeps holding and asks you to type `RELEASE`.

All limits are at the top of `armkit.py`.

## Joint map

1 base (shoulder pan) · 2 shoulder lift · 3 elbow · 4 wrist flex · 5 wrist yaw · 6 wrist roll · 7 gripper
