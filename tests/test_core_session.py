import asyncio
from pathlib import Path

import pytest

from tasm.config import Settings
from tasm.core.ports import MediaIn, Outbound, Speaker
from tasm.core.session import CoreSession, Turn


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
        speaker=Speaker(role="coder", name="Nova", emoji="⚙️"),
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
    assert "hello" in turn.text and ".tg-inbox" in turn.text
    assert (tmp_path / ".tg-inbox" / "a.png").is_file()


def test_submit_media_sanitizes_filename(tmp_path):
    sess = _session(tmp_path)
    asyncio.run(sess.submit_media(
        "", [MediaIn("file", "../../evil.txt", "text/plain", b"x")]))
    assert (tmp_path / ".tg-inbox" / "evil.txt").is_file()
    assert not (tmp_path.parent / "evil.txt").exists()


@pytest.mark.parametrize("path,fragment", [
    ("../escape.png", "outside"),
    ("nope.png", "no such file"),
    ("  ", "required"),
])
def test_tool_send_guards(tmp_path, path, fragment):
    r = asyncio.run(_session(tmp_path)._tool_send({"path": path}, "photo"))
    assert r["is_error"] and fragment in r["content"][0]["text"]


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
