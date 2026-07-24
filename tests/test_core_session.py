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


def test_scrubbed_env_removes_bot_secrets(monkeypatch):
    from beaboss.core.agent_backend import scrubbed_env
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("SOMETHING_ELSE", "keepme")
    env = scrubbed_env()
    assert "GH_TOKEN" not in env and "TELEGRAM_BOT_TOKEN" not in env
    assert env.get("SOMETHING_ELSE") == "keepme"  # non-secrets pass through


def test_build_options_scrubs_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    opts = _session(tmp_path)._build_options()
    assert "GH_TOKEN" not in (opts.env or {})


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
    assert any("turn ended · 2 turns" in t for t in texts)
    assert sess.session_id == "sess-xyz" and captured_sids == ["sess-xyz"]
    assert sess.turns == 1
    assert done and done[0].session_id == "sess-xyz"


def test_final_only_session_posts_one_clean_reply(tmp_path):
    """The orchestrator texts like a person: ONE message per turn — the final
    reply — no streamed narration, no tool lines, no cost footer."""
    post = SinkPost()
    backend = FakeBackend([
        AssistantMessage(content=[TextBlock(text="let me check the fleet…")], model="fake"),
        AssistantMessage(content=[TextBlock(text="spawning now")], model="fake"),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=3, session_id="s1", total_cost_usd=0.02,
            result="Nova is hired and working on it.",
        ),
    ])
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend, final_only=True,
    )

    async def drive():
        await sess.start()
        await sess.submit("hire someone")
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    assert [o.text for o in post.out] == ["Nova is hired and working on it."]


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


def test_final_only_appends_action_footer(tmp_path):
    """The code-generated ⚙ action line rides on the reply itself — the boss sees
    what actually happened in the same message."""
    post = SinkPost()
    backend = FakeBackend([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=2, session_id="s1", total_cost_usd=0.01,
            result="Nova is on it.",
        ),
    ])
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend, final_only=True,
        footer_fn=lambda: "⚙ spawn_worker → Nova · myapp",
    )

    async def drive():
        await sess.start()
        await sess.submit("build it", reply_to="dm:1")
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    assert [o.text for o in post.out] == ["Nova is on it.\n\n⚙ spawn_worker → Nova · myapp"]


def test_streaming_session_batches_tool_lines(tmp_path):
    """A many-tool worker turn must not be a message per tool call — consecutive 🔧
    lines are flushed as one message."""
    from claude_agent_sdk import ToolUseBlock
    post = SinkPost()
    backend = FakeBackend([
        AssistantMessage(content=[
            ToolUseBlock(id="1", name="Bash", input={"command": "ls"}),
            ToolUseBlock(id="2", name="Read", input={"file_path": "a.py"}),
            ToolUseBlock(id="3", name="Bash", input={"command": "pytest"}),
        ], model="fake"),
        AssistantMessage(content=[TextBlock(text="all tests pass")], model="fake"),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=2, session_id="s1", total_cost_usd=0.01,
        ),
    ])
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend,
    )

    async def drive():
        await sess.start()
        await sess.submit("run the tests")
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    texts = [o.text for o in post.out]
    assert len(texts) == 3                          # tools batch + reply + footer
    assert texts[0].count("🔧") == 3                # one message, three tool lines
    assert texts[1] == "all tests pass"


def test_interrupted_turn_says_stopped_not_diagnostics(tmp_path):
    """A human /stop MID-TURN must render as '⏹ stopped.' — not leak the backend's
    internal diagnostic result (observed: '⚠️ [ede_diagnostic] result_type=user …').
    The flag is set during the turn (after the per-turn reset), as interrupt() does."""
    post = SinkPost()
    holder = {}

    class StopMidTurn(FakeBackend):
        async def send(self, turn):
            await super().send(turn)
            holder["sess"]._interrupted = True   # boss hits /stop while busy

    backend = StopMidTurn([
        ResultMessage(
            subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
            is_error=True, num_turns=1, session_id="s1",
            result="[ede_diagnostic] result_type=user last_content_type=n/a",
        ),
    ])
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend, final_only=True,
    )
    holder["sess"] = sess

    async def drive():
        await sess.start()
        await sess.submit("do something", reply_to="dm:1")
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    assert [o.text for o in post.out] == ["⏹ stopped."]


def test_quiet_digest_posts_nothing_on_the_real_path(tmp_path):
    """The production path: a digest carries reply_to (never None) AND quiet_ok — an
    empty/sentinel reply must post NOTHING. (The old test used reply_to=None, a path
    the engine never takes, so it masked the '✓ done' spam bug.)"""
    post = SinkPost()
    backend = FakeBackend([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s1", result="NOTHING",
        ),
    ])
    sess = CoreSession(
        thread_id="general", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend, final_only=True,
    )

    async def drive():
        await sess.start()
        # exactly what _wake_orchestrator does: reply_to set + quiet_ok=True
        await sess.submit("[fleet inbox]\n- worker nova finished",
                          reply_to="general", quiet_ok=True)
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    assert post.out == []          # quiet checkpoint → no message, no "✓ done"


def test_boss_turn_never_ends_silent(tmp_path):
    """The counterpart: a BOSS turn (reply_to set, quiet_ok False) with an empty
    reply must still post something — never leave the boss hanging."""
    post = SinkPost()
    backend = FakeBackend([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s1", result="",
        ),
    ])
    sess = CoreSession(
        thread_id="general", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend, final_only=True)

    async def drive():
        await sess.start()
        await sess.submit("hi", reply_to="dm:1")     # boss turn: quiet_ok defaults False
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    assert post.out and post.out[0].text == "✓ done"


