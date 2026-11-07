"""The harness service: joins the rig's Portal room as an observer and serves a web UI + HTTP API.

    uv run harness                                   # http://127.0.0.1:8770
    uv run harness --env ../portal-makermods/.env --portal-config ../portal-makermods/portal.yaml
    uv run harness --tasks ../TacoBot/tacobot/tasks.toml

HTTP API (JSON; POST bodies are JSON):
    GET  /api/state                     everything: link, control, dream status, joints, cameras, sequence
    GET  /api/events?after=<id>         event log
    WS   /ws                            {"type": "state"|"event", ...} pushed live
    GET  /video/<camera>.mjpg           MJPEG stream (overhead_camera, left_wrist_camera, right_wrist_camera)
    GET  /snapshot/<camera>.jpg         newest frame
    POST /api/prompt {"task": "...", "start": false}   set Dream-1's task (pauses it); start=true also resumes
    POST /api/resume | /api/pause | /api/release | /api/teleop | /api/hand_teleop | /api/stop_hand_teleop
    POST /api/home | /api/park
    POST /api/sequence/{arm,start,done,retry,teleop,stop}   arm takes {"index": n}; /api/sequence/load {"path"}

arm-host mode (`--mode arm-host`, or auto when the room holds the Maker Arm kit's `arm-host`):
    POST /api/release                   hold every powered arm (anyone may)
    POST /api/arm/take | /api/arm/release_control
    POST /api/arm/hold {"arm"?} | /api/arm/park {"arm"} | /api/arm/jog {"arm", "joint", "deg"}

Safety: the harness never sends actions and never powers the arms up (park is park-only). `release` clears the
control gate so nobody drives and the arms hold. None of this is an e-stop: keep a person on the leader arms /
the robot terminal's `p` key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web
from dotenv import load_dotenv

from . import protocol as P
from .armhost import ArmHostLink
from .link import LinkBase, RigLink, RpcFailed, Settings
from .sequence import Sequence, load_steps

HERE = Path(__file__).resolve().parent
log = logging.getLogger("harness")
MJPEG_BOUNDARY = "harnessframe"


# -- configuration -----------------------------------------------------------------------------------------------

def _candidates(name: str) -> list[Path]:
    """Where to look for the rig's files when not given: next to the harness (rig/harness), then TacoBot's
    `upstream/` submodule (the server code), then a portal-makermods checkout beside TacoBot."""
    root = HERE.parent  # rig/harness
    repo = root.parents[1]  # TacoBot
    return [root / name, repo / "upstream" / name, repo.parent / "portal-makermods" / name]


def _first(paths: list[Path]) -> Path | None:
    return next((p for p in paths if p.exists()), None)


def load_connection(env_path: str | None) -> Path | None:
    """Load the connection settings (.env) and return where they came from."""
    env = Path(env_path) if env_path else _first(_candidates(".env"))
    if env:
        if not env.exists():
            raise SystemExit(f"no such env file: {env}")
        load_dotenv(env, override=False)
        log.info("env from %s", env)
    return env


def build_settings(args: argparse.Namespace) -> Settings:
    load_connection(args.env)
    if args.url:
        os.environ["LIVEKIT_URL"] = args.url
    cfg = Path(args.portal_config) if args.portal_config else (
        Path(os.environ["PORTAL_CONFIG"]) if os.environ.get("PORTAL_CONFIG") else _first(_candidates("portal.yaml")))
    if (not cfg or not cfg.exists()) and getattr(args, "mode", "portal") == "portal":
        raise SystemExit("portal.yaml not found: pass --portal-config (the server's portal-makermods/portal.yaml), "
                         "or run `git submodule update --init` in TacoBot")
    token = os.environ.get("LIVEKIT_TOKEN") or None  # a token minted on the server laptop: no API secret needed
    need = ["LIVEKIT_URL"] + ([] if token else ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_ROOM"])
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}: pass --env with the server laptop's LiveKit settings "
                         "(see .env.example)")
    return Settings(portal_config=cfg or Path("portal.yaml"), url=os.environ["LIVEKIT_URL"],
                    room=args.room or os.environ.get("LIVEKIT_ROOM") or "(from token)",
                    api_key=os.environ.get("LIVEKIT_API_KEY", ""), api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
                    identity=args.identity, video_fps=args.video_fps, max_width=args.max_width, token=token)


# -- app -----------------------------------------------------------------------------------------------------------

def full_state(app: web.Application) -> dict:
    link: RigLink = app["link"]
    seq: Sequence | None = app["sequence"]
    return dict(link.snapshot(), sequence=seq.snapshot() if seq else None, server_time=time.time())


def ok(data=None) -> web.Response:
    return web.json_response({"ok": True, "result": data})


def fail(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": f"bad JSON: {exc}"}),
                                 content_type="application/json") from exc
    return data if isinstance(data, dict) else {}


def command(fn):
    """Wrap a command handler: RPC failures and bad input come back as {"ok": false, "error": ...}."""
    async def handler(request: web.Request) -> web.Response:
        try:
            return ok(await fn(request))
        except (RpcFailed, ValueError) as exc:
            return fail(str(exc))
    return handler


@web.middleware
async def auth_middleware(request: web.Request, handler):
    token = request.app["token"]
    if token and request.path != "/" and not request.path.startswith("/static"):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.query.get("token")
        if not given or not secrets.compare_digest(given, token):
            return fail("unauthorized: pass Authorization: Bearer <HARNESS_TOKEN> or ?token=", 401)
    return await handler(request)


def make_app(link: LinkBase, tasks_path: Path | None = None, token: str | None = None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["link"], app["token"], app["sequence"] = link, token, None
    portal = isinstance(link, RigLink)
    if tasks_path and portal:
        app["sequence"] = Sequence(link, load_steps(tasks_path), str(tasks_path))
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_):
        return web.FileResponse(HERE / "static" / ("index.html" if portal else "armhost.html"))

    @routes.get("/api/state")
    async def state(_):
        return web.json_response(full_state(app))

    @routes.get("/api/events")
    async def events(request):
        after = int(request.query.get("after", 0))
        return web.json_response([e for e in link.events if e["id"] > after])

    @routes.get("/snapshot/{camera}.jpg")
    async def snapshot(request):
        cam = link.cameras.get(request.match_info["camera"])
        if cam is None:
            return fail(f"unknown camera; one of {link.tracks}", 404)
        if cam.jpeg is None:
            return fail("no frame yet", 503)
        return web.Response(body=cam.jpeg, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @routes.get("/video/{camera}.mjpg")
    async def video(request):
        name = request.match_info["camera"]
        if name not in link.cameras:
            return fail(f"unknown camera; one of {link.tracks}", 404)
        resp = web.StreamResponse(headers={"Content-Type": f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
                                           "Cache-Control": "no-store"})
        await resp.prepare(request)
        last = -1
        try:
            while True:
                cam = link.cameras[name]
                if cam.jpeg is not None and cam.seq != last:
                    last = cam.seq
                    await resp.write(f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     f"{len(cam.jpeg)}\r\n\r\n".encode() + cam.jpeg + b"\r\n")
                try:
                    await asyncio.wait_for(link.frame_event.wait(), timeout=2.0)
                except TimeoutError:
                    pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    @routes.get("/ws")
    async def ws(request):
        sock = web.WebSocketResponse(heartbeat=20)
        await sock.prepare(request)
        events_q = link.listen()
        await sock.send_json({"type": "events", "events": list(link.events)[-200:]})

        async def push():
            while True:
                await sock.send_json({"type": "state", "state": full_state(app)})
                deadline = time.monotonic() + 0.25
                while (left := deadline - time.monotonic()) > 0:
                    try:
                        item = await asyncio.wait_for(events_q.get(), timeout=left)
                        await sock.send_json({"type": "event", "event": item})
                    except TimeoutError:
                        break

        pusher = asyncio.create_task(push())
        try:
            async for msg in sock:  # the UI only sends pings; commands go over HTTP
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            pusher.cancel()
            link.unlisten(events_q)
        return sock

    if portal:
        add_portal_routes(app, routes, link)
    else:
        add_armhost_routes(routes, link)
    app.add_routes(routes)
    app.router.add_static("/static", HERE / "static")

    async def on_startup(_app):
        link.start()

    async def on_cleanup(_app):
        seq = app["sequence"]
        if seq is not None and seq.phase in ("running", "teleop") and link.connected:
            try:
                await link.pause()
            except Exception:  # noqa: BLE001
                pass
        await link.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def add_armhost_routes(routes: web.RouteTableDef, link: ArmHostLink) -> None:
    @routes.post("/api/prompt")
    @command
    async def prompt(_request):
        raise ValueError("no Dream-1 in this room: the arm laptop runs the Maker Arm kit (arm-host mode). "
                         "Run upstream's robot + dream-1-tasks there to send prompts.")

    @routes.post("/api/release")
    @command
    async def hold_all(_request):
        return await link.hold()

    @routes.post("/api/arm/take")
    @command
    async def take(_request):
        return await link.take()

    @routes.post("/api/arm/release_control")
    @command
    async def release_control(_request):
        return await link.release_control()

    @routes.post("/api/arm/hold")
    @command
    async def hold(request):
        return await link.hold((await body(request)).get("arm") or None)

    @routes.post("/api/arm/park")
    @command
    async def park(request):
        return await link.park(str((await body(request)).get("arm", "")))

    @routes.post("/api/arm/jog")
    @command
    async def jog(request):
        data = await body(request)
        try:
            joint, deg = int(data.get("joint")), float(data.get("deg"))
        except (TypeError, ValueError) as exc:
            raise ValueError("jog needs numeric joint and deg") from exc
        return await link.jog(str(data.get("arm", "")), joint, deg)


def add_portal_routes(app: web.Application, routes: web.RouteTableDef, link: RigLink) -> None:
    @routes.post("/api/prompt")
    @command
    async def prompt(request):
        data = await body(request)
        status = await link.set_task(data.get("task", ""))
        if data.get("start"):
            status = await link.resume()
        return status

    simple = {"resume": link.resume, "pause": link.pause, "release": link.release, "teleop": link.to_teleop,
              "hand_teleop": link.to_hand_teleop, "stop_hand_teleop": link.stop_hand_teleop, "home": link.home,
              "park": link.park}
    for name, fn in simple.items():
        routes.post(f"/api/{name}")(command(lambda _req, fn=fn: fn()))

    def seq_or_fail() -> Sequence:
        if app["sequence"] is None:
            raise ValueError("no sequence loaded: start with --tasks or POST /api/sequence/load")
        return app["sequence"]

    @routes.post("/api/sequence/load")
    @command
    async def seq_load(request):
        path = Path((await body(request)).get("path", ""))
        if not path.is_file():
            raise ValueError(f"no such file: {path}")
        if app["sequence"] is not None and app["sequence"].phase in ("running", "waiting", "teleop"):
            raise ValueError("stop the current sequence first")
        app["sequence"] = Sequence(link, load_steps(path), str(path))
        link.event("sequence", f"loaded {len(app['sequence'].steps)} steps from {path}")
        return app["sequence"].snapshot()

    @routes.post("/api/sequence/arm")
    @command
    async def seq_arm(request):
        index = (await body(request)).get("index")
        await seq_or_fail().arm(None if index is None else int(index))
        return seq_or_fail().snapshot()

    for name in ("start", "done", "retry", "teleop", "stop"):
        async def seq_action(_req, name=name):
            await getattr(seq_or_fail(), name)()
            return seq_or_fail().snapshot()
        routes.post(f"/api/sequence/{name}")(command(seq_action))


def run(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="LiveKit settings of the server laptop (LIVEKIT_URL/API_KEY/API_SECRET/ROOM)")
    ap.add_argument("--url", help="override LIVEKIT_URL, e.g. ws://192.168.1.20:7880 for a LAN livekit-server")
    ap.add_argument("--check", action="store_true", help="list the server's rooms and who is in ours, then exit")
    ap.add_argument("--mode", choices=["auto", "portal", "arm-host"], default="auto",
                    help="portal: upstream robot + Dream-1; arm-host: the Maker Arm kit's web_app.py --livekit; "
                         "auto (default): look at who is in the room")
    ap.add_argument("--portal-config", help="the server's portal.yaml (default: ../portal-makermods/portal.yaml)")
    ap.add_argument("--room", help="override LIVEKIT_ROOM")
    ap.add_argument("--identity", default=P.HARNESS_IDENTITY)
    ap.add_argument("--tasks", help="a TacoBot-style tasks.toml to step through")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default localhost only)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--video-fps", type=float, default=12.0, help="frames per second served per camera")
    ap.add_argument("--max-width", type=int, default=640, help="served frames are shrunk to this width")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ.setdefault("RUST_LOG", "warn,livekit_portal::sync_buffer=error,libwebrtc=error")
    requested = args.mode
    if args.check or requested == "auto":
        args.mode = "arm-host"  # don't insist on portal.yaml before we know which server runs
    settings = build_settings(args)
    if args.check:
        from .check import check_room

        if settings.token:
            raise SystemExit("--check needs LIVEKIT_API_KEY/SECRET (a LIVEKIT_TOKEN alone cannot list rooms)")
        good = asyncio.run(check_room(settings.url, settings.api_key, settings.api_secret, settings.room,
                                      list(P.CAMERAS)))
        raise SystemExit(0 if good else 1)
    mode = requested
    if mode == "auto":
        mode = asyncio.run(detect_mode(settings))
        log.info("mode: %s", mode)
    if mode == "portal" and not settings.portal_config.exists():
        args.mode = "portal"
        settings = build_settings(args)  # exits with the portal.yaml hint
    token = os.environ.get("HARNESS_TOKEN") or None
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        log.warning("serving on %s without HARNESS_TOKEN: anyone on the network can steer the rig", args.host)
    link = RigLink(settings) if mode == "portal" else ArmHostLink(settings)
    app = make_app(link, Path(args.tasks) if args.tasks else None, token)
    log.info("harness (%s mode) on http://%s:%d (room %r)", mode, args.host, args.port, settings.room)
    web.run_app(app, host=args.host, port=args.port, print=None)


async def detect_mode(settings: Settings) -> str:
    """Which server runs in the room: upstream's Portal robot, or the Maker Arm kit's arm-host. Waits for one."""
    if settings.token:
        log.info("LIVEKIT_TOKEN without API key/secret: cannot look into the room; assuming portal (use --mode)")
        return "portal"
    from livekit import api

    from .armhost import ARM_HOST

    said = None
    while True:
        lk = api.LiveKitAPI(settings.url, settings.api_key, settings.api_secret)
        try:
            people = (await lk.room.list_participants(api.ListParticipantsRequest(room=settings.room))).participants
            ids = {p.identity for p in people}
            if P.ROBOT_IDENTITY in ids:
                return "portal"
            if ARM_HOST in ids:
                return "arm-host"
            msg = f"neither {P.ROBOT_IDENTITY!r} nor {ARM_HOST!r} in room {settings.room!r} yet ({sorted(ids)})"
        except Exception as exc:  # noqa: BLE001 — room not created yet, network, ...
            msg = f"cannot list room {settings.room!r} yet ({exc})"
        finally:
            await lk.aclose()
        if msg != said:
            log.info("%s; waiting (or pass --mode portal|arm-host)", msg)
            said = msg
        await asyncio.sleep(3)


if __name__ == "__main__":
    run()
