"""CLI adapter: drive the whole org from a terminal — or a pipe.

The transport is UI-agnostic: it normalises core events (messages, threads, the
dashboard, media, typing) into small dicts and hands each to an async `emit`
callback. A driver decides how to render them:

- agent / `--json` mode: emit = print one JSON line. The event shapes match the
  websocket surface exactly, so anything that can drive the web app drives the CLI
  unchanged — and an agent drives the org from stdin/stdout with no browser.
- interactive mode: emit = paint a coloured line for a human at a terminal.

Media is delivered inline as base64 (same as the web surface) so a worker's
screenshot is real proof here too, not a placeholder.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Awaitable, Callable

from ..core.ports import Outbound, Speaker

EmitFn = Callable[[dict], Awaitable[None]]

OFFICE = "general"  # the orchestrator's thread (engine.main_thread)


def _speaker_json(s: Speaker) -> dict:
    return {"role": s.role, "name": s.name, "emoji": s.emoji}


class CLITransport:
    """Implements core.ports.Transport, emitting normalised event dicts."""

    _MEDIA_CAP = 8 * 1024 * 1024

    def __init__(self, emit: EmitFn, store=None) -> None:
        self._emit = emit
        self.threads: dict[str, dict] = {}   # id -> {"title", "open"}
        self.dashboard = ""
        self.history: list[dict] = []        # for a driver that wants a replay
        self._next = 0
        self._add_thread(OFFICE, "Orchestrator")
        if store is not None:
            self._rehydrate(store)

    # ---- thread bookkeeping ---------------------------------------------

    def _add_thread(self, thread_id: str, title: str) -> None:
        self.threads[thread_id] = {"title": title, "open": True}

    def _rehydrate(self, store) -> None:
        highest = 0
        for tid, rec in store.all().items():
            if tid == OFFICE:
                continue
            title = rec.name
            if rec.role == "worker" and rec.repo:
                title = f"⚙️ {rec.name} · {Path(rec.repo).name}"
            open_ = not (rec.role == "worker" and rec.worker_status == "dismissed")
            self.threads[tid] = {"title": title, "open": open_}
            if tid.isdigit():
                highest = max(highest, int(tid))
        self._next = highest

    async def _send(self, event: dict) -> None:
        self.history.append(event)
        if len(self.history) > 300:
            del self.history[:-300]
        await self._emit(event)

    # ---- Transport interface --------------------------------------------

    async def create_thread(self, title: str) -> str:
        self._next += 1
        thread_id = str(self._next)
        self._add_thread(thread_id, title)
        await self._emit({"type": "thread", "id": thread_id, "title": title,
                          "open": True})
        return thread_id

    async def close_thread(self, thread_id: str) -> None:
        t = self.threads.get(thread_id)
        if t is None:
            return
        t["open"] = False
        await self._emit({"type": "thread", "id": thread_id, "title": t["title"],
                          "open": False})

    async def delete_thread(self, thread_id: str) -> None:
        if self.threads.pop(thread_id, None) is not None:
            await self._emit({"type": "thread", "id": thread_id, "title": "",
                              "open": False, "removed": True})

    async def post(self, out: Outbound) -> None:
        if out.media_path is not None:
            event = self._media_event(out)
            if event is None:
                note = (f"{out.caption or ''}\n[{out.media_kind or 'file'}: "
                        f"{out.media_path.name} — unreadable/too large]").strip()
                event = {"type": "message", "thread_id": out.thread_id,
                         "speaker": _speaker_json(out.speaker), "text": note}
            await self._send(event)
            return
        if not out.text.strip():
            return
        await self._send({"type": "message", "thread_id": out.thread_id,
                          "speaker": _speaker_json(out.speaker), "text": out.text})

    def _media_event(self, out: Outbound) -> dict | None:
        try:
            data = out.media_path.read_bytes()
        except OSError:
            return None
        if len(data) > self._MEDIA_CAP:
            return None
        mime, _ = mimetypes.guess_type(out.media_path.name)
        return {
            "type": "media", "thread_id": out.thread_id,
            "speaker": _speaker_json(out.speaker),
            "kind": out.media_kind or "document",
            "filename": out.media_path.name,
            "mime": mime or "application/octet-stream",
            "data_b64": base64.b64encode(data).decode("ascii"),
            "caption": out.caption or "",
        }

    async def indicate_busy(self, thread_id: str) -> None:
        await self._emit({"type": "busy", "thread_id": thread_id})

    async def update_dashboard(self, text: str) -> None:
        self.dashboard = text
        await self._emit({"type": "dashboard", "text": text})

    async def delete_dashboard(self) -> None:
        self.dashboard = ""
        await self._emit({"type": "dashboard", "text": ""})

    def snapshot(self) -> dict:
        """The connect-time snapshot a driver replays to a fresh screen."""
        return {"type": "threads", "threads": [
            {"id": tid, "title": t["title"], "open": t["open"]}
            for tid, t in self.threads.items()]}