def test_stale_interrupt_flag_does_not_eat_the_next_reply(tmp_path):
    """Regression: an interrupted turn that ends with no ResultMessage must not leave
    _interrupted set and replace the NEXT turn's real reply with '⏹ stopped.'"""
    post = SinkPost()
    backend = FakeBackend([
        # turn 1: streams a block then ends with NO ResultMessage (racy interrupt)
        AssistantMessage(content=[TextBlock(text="partial…")], model="fake"),
        # turn 2: a normal successful reply
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s1", result="here is the real answer"),
    ])
    sess = CoreSession(
        thread_id="general", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend, final_only=True)

    async def drive():
        await sess.start()
        sess._interrupted = True            # leaked from a prior interrupted turn
        await sess.submit("do it", reply_to="dm:1")
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    # the reset at turn-start cleared the stale flag → the real answer survives
    assert any("real answer" in o.text for o in post.out)
    assert not any(o.text.startswith("⏹ stopped") for o in post.out)


def test_interrupt_sets_flag_only_when_busy(tmp_path):
    sess = _session(tmp_path)
    asyncio.run(sess.interrupt())          # idle: no-op
    assert sess._interrupted is False
    sess.status = "busy"
    sess._backend = FakeBackend([])        # interrupt() calls the backend
    asyncio.run(sess.interrupt())
    assert sess._interrupted is True


def test_quiet_turn_still_signals_idle(tmp_path):
    """Finding 2 regression: a final_only turn that posts NOTHING (a quiet
    orchestrator digest) must still clear the cockpit's 'working' indicator — idle
    fires at turn-end regardless of whether any text was posted."""
    busy_calls: list[str] = []
    idle_calls: list[str] = []

    async def rec_busy(tid): busy_calls.append(tid)
    async def rec_idle(tid): idle_calls.append(tid)

    post = SinkPost()
    backend = FakeBackend([
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                      is_error=False, num_turns=1, session_id="s",
                      total_cost_usd=0.0, result=None),   # nothing boss-facing
    ])
    sess = CoreSession(
        thread_id="general", cwd=tmp_path,
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"),
        settings=_settings(tmp_path), post=post, busy=rec_busy, idle=rec_idle,
        on_session_id=lambda _s: None, backend=backend, final_only=True,
    )

    async def drive():
        await sess.start()
        await sess.submit("digest")     # reply_to unset + quiet → posts nothing
        await sess._queue.join()
        await sess.stop()

    asyncio.run(drive())
    assert post.out == []               # confirmed: the quiet digest posted nothing
    assert busy_calls == ["general"]    # working was shown…
    assert idle_calls == ["general"]    # …and cleared at turn-end (no stuck indicator)


def test_safe_name_strips_nul_and_stays_basename():
    """Security-review follow-up: _safe_name keeps only the basename (never a
    traversal) AND strips NULs, which pass through Path().name but make write_bytes
    raise ValueError."""
    from beaboss.core.session import _safe_name
    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("a\x00b.png") == "ab.png"
    assert _safe_name("../../x\x00y") == "xy"
    assert _safe_name("\x00") == "file" and _safe_name("") == "file"


def test_submit_media_nul_filename_is_saved_not_crashed(tmp_path):
    """A crafted NUL filename must not raise out of submit_media (which would drop the
    web client's socket); it's sanitized and saved."""
    sess = _session(tmp_path)
    asyncio.run(sess.submit_media(
        "", [MediaIn("file", "ev\x00il.bin", "application/octet-stream", b"x")]))
    assert (tmp_path / ".beaboss-inbox" / "evil.bin").is_file()
    assert sess._queue.get_nowait().text  # a turn was still queued


def test_submit_media_unsupported_image_mime_saved_but_not_vision(tmp_path):
    """An image type the API can't see (svg/bmp/tiff) is saved as a file, not sent as
    a vision block that would 400 the boss's own turn."""
    sess = _session(tmp_path)
    asyncio.run(sess.submit_media("look", [
        MediaIn("image", "logo.svg", "image/svg+xml", b"<svg/>"),
        MediaIn("image", "shot.png", "image/png", b"\x89PNG"),
    ]))
    turn = sess._queue.get_nowait()
    assert [i["media_type"] for i in turn.images] == ["image/png"]   # svg excluded
    assert (tmp_path / ".beaboss-inbox" / "logo.svg").is_file()      # …but still saved


class ErroringBackend(FakeBackend):
    """Simulates the agent SDK dying mid-turn — e.g. the 1 MB JSON-buffer overflow
    that killed a live worker. receive() raises as soon as the turn streams, with no
    ResultMessage — the exact shape of the reported failure."""

    def receive(self):
        async def gen():
            raise RuntimeError(
                "Failed to decode JSON: JSON message exceeded maximum buffer size")
            yield  # unreachable — makes this an async generator

        return gen()


def test_crashed_turn_self_heals_and_notifies(tmp_path):
    """Robustness regression (reported live): a turn that crashes mid-stream must NOT
    die silently — the session reconnects itself AND fires on_turn_error so a
    supervisor can follow up, instead of leaving the worker stalled."""
    backend = ErroringBackend([])
    seen: list[BaseException] = []

    async def on_err(_sess, e):
        seen.append(e)

    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=_settings(tmp_path), post=SinkPost(), busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend,
    )
    sess.on_turn_error = on_err

    async def drive():
        await sess.start()
        await sess.submit("go")
        await sess._queue.join()
        status_after = sess.status     # capture before stop() flips it to "stopped"
        await sess.stop()
        return status_after

    status_after = asyncio.run(drive())
    assert seen and "buffer size" in str(seen[0])   # the supervisor WAS told of the crash
    assert backend.started == 2                      # it reconnected (initial + reconnect)
    assert status_after == "idle"                    # recovered and reusable, not wedged
