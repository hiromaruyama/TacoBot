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
| `web_app.py` + `static/index.html` | Arm laptop: web page at http://127.0.0.1:8765 (camera, arm dropdown, every joint). With `--livekit`, shares the arms and camera with the team |
| `remote.py` + `static/remote.html` | **Teammates, from anywhere:** live arm camera + controls over LiveKit (no arm hardware needed) |
| `sim_world.py` | Virtual tortilla + plate per arm and the grasp rule (forward kinematics from the SDK) |
| `first_move.py` | Simplest test: nudge one joint a few degrees and back. Also lists ports |
| `arm_server.py` | Runs next to an arm: joins a LiveKit room and accepts small jog commands |
| `controller.py` | Runs anywhere: keyboard (or `--demo`) control over LiveKit |
| `diagnose.py` | Motors-off health check of one arm: mode, faults, temperature, position, **supply voltage** |
| `zero_gripper.py` | Re-zero only the gripper (motor 7) when it lost its zero; torque stays off |
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

# 3. Try it without hardware: 3D view of two simulated arms, joysticks, keys
./run.sh web_app.py --sim           # opens http://127.0.0.1:8765 -> Power on, then W A S D

# 4. Plug in the arms' USB hub, find the ports, put them in .env
./run.sh first_move.py --list-ports # e.g. /dev/cu.usbmodem2101 and candle:0
open -e .env                        # ARM_PORTS=left=/dev/cu.usbmodem2101,right=candle:0

# 5. Go
./run.sh first_move.py --read-only  # motors OFF: move an arm by hand, angles should change
./run.sh                            # web app with the real arms (add --livekit for the team)
```

macOS notes:
- **Ports:** SLCAN adapters are `/dev/cu.usbmodem…` or `/dev/cu.usbserial…`. Use the `cu.` name
  (the `tty.` twin is accepted too). The listing marks the likely ones with `*`. The number in
  `usbmodem2101` follows the USB socket: plug the hub into a different socket and it changes. The
  web app's header shows the adapters it sees live (`USB: …`); update `ARM_PORTS` if it moved.
- **Arms not connected at start** is fine: the web app starts anyway and connects each arm by
  itself when its adapter is plugged in and the arm has power (or press **Connect**).
- **candleLight** (`candle:0`) needs no driver on macOS: `libusb` ships inside the
  `libusb-package` dependency. If it isn't found, `brew install libusb` and try again.
- **Camera:** the first time you click *Start camera*, allow it in the browser. If it stays
  black, enable your browser under System Settings → Privacy & Security → Camera, then restart
  the browser. USB cameras on the hub appear in the camera dropdown.
- **Browser:** Chrome, Safari, Edge or Firefox. Joysticks, keys and the 3D view work in all of
  them; `Cmd`/`Option` shortcuts are left alone, so `Cmd+R` still reloads (hold-keys are released
  when the window loses focus). The 3D view and LiveKit need internet.
- **Keyboard controller** (`controller.py`) reads single keys in Terminal/iTerm, arrows included.
- **Stop the app** with `Ctrl-C` in its Terminal tab (it parks the arms first); closing the
  browser tab does not stop it.
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

1. **Camera / 3D view**: **Start camera** and allow access (several cameras: pick one), or switch to
   **3D view**: the arm's real CAD model (the SDK's URDF) moving with the live joint angles of every
   arm, colored by state (grey off, light on, red fault). Drag to orbit, scroll to zoom. It's the
   default with `--sim`, so you can test everything without hardware. **Share view with team**
   sends whichever one is showing (keep that tab visible while sharing the 3D view). Needs internet
   (three.js and the model load from jsDelivr / esm.sh). The gripper jaws are an uncalibrated preview.
2. Pick the arm in the **Arm** dropdown, or switch with `Q` / `E`.
3. **Power on**, which asks you to confirm. The arm holds where it is.
4. Move in real time with the **3 on-screen joysticks** (mouse or touch; let go and the arm stops)
   or by **holding keys** (several at once, no gamepad needed):

   | Keys | Joint | Keys | Joint |
   |---|---|---|---|
   | `A` / `D` | J1 base | `W` / `S` | J2 shoulder |
   | `J` / `L` | J5 wrist yaw | `I` / `K` | J3 elbow |
   | `←` / `→` | J6 wrist roll | `↑` / `↓` | J4 wrist flex |
   | `O` / `C` | gripper open / close | `Shift` (hold) | precise, 40 % speed |
   | `Z` / `X` | speed − / + (10 % steps) | | |
   | `Space` | stop (hold) | `Q` / `E` | switch arm |

   **Speed** slider (or `Z`/`X`): 5–100 % of 60°/s (change the top speed with `--speed`).
   The gripper closes all the way to its profile limit (other joints stay 5° inside theirs). Switching tabs or windows
   releases every key and the arm holds.
5. **Park & power off** returns the arm to where it was when you powered it on and releases torque.
   Ctrl-C in the terminal parks every arm and quits.

Start around 30 % with J1 (base) and J4–J6 (wrist); J2/J3 carry the arm's weight.
The app starts even with **no arm connected**. It checks the USB adapters live (every 3 s, shown as
`USB: COM9, candle:0` in the header) and connects an arm by itself as soon as its adapter is plugged in
and its motors answer (retrying every 10 s). Pick a *not connected* arm and press **Connect** to try
right away. Unplugging an adapter while its motors are off frees that arm; it reconnects when plugged
back in. The other arm keeps working meanwhile.

### Steps & the virtual tortilla (pick & place practice)

Each arm has a small virtual world in the 3D view: a **tortilla** (16 cm) in front of it and a
**plate** to its right. It works in the simulator and with the real arms (a digital twin; there's no
real tortilla involved).

- **Grasp:** close the gripper with the **grasp point** (the dot between the jaws, from the SDK's URDF)
  on the tortilla's outer 5 cm, at most 3 cm above it. The dot turns amber when the jaws are closed
  and green while holding. **Open** the gripper to let go: it drops flat onto the table, or onto the
  plate ("placed on the plate!"). **Reset tortilla** puts it back.
- **Gripper cam:** the small inset is a camera mounted on the wrist, looking down the jaws, to aim
  before closing. Hide/show it with the button on the view. It is included when you share the view.
- **Steps card:** **+ Add current pose** records all 7 joints (the gripper too), then **▶ Play** goes
  through them in order (or **Go** for one step), at the Speed slider's speed, all joints arriving
  together. **Loop** repeats. Steps stay in the browser; **Save…/Load…** as JSON to share them.
  **Load tortilla demo** loads a sequence that picks the tortilla up and puts it on the plate.
- **Safety:** playback streams setpoints that the server clamps like the joysticks (safe range, at
  most 8° ahead of the real arm). A joystick, a key, `Space`, switching arm or losing control stops it
  and the arm holds. Remote teammates need control to play steps.

The grasp rule is a geometric check, not physics: use it to practise reach, approach and timing.
The gripper angle → jaw gap is the SDK's uncalibrated preview (0° closed, −96° fully open).
Try it now: `python web_app.py --sim` → **Power on** → **Load tortilla demo** → **▶ Play**.

## 7. Team control from anywhere (LiveKit)

Everyone on the team can watch the arm camera and drive the arms from their own laptop, on any
network. LiveKit carries the video and the commands.

```
 arm laptop                                LiveKit Cloud room                 each teammate
 web_app.py --livekit  ── arm state ──▶   ┌──────────────────┐   ◀── video ──  remote.py
   (arms on USB)       ◀── commands ───   │  "makerarm"      │   ── commands ▶  (browser page)
 local page: Share camera ── video ─────▶ └──────────────────┘
