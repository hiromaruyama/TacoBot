"""LiveKit names and token minting shared by arm_server.py and controller.py."""
from __future__ import annotations

import datetime

from livekit import api

ARM_IDENTITY = "makerarm"
CMD_TOPIC = "arm/cmd"
STATE_TOPIC = "arm/state"


def mint_token(key: str, secret: str, identity: str, room: str, hours: int = 6) -> str:
    grants = api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                             can_publish_data=True)
    return (api.AccessToken(key, secret)
            .with_identity(identity)
            .with_grants(grants)
            .with_ttl(datetime.timedelta(hours=hours))
            .to_jwt())
