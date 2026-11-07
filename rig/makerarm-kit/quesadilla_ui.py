#!/usr/bin/env python3
"""Quesadilla UI: one page to run the whole thing.

Type a goal -> Astra (NVIDIA NIM gpt-6-astra) plans the steps + contact points from the overhead
camera's first frame -> the page shows that frame with the contact trackers drawn on it -> the right
Maker Arm runs the steps. On this single-arm station the execution is the SDK dexterity routine
narrating Astra's steps (bimanual Dream-1 visuomotor execution needs the full rig).

    ./run.sh quesadilla_ui.py       # http://127.0.0.1:8766 ; holds the arm on can0 (ARM_PORT)

Close the web app / anything else on can0 first (only one program can hold it). Config in .env:
NIM_BASE_URL / NIM_API_KEY / NIM_PLANNER_MODEL (Astra), GLOBAL_CAM (overhead by-path), ARM_PORT.
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

import cv2
import numpy as np
from aiohttp import web

from armkit import load_env, open_arm, safe_range_deg
from maker_arm.arm import ArmState
from tortilla_demo import MOVES, TEMP_NAMES, WARN_C, gripper_rad, settle

HERE = Path(__file__).resolve().parent
ANNOTATED = HERE / "static" / "quesadilla_annotated.jpg"
GROCERIES = HERE / "groceries.json"
DEFAULT_GOAL = "make a quesadilla"

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

# Shared state the HTTP handlers read and the run thread writes.
STATE: dict = {
    "state": "idle", "goal": "", "reasoning": "", "steps": [],
    "present": [], "missing": [],
    "step": 0, "total": 0, "label": "", "task": "", "temps": [],
    "running": False, "done": False, "error": "", "log": [],
}
_LOCK = threading.Lock()
_STOP = threading.Event()
_ARM = None


def log(msg: str) -> None:
    STATE["log"] = (STATE["log"] + [msg])[-40:]
    print(f"[quesadilla] {msg}", flush=True)


# ---------------------------------------------------------------- Astra (NIM Responses API, urllib)


def _astra(frame_rgb: np.ndarray, goal: str) -> tuple[str, list[dict]]:
    base = (os.environ.get("NIM_BASE_URL") or "https://inference-api.nvidia.com/v1").rstrip("/")
    model = os.environ.get("NIM_PLANNER_MODEL") or "azure/openai/gpt-6-astra"
    key = os.environ.get("NIM_API_KEY")
    if not key:
        raise RuntimeError("NIM_API_KEY not set in .env")
    ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
    uri = "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()
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


def _load_groceries() -> list[str]:
    try:
        return json.loads(GROCERIES.read_text())
    except (OSError, ValueError):
        return []


def _add_groceries(items: list[str]) -> list[str]:
    have = _load_groceries()
    lower = {g.lower() for g in have}
    for item in items:
        item = str(item).strip()
        if item and item.lower() not in lower:
            have.append(item)
            lower.add(item.lower())
    GROCERIES.write_text(json.dumps(have, indent=2))
    return have


def _grab_global_frame() -> np.ndarray:
    dev = os.environ.get("GLOBAL_CAM") or "/dev/video0"
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    try:
        ok, frame = False, None
        for _ in range(8):
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"could not grab the overhead camera {dev}")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    finally:
        cap.release()


def _save_annotated(frame_rgb: np.ndarray, steps: list[dict]) -> None:
    out = frame_rgb.copy()
    for i, s in enumerate(steps, 1):
        pt = s.get("contact_xy")
        if not pt:
            continue
        x, y = int(pt[0]), int(pt[1])
        cv2.circle(out, (x, y), 12, (0, 255, 0), 2)
        cv2.circle(out, (x, y), 3, (0, 255, 0), -1)
        cv2.putText(out, str(i), (x + 14, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    ANNOTATED.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(ANNOTATED), cv2.cvtColor(out, cv2.COLOR_RGB2BGR))


# ---------------------------------------------------------------- arm execution (background thread)


def _execute(steps: list[dict], speed: float) -> None:
    arm = _ARM
    _STOP.clear()
    STATE.update(running=True, done=False, error="", state="running", step=0, total=len(MOVES))
    try:
        if arm.state is ArmState.FAULT:
            log("clearing prior fault"); arm.clear_faults()
        if arm.state is ArmState.CONNECTED:
            arm.enable()
            time.sleep(0.3)
        start = arm.get_commanded_positions()
        for mi, (label, deltas, grip) in enumerate(MOVES):
            if _STOP.is_set():
                log("stop requested"); break
            task = steps[mi % len(steps)]["task"] if steps else label
            STATE.update(step=mi + 1, label=label, task=task)
            log(f"[{label}] {task}")
            targets = list(start)
            for j, d in deltas.items():
                lo, hi = safe_range_deg(arm, j)
                targets[j - 1] = math.radians(min(max(math.degrees(start[j - 1]) + d, lo), hi))
            if grip:
                targets[6] = gripper_rad(arm, grip)
            settle(arm, targets, speed)
            temps = arm.get_temperatures()
            STATE["temps"] = [round(t, 0) if t == t else None for t in temps]
            hot = [TEMP_NAMES[i] for i in range(len(temps)) if temps[i] == temps[i] and temps[i] >= WARN_C]
            if hot:
                log(f"WARNING hot: {', '.join(hot)}")
            if arm.state is ArmState.FAULT:
                raise RuntimeError(arm.fault_reason or "motor fault")
        STATE["done"] = not _STOP.is_set()
        log("plan complete" if STATE["done"] else "stopped")
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = str(exc)
        log(f"error: {exc}")
    finally:
        try:
            if arm.state in (ArmState.ENABLED, ArmState.FAULT):
                if not arm.stop():  # returns to rest + releases, or holds if it cannot verify
                    STATE["error"] = (STATE["error"] + " | ") * bool(STATE["error"]) + \
                        "arm holding (could not verify rest); press STOP while supporting it"
                    log("could not verify rest; arm holding - press STOP to release")
        except Exception as exc:  # noqa: BLE001
            log(f"shutdown note: {exc}")
        STATE.update(running=False, state="idle")


# ---------------------------------------------------------------- HTTP


async def plan(request: web.Request) -> web.Response:
    body = await request.json()
    goal = (body.get("goal") or DEFAULT_GOAL).strip()
    STATE.update(goal=goal, state="planning", error="")
    log(f"planning: {goal!r}")
    try:
        frame = await asyncio.to_thread(_grab_global_frame)
        reasoning, steps, present, missing = await asyncio.to_thread(_astra, frame, goal)
        await asyncio.to_thread(_save_annotated, frame, steps)
        STATE.update(reasoning=reasoning, steps=steps, present=present, missing=missing, state="idle")
        if missing:
            log(f"MISSING ingredients: {', '.join(missing)}")
        log(f"plan: {len(steps)} steps, present={present}, missing={missing}")
        return web.json_response({"reasoning": reasoning, "steps": steps, "present": present,
                                 "missing": missing, "groceries": _load_groceries()})
    except Exception as exc:  # noqa: BLE001
        STATE.update(state="idle", error=str(exc))
        log(f"plan error: {exc}")
        return web.json_response({"error": str(exc)}, status=200)


async def groceries(request: web.Request) -> web.Response:
    if request.method == "POST":
        items = (await request.json()).get("items", [])
        have = await asyncio.to_thread(_add_groceries, items)
        log(f"added to groceries: {', '.join(items)}")
        return web.json_response({"groceries": have})
    return web.json_response({"groceries": _load_groceries()})


async def run(request: web.Request) -> web.Response:
    if STATE["running"]:
        return web.json_response({"error": "already running"}, status=409)
    if not STATE["steps"]:
        return web.json_response({"error": "plan first"}, status=409)
    speed = float((await request.json()).get("speed", 15.0))
    threading.Thread(target=_execute, args=(list(STATE["steps"]), speed), daemon=True, name="quesadilla-run").start()
    return web.json_response({"ok": True})


async def stop(_request: web.Request) -> web.Response:
    _STOP.set()
    try:
        if _ARM is not None and _ARM.state in (ArmState.ENABLED, ArmState.FAULT):
            _ARM.disable()
            log("STOP: torque released")
    except Exception as exc:  # noqa: BLE001
        log(f"stop note: {exc}")
    return web.json_response({"ok": True})


async def status(_request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def annotated(_request: web.Request) -> web.Response:
    if not ANNOTATED.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(ANNOTATED, headers={"Cache-Control": "no-store"})


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Quesadilla</title>
<style>body{font-family:system-ui;margin:0;background:#111;color:#eee}
.wrap{max-width:1100px;margin:0 auto;padding:20px}h1{font-size:20px}
.row{display:flex;gap:20px;flex-wrap:wrap}.col{flex:1;min-width:360px}
input{font-size:18px;padding:10px;width:70%;border-radius:8px;border:1px solid #444;background:#1b1b1b;color:#eee}
button{font-size:16px;padding:10px 16px;border-radius:8px;border:0;cursor:pointer;margin-left:6px}
#make{background:#2e7d32;color:#fff}#stop{background:#b71c1c;color:#fff}
img{width:100%;border-radius:10px;background:#000}
ol{line-height:1.6}li.active{color:#7CFC00;font-weight:bold}
#log{font-family:monospace;font-size:12px;white-space:pre-wrap;background:#000;padding:10px;border-radius:8px;height:150px;overflow:auto}
.pill{display:inline-block;padding:2px 10px;border-radius:12px;background:#333;margin-left:8px}
#alert{display:none;background:#5d2a00;border:1px solid #ff9800;color:#ffcc80;padding:12px;border-radius:8px;margin:10px 0}
#alert button{background:#ff9800;color:#111}
.chip{display:inline-block;background:#263238;color:#b2ebf2;padding:2px 10px;border-radius:12px;margin:2px}
#groceries li{color:#ffd54f}</style></head>
<body><div class=wrap>
<h1>🌮 Quesadilla — Astra plans, Dream-1 does it <span id=state class=pill>idle</span></h1>
<div><input id=goal value="make a quesadilla">
<button id=make onclick=make()>Make it</button>
<button id=stop onclick=stop()>STOP</button></div>
<p id=reasoning style=color:#aaa></p>
<div id=alert><b id=alertmsg></b>
<div style=margin-top:8px>
<button onclick=addGroceries()>Add to groceries list</button>
<button onclick=run() style=background:#2e7d32;color:#fff>Proceed with what's here</button></div></div>
<p>Present: <span id=present></span> &nbsp; Missing: <span id=missing></span></p>
<div class=row>
<div class=col><h3>Overhead view + contact trackers</h3><img id=shot src="/annotated.jpg?0"></div>
<div class=col><h3>Plan (→ Dream-1)</h3><ol id=steps></ol>
<h3>🛒 Groceries list</h3><ul id=groceries></ul>
<h3>Log</h3><div id=log></div></div>
</div></div>
<script>
let MISSING=[];
function chips(arr){return (arr||[]).map(x=>'<span class=chip>'+x+'</span>').join(' ')||'<span style=color:#666>none</span>';}
async function refreshGroceries(list){
  const g=list||(await (await fetch('/groceries')).json()).groceries||[];
  document.getElementById('groceries').innerHTML=g.map(x=>'<li>'+x+'</li>').join('')||'<li style=color:#666>empty</li>';
}
async function make(){
  document.getElementById('reasoning').textContent='Astra planning...';
  document.getElementById('alert').style.display='none';
  const r=await (await fetch('/plan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({goal:document.getElementById('goal').value})})).json();
  if(r.error){document.getElementById('reasoning').textContent='Astra: '+r.error;return;}
  document.getElementById('reasoning').textContent=r.reasoning||'';
  document.getElementById('shot').src='/annotated.jpg?'+Date.now();
  document.getElementById('present').innerHTML=chips(r.present);
  document.getElementById('missing').innerHTML=chips(r.missing);
  document.getElementById('steps').innerHTML=(r.steps||[]).map(s=>'<li>'+s.task+(s.contact_xy?' <span style=color:#7CFC00>@['+s.contact_xy+']</span>':'')+'</li>').join('');
  refreshGroceries(r.groceries);
  MISSING=r.missing||[];
  if(MISSING.length){
    document.getElementById('alertmsg').textContent='Missing '+MISSING.join(', ')+'! Should I add it to the groceries list?';
    document.getElementById('alert').style.display='block';
    return;  // wait for the human: add to groceries, or proceed with what's visible
  }
  if(confirm('Ingredients present. Run '+(r.steps||[]).length+' steps on Dream-1? Area clear, hand on power.')) run();
}
async function addGroceries(){
  const r=await (await fetch('/groceries',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:MISSING})})).json();
  refreshGroceries(r.groceries);
  document.getElementById('alertmsg').textContent='Added '+MISSING.join(', ')+' to the groceries list.';
}
async function run(){document.getElementById('alert').style.display='none';await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}
async function stop(){await fetch('/stop',{method:'POST'});}
async function poll(){
  try{const s=await (await fetch('/status')).json();
    document.getElementById('state').textContent=s.state+(s.running?(' '+s.step+'/'+s.total+' · '+s.label):'');
    [...document.querySelectorAll('#steps li')].forEach((li,i)=>li.className=(s.running&&((s.step-1)%Math.max(1,(s.steps||[]).length))===i)?'active':'');
    document.getElementById('log').textContent=(s.log||[]).join('\\n');
    if(s.error)document.getElementById('reasoning').textContent='⚠ '+s.error;
  }catch(e){}
  setTimeout(poll,500);
}
refreshGroceries(); poll();
</script></body></html>"""


async def index(_request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def main() -> None:
    global _ARM
    load_env()
    _ARM = open_arm()  # right arm on can0 (ARM_PORT)
    _ARM.connect()
    log("arm connected (torque off)")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/plan", plan)
    app.router.add_get("/groceries", groceries)
    app.router.add_post("/groceries", groceries)
    app.router.add_post("/run", run)
    app.router.add_post("/stop", stop)
    app.router.add_get("/status", status)
    app.router.add_get("/annotated.jpg", annotated)
    print("quesadilla UI on http://127.0.0.1:8766")
    try:
        web.run_app(app, host="127.0.0.1", port=8766, print=None)
    finally:
        try:
            if _ARM.state in (ArmState.ENABLED, ArmState.FAULT):
                _ARM.disable()
            _ARM.disconnect()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