```

**Once:** in LiveKit Cloud → your project → **Settings → API Keys**, create a key. Put
`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` and `LIVEKIT_ROOM` in `.env`. Teammates need
the **same four values** in their own `.env`. Send them privately (DM), never in the repo or a
public channel.

**On the arm laptop:**
```bash
python web_app.py --livekit          # or: ./run.sh web_app.py --livekit  /  .\run.ps1 web_app.py --livekit
```
1. Start the camera and click **Share camera with team**.
2. **Power on** the arms here. Power-on is only possible on the arm laptop.
3. The *Team* box shows who's connected and who has control. **Take control back** ends anyone's control.

**Teammates, easiest way: an invite link (nothing to install).** In the *Team* box, type their
name, pick how long it lasts (1/4/12 h) and click **Create invite link**, then **Copy** and send
it privately. They open it in any browser (Chrome, Safari, Edge, phone too) and click
**Join as <name>**. The link opens the public copy of `static/remote.html` served from this repo.
Its access token sits after the `#`, so it never reaches that host; it only lets them join this
room (no video publishing) and expires on its own. Anyone holding the link can join until then,
so don't post it publicly. If you fork or move the repo, point the links elsewhere with
`REMOTE_PAGE_URL=` in `.env`.

**Or, on a teammate's laptop with the code** (Windows, macOS or Linux; no arm hardware needed):
```bash
git clone https://github.com/hiromaruyama/TacoBot.git && cd TacoBot/rig/makerarm-kit
bash setup.sh                        # Windows: powershell -ExecutionPolicy Bypass -File .\setup.ps1
# put the team's LIVEKIT_* values in .env
./run.sh remote.py                   # Windows: .\run.ps1 remote.py   ->  opens http://127.0.0.1:8770
```
Type your name and **Join**, and you'll see the arm camera, or the **3D view** drawn from the arms' live data (no camera needed). Press **Take control** to drive. Same
controls as the local page: joysticks, the same held keys, arm dropdown or `Q`/`E`.

