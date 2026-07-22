"""WebSocket adapter: browser clients ⇄ core threads over one JSON socket.

The reusable base for any browser-style surface (web app, VS Code extension).
The wire protocol is deliberately tiny and platform-neutral:

server → client
  {"type": "threads",  "threads": [{"id", "title", "open"}]}   snapshot on connect
  {"type": "thread",   "id", "title", "open"}                  create / rename / close
  {"type": "message",  "thread_id", "speaker": {...}, "text"}  an Outbound
  {"type": "busy",     "thread_id"}                            best-effort typing hint

client → server
  {"type": "message",  "thread_id", "text"}                    a human message

Every connected client sees every thread — the core's threads map straight onto
the client's thread list. Speaker identity rides in the message body (role, name,
emoji) so the client can render header cards, exactly like the Telegram adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging

from websockets.asyncio.server import ServerConnection, serve

from ..core.ports import InboundMessage, Outbound, Speaker

log = logging.getLogger("beaboss.transport.websocket")

# The orchestrator's office (engine.main_thread). Seeded so a fresh client can
# talk to the orchestrator before any worker threads exist.
OFFICE = "general"


def _speaker_json(s: Speaker) -> dict:
    return {"role": s.role, "name": s.name, "emoji": s.emoji}


class WebSocketTransport:
    """Implements core.ports.Transport, fanning core events out to browsers."""

    def __init__(self) -> None:
        self.clients: set[ServerConnection] = set()
        self.threads: dict[str, dict] = {}  # id -> {"title", "open"}
        self._next = 0
        self._add_thread(OFFICE, "Orchestrator")

    # ---- thread bookkeeping ---------------------------------------------

    def _add_thread(self, thread_id: str, title: str) -> None:
        self.threads[thread_id] = {"title": title, "open": True}

    def _thread_event(self, thread_id: str) -> dict:
        t = self.threads[thread_id]
        return {"type": "thread", "id": thread_id, "title": t["title"],
                "open": t["open"]}

    # ---- Transport interface --------------------------------------------

    async def create_thread(self, title: str) -> str:
        self._next += 1
        thread_id = str(self._next)
        self._add_thread(thread_id, title)
        await self._broadcast(self._thread_event(thread_id))
        return thread_id

    async def rename_thread(self, thread_id: str, title: str) -> None:
        t = self.threads.get(thread_id)
        if t is None:
            return
        t["title"] = title
        await self._broadcast(self._thread_event(thread_id))

    async def close_thread(self, thread_id: str) -> None:
        t = self.threads.get(thread_id)
        if t is None:
            return
        t["open"] = False
        await self._broadcast(self._thread_event(thread_id))

    async def post(self, out: Outbound) -> None:
        # Media has no browser transport yet; surface the caption/filename so the
        # thread still reads coherently rather than dropping the event silently.
        text = out.text
        if out.media_path is not None:
            note = out.caption or f"[{out.media_kind or 'file'}: {out.media_path.name}]"
            text = f"{text}\n{note}".strip() if text else note
        if not text.strip():
            return
        await self._broadcast({
            "type": "message", "thread_id": out.thread_id,
            "speaker": _speaker_json(out.speaker), "text": text,
        })

    async def indicate_busy(self, thread_id: str) -> None:
        await self._broadcast({"type": "busy", "thread_id": thread_id})

    # ---- client plumbing -------------------------------------------------

    async def register(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        snapshot = {"type": "threads", "threads": [
            {"id": tid, "title": t["title"], "open": t["open"]}
            for tid, t in self.threads.items()
        ]}
        await ws.send(json.dumps(snapshot))

    def unregister(self, ws: ServerConnection) -> None:
        self.clients.discard(ws)

    async def _broadcast(self, payload: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(payload)
        results = await asyncio.gather(
            *(c.send(data) for c in self.clients), return_exceptions=True)
        for c, r in zip(list(self.clients), results):
            if isinstance(r, Exception):
                self.clients.discard(c)


def make_handler(engine, transport: WebSocketTransport):
    """Build the per-connection handler bound to this engine + transport."""

    async def handler(ws: ServerConnection) -> None:
        await transport.register(ws)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") != "message":
                    continue
                thread_id = str(msg.get("thread_id") or "").strip()
                text = str(msg.get("text") or "")
                if not thread_id or not text.strip():
                    continue
                await engine.on_inbound(InboundMessage(
                    thread_id=thread_id, text=text,
                    sender_name=str(msg.get("sender_name") or "the boss"),
                ))
        finally:
            transport.unregister(ws)

    return handler


async def serve_forever(engine, transport: WebSocketTransport,
                        host: str, port: int) -> None:
    """Run the WebSocket server until cancelled."""
    async with serve(make_handler(engine, transport), host, port):
        log.info("websocket transport listening on ws://%s:%s", host, port)
        await asyncio.Future()  # run forever
