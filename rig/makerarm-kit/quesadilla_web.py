"""Quesadilla window inside the web app: http://127.0.0.1:8765/quesadilla (button "Quesadilla").

The same flow as stable/step1's standalone quesadilla_ui.py (Astra plans from a photo, the arm runs
the steps), but served by web_app.py on the arms it already holds, so there is ONE program on the
adapters and the main window keeps working (it shows the routine and its Hold/Park stop it).

  * Photo for Astra: this browser's camera or an uploaded photo (any OS, no OpenCV needed), or the
    overhead GLOBAL_CAM like quesadilla_ui.py (needs opencv-python; Linux /dev/video*).
  * Astra: NVIDIA NIM Responses API (NIM_BASE_URL / NIM_API_KEY / NIM_PLANNER_MODEL in .env).
    No key? "Sample plan" fills a fixed tortilla plan so the run path can be tested.
  * Run: tortilla_demo.py's bounded choreography (relative, soft-limit-clamped moves from the pose at
    power-on) narrated with the plan's steps, exactly like quesadilla_ui.py. Powering on happens here
    (with a confirmation) through the same checks as the main window; at the end the arm parks.
  * STOP (here) or Hold/Park (main window) stop it after the current move; the arm parks (verified).
Kept in sync by hand with quesadilla_ui.py: SYSTEM_PROMPT, the groceries file, the run loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

from aiohttp import web

from armkit import is_on, safe_range_deg
from maker_arm.arm import ArmState
from tortilla_demo import DEFAULT_TASKS, MOVES, TEMP_NAMES, WARN_C, gripper_rad, settle

HERE = Path(__file__).resolve().parent
GROCERIES = HERE / "groceries.json"
DEFAULT_GOAL = "make a quesadilla"
DEFAULT_SPEED = 20.0
NAME = "Quesadilla routine"

SYSTEM_PROMPT = """\
You are the visual strategic planner (intent decomposer) for a MakerMods robot arm driven by the
Dream-1 policy. You see one overhead photo of the workspace and a goal.

First identify the ingredients/tools the GOAL needs, and split them into which are VISIBLE in the
photo ("present") and which are NOT ("missing"). Then decompose the goal into an ordered list of
short imperative manipulation steps that Dream-1 can follow, grounded ONLY in the visible objects.
For each step give a contact point: the pixel [x, y] where the gripper should make contact ([0,0] is
top-left; x right, y down).
- One atomic manipulation per step, named on the specific object you see.
- Plan only steps doable with the VISIBLE objects (e.g. only a tortilla -> lay it flat, fold it,
  press it). Never return an empty steps list unless there is truly no manipulable object.
- At most nine steps, each under 200 characters.

