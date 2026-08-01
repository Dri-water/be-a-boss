"""Backend-neutral agent runtime seam.

The core owns turns, tools, delivery, and supervision. Provider adapters own only
their native process/session protocol and translate it into the small event model
below. Neither Claude nor Codex is the core's vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Protocol

from claude_agent_sdk import (
    AssistantMessage as ClaudeAssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage as ClaudeResultMessage,
    SystemMessage as ClaudeSystemMessage,
    create_sdk_mcp_server,
    tool as claude_tool,
)

from .. import rendering

if TYPE_CHECKING:
    from .session import Turn

log = logging.getLogger("beaboss.core.agent_backend")

SENSITIVE_ENV = ("TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "WEB_TOKEN")
APP_SERVER_STREAM_LIMIT = 32 * 1024 * 1024


def scrubbed_env() -> dict[str, str]:
    """Return the process environment without the bot's own credentials."""
    return {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV}


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass(frozen=True)
class ToolNamespace:
    name: str
    description: str
    tools: tuple[AgentTool, ...]


@dataclass(frozen=True)
class AgentInit:
    session_id: str


@dataclass(frozen=True)
class AgentOutput:
    text: str


@dataclass
class AgentResult:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    result: str | None = None
    errors: list[str] | None = None
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None


AgentEvent = AgentInit | AgentOutput | AgentResult


class AgentBackend(Protocol):
    async def start(self) -> None: ...

    async def send(self, turn: "Turn") -> None: ...

    async def steer(self, turn: "Turn") -> bool: ...

    def receive(self) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...

    async def stop(self) -> None: ...


def claude_mcp_server(namespace: ToolNamespace):
    """Adapt neutral tool definitions to an in-process Claude SDK MCP server."""
    wrapped = []
    for spec in namespace.tools:
        async def invoke(args: dict[str, Any], handler=spec.handler):
            return await handler(args)

        invoke.__name__ = spec.name
        wrapped.append(claude_tool(
            spec.name, spec.description, spec.input_schema)(invoke))
    return create_sdk_mcp_server(namespace.name, tools=wrapped)


