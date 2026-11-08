"""Dream-1 web control panel: power, free-text prompts, home/reset, and the three camera feeds.

A plain Portal operator peer (it never sends actions). It drives the already-running robot and
dream-1-tasks over the same RPCs the CLI uses, and shows the overhead + left/right wrist cameras in
the browser so you can watch what Dream-1 sees.

Bring the rig up first, then this:

    # Terminal 1
    cd upstream && uv run robot
    # Terminal 2
    cd upstream && uv run dream-1-tasks
    # Bash
    cd upstream && uv run python ../tacobot/dream_ui.py        # or: make ui

Then open http://127.0.0.1:8808 and:
    - Power: toggle the arms on (power up) / park. Park-only button never powers up.
    - Prompt: type any instruction -> Dream-1 runs it (set_task + resume). Pause holds the arms.
    - Reset home: walk both arms back to their rest pose and release control (no park/power change).

Power-up/park is also `p` in the robot terminal, and Ctrl-C there is the hard stop; this panel only
sends the robot's park/home RPCs and the dream.* RPCs.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
UPSTREAM_SRC = HERE.parent / "upstream" / "src"
sys.path.insert(0, str(UPSTREAM_SRC))

from aiohttp import web
from livekit.portal import Operator, OperatorConfig, frame_bytes_to_numpy_rgb
from utils.common import (CONFIG_PATH, DREAM_PAUSE_RPC, DREAM_RESUME_RPC, DREAM_SET_TASK_RPC, DREAM_STATUS_RPC,
                          DREAM_TASKS_IDENTITY, HOME_RPC, PARK_ONLY, PARK_RPC, PARK_TOGGLE, ROBOT_IDENTITY,
                          load_env, mint_token, required_env)

PORT = 8808
TRACKS = {"overhead": "overhead_camera", "left": "left_wrist_camera", "right": "right_wrist_camera"}


class Panel:
    """One Portal operator connection: caches the camera frames and relays the control RPCs."""

    def __init__(self) -> None:
        self.op: Operator | None = None
        self.frames: dict[str, np.ndarray] = {}

    async def connect(self) -> None:
        url, room = required_env("LIVEKIT_URL"), required_env("LIVEKIT_ROOM")
        self.op = Operator(OperatorConfig.from_yaml_file(CONFIG_PATH, room))
        for role, track in TRACKS.items():
            self.op.on_video_frame(track, self._frame_cb(role))
        await self.op.connect(url, mint_token("dream-ui", room))

    def _frame_cb(self, role: str):
        def cb(_name, frame) -> None:
            try:
                self.frames[role] = frame_bytes_to_numpy_rgb(frame.data, frame.width, frame.height)
            except Exception:  # noqa: BLE001 -- a bad frame just means we keep the last good one
                pass
        return cb

    def jpeg(self, role: str) -> bytes | None:
        rgb = self.frames.get(role)
        if rgb is None:
            return None
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    async def dream(self, method: str, payload: dict | None = None) -> dict:
        reply = await self.op.perform_rpc(method, payload=json.dumps(payload or {}),
                                          destination=DREAM_TASKS_IDENTITY, response_timeout_ms=5000)
        try:
            return json.loads(reply)
        except (TypeError, ValueError):
            return {"reply": reply}

    async def robot(self, method: str, payload: str = "") -> str:
        return await self.op.perform_rpc(method, payload=payload, destination=ROBOT_IDENTITY,
                                         response_timeout_ms=8000)

    async def close(self) -> None:
        if self.op is not None:
            try:
                await self.op.disconnect()
            finally:
                self.op.close()
                self.op = None


_GRAY = None


def _placeholder() -> bytes:
    global _GRAY
    if _GRAY is None:
        img = np.full((360, 640, 3), 40, np.uint8)
        cv2.putText(img, "waiting for frames...", (120, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (180, 180, 180), 2)
        _GRAY = cv2.imencode(".jpg", img)[1].tobytes()
    return _GRAY


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Dream-1 control</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>
body{background:#0e0e12;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:18px}
h1{font-size:20px;margin:0 0 12px}
.pill{display:inline-block;padding:2px 10px;border-radius:12px;background:#333;margin-left:8px;font-size:13px}
.cams{display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;margin-bottom:14px}
.cam{background:#000;border-radius:8px;overflow:hidden}
.cam h3{margin:0;padding:6px 10px;font-size:13px;color:#aaa;background:#1a1a22}
.cam img{width:100%;display:block}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
input{flex:1;min-width:280px;padding:10px;border-radius:8px;border:1px solid #333;background:#1a1a22;color:#eee;font-size:15px}
button{padding:10px 16px;border:0;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.run{background:#2e7d32;color:#fff}.pause{background:#8d6e00;color:#fff}
.power{background:#1565c0;color:#fff}.park{background:#b71c1c;color:#fff}.home{background:#37474f;color:#fff}
#msg{font-family:monospace;font-size:13px;color:#9fd;white-space:pre-wrap;min-height:20px}
</style></head><body>
<h1>Dream-1 control <span id=state class=pill>connecting...</span></h1>
<div class=cams>
  <div class=cam><h3>Overhead (global)</h3><img id=overhead src="/cam/overhead.jpg"></div>
  <div class=cam><h3>Left wrist</h3><img id=left src="/cam/left.jpg"></div>
  <div class=cam><h3>Right wrist</h3><img id=right src="/cam/right.jpg"></div>
</div>
<div class=row>
  <input id=prompt placeholder="pick up the tortilla" value="pick up the tortilla">
  <button class=run onclick=run()>Run</button>
  <button class=pause onclick=call('/pause')>Pause</button>
</div>
<div class=row>
  <button class=power onclick=call('/power')>Power on / off (toggle)</button>
  <button class=park onclick=call('/park')>Park (power off)</button>
  <button class=home onclick=call('/home')>Reset to home</button>
</div>
<div id=msg></div>
<script>
function refreshCams(){
  for(const id of ['overhead','left','right'])
    document.getElementById(id).src='/cam/'+id+'.jpg?t='+Date.now();
}
async function post(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  return r.json();
}
async function call(path){
  const r=await post(path);
  document.getElementById('msg').textContent=r.message||r.error||JSON.stringify(r);
}
async function run(){
  const task=document.getElementById('prompt').value.trim();
  if(!task){document.getElementById('msg').textContent='type an instruction first';return;}
  const r=await post('/prompt',{task:task});
  document.getElementById('msg').textContent=r.message||r.error||JSON.stringify(r);
}
async function poll(){
  try{const s=await (await fetch('/status')).json();
    const who=s.driving?'Dream-1 driving':(s.active_operator?('operator: '+s.active_operator):'nobody driving');
    document.getElementById('state').textContent=(s.task?('task: '+s.task+' - '):'')+who;
  }catch(e){document.getElementById('state').textContent='robot/dream-1-tasks not reachable';}
  setTimeout(poll,1000);
}
setInterval(refreshCams,250); poll();
</script></body></html>"""


