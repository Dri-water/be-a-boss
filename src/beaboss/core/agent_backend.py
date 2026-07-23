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

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

if TYPE_CHECKING:
    from .session import Turn

log = logging.getLogger("beaboss.core.agent_backend")

# The bot's own secrets. An agent CLI runs with bypassPermissions, so it must not
# inherit these — they'd be a `cat`/`curl` away for a prompt-injected worker. The
# bot process keeps them; delivery's `gh` push runs in the bot process, not the agent.
SENSITIVE_ENV = ("TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "WEB_TOKEN")


def scrubbed_env() -> dict:
    """os.environ with the bot's secrets removed — for any agent subprocess."""
    return {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV}


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


class CodexBackend:
    """Alternative backend: OpenAI's Codex CLI (`codex exec`).

    Codex has no persistent connection — each turn is a fresh `codex exec`
    subprocess that streams newline-delimited JSON on stdout. The first turn
    starts a new thread; the thread_id it reports is reused via `codex exec
    resume` so later turns keep the same conversation. The backend's only job is
    to translate Codex's event lines into the SDK message objects the session
    already speaks, so nothing downstream knows which runtime produced them.
    """

    def __init__(self, cwd: Path):
        self._cwd = cwd
        self._thread_id: str | None = None  # captured from thread.started, for resume
        self._proc: asyncio.subprocess.Process | None = None
        self._final_text = ""  # last agent_message of the in-flight turn

    async def start(self) -> None:
        # No connection to open; a turn spawns its own process. Keep any captured
        # thread_id so a reconnect still resumes the same Codex conversation.
        pass

    async def send(self, turn: "Turn") -> None:
        await self._kill()  # never leave a previous turn's process running
        self._final_text = ""
        prompt = turn.text or "(no caption)"

        codex = shutil.which("codex") or "codex"
        flags = [
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(self._cwd),
        ]
        if self._thread_id is None:
            argv = [codex, "exec", *flags, prompt]
        else:
            argv = [codex, "exec", "resume", self._thread_id, *flags, prompt]

        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._cwd),
            env=scrubbed_env(),  # don't hand the bot's secrets to the agent
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def receive(self) -> AsyncIterator[Any]:
        assert self._proc is not None and self._proc.stdout is not None
        async for raw in self._proc.stdout:
            event = _parse_line(raw)
            if event is None:
                continue
            message = self._translate(event)
            if message is not None:
                yield message
            if event.get("type") == "turn.completed":
                break

    def _translate(self, event: dict) -> Any | None:
        """Map one Codex event onto an SDK message, or None to ignore it."""
        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if tid:
                self._thread_id = tid  # remember it so the next turn can resume
            return SystemMessage(subtype="init", data={"session_id": tid})
        if etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                self._final_text = text
                return AssistantMessage(content=[TextBlock(text=text)], model="codex")
            return None
        if etype == "turn.completed":
            return ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id=self._thread_id or "",
                result=self._final_text,
            )
        return None

    async def interrupt(self) -> None:
        await self._kill()

    async def stop(self) -> None:
        await self._kill()

    async def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("codex terminate failed: %s", e)


def _parse_line(raw: bytes) -> dict | None:
    """One JSONL line -> dict, tolerating blanks and non-JSON noise."""
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None
