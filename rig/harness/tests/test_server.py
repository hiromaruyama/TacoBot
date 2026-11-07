"""The HTTP API against a stub Portal observer: no LiveKit needed."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

from harness import protocol as P
from harness.link import RigLink, Settings
from harness.server import make_app


class StubObserver:
    def __init__(self):
        self.active = None
        self.rpcs = []
        self.task = None
        frame = np.full((48, 64, 3), 120, np.uint8)
        self.frame = SimpleNamespace(data=frame.tobytes(), width=64, height=48, timestamp_us=1)

    async def connect(self, url, token):
        pass

    async def disconnect(self):
        pass

    def close(self):
        pass

    def robot_identity(self):
        return P.ROBOT_IDENTITY

    def active_operator(self):
        return self.active

    def operators(self):
        return [P.DREAM_TASKS_IDENTITY]

    def observers(self):
        return ["harness"]

    def now_us(self):
        return 10_000

    def get_state(self):
        return SimpleNamespace(values={"left_joint1.pos": 0.5}, timestamp_us=5)

    def get_action(self):
        return None

    def get_video_frame(self, name):
        return self.frame

    async def set_active_operator(self, who):
        self.active = who

    async def perform_rpc(self, method, payload="", destination=None, response_timeout_ms=None):
        self.rpcs.append((destination, method, payload))
        if destination == P.ROBOT_IDENTITY:
            return "returning the arms home" if method == P.HOME_RPC else "parking the arms"
        if method == P.DREAM_SET_TASK_RPC:
            self.task, self.active = json.loads(payload)["task"], None
        if method == P.DREAM_RESUME_RPC:
            self.active = P.DREAM_TASKS_IDENTITY
        if method == P.DREAM_PAUSE_RPC:
            self.active = None
        return json.dumps({"task": self.task, "driving": self.active == P.DREAM_TASKS_IDENTITY, "presets": []})


@pytest.fixture
async def client():
    stub = StubObserver()
    settings = Settings(portal_config=Path("unused"), url="ws://x", room="r", api_key="k", api_secret="s" * 32,
                        video_fps=50)
    link = RigLink(settings, observer_factory=lambda: stub)
    link.tracks = list(P.CAMERAS)
    app = make_app(link, Path(__file__).resolve().parents[1] / "harness" / "example_tasks.toml")
    async with TestClient(TestServer(app)) as c:
        for _ in range(100):  # wait for the link to "connect" and grab a frame
            s = await (await c.get("/api/state")).json()
            if s["connected"] and s["cameras"]["overhead_camera"]["seq"]:
                break
            import asyncio
            await asyncio.sleep(0.02)
        c.stub = stub
        yield c


async def test_state(client):
    s = await (await client.get("/api/state")).json()
    assert s["connected"] and s["robot"] == "robot"
    assert s["state"]["left_joint1.pos"] == 0.5
    assert s["sequence"]["steps"][0]["id"] == "tortilla"


async def test_prompt_then_start(client):
    r = await (await client.post("/api/prompt", json={"task": "  wave   hello "})).json()
    assert r["ok"] and r["result"]["task"] == "wave hello" and not r["result"]["driving"]
    r = await (await client.post("/api/resume")).json()
    assert r["result"]["driving"]
    r = await (await client.post("/api/release")).json()
    assert r["ok"] and client.stub.active is None


async def test_prompt_validation(client):
    r = await client.post("/api/prompt", json={"task": "x" * 201})
    assert r.status == 400 and not (await r.json())["ok"]


async def test_park_is_park_only(client):
    await client.post("/api/park")
    assert client.stub.rpcs[-1] == (P.ROBOT_IDENTITY, P.PARK_RPC, P.PARK_ONLY)


async def test_snapshot(client):
    r = await client.get("/snapshot/overhead_camera.jpg")
    assert r.status == 200 and (await r.read())[:2] == b"\xff\xd8"
    assert (await client.get("/snapshot/nope.jpg")).status == 404


async def test_sequence_flow(client):
    r = await (await client.post("/api/sequence/arm", json={"index": 0})).json()
    assert r["ok"] and r["result"]["phase"] == "armed"
    assert client.stub.task.startswith("pick up the tortilla") and client.stub.active is None
    r = await (await client.post("/api/sequence/start")).json()
    assert r["result"]["phase"] == "running" and client.stub.active == P.DREAM_TASKS_IDENTITY
    r = await (await client.post("/api/sequence/done")).json()
    assert r["result"]["index"] == 1 and client.stub.active is None