def build_app(panel: Panel) -> web.Application:
    async def index(_req):
        return web.Response(text=PAGE, content_type="text/html")

    async def cam(req):
        data = panel.jpeg(req.match_info["role"]) or _placeholder()
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def prompt(req):
        task = " ".join(str((await req.json()).get("task", "")).split())
        if not task:
            return web.json_response({"error": "empty task"})
        try:
            await panel.dream(DREAM_SET_TASK_RPC, {"task": task})
            status = await panel.dream(DREAM_RESUME_RPC)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)})
        if status.get("driving"):
            return web.json_response({"message": f"Dream-1 running: {task!r}"})
        return web.json_response({"message": f"set task {task!r} but not driving "
                                             f"(active: {status.get('active_operator')}). Power on the arms first."})

    async def pause(_req):
        try:
            await panel.dream(DREAM_PAUSE_RPC)
            return web.json_response({"message": "paused: the arms hold"})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)})

    def robot_route(method, payload, ok):
        async def handler(_req):
            try:
                reply = await panel.robot(method, payload)
                return web.json_response({"message": reply or ok})
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": str(exc)})
        return handler

    async def status(_req):
        try:
            return web.json_response(await panel.dream(DREAM_STATUS_RPC))
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=200)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/cam/{role}.jpg", cam)
    app.router.add_post("/prompt", prompt)
    app.router.add_post("/pause", pause)
    app.router.add_post("/power", robot_route(PARK_RPC, PARK_TOGGLE, "power toggled"))
    app.router.add_post("/park", robot_route(PARK_RPC, PARK_ONLY, "parking"))
    app.router.add_post("/home", robot_route(HOME_RPC, "", "returning home"))
    app.router.add_get("/status", status)
    return app


async def amain() -> None:
    load_env()
    panel = Panel()
    await panel.connect()
    runner = web.AppRunner(build_app(panel))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    print(f"Dream-1 control panel on http://127.0.0.1:{PORT}")
    print("Power on the arms, type a prompt, watch the three camera feeds. Ctrl-C to stop the panel.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
        await panel.close()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
