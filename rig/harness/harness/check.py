"""Is the server laptop up, and in the room the harness will join? Asks LiveKit's server API; joins nothing.

    uv run harness --check --env server.env
"""
from __future__ import annotations

from . import protocol as P

KIND = {0: "audio", 1: "video", 2: "data"}


async def check_room(url: str, api_key: str, api_secret: str, room: str, cameras: list[str]) -> bool:
    """Print the rooms on the server and who is in ours. True when the robot and its cameras are there."""
    from livekit import api

    lk = api.LiveKitAPI(url, api_key, api_secret)
    try:
        rooms = (await lk.room.list_rooms(api.ListRoomsRequest())).rooms
        print(f"LiveKit {url}: {len(rooms)} active room(s): {', '.join(r.name for r in rooms) or 'none'}")
        if room not in {r.name for r in rooms}:
            print(f"✗ room {room!r} is not active: the server laptop is not connected, or uses another "
                  "LIVEKIT_ROOM. Use the same LIVEKIT_ROOM as its .env (or --room).")
            return False
        people = (await lk.room.list_participants(api.ListParticipantsRequest(room=room))).participants
    finally:
        await lk.aclose()

    print(f"room {room!r}: {len(people)} participant(s)")
    tracks: set[str] = set()
    for p in people:
        role = p.attributes.get("lk.portal.role", "")
        names = [f"{t.name}({KIND.get(t.type, t.type)})" for t in p.tracks]
        tracks |= {t.name for t in p.tracks}
        print(f"  {p.identity:24} {role:9} {', '.join(names)}")
    ids = {p.identity for p in people}
    if "arm-host" in ids and P.ROBOT_IDENTITY not in ids:
        cam = "arm-camera" in ids
        print("✓ arm-host               present: the Maker Arm kit (web_app.py --livekit). The harness runs in "
              "arm-host mode: arm state, hold/park/jog, no Dream-1 prompts.")
        print(f"{'✓' if cam else '✗'} arm-camera             {'present' if cam else 'missing'}"
              + ("" if cam else ": no video until someone presses the camera share on the arm laptop's page"))
        return True
    ok = True
    for who, why in ((P.ROBOT_IDENTITY, "cameras and arms (`uv run robot` on the server laptop)"),
                     (P.DREAM_TASKS_IDENTITY, "prompts (`uv run dream-1-tasks` on the server laptop)")):
        here = who in ids
        ok &= here or who != P.ROBOT_IDENTITY
        print(f"{'✓' if here else '✗'} {who:22} {'present' if here else 'missing'}: needed for {why}")
    missing = [c for c in cameras if not any(c in t for t in tracks)]
    if P.ROBOT_IDENTITY in ids:
        print("✓ cameras published: " + ", ".join(c for c in cameras if c not in missing) if not missing else
              f"? camera tracks not listed by name: {missing} (Portal may name them differently; the harness "
              "will show what arrives)")
    if ok and not ids & {P.ROBOT_IDENTITY, "arm-host"}:
        ok = False
    if P.HARNESS_IDENTITY in ids:
        print(f"! {P.HARNESS_IDENTITY!r} is already in the room: another harness runs. Use --identity to pick "
              "another name, or the newer one will kick the older out.")
    return ok
