"""One live Claude Code session, bound to a Telegram forum topic.

Each session owns a single long-lived ClaudeSDKClient (a real, resumable Claude
Code session with a working directory) plus a worker task that drains an inbound
queue. User turns are serialized per topic — which is correct, a single Claude
session is inherently one-turn-at-a-time.

Media:
- Inbound (user -> session): images arrive as vision blocks AND are saved to
  ./.tg-inbox/; other files are saved there and referenced in the turn text.
- Outbound (session -> user): the session gets in-process MCP tools
  (send_photo/send_video/send_file/send_message) wired to this topic.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    tool,
)

from . import rendering

log = logging.getLogger("tasm.session")

SETTING_SOURCES = ["project", "local"]
INBOX_DIRNAME = ".tg-inbox"
MAX_SEND_BYTES = 50 * 1024 * 1024  # Telegram bot upload ceiling

# Appended (not substituted) onto Claude Code's own system prompt so the session
# understands its unusual runtime + its Telegram powers, and doesn't confabulate
# around environment quirks. Override via SESSION_SYSTEM_APPEND (empty = no note).
DEFAULT_SESSION_APPEND = (
    "Runtime context: you are a headless Claude Code session driven over a Telegram "
    "chat, not an interactive terminal. You typically run inside a Linux container, "
    "and your working directory may be a bind-mounted host folder (on Windows/macOS "
    "this crosses a VM boundary). Implications:\n"
    "- File reads/writes are reliable, but filesystem WATCHING (inotify) may not "
    "fire. Prefer one-shot builds/tests over --watch/hot-reload modes, and never "
    "assume a watcher picked up a change — re-run explicitly.\n"
    "- If a command fails with an unusual filesystem, permission, path-case, or "
    "symlink error, treat it as an environment quirk and REPORT it plainly; do not "
    "invent a code-level cause or silently work around it.\n"
    "- You generally have root and may install what you need (apt-get/npm/pip). "
    "Such installs are ephemeral across container restarts — if a tool should "
    "persist, add it to the project's Dockerfile instead.\n"
    "- Only paths under your workspace are shared with the host; nothing outside it "
    "is visible. Keep your work within the workspace.\n"
    "- Your normal text replies are delivered to the user in this topic. To send "
    "MEDIA, use the Telegram tools: mcp__telegram__send_photo (renders an image "
    "inline — use for screenshots, charts, diagrams you generate), "
    "mcp__telegram__send_video, mcp__telegram__send_file (any document), and "
    "mcp__telegram__send_message (extra plain text). Paths must be inside your "
    "workspace.\n"
    f"- Files the user sends you are saved under ./{INBOX_DIRNAME}/ and referenced "
    "in their message; images are also attached so you can see them directly."
)


class Emitter(Protocol):
    async def send(self, thread_id: int, text: str) -> None: ...
    async def typing(self, thread_id: int) -> None: ...
    async def send_photo(self, thread_id: int, path: Path, caption: str | None) -> None: ...
    async def send_video(self, thread_id: int, path: Path, caption: str | None) -> None: ...
    async def send_document(self, thread_id: int, path: Path, caption: str | None) -> None: ...


@dataclass
class MediaItem:
    """A file received from Telegram, ready to hand to a session."""
    kind: str  # "image" | "file"
    filename: str
    mime: str | None
    data: bytes


@dataclass
class Turn:
    """One queued user turn: text plus optional inline images (base64)."""
    text: str
    images: list[dict] = field(default_factory=list)  # {media_type, data}


def _safe_name(name: str) -> str:
    return Path(name).name or "file"


class ClaudeSession:
    def __init__(
        self,
        thread_id: int,
        cwd: Path,
        name: str,
        settings,
        emitter: Emitter,
        on_session_id: Callable[[str], None],
        session_id: str | None = None,
    ):
        self.thread_id = thread_id
        self.cwd = cwd
        self.name = name
        self.settings = settings
        self.emitter = emitter
        self._on_session_id = on_session_id
        self.session_id = session_id

        self._client: ClaudeSDKClient | None = None
        self._queue: asyncio.Queue[Turn] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self.status = "new"  # new | idle | busy | error | stopped
        self.turns = 0

    # ---- lifecycle -------------------------------------------------------

    def _build_options(self) -> ClaudeAgentOptions:
        append = self.settings.session_system_append
        if append is None:
            append = DEFAULT_SESSION_APPEND
        system_prompt = (
            {"type": "preset", "preset": "claude_code", "append": append}
            if append
            else None
        )
        return ClaudeAgentOptions(
            cwd=str(self.cwd),
            permission_mode=self.settings.permission_mode,
            include_partial_messages=False,
            resume=self.session_id,  # None => fresh session
            model=self.settings.model or None,
            max_turns=self.settings.max_turns,
            cli_path=self.settings.cli_path or None,
            setting_sources=SETTING_SOURCES,
            system_prompt=system_prompt,
            mcp_servers={"telegram": self._build_mcp_server()},
            stderr=self._on_stderr,
        )

    def _build_mcp_server(self):
        """In-process MCP tools that let the session push media/text to this topic."""
        session = self

        @tool(
            "send_photo",
            "Send an image file to the user in this Telegram topic so it renders "
            "inline (screenshots, charts, generated images). Path must be inside "
            "the workspace.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
             "required": ["path"]},
        )
        async def send_photo(args: dict[str, Any]) -> dict[str, Any]:
            return await session._tool_send(args, "photo")

        @tool(
            "send_video",
            "Send a video file to the user in this Telegram topic. Path must be "
            "inside the workspace.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
             "required": ["path"]},
        )
        async def send_video(args: dict[str, Any]) -> dict[str, Any]:
            return await session._tool_send(args, "video")

        @tool(
            "send_file",
            "Send any file to the user in this Telegram topic as a document. Path "
            "must be inside the workspace.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
             "required": ["path"]},
        )
        async def send_file(args: dict[str, Any]) -> dict[str, Any]:
            return await session._tool_send(args, "document")

        @tool(
            "send_message",
            "Send an extra plain-text message to the user in this Telegram topic "
            "(separate from your normal reply).",
            {"type": "object",
             "properties": {"text": {"type": "string"}},
             "required": ["text"]},
        )
        async def send_message(args: dict[str, Any]) -> dict[str, Any]:
            text = str(args.get("text", "")).strip()
            if text:
                await session.emitter.send(session.thread_id, text)
            return {"content": [{"type": "text", "text": "sent"}]}

        return create_sdk_mcp_server(
            "telegram", tools=[send_photo, send_video, send_file, send_message]
        )

    async def _tool_send(self, args: dict[str, Any], kind: str) -> dict[str, Any]:
        def err(msg: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": msg}], "is_error": True}

        raw = str(args.get("path", "")).strip()
        if not raw:
            return err("path is required")
        caption = (args.get("caption") or None)
        p = Path(raw)
        if not p.is_absolute():
            p = self.cwd / raw
        p = p.resolve()

        try:  # keep sends confined to the workspace
            p.relative_to(self.cwd.resolve())
        except ValueError:
            return err(f"refused: {p} is outside the session workspace")
        if not p.is_file():
            return err(f"no such file: {p}")
        size = p.stat().st_size
        if size > MAX_SEND_BYTES:
            return err(f"file too large to send ({size} bytes > 50MB)")

        try:
            if kind == "photo":
                await self.emitter.send_photo(self.thread_id, p, caption)
            elif kind == "video":
                await self.emitter.send_video(self.thread_id, p, caption)
            else:
                await self.emitter.send_document(self.thread_id, p, caption)
        except Exception as e:  # noqa: BLE001
            return err(f"send failed: {e}")
        return {"content": [{"type": "text", "text": f"sent {p.name} to the user"}]}

    async def start(self) -> None:
        self._client = ClaudeSDKClient(self._build_options())
        await self._client.connect()
        self.status = "idle"
        self._worker = asyncio.create_task(self._run(), name=f"session-{self.thread_id}")
        log.info("session started thread=%s cwd=%s resume=%s",
                 self.thread_id, self.cwd, self.session_id)

    async def stop(self) -> None:
        self.status = "stopped"
        if self._worker:
            self._worker.cancel()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.warning("disconnect failed thread=%s: %s", self.thread_id, e)

    async def interrupt(self) -> None:
        if self._client and self.status == "busy":
            try:
                await self._client.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning("interrupt failed thread=%s: %s", self.thread_id, e)

    # ---- messaging -------------------------------------------------------

    async def submit(self, text: str) -> None:
        await self._queue.put(Turn(text=text))

    async def submit_media(self, caption: str, items: list[MediaItem]) -> None:
        """Save incoming files to ./.tg-inbox/ and queue a turn describing them."""
        inbox = self.cwd / INBOX_DIRNAME
        inbox.mkdir(parents=True, exist_ok=True)
        images: list[dict] = []
        saved: list[tuple[Path, str | None]] = []
        for it in items:
            dest = inbox / _safe_name(it.filename)
            try:
                dest.write_bytes(it.data)
            except OSError as e:
                log.warning("could not save inbox file %s: %s", dest, e)
                continue
            saved.append((dest, it.mime))
            if it.kind == "image":
                images.append({
                    "media_type": it.mime or "image/jpeg",
                    "data": base64.b64encode(it.data).decode("ascii"),
                })

        lines: list[str] = []
        if caption.strip():
            lines.append(caption.strip())
        if saved:
            lines.append(f"[The user sent {len(saved)} file(s), saved under ./{INBOX_DIRNAME}/:]")
            for dest, mime in saved:
                lines.append(f"- {dest.relative_to(self.cwd)} ({mime or 'unknown type'})")
        text = "\n".join(lines) if lines else "(the user sent media with no caption)"
        await self._queue.put(Turn(text=text, images=images))

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def _run(self) -> None:
        while True:
            turn = await self._queue.get()
            self.status = "busy"
            try:
                await self._do_turn(turn)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("turn failed thread=%s", self.thread_id)
                await self.emitter.send(self.thread_id, f"⚠️ session error: {e}")
                await self._try_reconnect()
            finally:
                if self.status != "stopped":
                    self.status = "idle"
                self._queue.task_done()

    async def _do_turn(self, turn: Turn) -> None:
        assert self._client is not None
        await self.emitter.typing(self.thread_id)

        if turn.images:
            content: list[dict] = [
                {"type": "image", "source": {
                    "type": "base64", "media_type": img["media_type"], "data": img["data"]}}
                for img in turn.images
            ]
            content.append({"type": "text", "text": turn.text or "(no caption)"})
            message = {"type": "user", "message": {"role": "user", "content": content}}

            async def stream():
                yield message

            await self._client.query(stream())
        else:
            await self._client.query(turn.text)

        async for message in self._client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                sid = message.data.get("session_id")
                if sid:
                    self._capture_session_id(sid)
            elif isinstance(message, AssistantMessage):
                for piece in rendering.render_assistant(message):
                    await self._send(piece)
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    self._capture_session_id(message.session_id)
                self.turns += 1
                for piece in rendering.render_result(message):
                    await self._send(piece)

    async def _send(self, text: str) -> None:
        for part in rendering.chunk(text):
            await self.emitter.send(self.thread_id, part)

    async def _try_reconnect(self) -> None:
        """Rebuild the client, resuming the same session id if we have one."""
        if self.status == "stopped":
            return
        try:
            if self._client:
                await self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._client = ClaudeSDKClient(self._build_options())
            await self._client.connect()
            log.info("session reconnected thread=%s resume=%s",
                     self.thread_id, self.session_id)
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            log.exception("reconnect failed thread=%s", self.thread_id)
            await self.emitter.send(
                self.thread_id, f"⚠️ could not reconnect the session: {e}"
            )

    # ---- helpers ---------------------------------------------------------

    def _capture_session_id(self, sid: str) -> None:
        if sid and sid != self.session_id:
            self.session_id = sid
            self._on_session_id(sid)

    def _on_stderr(self, line: str) -> None:
        log.debug("claude stderr thread=%s: %s", self.thread_id, line.rstrip())
