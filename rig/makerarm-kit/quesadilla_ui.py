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
DEFAULT_GOAL = "make a quesadilla"

SYSTEM_PROMPT = """\
You are the visual strategic planner for a MakerMods robot arm. You see one overhead photo of the
workspace and a goal. Produce an ordered list of short imperative manipulation steps grounded ONLY
in what the photo shows. For each step give a contact point: the pixel [x, y] in THIS image where
the gripper should make contact. [0,0] is top-left; x right, y down.
- One atomic manipulation per step, named on the specific object you see.
- If an ingredient the goal needs is not visible, do not refuse: plan the steps you CAN do with the
  visible objects (e.g. only a tortilla -> lay it flat, fold it, press it). Never return empty unless
  there is truly no manipulable object. At most nine steps, each under 200 characters.
Respond with ONLY this JSON: {"reasoning": "<one sentence>", "steps": [{"task": "...", "contact_xy": [x, y]}, ...]}"""

# Shared state the HTTP handlers read and the run thread writes.
STATE: dict = {
    "state": "idle", "goal": "", "reasoning": "", "steps": [],
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
    steps = []
    for s in obj.get("steps", []) if isinstance(obj, dict) else []:
        task = " ".join(str(s.get("task", "")).split())[:200]
        xy = s.get("contact_xy")
        pt = [int(xy[0]), int(xy[1])] if isinstance(xy, (list, tuple)) and len(xy) == 2 else None
        if task:
            steps.append({"task": task, "contact_xy": pt})
    return str(obj.get("reasoning", "")).strip(), steps[:9]


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
        reasoning, steps = await asyncio.to_thread(_astra, frame, goal)
        if not steps:
            STATE.update(state="idle", error="Astra returned no steps for this scene")
            return web.json_response({"error": STATE["error"]}, status=200)
        await asyncio.to_thread(_save_annotated, frame, steps)
        STATE.update(reasoning=reasoning, steps=steps, state="idle")
        log(f"plan: {len(steps)} steps")
        return web.json_response({"reasoning": reasoning, "steps": steps})
    except Exception as exc:  # noqa: BLE001
        STATE.update(state="idle", error=str(exc))
        log(f"plan error: {exc}")
        return web.json_response({"error": str(exc)}, status=200)


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
#log{font-family:monospace;font-size:12px;white-space:pre-wrap;background:#000;padding:10px;border-radius:8px;height:160px;overflow:auto}
.pill{display:inline-block;padding:2px 10px;border-radius:12px;background:#333;margin-left:8px}</style></head>
<body><div class=wrap>
<h1>🌮 Quesadilla — Astra plans, the arm does it <span id=state class=pill>idle</span></h1>
<div><input id=goal value="make a quesadilla">
<button id=make onclick=make()>Make it</button>
<button id=stop onclick=stop()>STOP</button></div>
<p id=reasoning style=color:#aaa></p>
<div class=row>
<div class=col><h3>Overhead view + contact trackers</h3><img id=shot src="/annotated.jpg?0"></div>
<div class=col><h3>Plan</h3><ol id=steps></ol><h3>Log</h3><div id=log></div></div>
</div></div>
<script>
async function make(){
  document.getElementById('reasoning').textContent='planning...';
  const r=await (await fetch('/plan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({goal:document.getElementById('goal').value})})).json();
  if(r.error){document.getElementById('reasoning').textContent='Astra: '+r.error;return;}
  document.getElementById('reasoning').textContent=r.reasoning||'';
  document.getElementById('shot').src='/annotated.jpg?'+Date.now();
  document.getElementById('steps').innerHTML=(r.steps||[]).map(s=>'<li>'+s.task+(s.contact_xy?' <span style=color:#7CFC00>@['+s.contact_xy+']</span>':'')+'</li>').join('');
  if(confirm('Run '+(r.steps||[]).length+' steps on the arm? Area clear, hand on power.')) run();
}
async function run(){await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}
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
poll();
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
