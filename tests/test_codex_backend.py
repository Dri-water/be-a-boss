"""Unit tests for the Codex translation — no `codex` binary, no subprocess.

We feed the backend a fake process whose stdout replays the exact JSONL lines
`codex exec --json` emits, and assert the seam produces the SDK message objects
CoreSession already speaks.
"""

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from beaboss.core.agent_backend import CodexBackend


class _FakeStdout:
    """An async-iterable of pre-baked byte lines, like a real process stdout."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
        return gen()


class _FakeProc:
    def __init__(self, lines: list[bytes]):
        self.stdout = _FakeStdout(lines)
        self.returncode = 0


# The example stream from `codex exec --json`, verbatim from the CLI docs.
EXAMPLE_LINES = [
    b'{"type":"thread.started","thread_id":"019-abc"}\n',
    b'{"type":"turn.started"}\n',
    b'\n',  # blank line — must be tolerated
    b'not json at all\n',  # noise — must be ignored
    b'{"type":"item.completed","item":{"type":"reasoning","text":"thinking"}}\n',
    b'{"type":"item.completed","item":{"type":"agent_message","text":"PONG"}}\n',
    b'{"type":"turn.completed","usage":{"input_tokens":1}}\n',
]


def _drain(backend: CodexBackend) -> list:
    async def go():
        return [m async for m in backend.receive()]
    return asyncio.run(go())


def test_translation_yields_sdk_objects():
    backend = CodexBackend(cwd=None)
    backend._proc = _FakeProc(EXAMPLE_LINES)

    messages = _drain(backend)

    # init -> assistant -> result (reasoning item and noise dropped)
    assert len(messages) == 3

    init, assistant, result = messages

    assert isinstance(init, SystemMessage)
    assert init.subtype == "init"
    assert init.data["session_id"] == "019-abc"

    assert isinstance(assistant, AssistantMessage)
    assert len(assistant.content) == 1
    assert isinstance(assistant.content[0], TextBlock)
    assert assistant.content[0].text == "PONG"

    assert isinstance(result, ResultMessage)
    assert result.subtype == "success"
    assert result.is_error is False
    assert result.session_id == "019-abc"
    assert result.result == "PONG"


def test_thread_id_captured_for_resume():
    backend = CodexBackend(cwd=None)
    backend._proc = _FakeProc(EXAMPLE_LINES)
    _drain(backend)
    # The thread_id is retained so the next turn resumes the same conversation.
    assert backend._thread_id == "019-abc"


def test_receive_stops_at_turn_completed():
    """Anything after turn.completed must not be yielded — the turn is over."""
    backend = CodexBackend(cwd=None)
    backend._proc = _FakeProc([
        b'{"type":"thread.started","thread_id":"t"}\n',
        b'{"type":"turn.completed"}\n',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"leaked"}}\n',
    ])
    messages = _drain(backend)
    assert [type(m).__name__ for m in messages] == ["SystemMessage", "ResultMessage"]
