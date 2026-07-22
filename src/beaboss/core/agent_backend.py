"""The seam between a CoreSession and the agent that actually runs turns.

CoreSession owns the queue, media, tool wiring, and rendering; it does NOT own
the agent runtime. Everything a session does to a live agent — connect in a cwd,
send a turn, stream the result events, interrupt, disconnect — goes through this
tiny interface. The default `ClaudeAgentBackend` is the Claude Code SDK; a test
(or a future alternative runtime) can supply its own without touching the
session logic.

The streamed events are the Claude Agent SDK message objects (AssistantMessage /
ResultMessage / SystemMessage). Those are plain dataclasses that CoreSession and
`rendering` already speak, so they double as the neutral vocabulary between a
backend and the session — a backend's only job is to produce that stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Protocol

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

if TYPE_CHECKING:
    from .session import Turn


class AgentBackend(Protocol):
    """What a CoreSession needs from an agent runtime — nothing more.

    `start` connects a fresh agent (honouring cwd / resume built into its
    config); it may be called again to reconnect. `send` submits one turn;
    `receive` yields that turn's events until the turn ends; `interrupt` cancels
    an in-flight turn; `stop` disconnects.
    """

    async def start(self) -> None: ...

    async def send(self, turn: "Turn") -> None: ...

    def receive(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def stop(self) -> None: ...


class ClaudeAgentBackend:
    """Default backend: a live Claude Code SDK client.

    Options are rebuilt on every `start` via the injected callback so a reconnect
    picks up the latest resume id (the session updates it from streamed events).
    """

    def __init__(self, build_options: Callable[[], ClaudeAgentOptions]):
        self._build_options = build_options
        self._client: ClaudeSDKClient | None = None

    async def start(self) -> None:
        self._client = ClaudeSDKClient(self._build_options())
        await self._client.connect()

    async def send(self, turn: "Turn") -> None:
        assert self._client is not None
        if turn.images:
            content: list[dict] = [
                {"type": "image", "source": {
                    "type": "base64", "media_type": img["media_type"],
                    "data": img["data"]}}
                for img in turn.images
            ]
            content.append({"type": "text", "text": turn.text or "(no caption)"})
            message = {"type": "user", "message": {"role": "user", "content": content}}

            async def stream():
                yield message

            await self._client.query(stream())
        else:
            await self._client.query(turn.text)

    def receive(self) -> AsyncIterator[Any]:
        assert self._client is not None
        return self._client.receive_response()

    async def interrupt(self) -> None:
        if self._client:
            await self._client.interrupt()

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()
