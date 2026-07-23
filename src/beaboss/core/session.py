"""One live coding-agent session bound to a thread — transport- and backend-agnostic.

Ported from the original claude_session.py: same queue/worker/turn logic, but all
output flows through a single `post(Outbound)` callback with a Speaker identity,
and media tools are generic. A tap can observe everything the session says
(used by the orchestrator to 'see' worker threads) and inbound turns can carry an
extra observer note (used for human interjections).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    tool,
)

from .. import rendering
from .agent_backend import AgentBackend, ClaudeAgentBackend, scrubbed_env
from .ports import MediaIn, Outbound, Speaker

log = logging.getLogger("beaboss.core.session")

SETTING_SOURCES = ["project", "local"]
INBOX_DIRNAME = ".beaboss-inbox"
MAX_SEND_BYTES = 50 * 1024 * 1024

DEFAULT_SESSION_APPEND = (
    "Runtime context: you are a headless coding-agent session driven programmatically, "
    "not an interactive terminal. You typically run inside a Linux container, "
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
    "- Your normal text replies are delivered into this thread. To send MEDIA, use "
    "the chat tools: mcp__chat__send_photo (renders inline — screenshots, charts), "
    "mcp__chat__send_video, mcp__chat__send_file (any document), and "
    "mcp__chat__send_message (extra plain text). Paths must be inside your "
    "workspace.\n"
    f"- Files the user sends you are saved under ./{INBOX_DIRNAME}/ and referenced "
    "in their message; images are also attached so you can see them directly."
)


@dataclass
class Turn:
    """One queued user turn: text plus optional inline images (base64)."""

    text: str
    images: list[dict] = field(default_factory=list)  # {media_type, data}
    reply_to: str | None = None  # where to post this turn's reply (default: own thread)


def _safe_name(name: str) -> str:
    return Path(name).name or "file"


PostFn = Callable[[Outbound], Awaitable[None]]
BusyFn = Callable[[str], Awaitable[None]]
TapFn = Callable[[str, str, str], Awaitable[None]]  # (thread_id, kind, text)


class CoreSession:
    """A live coding-agent session that posts as `speaker` into `thread_id`."""

    # A wedged backend (no events, never ends the turn) must not pin a session in
    # "busy" forever. If nothing arrives for this long, the turn is treated as hung
    # and the session interrupts + reconnects. Generous, so a long-but-active tool
    # call (a slow build/test that streams no events) isn't killed.
    TURN_IDLE_TIMEOUT = 900

    def __init__(
        self,
        thread_id: str,
        cwd: Path,
        speaker: Speaker,
        settings,
        post: PostFn,
        busy: BusyFn,
        on_session_id: Callable[[str], None],
        session_id: str | None = None,
        system_append: str | None = None,
        tap: TapFn | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
        max_turns: int | None = None,
        backend: AgentBackend | None = None,
        final_only: bool = False,
        footer_fn: Callable[[], str | None] | None = None,
    ):
        self.thread_id = thread_id
        self.cwd = cwd
        self.speaker = speaker
        self.settings = settings
        self._post = post
        self._busy = busy
        self._on_session_id = on_session_id
        self.session_id = session_id
        self._system_append = system_append
        self._tap = tap
        self._extra_mcp = extra_mcp_servers or {}
        self._max_turns = max_turns
        # final_only: post ONE message per turn — the final reply — instead of
        # streaming every text block, tool line, and cost footer. Used for the
        # orchestrator, who should text the boss like a person, not narrate.
        # (Workers keep streaming into their topics: that's the glass wall.)
        self._final_only = final_only
        # footer_fn: called at reply time; whatever it returns is appended to the
        # reply as code-generated ground truth (e.g. the fleet actions this turn).
        self._footer_fn = footer_fn
        self._tool_buf: list[str] = []   # batched 🔧 lines (streaming sessions)
        # The agent runtime is a swappable seam; default to the Claude Code SDK.
        self._backend = backend or ClaudeAgentBackend(self._build_options)
        self._queue: asyncio.Queue[Turn] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._reply_to = thread_id   # current turn's reply target (see _do_turn)
        self.status = "new"  # new | idle | busy | error | stopped
        self.turns = 0
        self.on_turn_done: Callable[["CoreSession", ResultMessage], Awaitable[None]] | None = None

    # ---- lifecycle -------------------------------------------------------

    def _resolve_append(self) -> str:
        if self._system_append is not None:
            return self._system_append
        if self.settings.session_system_append is not None:
            return self.settings.session_system_append
        return DEFAULT_SESSION_APPEND

    def _build_options(self) -> ClaudeAgentOptions:
        append = self._resolve_append()
        system_prompt = (
            {"type": "preset", "preset": "claude_code", "append": append}
            if append
            else None
        )
        mcp = {"chat": self._build_chat_server()}
        mcp.update(self._extra_mcp)
        return ClaudeAgentOptions(
            cwd=str(self.cwd),
            env=scrubbed_env(),  # don't hand the bot's secrets to the agent CLI
            permission_mode=self.settings.permission_mode,
            include_partial_messages=False,
            resume=self.session_id,
            model=self.settings.model or None,
            max_turns=self._max_turns if self._max_turns is not None else self.settings.max_turns,
            cli_path=self.settings.cli_path or None,
            setting_sources=SETTING_SOURCES,
            system_prompt=system_prompt,
            mcp_servers=mcp,
            stderr=self._on_stderr,
        )

    def _build_chat_server(self):
        session = self

        @tool(
            "send_photo",
            "Send an image file into this thread so it renders inline "
            "(screenshots, charts, generated images). Path must be inside the "
            "workspace.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
             "required": ["path"]},
        )
        async def send_photo(args: dict[str, Any]) -> dict[str, Any]:
            return await session._tool_send(args, "photo")

        @tool(
            "send_video",
            "Send a video file into this thread. Path must be inside the workspace.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
             "required": ["path"]},
        )
        async def send_video(args: dict[str, Any]) -> dict[str, Any]:
            return await session._tool_send(args, "video")

        @tool(
            "send_file",
            "Send any file into this thread as a document. Path must be inside "
            "the workspace.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
             "required": ["path"]},
        )
        async def send_file(args: dict[str, Any]) -> dict[str, Any]:
            return await session._tool_send(args, "document")

        @tool(
            "send_message",
            "Send an extra plain-text message into this thread (separate from "
            "your normal reply).",
            {"type": "object",
             "properties": {"text": {"type": "string"}},
             "required": ["text"]},
        )
        async def send_message(args: dict[str, Any]) -> dict[str, Any]:
            text = str(args.get("text", "")).strip()
            if text:
                await session._emit_text(text)
            return {"content": [{"type": "text", "text": "sent"}]}

        tools = [send_photo, send_video, send_file]
        if not self._final_only:
            # send_message would let a final_only session narrate around its
            # one-reply-per-turn contract — don't mount it there.
            tools.append(send_message)
        return create_sdk_mcp_server("chat", tools=tools)

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
        try:
            p.relative_to(self.cwd.resolve())
        except ValueError:
            return err(f"refused: {p} is outside the session workspace")
        if not p.is_file():
            return err(f"no such file: {p}")
        if p.stat().st_size > MAX_SEND_BYTES:
            return err(f"file too large to send (> {MAX_SEND_BYTES} bytes)")
        try:
            await self._post(Outbound(
                thread_id=self._reply_to, speaker=self.speaker,
                media_path=p, media_kind=kind, caption=caption,
            ))
        except Exception as e:  # noqa: BLE001
            return err(f"send failed: {e}")
        return {"content": [{"type": "text", "text": f"sent {p.name} into the thread"}]}

    async def start(self) -> None:
        await self._backend.start()
        self.status = "idle"
        self._worker = asyncio.create_task(
            self._run(), name=f"session-{self.thread_id}"
        )
        log.info("session started thread=%s cwd=%s resume=%s",
                 self.thread_id, self.cwd, self.session_id)

    async def stop(self) -> None:
        self.status = "stopped"
        if self._worker:
            self._worker.cancel()
        try:
            await self._backend.stop()
        except Exception as e:  # noqa: BLE001
            log.warning("disconnect failed thread=%s: %s", self.thread_id, e)

    async def interrupt(self) -> None:
        if self.status == "busy":
            try:
                await self._backend.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning("interrupt failed thread=%s: %s", self.thread_id, e)

    @property
    def alive(self) -> bool:
        """Healthy = the run loop is still consuming turns and the session isn't in a
        terminal error state. The engine uses this to avoid handing back a zombie (a
        run task that died, or a session that couldn't reconnect)."""
        return (self.status != "error"
                and self._worker is not None and not self._worker.done())

    # ---- messaging -------------------------------------------------------

    async def submit(self, text: str, reply_to: str | None = None) -> None:
        await self._queue.put(Turn(text=text, reply_to=reply_to))

    async def submit_media(self, caption: str, items: list[MediaIn],
                           reply_to: str | None = None) -> None:
        """Save incoming files under the workspace inbox and queue a turn."""
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
            lines.append(
                f"[The user sent {len(saved)} file(s), saved under ./{INBOX_DIRNAME}/:]"
            )
            for dest, mime in saved:
                lines.append(f"- {dest.relative_to(self.cwd)} ({mime or 'unknown type'})")
        text = "\n".join(lines) if lines else "(the user sent media with no caption)"
        await self._queue.put(Turn(text=text, images=images, reply_to=reply_to))

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
                # best-effort: a failing post here (e.g. the thread was deleted) must
                # never escape and kill this loop — that would strand the session.
                await self._safe_emit(f"⚠️ session error: {e}")
                await self._try_reconnect()
            finally:
                # Don't clobber a terminal "error" (a failed reconnect) back to idle,
                # or a dead session masquerades as healthy and keeps being reused.
                if self.status not in ("stopped", "error"):
                    self.status = "idle"
                self._queue.task_done()

    async def _keep_busy(self) -> None:
        """Telegram's typing indicator dies after ~5s; keep it alive for the whole
        turn so a long think never looks like dead air. Best-effort only."""
        try:
            while True:
                await asyncio.sleep(4.5)
                await self._busy(self._reply_to)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    async def _do_turn(self, turn: Turn) -> None:
        # Reply to wherever this turn came from — so a DM to the orchestrator is
        # answered in that DM, not in the group. Workers/direct sessions leave
        # reply_to unset and post to their own thread as before.
        self._reply_to = turn.reply_to or self.thread_id
        self._tool_buf = []
        await self._busy(self._reply_to)
        keepalive = asyncio.create_task(self._keep_busy())
        try:
            await self._do_turn_inner(turn)
        finally:
            keepalive.cancel()

    async def _do_turn_inner(self, turn: Turn) -> None:
        await self._backend.send(turn)

        result: ResultMessage | None = None
        stream = self._backend.receive().__aiter__()
        while True:
            try:
                message = await asyncio.wait_for(
                    stream.__anext__(), timeout=self.TURN_IDLE_TIMEOUT)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                # No event for TURN_IDLE_TIMEOUT — the backend is wedged. Interrupt
                # and raise; _run reports it plainly and reconnects the session.
                try:
                    await self._backend.interrupt()
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"no response from the agent for {self.TURN_IDLE_TIMEOUT}s "
                    f"(turn wedged) — reconnecting")
            if isinstance(message, SystemMessage) and message.subtype == "init":
                sid = message.data.get("session_id")
                if sid:
                    self._capture_session_id(sid)
            elif isinstance(message, AssistantMessage):
                if not self._final_only:
                    for piece in rendering.render_assistant(message):
                        # Batch consecutive 🔧 one-liners into a single message —
                        # a 40-tool worker turn must not be 40 notifications.
                        if piece.startswith("🔧"):
                            self._tool_buf.append(piece)
                        else:
                            await self._flush_tools()
                            await self._emit_text(piece)
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    self._capture_session_id(message.session_id)
                self.turns += 1
                result = message
                await self._flush_tools()
                if self._final_only:
                    # One clean message: the final reply (+ the code-generated
                    # action footer, if any). Errors still surface; the success
                    # footer (turn count / cost) is noise here.
                    footer = self._footer_fn() if self._footer_fn else None
                    if message.is_error or (message.subtype and message.subtype != "success"):
                        for piece in rendering.render_result(message):
                            await self._emit_text(piece)
                    elif (message.result or "").strip():
                        text = message.result.strip()
                        if footer:
                            text = f"{text}\n\n{footer}"
                        await self._emit_text(text)
                    elif turn.reply_to:
                        # A boss-initiated turn must never end in total silence.
                        # (Digest turns may stay quiet on purpose.)
                        await self._emit_text(footer or "✓ done")
                else:
                    for piece in rendering.render_result(message):
                        await self._emit_text(piece)

        if result is not None and self.on_turn_done is not None:
            try:
                await self.on_turn_done(self, result)
            except Exception:  # noqa: BLE001
                log.exception("on_turn_done hook failed thread=%s", self.thread_id)

    async def _flush_tools(self) -> None:
        if self._tool_buf:
            buf, self._tool_buf = self._tool_buf, []
            await self._emit_text("\n".join(buf))

    async def _emit_text(self, text: str) -> None:
        if not text.strip():
            return
        for part in rendering.chunk(text):
            await self._post(Outbound(
                thread_id=self._reply_to, speaker=self.speaker, text=part
            ))
        if self._tap is not None:
            try:
                await self._tap(self.thread_id, "said", text)
            except Exception:  # noqa: BLE001
                log.exception("tap failed thread=%s", self.thread_id)

    async def _safe_emit(self, text: str) -> None:
        """Emit that can never raise — used on error paths so a failing post (e.g. a
        deleted thread) can't escape and kill the session's run loop."""
        try:
            await self._emit_text(text)
        except Exception:  # noqa: BLE001
            log.warning("could not post to thread=%s (dropped): %s",
                        self.thread_id, text[:80])

    async def _try_reconnect(self) -> None:
        if self.status == "stopped":
            return
        try:
            await self._backend.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._backend.start()
            log.info("session reconnected thread=%s resume=%s",
                     self.thread_id, self.session_id)
        except Exception as e:  # noqa: BLE001
            self.status = "error"
            log.exception("reconnect failed thread=%s", self.thread_id)
            await self._safe_emit(f"⚠️ could not reconnect the session: {e}")

    # ---- helpers ---------------------------------------------------------

    def _capture_session_id(self, sid: str) -> None:
        if sid and sid != self.session_id:
            self.session_id = sid
            self._on_session_id(sid)

    def _on_stderr(self, line: str) -> None:
        log.debug("backend stderr thread=%s: %s", self.thread_id, line.rstrip())
