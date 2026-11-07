"""LiveKit names and token minting shared by the web app, remote.py, arm_server.py and controller.py."""
from __future__ import annotations

import datetime

from livekit import api

ARM_IDENTITY = "makerarm"        # arm_server.py (single arm, terminal)
HOST_IDENTITY = "arm-host"       # web_app.py --livekit (all arms of this laptop)
CAMERA_IDENTITY = "arm-camera"   # the arm laptop's browser, publishing the camera
CMD_TOPIC = "arm/cmd"
STATE_TOPIC = "arm/state"
REPLY_TOPIC = "arm/reply"


def mint_token(key: str, secret: str, identity: str, room: str, hours: int = 6,
               publish_media: bool = True, name: str | None = None) -> str:
    """A room-scoped token. Viewers get publish_media=False: they can send data, not video/audio."""
    grants = api.VideoGrants(room_join=True, room=room, can_publish=publish_media, can_subscribe=True,
                             can_publish_data=True)
    token = (api.AccessToken(key, secret)
             .with_identity(identity)
             .with_grants(grants)
             .with_ttl(datetime.timedelta(hours=hours)))
    if name:
        token = token.with_name(name)
    return token.to_jwt()
