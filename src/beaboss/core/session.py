"""One live coding-agent session bound to a thread — transport- and backend-agnostic.

All output flows through a single `post(Outbound)` callback with a Speaker
identity; media tools are generic. The engine observes workers via the
`on_turn_done` hook, not by watching the stream.
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
# The SDK caps a single JSON message from the CLI at 1 MB by default; a large tool
# result (a big file, an inline asset) blows past it and kills the turn. Raise the
# ceiling so ordinary big outputs survive — self-healing (below) covers the rest.
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Image types the model's vision actually accepts — anything else is saved as a plain
# file (readable from its path) rather than sent as a vision block the API would 400 on.
VISION_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

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
    quiet_ok: bool = False       # may end with no reply text (digest wakes) — no "✓ done"


def _safe_name(name: str) -> str:
    # Path(name).name keeps only the final path component — no separator can survive,
    # so the result is always a direct child of the inbox, never a traversal. Also
    # strip NULs: they pass through .name but make write_bytes raise ValueError.
    return Path(name).name.replace("\x00", "") or "file"


PostFn = Callable[[Outbound], Awaitable[None]]
BusyFn = Callable[[str], Awaitable[None]]

# The words a quiet digest may reply with to post nothing (the model resists
# emitting truly empty text, so it's given an explicit token — NOTHING — instead).
# Kept to unambiguous sentinels: "none"/"quiet" are ordinary one-word answers.
_QUIET_SENTINELS = {"nothing", "noreply", "noupdate"}


def _is_quiet_reply(text: str) -> bool:
    return "".join(c for c in text.lower() if c.isalpha()) in _QUIET_SENTINELS


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
        idle: BusyFn | None = None,
        session_id: str | None = None,
        system_append: str | None = None,
        extra_mcp_servers: dict[str, Any] | None = None,
        backend: AgentBackend | None = None,
        final_only: bool = False,
        footer_fn: Callable[[], str | None] | None = None,
        model_override: str | None = None,
    ):
        self.thread_id = thread_id
        self.cwd = cwd
        self.speaker = speaker
        self.settings = settings
        self._post = post
        self._busy = busy
        self._idle = idle
        self._on_session_id = on_session_id
        self.session_id = session_id
        self._system_append = system_append
        self._extra_mcp = extra_mcp_servers or {}
        # final_only: post ONE message per turn — the final reply — instead of
        # streaming every text block, tool line, and cost footer. Used for the
        # orchestrator, who should text the boss like a person, not narrate.
        # (Workers keep streaming into their topics: that's the glass wall.)
        self._final_only = final_only
        # footer_fn: called at reply time; whatever it returns is appended to the
        # reply as code-generated ground truth (e.g. the fleet actions this turn).
        self._footer_fn = footer_fn
        # A per-session model (a worker's dispatched tier); None => the global settings.model.
        self._model_override = model_override
        self._tool_buf: list[str] = []   # batched 🔧 lines (streaming sessions)
        # The agent runtime is a swappable seam; default to the Claude Code SDK.
        self._backend = backend or ClaudeAgentBackend(self._build_options)
        self._queue: asyncio.Queue[Turn] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._reply_to = thread_id   # current turn's reply target (see _do_turn)
        self._interrupted = False    # a human /stop is in flight for this turn
        self.status = "new"  # new | idle | busy | error | stopped
        self.turns = 0
        self.on_turn_done: Callable[["CoreSession", ResultMessage], Awaitable[None]] | None = None
        # Fires when a turn CRASHES (no ResultMessage) — so a supervisor can follow up
        # on a worker that died mid-turn instead of leaving it silently stalled.
        self.on_turn_error: Callable[["CoreSession", BaseException], Awaitable[None]] | None = None
        self._retries = 0                 # consecutive recoverable failures (resets on success)
        self._retry_task: asyncio.Task | None = None

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
            model=self._model_override or self.settings.model or None,
            max_turns=self.settings.max_turns,
            cli_path=self.settings.cli_path or None,
            setting_sources=SETTING_SOURCES,
            system_prompt=system_prompt,
            mcp_servers=mcp,
            stderr=self._on_stderr,
            max_buffer_size=_MAX_MESSAGE_BYTES,
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
            self._interrupted = True
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

    async def submit(self, text: str, reply_to: str | None = None,
                     quiet_ok: bool = False) -> None:
        await self._queue.put(Turn(text=text, reply_to=reply_to, quiet_ok=quiet_ok))

    async def submit_media(self, caption: str, items: list[MediaIn],
                           reply_to: str | None = None,
                           quiet_ok: bool = False) -> None:
        """Save incoming files under the workspace inbox and queue a turn."""
        inbox = self.cwd / INBOX_DIRNAME
        inbox.mkdir(parents=True, exist_ok=True)
        images: list[dict] = []
        saved: list[tuple[Path, str | None]] = []
        for it in items:
            dest = inbox / _safe_name(it.filename)
            try:
                dest.write_bytes(it.data)
            except (OSError, ValueError) as e:  # ValueError: e.g. NUL in a crafted name
                log.warning("could not save inbox file %s: %s", dest, e)
                continue
            saved.append((dest, it.mime))
            # Only send a vision block for image types the API accepts; other images
            # (svg/tiff/bmp) are still saved as files the agent can open by path.
            if it.kind == "image" and (it.mime or "").lower() in VISION_MIMES:
                images.append({
                    "media_type": it.mime,
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
        await self._queue.put(
            Turn(text=text, images=images, reply_to=reply_to, quiet_ok=quiet_ok))

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
                await self._try_reconnect()
                if (self.status != "error" and self._is_recoverable(e)
                        and self._retries < self.MAX_RETRIES):
                    # A transient limit (rate limit, overload, out of credits) clears on
                    # its own, so auto-retry the SAME turn with backoff — the boss never
                    # has to say "continue". The session_id resumes the context.
                    self._retries += 1
                    delay = min(self.RETRY_BASE * 2 ** (self._retries - 1), self.RETRY_MAX)
                    await self._safe_emit(
                        f"⏳ transient limit hit (likely rate/credit) — I'll retry on my "
                        f"own in ~{int(delay)}s (attempt {self._retries}/{self.MAX_RETRIES}); "
                        "you don't need to do anything.")
                    self._retry_task = asyncio.create_task(self._retry_after(turn, delay))
                else:
                    # Non-recoverable, or retries exhausted: a crashed turn has no
                    # ResultMessage, so on_turn_done never fires — surface it and wake the
                    # supervisor so a worker that truly died isn't left silently stalled.
                    await self._safe_emit(f"⚠️ session error: {e}")
                    self._retries = 0
                    if self.on_turn_error is not None:
                        try:
                            await self.on_turn_error(self, e)
                        except Exception:  # noqa: BLE001
                            log.exception("on_turn_error hook failed thread=%s", self.thread_id)
            else:
                self._retries = 0   # a clean turn resets the backoff
            finally:
                # Don't clobber a terminal "error" (a failed reconnect) back to idle,
                # or a dead session masquerades as healthy and keeps being reused.
                if self.status not in ("stopped", "error"):
                    self.status = "idle"
                self._queue.task_done()

    # Backoff for auto-retrying a transient failure (rate/credit/overload). Class attrs
    # so a test can shrink RETRY_BASE; ~30s→10min over 10 tries ≈ an hour of patience.
    MAX_RETRIES = 10
    RETRY_BASE = 30.0
    RETRY_MAX = 600.0
    _RECOVERABLE = ("rate limit", "rate_limit", "overloaded", "credit balance",
                    "insufficient", "quota", "usage limit", "too many requests",
                    "429", "529", "timed out", "timeout", "temporarily", "try again",
                    "connection", "unavailable")

    def _is_recoverable(self, e: BaseException) -> bool:
        """A failure that clears on its own if we just wait and retry — vs. one where
        retrying would only hit the same wall (e.g. the oversized-message crash)."""
        msg = str(e).lower()
        if "buffer" in msg:            # oversized message: waiting won't help
            return False
        return any(k in msg for k in self._RECOVERABLE)

    async def _retry_after(self, turn: Turn, delay: float) -> None:
        """Re-run a turn after a backoff — how the session picks itself back up when a
        transient limit (like exhausted credits) clears, with no nudge from the boss."""
        try:
            await asyncio.sleep(delay)
            if self.status != "stopped":
                await self._queue.put(turn)   # same turn; session_id resumes the context
        except asyncio.CancelledError:
            pass

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
        # Reset per-turn state so a turn that ends WITHOUT a ResultMessage (a racy
        # interrupt, a wedge) can't leak into the next turn: a stale _interrupted
        # would replace the next real reply with "⏹ stopped.", and stale actions
        # would append a wrong ⚙ footer. Drain both at the boundary.
        self._interrupted = False
        if self._footer_fn:
            self._footer_fn()
        await self._busy(self._reply_to)
        keepalive = asyncio.create_task(self._keep_busy())
        try:
            await self._do_turn_inner(turn)
        finally:
            keepalive.cancel()
            # Clear the "working" indicator at true turn-end — even a quiet digest
            # that posted nothing must not leave the cockpit showing motion forever.
            # (Fires after any reply, so long worker turns still read as busy.)
            if self._idle is not None:
                try:
                    await self._idle(self._reply_to)
                except Exception:  # noqa: BLE001
                    pass

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
                if self._interrupted:
                    # A human /stop ended this turn: say so plainly instead of
                    # surfacing the backend's internal diagnostics — but keep the
                    # footer, so actions taken before the stop stay visible.
                    self._interrupted = False
                    footer = self._footer_fn() if self._footer_fn else None
                    await self._emit_text(
                        f"⏹ stopped.\n\n{footer}" if footer else "⏹ stopped.")
                elif self._final_only:
                    # One clean message: the final reply (+ the code-generated
                    # action footer, if any). Errors still surface; the success
                    # footer (turn count / cost) is noise here.
                    footer = self._footer_fn() if self._footer_fn else None
                    reply = (message.result or "").strip()
                    if _is_quiet_reply(reply):
                        reply = ""  # the "no boss-facing update" sentinel → silence
                    # A boss-initiated turn must never end silent; a digest wake may.
                    must_answer = bool(turn.reply_to) and not turn.quiet_ok
                    if message.is_error or (message.subtype and message.subtype != "success"):
                        for piece in rendering.render_result(message):
                            await self._emit_text(piece)
                    elif reply:
                        await self._emit_text(f"{reply}\n\n{footer}" if footer else reply)
                    elif must_answer:
                        await self._emit_text(footer or "✓ done")
                    # else: quiet digest, nothing boss-facing → post nothing
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
