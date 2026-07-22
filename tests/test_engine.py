import asyncio
from pathlib import Path

from tasm.config import Settings
from tasm.core.engine import Engine
from tasm.core.ports import InboundMessage, Outbound
from tasm.core.store import CoreStore, ThreadRecord


def _settings(tmp: Path) -> Settings:
    return Settings(
        bot_token="t", allowed_user_ids={1}, chat_id=None,
        permission_mode="bypassPermissions", projects_root=tmp / "projects",
        cli_path=None, model=None, max_turns=None, state_dir=tmp / "state",
        bot_name="Lim Wei Jie", session_system_append=None,
    )


class FakeTransport:
    def __init__(self):
        self.posts: list[Outbound] = []
        self.threads: list[str] = []
        self.closed: list[str] = []
        self._next = 100

    async def create_thread(self, title: str) -> str:
        self._next += 1
        tid = str(self._next)
        self.threads.append(title)
        return tid

    async def rename_thread(self, thread_id, title):
        pass

    async def close_thread(self, thread_id):
        self.closed.append(thread_id)

    async def post(self, out: Outbound):
        self.posts.append(out)

    async def indicate_busy(self, thread_id):
        pass


class FakeSession:
    """Stands in for CoreSession — records submits, no Claude involved."""

    def __init__(self):
        self.submitted: list[str] = []
        self.media: list[tuple[str, int]] = []
        self.status = "idle"
        self.pending = 0

    async def submit(self, text):
        self.submitted.append(text)

    async def submit_media(self, caption, items):
        self.media.append((caption, len(items)))

    async def stop(self):
        self.status = "stopped"

    async def interrupt(self):
        pass


def _engine(tmp: Path) -> tuple[Engine, FakeTransport]:
    (tmp / "projects").mkdir(parents=True, exist_ok=True)
    engine = Engine(_settings(tmp), CoreStore(tmp / "state"))
    engine.WAKE_COALESCE_SECS = 0  # no waiting in tests
    t = FakeTransport()
    engine.attach_transport(t)
    return engine, t


def test_unknown_thread_gets_system_hint(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.set_orchestrator_thread("general")  # office exists elsewhere
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="999", text="hi")))
    assert len(t.posts) == 1 and t.posts[0].speaker.role == "system"


def test_interjection_reaches_coder_and_inbox(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="coder", name="Nova", cwd=str(tmp_path), coder_id="nova",
        repo=str(tmp_path), task="fix"))
    fake = FakeSession()
    engine.sessions["55"] = fake

    asyncio.run(engine.on_inbound(InboundMessage(
        thread_id="55", text="check the TTL too", sender_name="Jon")))

    assert len(fake.submitted) == 1
    assert "Interjection from Jon" in fake.submitted[0]
    assert "check the TTL too" in fake.submitted[0]
    assert any("Jon said in nova's thread" in n for n in engine._inbox)


def test_orchestrator_wake_digest_drains_inbox(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    fake = FakeSession()
    engine.sessions["general"] = fake

    engine._note("coder nova finished: STATUS: done")
    engine._note("coder kite blocked: STATUS: blocked: need creds")
    asyncio.run(engine._wake_orchestrator())

    assert len(fake.submitted) == 1
    digest = fake.submitted[0]
    assert digest.startswith("[fleet inbox]")
    assert "nova finished" in digest and "kite blocked" in digest
    assert engine._inbox == []


def test_orchestrator_message_routes_plain(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    fake = FakeSession()
    engine.sessions["general"] = fake
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="general", text="status?")))
    assert fake.submitted == ["status?"]


def test_speakers(tmp_path):
    engine, _ = _engine(tmp_path)
    o = engine.orchestrator_speaker()
    assert o.role == "orchestrator" and o.name == "Lim Wei Jie" and o.emoji == "🧭"
    c = engine.coder_speaker("Nova")
    assert c.label == "⚙️ Nova"


def test_listing_and_kill_direct(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("7", ThreadRecord(role="direct", name="d", cwd=str(tmp_path)))
    rows = engine.listing()
    assert rows[0][2] == "dormant"
    assert asyncio.run(engine.kill("7")) is True
    assert engine.store.get("7") is None
