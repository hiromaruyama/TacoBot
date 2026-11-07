"""Command line for a running harness (`uv run harness`). Handy for scripts and coding agents.

    harnessctl status                       # who drives, Dream-1's task, joints, camera rates
    harnessctl prompt "put the red cube in the box"          # set the task (Dream-1 pauses, nothing moves)
    harnessctl prompt "put the red cube in the box" --start  # ... and give Dream-1 control
    harnessctl resume | pause | release | teleop | hand-teleop | stop-hand-teleop | home | park
    harnessctl snapshot overhead_camera out.jpg
    harnessctl watch                        # stream the event log
    harnessctl seq arm 0 | start | done | retry | teleop | stop | show

Set HARNESS_URL (default http://127.0.0.1:8770) and HARNESS_TOKEN if the harness requires one.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

from .protocol import JOINTS, SIDES


class Client:
    def __init__(self, base: str | None = None, token: str | None = None) -> None:
        self.base = (base or os.environ.get("HARNESS_URL") or "http://127.0.0.1:8770").rstrip("/")
        self.token = token if token is not None else os.environ.get("HARNESS_TOKEN")

    def _req(self, method: str, path: str, data: dict | None = None, raw: bool = False):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if data is None else json.dumps(data).encode())
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
        except urllib.error.HTTPError as exc:
            content = exc.read()
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                raise SystemExit(f"HTTP {exc.code}: {content[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach the harness at {self.base} ({exc.reason}); "
                             "is `uv run harness` up?") from exc
        return content if raw else json.loads(content)

    def get(self, path: str):
        return self._req("GET", path)

    def post(self, path: str, data: dict | None = None):
        return self._req("POST", path, data or {})

    def snapshot(self, camera: str) -> bytes:
        return self._req("GET", f"/snapshot/{camera}.jpg", raw=True)


def format_status(s: dict) -> str:
    lines = [f"link      {'connected' if s['connected'] else 'NOT connected: ' + str(s.get('connect_error'))}"
             f"  room={s['room']}  robot={s.get('robot')}",
             f"control   {s.get('active_operator') or 'nobody (arms hold)'}"
             f"   operators={s.get('operators')}  observers={s.get('observers')}"]
    d = s.get("dream")
    if d:
        lines.append(f"dream-1   task={d.get('task')!r}  driving={d.get('driving')}  "
                     f"teleop={'yes' if d.get('teleoperator_connected') else 'no'}  "
                     f"hand={'yes' if d.get('hand_teleop_connected') else 'no'}")
    if s.get("dream_error"):
        lines.append(f"dream-1   ! {s['dream_error']}")
    st = s.get("state")
    if st:
        lines.append(f"state     (age {s.get('state_age_s')}s)   joints in degrees, gripper in mm")
        for side in SIDES:
            vals = []
            for j in JOINTS:
                v = st.get(f"{side}_{j}.pos")
                vals.append("   -  " if v is None else f"{v:6.1f}" if j == "gripper" else f"{math.degrees(v):6.1f}")
            lines.append(f"  {side:5}  " + " ".join(vals))
    for name, c in (s.get("cameras") or {}).items():
        lines.append(f"camera    {name:20} {c['width']}x{c['height']}  {c['fps']} fps  age {c['age_s']}s")
    seq = s.get("sequence")
    if seq:
        cur = seq["steps"][seq["index"]]["id"] if seq["index"] < len(seq["steps"]) else "-"
        lines.append(f"sequence  {seq['phase']}  step {seq['index'] + 1}/{len(seq['steps'])} ({cur})")
    return "\n".join(lines)


def show(result: dict) -> int:
    if not result.get("ok", True):
        print(f"error: {result.get('error')}", file=sys.stderr)
        return 1
    r = result.get("result")
    print(json.dumps(r, indent=2) if isinstance(r, (dict, list)) else r)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="harness URL (default $HARNESS_URL or http://127.0.0.1:8770)")
    ap.add_argument("--json", action="store_true", help="raw JSON output for status")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("prompt")
    p.add_argument("task", nargs="+")
    p.add_argument("--start", action="store_true", help="also give Dream-1 control (motion starts)")
    for name in ("resume", "pause", "release", "teleop", "hand-teleop", "stop-hand-teleop", "home", "park"):
        sub.add_parser(name)
    p = sub.add_parser("snapshot")
    p.add_argument("camera")
    p.add_argument("out")
    sub.add_parser("watch")
    p = sub.add_parser("seq")
    p.add_argument("action", choices=["show", "arm", "start", "done", "retry", "teleop", "stop", "load"])
    p.add_argument("arg", nargs="?")
    args = ap.parse_args(argv)
    c = Client(args.url)

    if args.cmd == "status":
        s = c.get("/api/state")
        print(json.dumps(s, indent=2) if args.json else format_status(s))
        return 0
    if args.cmd == "prompt":
        return show(c.post("/api/prompt", {"task": " ".join(args.task), "start": args.start}))
    if args.cmd == "snapshot":
        data = c.snapshot(args.camera)
        if data[:2] != b"\xff\xd8":
            print(f"error: {data[:200].decode(errors='replace')}", file=sys.stderr)
            return 1
        with open(args.out, "wb") as f:
            f.write(data)
        print(f"wrote {args.out} ({len(data)} bytes)")
        return 0
    if args.cmd == "watch":
        after = 0
        try:
            while True:
                for e in c.get(f"/api/events?after={after}"):
                    after = e["id"]
                    print(f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['level']:5} "
                          f"[{e['kind']}] {e['text']}", flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0
    if args.cmd == "seq":
        if args.action == "show":
            print(json.dumps(c.get("/api/state").get("sequence"), indent=2))
            return 0
        payload = {}
        if args.action == "arm" and args.arg is not None:
            payload = {"index": int(args.arg)}
        if args.action == "load":
            payload = {"path": os.path.abspath(args.arg or "")}
        return show(c.post(f"/api/sequence/{args.action}", payload))
    return show(c.post("/api/" + args.cmd.replace("-", "_")))


if __name__ == "__main__":
    raise SystemExit(main())