class ClaudeAgentBackend:
    """Claude Code SDK adapter."""

    def __init__(self, build_options: Callable[[], ClaudeAgentOptions]):
        self._build_options = build_options
        self._client: ClaudeSDKClient | None = None

    async def start(self) -> None:
        self._client = ClaudeSDKClient(self._build_options())
        await self._client.connect()

    async def send(self, turn: "Turn") -> None:
        assert self._client is not None
        if turn.images:
            content: list[dict[str, Any]] = [
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

    async def steer(self, turn: "Turn") -> bool:
        """Claude's SDK does not expose a turn-id-guarded steer operation.

        Returning False lets the provider-neutral session retain the message as the
        next FIFO turn.  Writing another query into the stream here is ambiguous: it
        may become a separate response while receive_response() is still draining the
        first one, which can detach output from the queued turn that owns it.
        """
        return False

    async def receive(self) -> AsyncIterator[AgentEvent]:
        assert self._client is not None
        async for message in self._client.receive_response():
            if isinstance(message, ClaudeSystemMessage) and message.subtype == "init":
                sid = str(message.data.get("session_id") or "")
                if sid:
                    yield AgentInit(sid)
            elif isinstance(message, ClaudeAssistantMessage):
                for piece in rendering.render_assistant(message):
                    yield AgentOutput(piece)
            elif isinstance(message, ClaudeResultMessage):
                yield AgentResult(
                    subtype=message.subtype,
                    duration_ms=message.duration_ms,
                    duration_api_ms=message.duration_api_ms,
                    is_error=message.is_error,
                    num_turns=message.num_turns,
                    session_id=message.session_id,
                    result=message.result,
                    errors=message.errors,
                    usage=message.usage,
                    total_cost_usd=message.total_cost_usd,
                )

    async def interrupt(self) -> None:
        if self._client:
            await self._client.interrupt()

    async def stop(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None


class CodexBackend:
    """Persistent Codex app-server adapter with dynamic tool and vision support."""

    def __init__(
        self,
        cwd: Path,
        system_prompt: str = "",
        resume_id: str | None = None,
        model: str | None = None,
        cli_path: str | None = None,
        worker_thread_id: str | None = None,
        tool_namespaces: tuple[ToolNamespace, ...] = (),
        permission_mode: str = "bypassPermissions",
    ):
        self._cwd = Path(cwd)
        self._system_prompt = system_prompt
        self._thread_id = resume_id
        self._model = model
        self._cli_path = cli_path
        self._worker_thread_id = worker_thread_id
        self._tool_namespaces = tool_namespaces
        self._permission_mode = permission_mode
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._tool_tasks: set[asyncio.Task] = set()
        self._responses: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._active_turn_id: str | None = None
        self._emit_init = False
        self._final_text = ""
        self._turn_error = ""
        self._stderr: list[str] = []
        self.rate_limits: dict[str, Any] | None = None

    @property
    def session_id(self) -> str | None:
        return self._thread_id

    def _approval_policy(self) -> str:
        if self._permission_mode in ("bypassPermissions", "dontAsk", "auto"):
            return "never"
        if self._permission_mode == "acceptEdits":
            return "on-request"
        return "untrusted"

    def _dynamic_tools(self) -> list[dict[str, Any]]:
        return [{
            "type": "namespace",
            "name": namespace.name,
            "description": namespace.description,
            "tools": [{
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.input_schema,
            } for spec in namespace.tools],
        } for namespace in self._tool_namespaces if namespace.tools]

    def _thread_options(self, *, include_dynamic_tools: bool) -> dict[str, Any]:
        """Current thread configuration for the installed app-server protocol.

        Codex 0.146 persists dynamic tools in rollout metadata and restores them on
        resume, but its generated ThreadResumeParams schema does not accept a
        dynamicTools override. Only send definitions on native thread creation.
        """
        options: dict[str, Any] = {
            "cwd": str(self._cwd),
            "approvalPolicy": self._approval_policy(),
            "sandbox": "danger-full-access",
            "developerInstructions": self._system_prompt or None,
        }
        if include_dynamic_tools:
            options["dynamicTools"] = self._dynamic_tools()
        if self._model:
            options["model"] = self._model
        return options

    async def start(self) -> None:
        await self._terminate_process()
        codex = self._cli_path or shutil.which("codex") or "codex"
        env = scrubbed_env()
        if self._worker_thread_id:
            env["BEABOSS_WORKER_THREAD_ID"] = self._worker_thread_id
        self._stderr = []
        self._events = asyncio.Queue()
        self._responses = {}
        self._proc = await asyncio.create_subprocess_exec(
            codex, "app-server",
            cwd=str(self._cwd), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=APP_SERVER_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._request("initialize", {
            "clientInfo": {
                "name": "be_a_boss", "title": "be-a-boss", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self._notify("initialized", {})

        common = self._thread_options(include_dynamic_tools=not bool(self._thread_id))
        if self._thread_id:
            response = await self._request(
                "thread/resume", {"threadId": self._thread_id, **common})
        else:
            common["serviceName"] = "be_a_boss"
            response = await self._request("thread/start", common)
        thread = response.get("thread") or {}
        sid = str(thread.get("id") or thread.get("sessionId") or "")
        if not sid:
            raise RuntimeError("Codex app-server did not return a thread id")
        self._thread_id = sid
        self._emit_init = True

    async def send(self, turn: "Turn") -> None:
        if not self._thread_id:
            raise RuntimeError("Codex thread is not initialized")
        self._final_text = ""
        self._turn_error = ""
        response = await self._request("turn/start", {
            "threadId": self._thread_id,
            "input": self._turn_inputs(turn),
            "cwd": str(self._cwd),
            "approvalPolicy": self._approval_policy(),
        })
        turn_data = response.get("turn") or {}
        self._active_turn_id = str(turn_data.get("id") or "") or None

    def _turn_inputs(self, turn: "Turn") -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = [
            {"type": "text", "text": turn.text or "(no caption)"}]
        for image in turn.images:
            path = image.get("path")
            if path and Path(path).is_file():
                inputs.append({"type": "localImage", "path": str(path)})
            elif image.get("data") and image.get("media_type"):
                inputs.append({
                    "type": "image",
                    "url": (f"data:{image['media_type']};base64,{image['data']}"),
                })
        return inputs

    async def steer(self, turn: "Turn") -> bool:
        """Append input to the active Codex turn, guarded against completion races.

        False means there is no steerable turn anymore and the caller must preserve
        the message as a normal queued turn. Other app-server failures are also a
        safe FIFO fallback: user input is never discarded merely because steering
        lost a race with turn completion.
        """
        if not self._thread_id or not self._active_turn_id or not self._proc:
            return False
        expected = self._active_turn_id
        try:
            response = await self._request("turn/steer", {
                "threadId": self._thread_id,
                "input": self._turn_inputs(turn),
                "expectedTurnId": expected,
            })
        except Exception as exc:  # completion/non-steerable races become FIFO
            log.info("Codex steer fell back to queue thread=%s turn=%s: %s",
                     self._thread_id, expected, exc)
            return False
        return str(response.get("turnId") or "") == expected

    async def receive(self) -> AsyncIterator[AgentEvent]:
        if self._emit_init and self._thread_id:
            self._emit_init = False
            yield AgentInit(self._thread_id)
        started = time.monotonic()
        while True:
            event = await self._events.get()
            method = event.get("method")
            params = event.get("params") or {}
            if method == "backend/eof":
                detail = params.get("error") or "Codex app-server exited unexpectedly"
                yield AgentResult(
                    subtype="error", duration_ms=0, duration_api_ms=0,
                    is_error=True, num_turns=1, session_id=self._thread_id or "",
                    result=str(detail))
                return
            if method == "item/started":
                item = params.get("item") or {}
                itype = item.get("type")
                if itype == "commandExecution":
                    command = item.get("command") or ""
                    yield AgentOutput(rendering.tool_line(
                        "shell", {"command": command}))
                elif itype == "fileChange":
                    yield AgentOutput(rendering.tool_line("file change", {}))
                elif itype == "dynamicToolCall":
                    prefix = f"{item.get('namespace')}." if item.get("namespace") else ""
                    yield AgentOutput(rendering.tool_line(
                        prefix + str(item.get("tool") or "tool"),
                        item.get("arguments") or {}))
                elif itype == "imageView":
                    yield AgentOutput(rendering.tool_line(
                        "view image", {"path": item.get("path")}))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    text = str(item.get("text") or "").strip()
                    if text:
                        if item.get("phase") == "final_answer":
                            self._final_text = text
                        yield AgentOutput(text)
            elif method == "error":
                error = params.get("error") or params
                self._turn_error = _codex_error_detail(error)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "failed")
                error = turn.get("error") or {}
                detail = _codex_error_detail(error) or self._turn_error
                is_error = status != "completed"
                self._active_turn_id = None
                yield AgentResult(
                    subtype="success" if not is_error else status,
                    duration_ms=int(turn.get("durationMs") or (
                        (time.monotonic() - started) * 1000)),
                    duration_api_ms=0,
                    is_error=is_error,
                    num_turns=1,
                    session_id=self._thread_id or "",
                    result=(detail or self._final_text or None),
                    errors=[str(detail)] if detail else None,
                )
                return

    async def interrupt(self) -> None:
        if self._thread_id and self._active_turn_id and self._proc:
            await self._request("turn/interrupt", {
                "threadId": self._thread_id, "turnId": self._active_turn_id})

    async def stop(self) -> None:
        await self._terminate_process()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.returncode is not None:
            raise RuntimeError("Codex app-server is not running")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        try:
            await self._write({"method": method, "id": request_id, "params": params})
            response = await asyncio.wait_for(future, timeout=60)
        finally:
            self._responses.pop(request_id, None)
        if response.get("error"):
            error = response["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        return response.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            proc.stdin.write(data)
            await proc.stdin.drain()

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        reader_error: str | None = None
        try:
            async for raw in proc.stdout:
                message = _parse_line(raw)
                if message is None:
                    continue
                if message.get("method") == "item/tool/call" and "id" in message:
                    task = asyncio.create_task(self._handle_tool_request(message))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                    continue
                request_id = message.get("id")
                if request_id in self._responses and not message.get("method"):
                    future = self._responses[request_id]
                    if not future.done():
                        future.set_result(message)
                    continue
                method = message.get("method")
                if method and "id" in message:
                    # Approval, elicitation, or a newly introduced server request must
                    # never sit unanswered until the session watchdog fires. This
                    # integration is headless and cannot render an interactive prompt;
                    # fail closed so Codex can finish the item/turn with a real error.
                    task = asyncio.create_task(
                        self._reject_unsupported_request(message))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                    continue
                if method == "account/rateLimits/updated":
                    self.rate_limits = (message.get("params") or {}).get("rateLimits")
                    continue
                if method in {
                    "item/agentMessage/delta", "item/reasoning/summaryTextDelta",
                    "item/reasoning/textDelta", "thread/tokenUsage/updated",
                    "thread/status/changed", "mcpServer/startupStatus/updated",
                    "remoteControl/status/changed", "thread/started",
                }:
                    continue
                if method:
                    await self._events.put(message)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Codex app-server reader failed")
            reader_error = f"Codex app-server stream failed: {exc}"

        if reader_error is None:
            if proc.returncode is None:
                await proc.wait()
            reader_error = f"Codex app-server exited {proc.returncode}"
        tail = "".join(self._stderr)[-1200:].strip()
        if tail:
            reader_error += f": {tail}"
        for future in list(self._responses.values()):
            if not future.done():
                future.set_exception(RuntimeError(reader_error))
        await self._events.put({
            "method": "backend/eof", "params": {"error": reader_error}})

    async def _handle_tool_request(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        namespace = str(params.get("namespace") or "")
        name = str(params.get("tool") or "")
        spec = next((tool for group in self._tool_namespaces
                     if group.name == namespace for tool in group.tools
                     if tool.name == name), None)
        success = True
        content_items: list[dict[str, Any]] = []
        try:
            if spec is None:
                raise RuntimeError(f"unknown tool {namespace}.{name}")
            result = await spec.handler(params.get("arguments") or {})
            success = not bool(result.get("is_error"))
            for item in result.get("content") or []:
                if item.get("type") == "text":
                    content_items.append({
                        "type": "inputText", "text": str(item.get("text") or "")})
            if not content_items:
                content_items.append({"type": "inputText", "text": "done"})
        except Exception as exc:  # noqa: BLE001
            success = False
            content_items = [{"type": "inputText", "text": f"tool failed: {exc}"}]
        try:
            await self._write({
                "id": message["id"],
                "result": {"contentItems": content_items, "success": success},
            })
        except Exception:  # noqa: BLE001
            log.exception("could not answer Codex dynamic tool call %s.%s", namespace, name)

    async def _reject_unsupported_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "unknown")
        log.warning("rejecting unsupported Codex app-server request: %s", method)
        try:
            await self._write({
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": (
                        f"be-a-boss cannot answer interactive request {method} in "
                        "a headless session"),
                },
            })
        except Exception:  # noqa: BLE001
            log.exception("could not reject Codex app-server request %s", method)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            async for line in proc.stderr:
                self._stderr.append(line.decode("utf-8", "replace"))
                if len(self._stderr) > 200:
                    self._stderr = self._stderr[-200:]
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    async def _terminate_process(self) -> None:
        self._active_turn_id = None
        for task in list(self._tool_tasks):
            task.cancel()
        self._tool_tasks.clear()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


def _parse_line(raw: bytes) -> dict[str, Any] | None:
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _codex_error_detail(error: Any) -> str:
    """Keep Codex's structured error kind as well as its human message.

    The retry layer historically saw only ``error.message``. App-server exposes
    stable kinds such as ``UsageLimitExceeded`` and ``HttpConnectionFailed`` under
    ``codexErrorInfo``; retaining them prevents a transient provider failure from
    being silently misclassified as terminal when the wording changes.
    """
    if not error:
        return ""
    if not isinstance(error, dict):
        return str(error)
    parts: list[str] = []
    message = error.get("message")
    if message:
        parts.append(str(message))
    info = error.get("codexErrorInfo")
    if info:
        if isinstance(info, str):
            parts.append(info)
        elif isinstance(info, dict):
            kind = info.get("type") or info.get("kind")
            if kind:
                parts.append(str(kind))
            else:
                parts.extend(str(key) for key, value in info.items() if value is not None)
    details = error.get("additionalDetails")
    if details:
        parts.append(str(details))
    return " · ".join(dict.fromkeys(part for part in parts if part))
