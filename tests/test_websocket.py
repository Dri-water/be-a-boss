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
        gate = _gatekeeper("s3cret", {"null"}, {})
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


def _http_probe(base: str) -> dict:
    """Blocking HTTP checks — run in an executor so they don't stall the loop the
    server itself is running on."""
    import urllib.error
    import urllib.request
    out: dict = {}
    with urllib.request.urlopen(base + "/") as r:                 # "/" → index.html
        out["index"] = (r.status, r.headers["Content-Type"], r.read())
    with urllib.request.urlopen(base + "/client.js") as r:
        out["js"] = (r.status, r.headers["Content-Type"])
    try:
        urllib.request.urlopen(base + "/nope")
        out["missing"] = None
    except urllib.error.HTTPError as e:
        out["missing"] = e.code
    return out


def test_http_serves_app_shell_and_still_gates_ws():
    """One port, two jobs: a plain browser GET is served the static shell; the
    WebSocket upgrade is still refused without the token."""

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        assets = {
            "/index.html": ("text/html; charset=utf-8",
                            b"<!doctype html><title>be-a-boss</title>"),
            "/client.js": ("text/javascript; charset=utf-8", b"window.beaboss={};"),
        }
        gate = _gatekeeper("s3cret", {"null"}, assets)
        async with serve(make_handler(engine, transport), "127.0.0.1", 0,
                         process_request=gate) as server:
            port = server.sockets[0].getsockname()[1]
            probe = await asyncio.get_running_loop().run_in_executor(
                None, _http_probe, f"http://127.0.0.1:{port}")

            status, ctype, body = probe["index"]
            assert status == 200 and ctype.startswith("text/html")
            assert b"be-a-boss" in body                            # served the shell
            assert probe["js"][1].startswith("text/javascript")    # right content-type
            assert probe["missing"] == 404                         # unknown path → 404

            # the capability is still gated: a wrong-token WS upgrade is refused
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://127.0.0.1:{port}?token=nope"):
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


def test_history_replayed_on_reconnect(tmp_path):
    """A reconnecting/reloading client is not amnesiac: the server replays recent
    messages after the threads snapshot (HIGH-2 from the UX review)."""
    from beaboss.core.ports import Outbound, Speaker

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            # first client sends a message + gets a reply, then disconnects
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()  # snapshot
                await ws.send(json.dumps({"type": "message", "thread_id": "general",
                                          "text": "build X"}))
                await _wait_for(lambda: len(engine.inbound) == 1)
                await transport.post(Outbound(
                    thread_id="general",
                    speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
                    text="on it"))
            # a fresh client replays the whole exchange
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
                assert json.loads(await ws2.recv())["type"] == "threads"
                replay = [json.loads(await ws2.recv()) for _ in range(2)]
        texts = [e["text"] for e in replay]
        assert texts == ["build X", "on it"]        # both sides, in order

    asyncio.run(scenario())


def test_web_new_empty_path_is_guarded(tmp_path):
    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "new", "path": ""}))
                evt = json.loads(await ws.recv())
        assert "Usage" in evt["text"]
        assert not any(c[0] == "new" for c in engine.calls)   # no session opened

    asyncio.run(scenario())


def test_app_shell_ships_inside_the_package():
    """Finding 1 regression: the web assets must live INSIDE the package so a wheel /
    Docker image serves them — not only a source checkout. If they move back out,
    `python -m beaboss.web` 404s in every non-source deploy."""
    import pathlib
    import beaboss.web
    from beaboss.transports.websocket import _load_assets
    static = pathlib.Path(beaboss.web.__file__).resolve().parent / "static"
    assets = _load_assets(static)
    assert "/index.html" in assets and "/client.js" in assets
    assert assets["/index.html"][0].startswith("text/html")


def test_decode_inbound_media_validates_caps_and_kind(monkeypatch):
    """Inbound media is decoded defensively: bad base64 / non-dicts skipped, kind
    derived server-side from the mime, count and per-file size capped."""
    import base64
    from beaboss.transports import websocket as ws
    good = base64.b64encode(b"\x89PNGDATA").decode()
    items = ws._decode_inbound_media([
        {"filename": "a.png", "mime": "image/png", "data_b64": good},
        {"filename": "b.txt", "mime": "text/plain",
         "data_b64": base64.b64encode(b"hi").decode()},
        {"filename": "bad", "mime": "image/png", "data_b64": "!!! not base64"},
        "not-a-dict",
    ])
    assert [i.filename for i in items] == ["a.png", "b.txt"]     # junk dropped
    assert items[0].kind == "image" and items[1].kind == "file"  # kind from mime
    assert items[0].data == b"\x89PNGDATA"
    assert ws._decode_inbound_media(None) == [] and ws._decode_inbound_media("x") == []

    # count cap
    many = [{"filename": f"{i}", "mime": "image/png",
             "data_b64": base64.b64encode(b"x").decode()}
            for i in range(ws._INBOUND_MEDIA_MAX + 5)]
    assert len(ws._decode_inbound_media(many)) == ws._INBOUND_MEDIA_MAX

    # per-file size cap (patched small so the test stays cheap)
    monkeypatch.setattr(ws, "_INBOUND_MEDIA_CAP", 4)
    over = [{"filename": "big", "mime": "image/png",
             "data_b64": base64.b64encode(b"12345").decode()}]  # 5 bytes > 4
    assert ws._decode_inbound_media(over) == []


def test_ws_media_message_routes_with_decoded_media():
    """A browser message carrying base64 media reaches engine.on_inbound with real
    MediaIn — and a media-only message (empty caption) is delivered too."""
    import base64

    async def scenario():
        engine = FakeEngine()
        transport = WebSocketTransport()
        async with serve(make_handler(engine, transport), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws_:
                await ws_.recv()  # snapshot
                await ws_.send(json.dumps({
                    "type": "message", "thread_id": "general", "text": "look at this",
                    "media": [{"filename": "shot.png", "mime": "image/png",
                               "data_b64": base64.b64encode(b"PNGDATA").decode()}]}))
                await _wait_for(lambda: len(engine.inbound) == 1)
                m = engine.inbound[0]
                assert m.text == "look at this" and len(m.media) == 1
                assert m.media[0].filename == "shot.png"
                assert m.media[0].data == b"PNGDATA" and m.media[0].kind == "image"

                # media-only (empty caption) still routes
                await ws_.send(json.dumps({
                    "type": "message", "thread_id": "general", "text": "",
                    "media": [{"filename": "f.bin", "mime": "application/octet-stream",
                               "data_b64": base64.b64encode(b"x").decode()}]}))
                await _wait_for(lambda: len(engine.inbound) == 2)
                assert engine.inbound[1].text == "" and engine.inbound[1].media[0].kind == "file"

    asyncio.run(scenario())
