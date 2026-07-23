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
    subprocess that streams newline-delimited JSON on stdout. The first turn starts
    a new thread and carries the worker's system prompt (codex exec has no separate
    system-prompt channel — it goes into the initial instructions); the thread_id it
    reports is reused via `codex exec resume` so later turns keep the same
    conversation (and, given a persisted id, across a process restart). The backend
    translates Codex's event lines into the SDK message objects the session speaks.
    """

    def __init__(self, cwd: Path, system_prompt: str = "", resume_id: str | None = None,
                 model: str | None = None, cli_path: str | None = None):
        self._cwd = cwd
        self._system_prompt = system_prompt
        self._thread_id = resume_id  # resume the same Codex thread across restarts
        self._model = model          # honors AGENT_MODEL / CODEX_MODEL
        self._cli_path = cli_path    # honors AGENT_CLI_PATH / CODEX_CLI_PATH
        self._proc: asyncio.subprocess.Process | None = None
        self._final_text = ""  # last agent_message of the in-flight turn
        self._stderr: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        # No connection to open; a turn spawns its own process. Keep any captured
        # thread_id so a reconnect still resumes the same Codex conversation.
        pass

    async def send(self, turn: "Turn") -> None:
        await self._kill()  # never leave a previous turn's process running
        self._final_text = ""
        self._stderr = []
        prompt = turn.text or "(no caption)"
        # codex exec has no system-prompt flag — fold the worker persona into the
        # first turn; later turns resume the thread that already carries it.
        if self._thread_id is None and self._system_prompt:
            prompt = f"{self._system_prompt}\n\n---\n\n{prompt}"

        codex = self._cli_path or shutil.which("codex") or "codex"
        flags = [
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(self._cwd),
        ]
        if self._model:
            flags += ["-m", self._model]
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
        # Drain stderr concurrently: an unread stderr pipe filling its buffer would
        # block codex mid-write and hang the turn forever.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            async for line in proc.stderr:
                self._stderr.append(line.decode("utf-8", "replace"))
                if len(self._stderr) > 200:
                    self._stderr = self._stderr[-200:]
        except Exception:  # noqa: BLE001
            pass

    async def receive(self) -> AsyncIterator[Any]:
        assert self._proc is not None and self._proc.stdout is not None
        completed = False
        async for raw in self._proc.stdout:
            event = _parse_line(raw)
            if event is None:
                continue
            message = self._translate(event)
            if message is not None:
                yield message
            if event.get("type") == "turn.completed":
                completed = True
                break
        if not completed:
            # stdout ended with no turn.completed — codex died/errored. Surface it
            # as an error result so the turn isn't a silent no-op and on_turn_done
            # fires (the orchestrator learns the worker failed).
            code = await self._proc.wait()
            if self._stderr_task is not None:
                try:
                    await asyncio.wait_for(self._stderr_task, timeout=2)
                except Exception:  # noqa: BLE001
                    pass
            tail = "".join(self._stderr)[-800:].strip()
            yield ResultMessage(
                subtype="error", duration_ms=0, duration_api_ms=0, is_error=True,
                num_turns=1, session_id=self._thread_id or "",
                result=(f"codex exited {code} without completing the turn"
                        + (f": {tail}" if tail else "")))

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
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
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
