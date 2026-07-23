"""Headless smoke test for the WebSocket transport.

Wires the transport to a fake engine, connects a real python websocket client,
and proves both directions: client message -> engine.on_inbound, and
engine Outbound -> client. No browser, no Claude.
"""

import asyncio
import json

import pytest
import websockets
from websockets.asyncio.server import serve

from beaboss.core.ports import Outbound, Speaker, Transport
from beaboss.transports.websocket import WebSocketTransport, _gatekeeper, make_handler


def test_implements_transport_contract():
    assert isinstance(WebSocketTransport(), Transport)


class FakeEngine:
    def __init__(self):
        self.inbound = []
        self.calls = []

    async def on_inbound(self, msg):
        self.inbound.append(msg)

    async def interrupt(self, tid):
        self.calls.append(("interrupt", tid)); return True

    async def kill(self, tid):
        self.calls.append(("kill", tid)); return True

    async def new_direct(self, path, name):
        self.calls.append(("new", path, name)); return ("99", "d")

    async def approve_delivery(self, wid):
        self.calls.append(("approve", wid)); return "approved"

    async def reject_delivery(self, wid):
        self.calls.append(("reject", wid)); return "rejected"


async def _wait_for(predicate, timeout=2.0):
    deadline = timeout / 0.01
    for _ in range(int(deadline)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def _roundtrip():
    engine = FakeEngine()
    transport = WebSocketTransport()

    async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            # On connect the client gets a snapshot including the office thread.
            snap = json.loads(await ws.recv())
            assert snap["type"] == "threads"
            assert any(t["id"] == "general" for t in snap["threads"])

            # client -> engine.on_inbound
            await ws.send(json.dumps(
                {"type": "message", "thread_id": "general", "text": "hi there"}))
            await _wait_for(lambda: len(engine.inbound) == 1)
            msg = engine.inbound[0]
            assert msg.thread_id == "general"
            assert msg.text == "hi there"

            # engine Outbound -> client
            await transport.post(Outbound(
                thread_id="general",
                speaker=Speaker(role="orchestrator", name="Boss", emoji="🧭"),
                text="on it",
            ))
            out = json.loads(await ws.recv())
            assert out["type"] == "message"
            assert out["thread_id"] == "general"
            assert out["speaker"]["role"] == "orchestrator"
            assert out["text"] == "on it"


def test_websocket_roundtrip():
    asyncio.run(_roundtrip())


def test_gate_rejects_bad_origin_and_missing_token():
    """CSWSH defense: a cross-origin browser page and a token-less client are both
    refused; only the correct token (with an allowed/absent Origin) connects."""

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        gate = _gatekeeper("s3cret", {"null"})
        async with serve(make_handler(engine, transport), "127.0.0.1", 0,
                         process_request=gate) as server:
            port = server.sockets[0].getsockname()[1]
            base = f"ws://127.0.0.1:{port}"

            # correct token, no Origin (non-browser) → connects
            async with websockets.connect(f"{base}?token=s3cret") as ws:
                assert json.loads(await ws.recv())["type"] == "threads"

            # missing token → refused
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(base):
                    pass

            # wrong token → refused
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"{base}?token=nope"):
                    pass

            # cross-origin browser page (even with the token) → refused
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(
                        f"{base}?token=s3cret",
                        additional_headers={"Origin": "https://evil.example"}):
                    pass

    asyncio.run(scenario())


def test_rehydrate_seeds_threads_and_advances_id(tmp_path):
    """A web restart re-seeds worker threads from the store and advances the id
    counter past them, so a fresh thread can't reuse and overwrite a live id."""
    from beaboss.core.store import CoreStore, ThreadRecord
    store = CoreStore(tmp_path / "state")
    store.put("3", ThreadRecord(role="worker", name="Nova", worker_id="nova",
                                repo="/r/myapp", worker_status="working"))
    transport = WebSocketTransport(store)
    assert "3" in transport.threads and "Nova" in transport.threads["3"]["title"]
    assert asyncio.run(transport.create_thread("new")) == "4"  # no reuse of "3"


def test_ws_commands_route_to_engine():
    """The web kill switch + approval: interrupt/kill/approve/reject/new reach the
    engine (they were silently unsupported before)."""

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # snapshot
                await ws.send(json.dumps({"type": "interrupt", "thread_id": "5"}))
                await ws.send(json.dumps({"type": "approve", "worker_id": "nova"}))
                await ws.send(json.dumps({"type": "kill", "thread_id": "5"}))
                await _wait_for(lambda: len(engine.calls) == 3)
        assert ("interrupt", "5") in engine.calls
        assert ("approve", "nova") in engine.calls
        assert ("kill", "5") in engine.calls

    asyncio.run(scenario())


def test_create_thread_broadcasts_to_clients():
    """A new core thread appears on connected clients without a reconnect."""

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # snapshot
                tid = await transport.create_thread("⚙️ Nova · myrepo")
                evt = json.loads(await ws.recv())
                assert evt["type"] == "thread"
                assert evt["id"] == tid
                assert evt["title"] == "⚙️ Nova · myrepo"
                assert evt["open"] is True

    asyncio.run(scenario())


def test_media_posts_as_real_inline_event(tmp_path):
    """A worker's screenshot reaches the browser as an actual image event — parity
    with Telegram's send_photo, not a placeholder line."""
    import base64
    from beaboss.core.ports import Outbound, Speaker

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        pic = tmp_path / "shot.png"
        pic.write_bytes(b"\x89PNG\r\nfakedata")
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # snapshot
                await transport.post(Outbound(
                    thread_id="general",
                    speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
                    media_path=pic, media_kind="photo", caption="the result"))
                evt = json.loads(await ws.recv())
        assert evt["type"] == "media" and evt["kind"] == "photo"
        assert evt["filename"] == "shot.png" and evt["caption"] == "the result"
        assert base64.b64decode(evt["data_b64"]).startswith(b"\x89PNG")

    asyncio.run(scenario())


def test_ws_kill_cannot_kill_the_office():
    """Regression: web kill of 'general' must be refused — it would wipe the
    orchestrator's memory (Telegram already guards this)."""

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # snapshot
                await ws.send(json.dumps({"type": "kill", "thread_id": "general"}))
                evt = json.loads(await ws.recv())     # the refusal message
        assert not any(c[0] == "kill" for c in engine.calls)   # engine.kill NOT called
        assert "office" in evt["text"]

    asyncio.run(scenario())


def test_dashboard_broadcasts_and_snapshots(tmp_path):
    """The live board reaches connected clients and is included in the connect
    snapshot for late joiners — web parity with the pinned Telegram message."""

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # snapshot
                await transport.update_dashboard("📋 live status: 1 running")
                evt = json.loads(await ws.recv())
                assert evt == {"type": "dashboard", "text": "📋 live status: 1 running"}
            # a late joiner gets the board right in its snapshot
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
                assert json.loads(await ws2.recv())["type"] == "threads"
                assert json.loads(await ws2.recv())["type"] == "dashboard"

    asyncio.run(scenario())
