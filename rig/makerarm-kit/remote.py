"""Remote control from anywhere: watch the arm camera and drive the arms over LiveKit.

    python remote.py                 # opens http://127.0.0.1:8770 in your browser
    python remote.py --name Ana      # your name as others see it

Needs the team's LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET / LIVEKIT_ROOM in .env
(same values as the arm laptop; share them privately, never commit them). No arm hardware,
no arm SDK needed on this machine. The arm laptop must be running: python web_app.py --livekit

One person controls at a time: press "Take control". Anyone can press Hold or Park.
"""
from __future__ import annotations

import argparse
import getpass
import pathlib
import re
import secrets
import webbrowser

from aiohttp import web
from dotenv import load_dotenv

from lk import HOST_IDENTITY, mint_token

HERE = pathlib.Path(__file__).resolve().parent


def env(name: str) -> str:
    import os
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Put the team's LiveKit values in .env (see .env.example).")
    return value


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="your name in the room (default: your computer user name)")
    ap.add_argument("--http-port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    for f in (HERE / ".env", HERE / ".env.local"):
        if f.exists():
            load_dotenv(f, override=f.name == ".env.local")
    url, key, secret, room = env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"), env("LIVEKIT_ROOM")
    default_name = re.sub(r"[^A-Za-z0-9_-]", "", a.name or getpass.getuser())[:24] or "guest"

    async def index(_):
        return web.FileResponse(HERE / "static" / "remote.html")

    async def token(request: web.Request):
        name = re.sub(r"[^A-Za-z0-9 _-]", "", request.query.get("name", default_name)).strip()[:24] or default_name
        identity = f"{name.replace(' ', '-')}-{secrets.token_hex(2)}"  # unique even if two people pick one name
        tok = mint_token(key, secret, identity, room, publish_media=False, name=name)
        return web.json_response({"url": url, "token": tok, "identity": identity, "room": room,
                                  "host": HOST_IDENTITY, "name": name})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/token", token)
    page = f"http://127.0.0.1:{a.http_port}/?name={default_name}"
    print(f"[remote] room '{room}' on {url}")
    print(f"[remote] open {page}   (Ctrl-C to quit; the arms hold if you were in control)")
    if not a.no_browser:
        webbrowser.open(page)
    web.run_app(app, host="127.0.0.1", port=a.http_port, print=None)


if __name__ == "__main__":
    main()