Respond with ONLY this JSON:
{"reasoning": "<one sentence on the scene and plan>",
 "present": ["tortilla", ...],
 "missing": ["cheese", ...],
 "steps": [{"task": "...", "contact_xy": [x, y]}, ...]}"""

STATE: dict = {
    "state": "idle", "goal": "", "reasoning": "", "steps": [], "present": [], "missing": [],
    "step": 0, "total": 0, "label": "", "task": "", "temps": [], "arm": None,
    "running": False, "done": False, "error": "", "log": [], "frame": False,
}
_FRAME: dict = {"jpeg": None}


def log(msg: str) -> None:
    STATE["log"] = (STATE["log"] + [msg])[-40:]
    print(f"[quesadilla] {msg}", flush=True)


# ---------------------------------------------------------------- Astra (NIM Responses API, urllib)
def astra(jpeg: bytes, goal: str) -> tuple[str, list[dict], list[str], list[str]]:
    base = (os.environ.get("NIM_BASE_URL") or "https://inference-api.nvidia.com/v1").rstrip("/")
    model = os.environ.get("NIM_PLANNER_MODEL") or "azure/openai/gpt-6-astra"
    key = os.environ.get("NIM_API_KEY")
    if not key:
        raise RuntimeError("NIM_API_KEY is not set in .env (use 'Sample plan' to test without Astra)")
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    body = {
        "model": model, "instructions": SYSTEM_PROMPT, "max_output_tokens": 2048,
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": f"Goal: {goal}. Plan the ordered steps from this scene."},
            {"type": "input_image", "image_url": uri}]}],
    }
    req = urllib.request.Request(f"{base}/responses", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    text = data.get("output_text") or "".join(
        c.get("text", "") for o in data.get("output", []) for c in o.get("content", []) if c.get("type") == "output_text")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        obj = json.loads(m.group(0)) if m else {}
    if not isinstance(obj, dict):
        obj = {}
    steps = []
    for s in obj.get("steps", []):
        task = " ".join(str(s.get("task", "")).split())[:200]
        xy = s.get("contact_xy")
        pt = [int(xy[0]), int(xy[1])] if isinstance(xy, (list, tuple)) and len(xy) == 2 else None
        if task:
            steps.append({"task": task, "contact_xy": pt})
    present = [str(x) for x in obj.get("present", []) if str(x).strip()]
    missing = [str(x) for x in obj.get("missing", []) if str(x).strip()]
    return str(obj.get("reasoning", "")).strip(), steps[:9], present, missing


def global_cam_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("GLOBAL_CAM")) or os.path.exists("/dev/video0")


def grab_global_jpeg() -> bytes:
    """The overhead camera, as quesadilla_ui.py does it (needs opencv-python)."""
    import cv2
    dev = os.environ.get("GLOBAL_CAM") or "/dev/video0"
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2) if dev.startswith("/dev/") else cv2.VideoCapture(int(dev))
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    try:
        ok, frame = False, None
        for _ in range(8):
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"could not grab the overhead camera {dev}")
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return jpg.tobytes()
    finally:
        cap.release()


def load_groceries() -> list[str]:
    try:
        return json.loads(GROCERIES.read_text())
    except (OSError, ValueError):
        return []


def add_groceries(items: list[str]) -> list[str]:
    have = load_groceries()
    lower = {g.lower() for g in have}
    for item in items:
        item = str(item).strip()
        if item and item.lower() not in lower:
            have.append(item)
            lower.add(item.lower())
    GROCERIES.write_text(json.dumps(have, indent=2))
    return have


# ---------------------------------------------------------------- the run (background thread)
def execute(slot, steps: list[dict], speed: float, stop: threading.Event) -> None:
    bridge = slot.bridge
    arm = bridge.arm
    STATE.update(running=True, done=False, error="", state="running", step=0, total=len(MOVES), arm=slot.id)
    try:
        if arm.state is ArmState.FAULT:
            raise RuntimeError(f"the arm is in FAULT ({arm.fault_reason}); use Clear fault in the main window first")
        if not is_on(arm):
            msg = bridge.power_up()  # same checks as the main window: temperature, gripper watchdog
            log(msg)
            if not is_on(arm):
                raise RuntimeError(msg)
            time.sleep(0.3)
        start = arm.get_commanded_positions()  # all moves are relative to this pose
        for mi, (label, deltas, grip) in enumerate(MOVES):
            if stop.is_set():
                log("stop requested")
                break
            task = steps[mi % len(steps)]["task"] if steps else label
            STATE.update(step=mi + 1, label=label, task=task)
            bridge.last_msg = f"{NAME} {mi + 1}/{len(MOVES)}: [{label}] {task}"
            log(f"[{label}] {task}")
            targets = list(start)
            for j, d in deltas.items():
                lo, hi = safe_range_deg(arm, j)
                targets[j - 1] = math.radians(min(max(math.degrees(start[j - 1]) + d, lo), hi))
            if grip:
                targets[6] = gripper_rad(arm, grip)
            settle(arm, targets, speed)
            bridge.targets_deg = [math.degrees(t) for t in targets]
            temps = arm.get_temperatures()
            STATE["temps"] = [round(t, 0) if t == t else None for t in temps]
            hot = [TEMP_NAMES[i] for i in range(len(temps)) if temps[i] == temps[i] and temps[i] >= WARN_C]
            if hot:
                log(f"WARNING hot: {', '.join(hot)}")
            if arm.state is ArmState.FAULT:
                raise RuntimeError(arm.fault_reason or "motor fault")
        STATE["done"] = not stop.is_set()
        log("plan complete" if STATE["done"] else "stopped")
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = str(exc)
        log(f"error: {exc}")
    finally:
        try:
            if arm.state in (ArmState.ENABLED, ArmState.FAULT):
                if arm.stop():  # back to the power-on pose, verified, torque off
                    log("parked: back at the start pose, torque off")
                else:
                    STATE["error"] = (STATE["error"] + " | ") * bool(STATE["error"]) + \
                        "could not verify the return to rest: the arm is HOLDING; support it, then Park"
                    log("could not verify rest; arm holding")
        except Exception as exc:  # noqa: BLE001
            log(f"shutdown note: {exc}")
        if not is_on(arm):
            bridge.targets_deg = None
        bridge.last_msg = f"{NAME}: " + ("done, parked." if STATE["done"] else (STATE["error"] or "stopped, parked."))
        slot.busy, slot.busy_stop = None, None
        STATE.update(running=False, state="idle")


# ---------------------------------------------------------------- HTTP (mounted on web_app)
def mount(app: web.Application, hub) -> None:
    def arms_info() -> list[dict]:
        return [{"id": s.id, "port": s.port or "sim", "ok": s.bridge is not None,
                 "on": bool(s.bridge and is_on(s.bridge.arm))} for s in hub.slots]

    async def page(_):
        return web.Response(text=PAGE, content_type="text/html")

    async def info(_):
        return web.json_response({"arms": arms_info(), "astra": bool(os.environ.get("NIM_API_KEY")),
                                  "global_cam": global_cam_available(), "goal": DEFAULT_GOAL,
                                  "speed": DEFAULT_SPEED, "moves": len(MOVES), "sim": hub.sim})

    async def plan(request: web.Request):
        body = await request.json()
        goal = (body.get("goal") or DEFAULT_GOAL).strip()[:200]
        STATE.update(goal=goal, state="planning", error="")
        log(f"planning: {goal!r}")
        try:
            image = body.get("image") or ""
            if image:
                jpeg = base64.b64decode(image.split(",", 1)[-1])
            elif body.get("source") == "global":
                jpeg = await asyncio.to_thread(grab_global_jpeg)
            else:
                raise RuntimeError("no photo: use the camera, upload a photo, or the overhead camera")
            _FRAME["jpeg"] = jpeg
            STATE["frame"] = True
            reasoning, steps, present, missing = await asyncio.to_thread(astra, jpeg, goal)
            STATE.update(reasoning=reasoning, steps=steps, present=present, missing=missing, state="idle")
            if missing:
                log(f"MISSING ingredients: {', '.join(missing)}")
            log(f"plan: {len(steps)} steps, present={present}, missing={missing}")
            return web.json_response({"reasoning": reasoning, "steps": steps, "present": present,
                                      "missing": missing, "groceries": load_groceries()})
        except Exception as exc:  # noqa: BLE001
            STATE.update(state="idle", error=str(exc))
            log(f"plan error: {exc}")
            return web.json_response({"error": str(exc)})

    async def sample(_):
        steps = [{"task": t, "contact_xy": None} for t in DEFAULT_TASKS]
        STATE.update(goal=DEFAULT_GOAL, reasoning="Sample plan (no Astra call): the default tortilla tasks.",
                     steps=steps, present=["tortilla"], missing=[], error="")
        log(f"sample plan: {len(steps)} steps")
        return web.json_response({"reasoning": STATE["reasoning"], "steps": steps, "present": ["tortilla"],
                                  "missing": [], "groceries": load_groceries()})

    async def groceries(request: web.Request):
        if request.method == "POST":
            items = [str(x) for x in (await request.json()).get("items", [])][:20]
            have = await asyncio.to_thread(add_groceries, items)
            log(f"added to groceries: {', '.join(items)}")
            return web.json_response({"groceries": have})
        return web.json_response({"groceries": load_groceries()})

    async def run(request: web.Request):
        body = await request.json()
        if STATE["running"]:
            return web.json_response({"error": "already running"}, status=409)
        if not STATE["steps"]:
            return web.json_response({"error": "make a plan first (or Sample plan)"}, status=409)
        slot = hub.by_id.get(str(body.get("arm") or ""))
        if slot is None or slot.bridge is None:
            return web.json_response({"error": "that arm is not connected"}, status=409)
        if hub.control.holder:
            return web.json_response({"error": f"{hub.control.holder} is driving remotely; take control back "
                                               "in the main window first"}, status=409)
        speed = min(max(float(body.get("speed") or DEFAULT_SPEED), 5.0), 30.0)
        stop = threading.Event()
        slot.busy, slot.busy_stop = NAME, stop
        STATE.update(running=True, state="running", error="")  # before the thread starts: no double run
        threading.Thread(target=execute, args=(slot, list(STATE["steps"]), speed, stop), daemon=True,
                         name="quesadilla-run").start()
        return web.json_response({"ok": True})

    async def stop(_):
        n = 0
        for s in hub.slots:
            if s.busy_stop is not None:
                s.busy_stop.set()
                n += 1
        log("STOP: finishing the current move, then parking" if n else "STOP: nothing running")
        return web.json_response({"ok": True})

    async def status(_):
        return web.json_response({**STATE, "arms": arms_info()})

    async def frame(_):
        if not _FRAME["jpeg"]:
            raise web.HTTPNotFound()
        return web.Response(body=_FRAME["jpeg"], content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    app.router.add_get("/quesadilla", page)
    app.router.add_get("/quesadilla/info", info)
    app.router.add_post("/quesadilla/plan", plan)
    app.router.add_post("/quesadilla/sample", sample)
    app.router.add_get("/quesadilla/groceries", groceries)
    app.router.add_post("/quesadilla/groceries", groceries)
    app.router.add_post("/quesadilla/run", run)
    app.router.add_post("/quesadilla/stop", stop)
    app.router.add_get("/quesadilla/status", status)
    app.router.add_get("/quesadilla/frame.jpg", frame)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quesadilla</title>
<style>
  :root { --bg:#f5f5f3; --panel:#fff; --line:#e3e2de; --text:#1d1d1b; --muted:#6d6c67; --accent:#2f6fde;
          --accent-soft:#e6eefc; --ok:#1f8a4c; --ok-soft:#e3f4ea; --warn:#b4540f; --warn-soft:#fbeee2;
          --bad:#c62f2f; --bad-soft:#fbe6e6; --track:#ecebe7; }
  @media (prefers-color-scheme: dark) { :root { --bg:#151514; --panel:#1f1f1d; --line:#33322f; --text:#ecebe6;
          --muted:#9c9a93; --accent:#6c9cf0; --accent-soft:#23304a; --ok:#4cc283; --ok-soft:#1c3528; --warn:#e59a5b;
          --warn-soft:#3a2a1c; --bad:#ef7070; --bad-soft:#3e2020; --track:#2c2b28; } }
  * { box-sizing:border-box; } [hidden] { display:none !important; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { display:flex; gap:10px; align-items:center; flex-wrap:wrap; padding:14px 20px; background:var(--panel);
           border-bottom:1px solid var(--line); }
  h1 { font-size:16px; margin:0; } .spacer { flex:1; }
  .pill { padding:3px 10px; border-radius:99px; font-size:12px; font-weight:600; background:var(--track); color:var(--muted); }
  .pill.run { background:var(--ok-soft); color:var(--ok); } .pill.bad { background:var(--bad-soft); color:var(--bad); }
  main { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(320px,1fr); gap:16px; padding:16px 20px; max-width:1300px; margin:0 auto; }
  @media (max-width:900px) { main { grid-template-columns:1fr; padding:12px 16px; } }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; }
  .card h2 { font-size:13px; margin:0 0 10px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px; }
  input, select, button { font:inherit; color:var(--text); }
  input, select { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:7px 9px; }
  button { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:7px 12px; cursor:pointer; font-weight:550; }
  button:hover:not(:disabled) { border-color:var(--accent); } button:disabled { opacity:.45; cursor:not-allowed; }
  .primary { background:var(--ok); border-color:var(--ok); color:#fff; }
  .danger { background:var(--bad); border-color:var(--bad); color:#fff; }
  .seg { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .seg button { border:0; border-radius:0; } .seg button.sel { background:var(--accent-soft); color:var(--accent); }
  .shot { position:relative; background:#000; border-radius:8px; overflow:hidden; aspect-ratio:16/9; }
  .shot video, .shot canvas { width:100%; height:100%; object-fit:contain; display:block; }
  .empty { position:absolute; inset:0; display:grid; place-items:center; color:#aaa; text-align:center; padding:20px; }
  #goal { flex:1; min-width:200px; font-size:16px; }
  ol { margin:0; padding-left:22px; } ol li { padding:3px 0; } li.active { color:var(--ok); font-weight:700; }
  .msg { margin-top:10px; padding:8px 10px; border-radius:8px; background:var(--track); color:var(--muted); font-size:13px; min-height:2.4em; }
  .msg.bad { background:var(--bad-soft); color:var(--bad); }
  #alert { background:var(--warn-soft); color:var(--warn); border:1px solid var(--warn); padding:10px; border-radius:8px; margin-top:10px; }
  .chip { display:inline-block; background:var(--accent-soft); color:var(--accent); padding:1px 9px; border-radius:99px; margin:2px; font-size:12px; }
  #log { font:12px/1.4 ui-monospace,Consolas,monospace; white-space:pre-wrap; background:var(--track); padding:8px; border-radius:8px;
         height:160px; overflow:auto; }
  .muted { color:var(--muted); font-size:12px; }
</style></head>
<body>
<header>
  <h1>🌮 Quesadilla · Astra plans, the arm runs it</h1>
  <span id="state" class="pill">idle</span>
  <span class="spacer"></span>
  <label class="muted" for="arm">Arm</label><select id="arm"></select>
  <label class="muted" for="speed">Speed</label>
  <select id="speed"><option value="10">10°/s</option><option value="20" selected>20°/s</option><option value="30">30°/s</option></select>
  <button id="stop" class="danger">STOP</button>
</header>
<main>
  <section class="card">
    <h2>Scene photo for Astra</h2>
    <div class="row" style="margin-top:0">
      <div class="seg" id="src"><button data-s="cam">This camera</button><button data-s="file">Upload photo</button><button data-s="global">Overhead cam</button></div>
      <select id="camSel" hidden></select>
      <input type="file" id="file" accept="image/*" hidden>
    </div>
    <div class="shot" style="margin-top:10px">
      <video id="video" autoplay playsinline muted></video>
      <canvas id="canvas" hidden></canvas>
      <div id="empty" class="empty">Allow the camera, upload a photo, or use the overhead camera.</div>
    </div>
    <div class="muted" style="margin-top:6px">After planning, the photo freezes with Astra's contact points (green, numbered by step).</div>
  </section>
  <section class="card">
    <h2>Goal</h2>
    <div class="row" style="margin-top:0">
      <input id="goal" value="make a quesadilla">
      <button id="make" class="primary">Plan with Astra</button>
      <button id="sample" title="Fills the default tortilla plan without calling Astra">Sample plan</button>
    </div>
    <div id="reasoning" class="msg">Plan first, then Run. Powering the motors happens here, with a confirmation.</div>
    <div id="alert" hidden><b id="alertmsg"></b>
      <div class="row"><button id="addG">Add to groceries list</button><button id="proceed">Proceed with what's here</button></div></div>
    <p>Present: <span id="present">–</span><br>Missing: <span id="missing">–</span></p>
    <h2>Plan (→ the arm)</h2>
    <ol id="steps"><li class="muted">no plan yet</li></ol>
    <div class="row"><button id="run" class="primary" disabled>▶ Run on the arm</button><span id="armnote" class="muted"></span></div>
    <h2 style="margin-top:16px">🛒 Groceries list</h2><ul id="groceries"></ul>
    <h2>Log</h2><div id="log"></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let info = null, src = "cam", plan = null, MISSING = [], frozen = false;
const esc = (t) => String(t).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const chips = (a) => (a || []).map(x => `<span class="chip">${esc(x)}</span>`).join(" ") || '<span class="muted">none</span>';
async function j(url, body) {
  const r = await fetch(url, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}
// ---- photo sources ----
async function startCam(id) {
  try {
    const v = $("video");
    if (v.srcObject) v.srcObject.getTracks().forEach(t => t.stop());
    v.srcObject = await navigator.mediaDevices.getUserMedia({ video: id ? { deviceId: { exact: id }, width: 1280, height: 720 } : { width: 1280, height: 720 } });
    const cams = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput");
    $("camSel").innerHTML = cams.map((c, i) => `<option value="${c.deviceId}">${esc(c.label || "Camera " + (i + 1))}</option>`).join("");
    $("camSel").value = v.srcObject.getVideoTracks()[0].getSettings().deviceId; $("camSel").hidden = cams.length < 2;
    $("empty").hidden = true; unfreeze();
  } catch (e) { $("empty").hidden = false; $("empty").textContent = "Camera unavailable: " + e.message; }
}
$("camSel").onchange = (e) => startCam(e.target.value);
function setSrc(s) {
  src = s;
  $("src").querySelectorAll("button").forEach(b => b.classList.toggle("sel", b.dataset.s === s));
  if (s === "cam") startCam();
  if (s === "file") $("file").click();
  if (s === "global") { unfreeze(); $("video").hidden = true; $("empty").hidden = false; $("empty").textContent = "The overhead camera (GLOBAL_CAM) is read on the arm laptop when you plan."; }
}
$("src").querySelectorAll("button").forEach(b => b.onclick = () => setSrc(b.dataset.s));
$("file").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  const img = new Image(); img.src = URL.createObjectURL(f); await img.decode();
  drawFrame(img, []); $("empty").hidden = true;
};
function unfreeze() { frozen = false; $("canvas").hidden = true; $("video").hidden = src !== "cam"; }
function drawFrame(img, steps) {
  const c = $("canvas"), w = img.videoWidth || img.naturalWidth || img.width, h = img.videoHeight || img.naturalHeight || img.height;
  c.width = w; c.height = h;
  const g = c.getContext("2d"); g.drawImage(img, 0, 0, w, h);
  (steps || []).forEach((s, i) => {
    if (!s.contact_xy) return;
    const [x, y] = s.contact_xy;
    g.strokeStyle = g.fillStyle = "#00ff66"; g.lineWidth = 3;
    g.beginPath(); g.arc(x, y, 14, 0, 7); g.stroke(); g.beginPath(); g.arc(x, y, 4, 0, 7); g.fill();
    g.font = "bold 22px system-ui"; g.fillText(String(i + 1), x + 16, y - 10);
  });
  c.hidden = false; $("video").hidden = true; frozen = true;
}
function snapshotJpeg() {
  if (src === "global") return null;
  if (src === "cam") { const v = $("video"); if (!v.videoWidth) return null; drawFrame(v, []); }
  const c = $("canvas"); if (c.hidden && !frozen) return null;
  return c.toDataURL("image/jpeg", 0.88);
}
// ---- plan ----
function showPlan(r) {
  plan = r;
  $("reasoning").textContent = r.reasoning || ""; $("reasoning").classList.remove("bad");
  $("present").innerHTML = chips(r.present); $("missing").innerHTML = chips(r.missing);
  $("steps").innerHTML = (r.steps || []).map(s => `<li>${esc(s.task)}${s.contact_xy ? ` <span class="muted">@[${s.contact_xy}]</span>` : ""}</li>`).join("") || '<li class="muted">no steps</li>';
  refreshGroceries(r.groceries);
  MISSING = r.missing || [];
  $("alert").hidden = !MISSING.length;
  if (MISSING.length) $("alertmsg").textContent = `Missing ${MISSING.join(", ")}! Add it to the groceries list?`;
  updateRun();
}
$("make").onclick = async () => {
  const image = snapshotJpeg();
  if (!image && src !== "global") { bad("No photo yet: allow the camera, upload a photo, or pick Overhead cam."); return; }
  $("make").disabled = true; $("reasoning").textContent = "Astra is planning…"; $("reasoning").classList.remove("bad");
  try {
    const r = await j("quesadilla/plan", { goal: $("goal").value, image, source: src });
    if (r.error) { bad("Astra: " + r.error); return; }
    if (src === "global") { const img = new Image(); img.src = "quesadilla/frame.jpg?" + Date.now(); await img.decode(); drawFrame(img, r.steps); }
    else { const img = new Image(); img.src = image; await img.decode(); drawFrame(img, r.steps); }
    showPlan(r);
  } finally { $("make").disabled = false; }
};
$("sample").onclick = async () => showPlan(await j("quesadilla/sample", {}));
function bad(t) { $("reasoning").textContent = t; $("reasoning").classList.add("bad"); }
async function refreshGroceries(list) {
  const g = list || (await j("quesadilla/groceries")).groceries || [];
  $("groceries").innerHTML = g.map(x => `<li>${esc(x)}</li>`).join("") || '<li class="muted">empty</li>';
}
$("addG").onclick = async () => { const r = await j("quesadilla/groceries", { items: MISSING }); refreshGroceries(r.groceries); $("alertmsg").textContent = `Added ${MISSING.join(", ")} to the groceries list.`; };
$("proceed").onclick = () => { $("alert").hidden = true; runIt(); };
// ---- run ----
function armState() { return (info && info.arms || []).find(a => a.id === $("arm").value); }
function updateRun() {
  const a = armState();
  $("run").disabled = !plan || !(plan.steps || []).length || !a || !a.ok || (last && last.running);
  $("armnote").textContent = !a ? "" : !a.ok ? `the ${a.id} arm is not connected` : a.on ? `the ${a.id} arm is on` : `the ${a.id} arm is off: Run powers it on`;
}
async function runIt() {
  const a = armState(); if (!a) return;
  if (!confirm(`Run ${info.moves} moves on the ${a.id} arm${info.sim ? " (simulator)" : ""}?\n\nArea clear, arm clamped, a hand near the power switch.\nIt powers on if needed and parks at the end.`)) return;
  const r = await j("quesadilla/run", { arm: a.id, speed: +$("speed").value });
  if (r.error) bad(r.error);
}
$("run").onclick = runIt;
$("stop").onclick = () => j("quesadilla/stop", {});
$("arm").onchange = () => { try { localStorage.setItem("qArm", $("arm").value); } catch (e) {} updateRun(); };
// ---- status ----
let last = null;
async function poll() {
  try {
    const s = last = await j("quesadilla/status");
    info.arms = s.arms;
    $("state").textContent = s.state + (s.running ? ` ${s.step}/${s.total} · ${s.label} (${s.arm})` : "");
    $("state").className = "pill" + (s.running ? " run" : s.error ? " bad" : "");
    const n = (s.steps || []).length;
    [...$("steps").querySelectorAll("li")].forEach((li, i) => li.classList.toggle("active", s.running && n && ((s.step - 1) % n) === i));
    const lg = $("log"), atEnd = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 4;
    lg.textContent = (s.log || []).join("\n"); if (atEnd) lg.scrollTop = lg.scrollHeight;
    if (s.error && !s.running) bad("⚠ " + s.error);
    [...$("arm").options].forEach(o => { const a = s.arms.find(x => x.id === o.value); o.textContent = a ? `${a.id} · ${!a.ok ? "not connected" : a.on ? "on" : "off"}` : o.value; });
    updateRun();
  } catch (e) {}
  setTimeout(poll, 600);
}
(async () => {
  info = await j("quesadilla/info");
  $("goal").value = info.goal;
  let saved = null; try { saved = localStorage.getItem("qArm"); } catch (e) {}
  $("arm").innerHTML = info.arms.map(a => `<option value="${a.id}">${a.id}</option>`).join("");
  $("arm").value = (info.arms.find(a => a.id === saved) || info.arms.find(a => a.id === "right" && a.ok) || info.arms.find(a => a.ok) || info.arms[0] || {}).id || "";
  $("src").querySelector('[data-s="global"]').hidden = !info.global_cam;
  if (!info.astra) $("reasoning").textContent = "NIM_API_KEY isn't set in .env, so Astra can't plan here: use Sample plan to test the run.";
  refreshGroceries(); setSrc("cam"); poll();
})();
</script>
</body></html>"""
