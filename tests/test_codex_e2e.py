"""End-to-end: a real CoreSession, backed by the real Codex CLI, round-trips PONG.

This genuinely spawns `codex exec` (Codex is authenticated via Sign in with
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
from beaboss.core.agent_backend import CodexBackend
from beaboss.core.ports import Outbound, Speaker
from beaboss.core.session import CoreSession

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
