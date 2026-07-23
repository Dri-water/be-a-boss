import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from beaboss.config import Settings
from beaboss.core.ports import MediaIn, Outbound, Speaker
from beaboss.core.session import CoreSession, Turn


def _settings(tmp: Path) -> Settings:
    return Settings(
        bot_token="t", allowed_user_ids={1}, chat_id=None,
        permission_mode="bypassPermissions", projects_root=tmp, cli_path=None,
        model=None, max_turns=None, state_dir=tmp / "state",
        bot_name="X", session_system_append=None,
    )


class SinkPost:
    def __init__(self):
        self.out: list[Outbound] = []

    async def __call__(self, out: Outbound):
        self.out.append(out)


async def _noop_busy(thread_id: str):
    pass


def _session(tmp: Path, post=None) -> CoreSession:
    return CoreSession(
        thread_id="t1", cwd=tmp,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=_settings(tmp), post=post or SinkPost(), busy=_noop_busy,
        on_session_id=lambda _s: None,
    )


def test_build_options_has_chat_mcp_and_env_prompt(tmp_path):
    opts = _session(tmp_path)._build_options()
    assert "chat" in opts.mcp_servers
    assert opts.system_prompt["preset"] == "claude_code"
    assert "send_photo" in opts.system_prompt["append"]


def test_extra_mcp_servers_merge(tmp_path):
    s = _session(tmp_path)
    s._extra_mcp = {"fleet": object()}
    opts = s._build_options()
    assert set(opts.mcp_servers.keys()) == {"chat", "fleet"}


def test_system_append_override(tmp_path):
    s = _session(tmp_path)
    s._system_append = ""
    assert s._build_options().system_prompt is None
    s._system_append = "only this"
    assert s._build_options().system_prompt["append"] == "only this"


def test_submit_media_saves_inbox_and_builds_turn(tmp_path):
    sess = _session(tmp_path)
    items = [
        MediaIn("image", "a.png", "image/png", b"\x89PNG\r\n"),
        MediaIn("file", "n.txt", "text/plain", b"hi"),
    ]
    asyncio.run(sess.submit_media("hello", items))
    turn = sess._queue.get_nowait()
    assert isinstance(turn, Turn)
    assert len(turn.images) == 1 and turn.images[0]["media_type"] == "image/png"
    assert "hello" in turn.text and ".beaboss-inbox" in turn.text
    assert (tmp_path / ".beaboss-inbox" / "a.png").is_file()


def test_submit_media_sanitizes_filename(tmp_path):
    sess = _session(tmp_path)
    asyncio.run(sess.submit_media(
        "", [MediaIn("file", "../../evil.txt", "text/plain", b"x")]))
    assert (tmp_path / ".beaboss-inbox" / "evil.txt").is_file()
    assert not (tmp_path.parent / "evil.txt").exists()


@pytest.mark.parametrize("path,fragment", [
    ("../escape.png", "outside"),
    ("nope.png", "no such file"),
    ("  ", "required"),
])
def test_tool_send_guards(tmp_path, path, fragment):
    r = asyncio.run(_session(tmp_path)._tool_send({"path": path}, "photo"))
    assert r["is_error"] and fragment in r["content"][0]["text"]


class FakeBackend:
    """An in-memory AgentBackend — no Claude client, no subprocess, no network.

    It replays a scripted list of SDK message objects for each turn, which is all
    a backend owes the session: connect, take a turn, stream events, stop.
    """

    def __init__(self, scripted: list):
        self._scripted = scripted
        self.started = 0
        self.stopped = 0
        self.interrupted = 0
        self.sent: list[Turn] = []

    async def start(self):
        self.started += 1

    async def send(self, turn: Turn):
        self.sent.append(turn)

    def receive(self):
        async def gen():
            for m in self._scripted:
                yield m
        return gen()

    async def interrupt(self):
        self.interrupted += 1

    async def stop(self):
        self.stopped += 1


def test_fake_backend_drives_a_turn(tmp_path):
    """Proves the seam: a session runs a full turn against a swapped-in backend,
    rendering its events and firing the done hook — with no Claude involved."""
    post = SinkPost()
    backend = FakeBackend([
        AssistantMessage(content=[TextBlock(text="hello from fake")], model="fake"),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=2, session_id="sess-xyz", total_cost_usd=0.01,
        ),
    ])
    captured_sids: list[str] = []
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=captured_sids.append, backend=backend,
    )
    done: list[ResultMessage] = []

    async def on_done(_s, result):
        done.append(result)

    sess.on_turn_done = on_done

    async def drive():
        await sess.start()
        await sess.submit("hi there")
        await sess._queue.join()  # wait for the worker to finish the turn
        await sess.stop()

    asyncio.run(drive())

    assert backend.started == 1 and backend.stopped == 1
    assert [t.text for t in backend.sent] == ["hi there"]
    texts = [o.text for o in post.out]
    assert any("hello from fake" in t for t in texts)
    assert any("done · 2 turns" in t for t in texts)
    assert sess.session_id == "sess-xyz" and captured_sids == ["sess-xyz"]
    assert sess.turns == 1
    assert done and done[0].session_id == "sess-xyz"


class HangingBackend(FakeBackend):
    """receive() blocks forever without ending the turn — a wedged agent."""

    def receive(self):
        async def gen():
            await asyncio.Event().wait()  # never fires
            yield  # unreachable
        return gen()


def test_turn_idle_timeout_recovers_a_wedged_backend(tmp_path):
    """A backend that hangs mid-turn must not pin the session in 'busy' forever:
    the idle timeout interrupts, reports it, and reconnects."""
    post = SinkPost()
    backend = HangingBackend([])
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend,
    )
    sess.TURN_IDLE_TIMEOUT = 0.05  # trip fast

    async def drive():
        await sess.start()
        await sess.submit("do something")
        await sess._queue.join()  # resolves via timeout, does NOT hang
        await sess.stop()

    asyncio.run(asyncio.wait_for(drive(), timeout=5))
    assert backend.interrupted >= 1                   # the wedged turn was interrupted
    assert any("wedged" in o.text for o in post.out)  # legible
    assert backend.started >= 2                        # reconnected after the timeout
    assert sess.status != "busy"                        # not stuck busy


def test_alive_reflects_worker_and_status(tmp_path):
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=_settings(tmp_path), post=SinkPost(), busy=_noop_busy,
        on_session_id=lambda _s: None, backend=FakeBackend([]),
    )
    assert sess.alive is False  # not started → no run task

    async def drive():
        await sess.start()
        assert sess.alive is True          # run task consuming
        sess.status = "error"              # a failed reconnect
        assert sess.alive is False         # terminal error → unhealthy
        sess.status = "idle"
        await sess.stop()
        await asyncio.sleep(0.05)          # let the cancellation settle
        assert sess.alive is False         # run task gone → unhealthy

    asyncio.run(drive())


def test_tool_send_posts_outbound_with_speaker(tmp_path):
    post = SinkPost()
    sess = _session(tmp_path, post=post)
    f = tmp_path / "pic.png"
    f.write_bytes(b"\x89PNG")
    r = asyncio.run(sess._tool_send({"path": "pic.png", "caption": "c"}, "photo"))
    assert not r.get("is_error")
    assert len(post.out) == 1
    out = post.out[0]
    assert out.media_kind == "photo" and out.speaker.name == "Nova"
    assert out.thread_id == "t1" and out.caption == "c"
