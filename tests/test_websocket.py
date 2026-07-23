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

    async def on_inbound(self, msg):
        self.inbound.append(msg)


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
