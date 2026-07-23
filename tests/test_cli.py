"""The CLI surface: an agent (or a human) drives the whole org from stdin, and
sees the same JSON events the web surface emits. Fully headless — no engine start,
no Claude — we assert command dispatch reaches the engine and events are emitted."""

import asyncio
from pathlib import Path

from beaboss.cli.__main__ import State, handle_line
from beaboss.core.ports import Outbound, Speaker
from beaboss.transports.cli import CLITransport


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
        self.calls.append(("new", path, name)); return ("99", "myproj")

    async def approve_delivery(self, wid):
        self.calls.append(("approve", wid)); return "approved"

    async def reject_delivery(self, wid):
        self.calls.append(("reject", wid)); return "rejected"

    async def factory_reset(self):
        self.calls.append(("reset",)); return "🏭 wiped"


def _wired():
    events = []

    async def emit(ev):
        events.append(ev)

    transport = CLITransport(emit)
    engine = FakeEngine()
    return engine, transport, events


def _run(engine, transport, state, *lines):
    async def go():
        for line in lines:
            await handle_line(engine, transport, state, line)
    asyncio.run(go())


# ---- transport emits the shared event shapes --------------------------------

def test_transport_emits_websocket_compatible_events(tmp_path):
    events = []

    async def emit(ev):
        events.append(ev)

    async def go():
        t = CLITransport(emit)
        tid = await t.create_thread("⚙️ Nova · app")
        await t.post(Outbound(thread_id=tid,
                              speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
                              text="on it"))
        await t.update_dashboard("📋 1 running")
        pic = tmp_path / "shot.png"; pic.write_bytes(b"\x89PNGxx")
        await t.post(Outbound(thread_id=tid,
                              speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
                              media_path=pic, media_kind="photo", caption="result"))
        return tid

    tid = asyncio.run(go())
    kinds = [e["type"] for e in events]
    assert kinds == ["thread", "message", "dashboard", "media"]
    assert events[1] == {"type": "message", "thread_id": tid,
                         "speaker": {"role": "worker", "name": "Nova", "emoji": "⚙️"},
                         "text": "on it"}
    assert events[3]["kind"] == "photo" and events[3]["filename"] == "shot.png"


# ---- input drives the engine identically for text / slash / JSON ------------

def test_plain_text_goes_to_active_thread():
    engine, transport, _ = _wired()
    state = State()
    _run(engine, transport, state, "build the login page")
    assert len(engine.inbound) == 1
    assert engine.inbound[0].thread_id == "general"
    assert engine.inbound[0].text == "build the login page"


def test_thread_switch_retargets_plain_text():
    engine, transport, _ = _wired()
    state = State()

    async def go():
        await transport.create_thread("⚙️ Nova")   # id "1"
        await handle_line(engine, transport, state, "/thread 1")
        await handle_line(engine, transport, state, "focus on the header")
    asyncio.run(go())
    assert state.active == "1"
    assert engine.inbound[-1].thread_id == "1"


def test_slash_and_json_commands_are_equivalent():
    for line in ("/approve nova", '{"type": "approve", "worker_id": "nova"}'):
        engine, transport, _ = _wired()
        _run(engine, transport, State(), line)
        assert ("approve", "nova") in engine.calls


def test_kill_office_is_refused():
    engine, transport, events = _wired()
    _run(engine, transport, State(), "/kill")     # active is the office
    assert not any(c[0] == "kill" for c in engine.calls)
    assert any("Can't kill" in e.get("text", "") for e in events if e["type"] == "message")


def test_new_switches_active_and_reset_needs_confirm():
    engine, transport, events = _wired()
    state = State()
    _run(engine, transport, state, "/new /some/repo cool")
    assert ("new", "/some/repo", "cool") in engine.calls
    assert state.active == "99"                    # follow the new session

    engine2, transport2, ev2 = _wired()
    _run(engine2, transport2, State(), "/reset")   # no confirm → no wipe
    assert not any(c[0] == "reset" for c in engine2.calls)
    _run(engine2, transport2, State(), "/reset confirm")
    assert ("reset",) in engine2.calls


def test_bad_json_is_reported_not_fatal():
    engine, transport, events = _wired()
    _run(engine, transport, State(), '{"type": broken')
    assert any("not valid JSON" in e.get("text", "") for e in events)
    assert engine.inbound == [] and engine.calls == []


def test_tui_reveals_frames_as_work_appears():
    """The self-assembling cockpit: idle it's minimal; the sidebar + dashboard reveal
    only once a worker exists and the fleet is moving."""
    import pytest
    pytest.importorskip("textual")
    from beaboss.cli.tui import Cockpit

    async def go():
        app = Cockpit(bot_name="X")
        async with app.run_test(size=(90, 26)):
            assert not app.query_one("#sidebar").has_class("show")   # idle: hidden
            assert not app.query_one("#dash").has_class("show")
            await app.apply_event({"type": "thread", "id": "1",
                                   "title": "⚙️ Nova · app", "open": True})
            await app.apply_event({"type": "dashboard",
                                   "text": "📋 status\n🟢 1 running"})
            assert app.query_one("#sidebar").has_class("show")       # revealed
            assert app.query_one("#dash").has_class("show")

    asyncio.run(go())
