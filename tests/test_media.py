import asyncio
from pathlib import Path

from tasm.claude_session import ClaudeSession, MediaItem, Turn
from tasm.config import Settings


def _settings(tmp: Path) -> Settings:
    return Settings(
        bot_token="t", allowed_user_ids={1}, chat_id=None,
        permission_mode="bypassPermissions", projects_root=tmp, cli_path=None,
        model=None, max_turns=None, state_dir=tmp / "state",
        bot_name="X", session_system_append=None,
    )


def _session(tmp: Path) -> ClaudeSession:
    return ClaudeSession(1, tmp, "t", _settings(tmp), emitter=None, on_session_id=lambda _s: None)


def test_build_options_has_mcp_and_env_prompt(tmp_path):
    opts = _session(tmp_path)._build_options()
    assert "telegram" in opts.mcp_servers
    assert opts.system_prompt["preset"] == "claude_code"
    assert "send_photo" in opts.system_prompt["append"]


def test_submit_media_saves_inbox_and_builds_turn(tmp_path):
    sess = _session(tmp_path)
    items = [
        MediaItem("image", "a.png", "image/png", b"\x89PNG\r\n"),
        MediaItem("file", "n.txt", "text/plain", b"hi"),
    ]
    asyncio.run(sess.submit_media("hello", items))
    turn = sess._queue.get_nowait()
    assert isinstance(turn, Turn)
    assert len(turn.images) == 1 and turn.images[0]["media_type"] == "image/png"
    assert "hello" in turn.text and ".tg-inbox" in turn.text
    assert (tmp_path / ".tg-inbox" / "a.png").is_file()
    assert (tmp_path / ".tg-inbox" / "n.txt").read_bytes() == b"hi"


def test_submit_media_sanitizes_filename(tmp_path):
    sess = _session(tmp_path)
    asyncio.run(sess.submit_media("", [MediaItem("file", "../../evil.txt", "text/plain", b"x")]))
    assert (tmp_path / ".tg-inbox" / "evil.txt").is_file()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_tool_send_rejects_escape(tmp_path):
    r = asyncio.run(_session(tmp_path)._tool_send({"path": "../escape.png"}, "photo"))
    assert r["is_error"] and "outside" in r["content"][0]["text"]


def test_tool_send_missing_file(tmp_path):
    r = asyncio.run(_session(tmp_path)._tool_send({"path": "nope.png"}, "photo"))
    assert r["is_error"] and "no such file" in r["content"][0]["text"]


def test_tool_send_requires_path(tmp_path):
    r = asyncio.run(_session(tmp_path)._tool_send({"path": "  "}, "photo"))
    assert r["is_error"] and "required" in r["content"][0]["text"]


def test_submit_plain_text_turn(tmp_path):
    sess = _session(tmp_path)
    asyncio.run(sess.submit("hi there"))
    turn = sess._queue.get_nowait()
    assert turn.text == "hi there" and turn.images == []