**Rules (enforced by the arm laptop):**
- **One controller at a time.** Others see who has control and must wait for *Release control*.
- **Anyone can stop:** *Hold* and *Park & power off* work for every viewer.
- **Power on and Clear fault stay on the arm laptop.**
- If the controller's tab closes or loses the network, they lose control **within 5 s** and the
  arms hold. After 60 s without commands, control is released too.
- The local page always overrides: it can move the arms and take control back at any time.

Wire format (JSON over LiveKit data, room `LIVEKIT_ROOM`): `arm/state` from `arm-host` at 5 Hz
(`arms`, `controller`, `viewers`, `info`); `arm/cmd` to `arm-host` (`take`, `release`, `ping`,
`jog {arm, joint, deg}`, `move {arm, v: {joint: deg, ...}}` (joysticks, 10 Hz), `goto {arm, v: {joint: deg}}` (steps), `reset_world {arm}`, `hold {arm}`, `park {arm}`); `arm/reply` back to the sender.

Terminal-only alternative for a single arm: `python arm_server.py` next to the arm (type `MOVE`)
and `python controller.py` anywhere (`1`–`7`, `a`/`d`, `[`/`]` step, `h` hold, `p` park, `q` quit).

## Safety built in

- Torque is switched on only next to the arm (the web page on 127.0.0.1, or `MOVE` in the
  terminal), never by a remote peer.
- A motor above **70 °C** blocks power-on. The web app shows joint temperatures in orange from
  60 °C and red above 70 °C.
- Each step is at most 10°; the web app ramps at up to 60°/s (`--speed`), the CLI tools at 15°/s. Targets stay 5° inside each joint's soft limits (gripper: 0°, so it closes fully),
  never run more than 10° ahead of the real position (8° for joystick/key moves, so the arm stops
  within ~0.15 s when input stops), and a joint resting outside its safe band
  can only be moved back toward the inside.
- Remote team control: one controller at a time, heartbeat every second (control lost after 5 s
  of silence), anyone can Hold/Park, power-on and fault clearing only on the arm laptop.
- If the remote operator leaves, the arm holds. Parking returns to the power-on pose before
  releasing torque. If that can't be verified, the SDK keeps holding and asks you to type `RELEASE`.

All limits are at the top of `armkit.py`.

## Troubleshooting

| Message | What it means / what to do |
|---|---|
| `too hot to power on (J3 78°C ...)` | A motor is above 70 °C. Let it cool; if it stays hot for 10–15 min, tell the MakerMods staff. |
| `gripper (motor 7) at +82.7 deg is outside its limits ... stale or missing zero` | The gripper lost its zero (common after a power cycle or a thermal cut-off). Power-on is blocked on purpose. Close every app using that arm, then `python zero_gripper.py --port candle:0` (that arm's port): motors stay off, you close the gripper fully by hand, type `ZERO`, and it checks the new reading is ~0°. Only the gripper is touched. If a *joint* (J1–J6) shows this, ask the staff: that needs the full `maker-arm zero` at the folded pose. |
| `motor N enable rejected: mode=0` | The motor answers but won't energize. Run `python diagnose.py --port <port>` (app closed): if every motor shows **vbus 0.0V**, the arm has **no motor power**: release the e-stop, check the power supply (on, plugged, LED) and any fuse. |
| `gripper: CAN timeout read back 0, expected 10000` | Handled automatically now: that gripper's firmware ignores its 500 ms watchdog setting, so it runs without it (like older grippers). If you still see it, update the kit. |
| Any first problem | `python diagnose.py --port <port>` with the app closed: it reads every motor (motors stay off) and explains common findings. |
| `not responding` / USB `Input/Output Error` | That arm's CAN bus isn't answering: check its power supply and e-stop. The app keeps retrying. |
| `Access denied` / `could not open port` | Another program holds the adapter (a second web_app, MakerModsLab, upstream's robot). Close it. |
| Arm in **FAULT** | Use **Clear fault** on the arm laptop: it walks back to the power-on pose, then clears. |

## Joint map

1 base (shoulder pan) · 2 shoulder lift · 3 elbow · 4 wrist flex · 5 wrist yaw · 6 wrist roll · 7 gripper
