from types import SimpleNamespace

from beaboss.core import recovery


def test_claude_transcript_tail_keeps_only_visible_bounded_text(monkeypatch):
    messages = [
        SimpleNamespace(type="user", message={"content": "hello"}),
        SimpleNamespace(type="assistant", message={"content": [
            {"type": "thinking", "thinking": "secret"},
            {"type": "text", "text": "visible answer"},
            {"type": "tool_use", "name": "Bash"},
        ]}),
    ]
    monkeypatch.setattr(
        "claude_agent_sdk.get_session_messages", lambda _sid: messages)
    recovery.claude_transcript_tail.cache_clear()
    text = recovery.claude_transcript_tail(
        "7970a0f3-14ff-414a-ae7c-7f3644cc373f", max_chars=100)
    assert "USER:\nhello" in text
    assert "ASSISTANT:\nvisible answer" in text
    assert "secret" not in text and "Bash" not in text


def test_legacy_backend_inference_prefers_transcript_then_uuid_version(monkeypatch):
    monkeypatch.setattr(recovery, "claude_transcript_tail", lambda *_args: "history")
    assert recovery.infer_legacy_backend(
        "019fbe10-fe0f-7771-ab55-64f2eee085e0") == "claude"

    monkeypatch.setattr(recovery, "claude_transcript_tail", lambda *_args: "")
    assert recovery.infer_legacy_backend(
        "019fbe10-fe0f-7771-ab55-64f2eee085e0") == "codex"
    assert recovery.infer_legacy_backend(
        "7970a0f3-14ff-414a-ae7c-7f3644cc373f") == "claude"
