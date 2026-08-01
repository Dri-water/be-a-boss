"""End-to-end: real CoreSession/Codex app-server integration checks.

This genuinely spawns `codex app-server` (authenticated via Sign in with
ChatGPT), so it is skipped wherever the binary is absent to keep `pytest` green.
Where Codex IS installed it proves the whole seam: env selects the backend, the
session drives it, and Codex's reply comes back through the same event path the
app uses.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from beaboss.config import Settings
from beaboss.core.agent_backend import AgentResult, AgentTool, CodexBackend, ToolNamespace
from beaboss.core.ports import Outbound, Speaker
from beaboss.core.session import CoreSession, Turn

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI not installed"
)


class SinkPost:
    def __init__(self):
        self.out: list[Outbound] = []

    async def __call__(self, out: Outbound):
        self.out.append(out)


async def _noop_busy(thread_id: str):
    pass


def _settings(tmp: Path) -> Settings:
    return Settings(
        bot_token="t", allowed_user_ids={1}, chat_id=None,
        permission_mode="bypassPermissions", projects_root=tmp, cli_path=None,
        model=None, max_turns=None, state_dir=tmp / "state",
        bot_name="X", session_system_append="", agent_backend="codex",
    )


def test_pong_roundtrips_through_codex(tmp_path):
    settings = _settings(tmp_path)
    # Same selection the engine's single worker-construction point makes.
    backend = CodexBackend(tmp_path) if settings.agent_backend == "codex" else None
    assert backend is not None

    post = SinkPost()
    sess = CoreSession(
        thread_id="t1", cwd=tmp_path,
        speaker=Speaker(role="worker", name="Nova", emoji="⚙️"),
        settings=settings, post=post, busy=_noop_busy,
        on_session_id=lambda _s: None, backend=backend,
    )

    async def drive():
        await sess.start()
        await sess.submit("Reply with exactly the text PONG and nothing else.")
        await sess._queue.join()
        await sess.stop()

    asyncio.run(asyncio.wait_for(drive(), timeout=120))

    replies = [o.text.strip() for o in post.out if o.text]
    assert any(r == "PONG" for r in replies), f"no PONG in replies: {replies}"
    print(f"\nE2E PROOF: PONG round-tripped through the seam. replies={replies}")


def test_dynamic_tool_survives_native_resume(tmp_path):
    calls: list[str] = []

    async def echo(args):
        value = str(args.get("value", ""))
        calls.append(value)
        return {"content": [{"type": "text", "text": f"echoed {value}"}]}

    tools = (ToolNamespace(
        "fleet", "test fleet tools", (AgentTool(
            "echo", "Echo a value", {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"]}, echo),)),)

    async def one_turn(backend, prompt):
        await backend.start()
        await backend.send(Turn(prompt))
        events = [event async for event in backend.receive()]
        sid = backend.session_id
        await backend.stop()
        assert isinstance(events[-1], AgentResult) and not events[-1].is_error
        return sid

    async def drive():
        first = CodexBackend(tmp_path, tool_namespaces=tools)
        sid = await one_turn(
            first, "Call fleet.echo with value 'first', then reply exactly FIRST_OK.")
        assert sid
        second = CodexBackend(tmp_path, resume_id=sid, tool_namespaces=tools)
        resumed = await one_turn(
            second, "Call fleet.echo with value 'second', then reply exactly SECOND_OK.")
        assert resumed == sid

    asyncio.run(asyncio.wait_for(drive(), timeout=180))
    assert calls == ["first", "second"]


def test_native_steer_reaches_the_inflight_turn(tmp_path):
    """Hold Codex inside a client tool so turn/steer can be exercised without a
    timing guess, then prove the steered instruction shaped that same turn's reply."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gate(_args):
        entered.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "gate released"}]}

    tools = (ToolNamespace(
        "probe", "steering integration probe", (AgentTool(
            "gate", "Wait at a deterministic integration-test gate",
            {"type": "object", "properties": {}}, gate),)),)

    async def drive():
        backend = CodexBackend(tmp_path, tool_namespaces=tools)
        await backend.start()
        try:
            await backend.send(Turn(
                "Call probe.gate now. After it returns, reply exactly ORIGINAL_OK."))
            receive_task = asyncio.create_task(
                _collect_backend_events(backend))
            await asyncio.wait_for(entered.wait(), timeout=120)
            accepted = await backend.steer(Turn(
                "Replace the requested final reply: answer exactly STEER_OK."))
            assert accepted is True
            release.set()
            events = await asyncio.wait_for(receive_task, timeout=120)
            result = events[-1]
            assert isinstance(result, AgentResult) and not result.is_error
            assert (result.result or "").strip() == "STEER_OK"
        finally:
            release.set()
            await backend.stop()

    asyncio.run(asyncio.wait_for(drive(), timeout=180))


async def _collect_backend_events(backend: CodexBackend) -> list:
    return [event async for event in backend.receive()]
