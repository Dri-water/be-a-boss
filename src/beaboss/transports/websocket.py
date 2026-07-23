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
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve

from ..core.ports import SYSTEM, InboundMessage, Outbound, Speaker

log = logging.getLogger("beaboss.transport.websocket")

# The orchestrator's office (engine.main_thread). Seeded so a fresh client can
# talk to the orchestrator before any worker threads exist.
OFFICE = "general"


def _speaker_json(s: Speaker) -> dict:
    return {"role": s.role, "name": s.name, "emoji": s.emoji}


class WebSocketTransport:
    """Implements core.ports.Transport, fanning core events out to browsers."""

    def __init__(self, store=None) -> None:
        self.clients: set[ServerConnection] = set()
        self.threads: dict[str, dict] = {}  # id -> {"title", "open"}
        self._next = 0
        self._add_thread(OFFICE, "Orchestrator")
        if store is not None:
            self._rehydrate(store)

    def _rehydrate(self, store) -> None:
        """Restart-proof: re-seed the thread list from the store so a web restart
        doesn't drop existing workers from the UI — and, critically, advance the id
        counter past them so a fresh thread can't reuse and overwrite a live id."""
        from pathlib import Path
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
                mtype = msg.get("type")
                if mtype == "message":
                    thread_id = str(msg.get("thread_id") or "").strip()
                    text = str(msg.get("text") or "")
                    if thread_id and text.strip():
                        await engine.on_inbound(InboundMessage(
                            thread_id=thread_id, text=text,
                            sender_name=str(msg.get("sender_name") or "the boss")))
                elif mtype == "interrupt":
                    tid = str(msg.get("thread_id") or "").strip()
                    if tid:
                        await engine.interrupt(tid)
                elif mtype == "kill":
                    tid = str(msg.get("thread_id") or "").strip()
                    if tid:
                        await engine.kill(tid)
                        await transport.post(Outbound(
                            thread_id=tid, speaker=SYSTEM, text="🗑 Session ended."))
                        await transport.close_thread(tid)
                elif mtype == "new":
                    result = await engine.new_direct(
                        str(msg.get("path") or ""),
                        str(msg.get("name") or "").strip() or None)
                    if isinstance(result, str):  # an error message
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM, text=result))
                elif mtype in ("approve", "reject"):
                    wid = str(msg.get("worker_id") or "").strip()
                    if wid:
                        fn = (engine.approve_delivery if mtype == "approve"
                              else engine.reject_delivery)
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM, text=await fn(wid)))
        finally:
            transport.unregister(ws)

    return handler


def _gatekeeper(token: str, allowed_origins: set[str]):
    """Reject the two ways a hostile client reaches a localhost WebSocket:

    - a cross-origin BROWSER page (the CSWSH → drive-by-RCE vector): browsers send
      an honest Origin header, so anything not in our allowlist is refused;
    - a NON-browser local process (no/forged Origin): gated by a required handshake
      token in the query string.

    A localhost bind alone is NOT a boundary against either — this is.
    """
    def process_request(connection: ServerConnection, request):
        origin = request.headers.get("Origin")
        if origin is not None and origin not in allowed_origins:
            log.warning("web: refused connection from origin %r", origin)
            return connection.respond(403, "origin not allowed\n")
        supplied = parse_qs(urlsplit(request.path).query).get("token", [""])[0]
        if not token or supplied != token:
            log.warning("web: refused connection with missing/invalid token")
            return connection.respond(401, "missing or invalid token\n")
        return None
    return process_request


async def serve_forever(engine, transport: WebSocketTransport,
                        host: str, port: int, token: str) -> None:
    """Run the WebSocket server until cancelled, gated by Origin + handshake token."""
    allowed = {"null", f"http://{host}:{port}",
               f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    async with serve(make_handler(engine, transport), host, port,
                     process_request=_gatekeeper(token, allowed)):
        log.info("websocket transport listening on ws://%s:%s", host, port)
        await asyncio.Future()  # run forever
