"""WebSocket adapter: browser clients ⇄ core threads over one JSON socket.

The reusable base for any browser-style surface. The wire protocol is
deliberately tiny and platform-neutral:

server → client
  {"type": "threads",  "threads": [{"id", "title", "open"}]}   snapshot on connect
  {"type": "thread",   "id", "title", "open", "removed"?}      create / close / delete
  {"type": "message",  "thread_id", "speaker": {...}, "text"}  an Outbound
  {"type": "media",    "thread_id", "speaker", "kind",
                       "filename", "mime", "data_b64", "caption"}  real files inline
  {"type": "dashboard","text"}                                 the live status board
  {"type": "busy",     "thread_id"}                            best-effort typing hint

client → server
  {"type": "message",  "thread_id", "text"}                    a human message
  {"type": "interrupt"|"kill", "thread_id"} · {"type": "approve"|"reject", "worker_id"}
  {"type": "new", "path", "name"?} · {"type": "reset", "confirm": bool}

Every connected client sees every thread — the core's threads map straight onto
the client's thread list. Speaker identity rides in the message body (role, name,
emoji) so the client can render header cards, exactly like the Telegram adapter.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from ..core.ports import SYSTEM, InboundMessage, Outbound, Speaker

log = logging.getLogger("beaboss.transport.websocket")

# The orchestrator's office (engine.main_thread). Seeded so a fresh client can
# talk to the orchestrator before any worker threads exist.
OFFICE = "general"


def _speaker_json(s: Speaker) -> dict:
    return {"role": s.role, "name": s.name, "emoji": s.emoji}


class WebSocketTransport:
    """Implements core.ports.Transport, fanning core events out to browsers."""

    _MEDIA_CAP = 8 * 1024 * 1024  # keep ws frames browser-friendly
    _HISTORY_CAP = 300            # recent messages replayed to a (re)connecting client

    def __init__(self, store=None) -> None:
        self.clients: set[ServerConnection] = set()
        self.threads: dict[str, dict] = {}  # id -> {"title", "open"}
        self.dashboard = ""                 # latest rendered status board
        self.history: list[dict] = []       # recent message events for reconnect replay
        self._next = 0
        self._add_thread(OFFICE, "Orchestrator")
        if store is not None:
            self._rehydrate(store)

    def record(self, event: dict) -> None:
        """Keep a bounded scrollback so a reconnecting/reloading client isn't
        amnesiac — the whole conversation shouldn't vanish on a dropped socket."""
        self.history.append(event)
        if len(self.history) > self._HISTORY_CAP:
            del self.history[:-self._HISTORY_CAP]

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
        if out.media_path is not None:
            live = self._media_event(out)
            if live is not None:
                # history keeps a light placeholder (no base64) so scrollback stays bounded
                self.record({"type": "message", "thread_id": out.thread_id,
                             "speaker": _speaker_json(out.speaker),
                             "text": f"{out.caption or ''}\n[image: {out.media_path.name}]".strip()})
                await self._broadcast(live)
                return
            # unreadable / oversized → a readable placeholder, never silence
            note = (f"{out.caption or ''}\n[{out.media_kind or 'file'}: "
                    f"{out.media_path.name} — too large for the browser]").strip()
            event = {"type": "message", "thread_id": out.thread_id,
                     "speaker": _speaker_json(out.speaker), "text": note}
            self.record(event)
            await self._broadcast(event)
            return
        if not out.text.strip():
            return
        event = {"type": "message", "thread_id": out.thread_id,
                 "speaker": _speaker_json(out.speaker), "text": out.text}
        self.record(event)
        await self._broadcast(event)

    def _media_event(self, out: Outbound) -> dict | None:
        """A worker's screenshot/file as a real inline event — the same proof the
        Telegram surface gets, not a placeholder line."""
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
        await self._broadcast({"type": "busy", "thread_id": thread_id})

    async def indicate_idle(self, thread_id: str) -> None:
        # Turn-end: clear the client's "working" indicator even if the turn was a
        # quiet digest that posted nothing. Transient — not recorded in history.
        await self._broadcast({"type": "idle", "thread_id": thread_id})

    async def update_dashboard(self, text: str) -> None:
        """The live status board, as a broadcast — web parity with the pinned
        Telegram message. New clients get it in their connect snapshot."""
        self.dashboard = text
        await self._broadcast({"type": "dashboard", "text": text})

    async def delete_dashboard(self) -> None:
        self.dashboard = ""
        await self._broadcast({"type": "dashboard", "text": ""})

    async def delete_thread(self, thread_id: str) -> None:
        """Factory reset: drop the thread from every client's list."""
        if self.threads.pop(thread_id, None) is not None:
            await self._broadcast({"type": "thread", "id": thread_id,
                                   "title": "", "open": False, "removed": True})

    # ---- client plumbing -------------------------------------------------

    async def register(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        # Snapshot history AFTER registering: anything posted from here on reaches ws
        # via _broadcast (live), so replaying a stable copy of what existed at register
        # time means no message is missed and none is delivered twice.
        history = list(self.history)
        dashboard = self.dashboard
        snapshot = {"type": "threads", "threads": [
            {"id": tid, "title": t["title"], "open": t["open"]}
            for tid, t in self.threads.items()
        ]}
        await ws.send(json.dumps(snapshot))
        for event in history:                 # replay recent conversation (no amnesia)
            await ws.send(json.dumps(event))
        if dashboard:
            await ws.send(json.dumps({"type": "dashboard", "text": dashboard}))

    def unregister(self, ws: ServerConnection) -> None:
        self.clients.discard(ws)

    async def _broadcast(self, payload: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(payload)
        # Snapshot the set ONCE: a client (un)registering mid-broadcast must not
        # misalign the results zip and evict the wrong connection.
        clients = list(self.clients)
        results = await asyncio.gather(
            *(c.send(data) for c in clients), return_exceptions=True)
        for c, r in zip(clients, results):
            if isinstance(r, Exception):
                self.clients.discard(c)
                # Close the socket so the peer LEARNS it was dropped and can
                # reconnect — a silently-evicted client reads as "the bot died".
                asyncio.get_running_loop().create_task(self._safe_close(c))

    @staticmethod
    async def _safe_close(ws: ServerConnection) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


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
                        # record the boss's own line so a reload shows both sides
                        transport.record({"type": "message", "thread_id": thread_id,
                                          "speaker": {"role": "you", "name": "You",
                                                      "emoji": ""}, "text": text})
                        await engine.on_inbound(InboundMessage(
                            thread_id=thread_id, text=text,
                            sender_name=str(msg.get("sender_name") or "the boss")))
                elif mtype == "interrupt":
                    tid = str(msg.get("thread_id") or "").strip()
                    if tid:
                        ok = await engine.interrupt(tid)
                        await transport.post(Outbound(
                            thread_id=tid, speaker=SYSTEM,
                            text="⏹ interrupting…" if ok else "Nothing running here."))
                elif mtype == "kill":
                    tid = str(msg.get("thread_id") or "").strip()
                    if tid == OFFICE or tid.startswith("dm:"):
                        # killing the office would wipe the orchestrator's memory
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM,
                            text="Can't kill the orchestrator's office — "
                                 "/reset confirm is the full wipe."))
                    elif tid:
                        await engine.kill(tid)
                        await transport.post(Outbound(
                            thread_id=tid, speaker=SYSTEM, text="🗑 Session ended."))
                        await transport.close_thread(tid)
                elif mtype == "new":
                    path = str(msg.get("path") or "").strip()
                    if not path:
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM,
                            text="Usage: /new <path> [name]"))
                    else:
                        result = await engine.new_direct(
                            path, str(msg.get("name") or "").strip() or None)
                        if isinstance(result, str):  # an error message
                            await transport.post(Outbound(
                                thread_id=OFFICE, speaker=SYSTEM, text=result))
                        else:
                            tid, title = result
                            await transport.post(Outbound(
                                thread_id=tid, speaker=SYSTEM,
                                text=f"✅ direct session ready: {title}. Type to talk to it."))
                elif mtype in ("approve", "reject"):
                    wid = str(msg.get("worker_id") or "").strip()
                    if wid:
                        fn = (engine.approve_delivery if mtype == "approve"
                              else engine.reject_delivery)
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM, text=await fn(wid)))
                elif mtype == "reset":
                    if msg.get("confirm") is True:
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM,
                            text=await engine.factory_reset()))
                    else:
                        await transport.post(Outbound(
                            thread_id=OFFICE, speaker=SYSTEM,
                            text="🏭 Factory reset wipes ALL bot memory and state. "
                                 "This cannot be undone — send /reset confirm."))
        finally:
            transport.unregister(ws)

    return handler


_ASSET_TYPES = {".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8"}


def _load_assets(web_dir: Path | None) -> dict[str, tuple[str, bytes]]:
    """Read the app shell (index.html, client.js) into memory once, keyed by URL
    path. Empty if no dir — then the port speaks WebSocket only, as before."""
    assets: dict[str, tuple[str, bytes]] = {}
    if web_dir is None:
        return assets
    for f in web_dir.glob("*"):
        if f.is_file() and f.suffix in _ASSET_TYPES:
            assets["/" + f.name] = (_ASSET_TYPES[f.suffix], f.read_bytes())
    return assets


def _gatekeeper(token: str, allowed_origins: set[str],
                assets: dict[str, tuple[str, bytes]]):
    """One port, two jobs.

    A plain browser GET (no `Upgrade: websocket`) is served the app shell —
    index.html / client.js — so the operator opens a real `http://…/?token=…` URL
    instead of juggling a `file://` path. The shell is inert without a valid socket,
    so it needs no auth of its own.

    A WebSocket upgrade is the capability, and is gated on both:

    - a cross-origin BROWSER page (the CSWSH → drive-by-RCE vector): browsers send
      an honest Origin header, so anything not in our allowlist is refused;
    - a NON-browser local process (no/forged Origin): gated by a required handshake
      token in the query string.

    A localhost bind alone is NOT a boundary against either — this is.
    """
    def process_request(connection: ServerConnection, request):
        is_ws = "websocket" in request.headers.get("Upgrade", "").lower()
        if not is_ws:                       # ordinary HTTP → serve the static shell
            path = urlsplit(request.path).path
            if path == "/":
                path = "/index.html"
            asset = assets.get(path)
            if asset is None:
                return connection.respond(404, "not found\n")
            ctype, body = asset
            headers = Headers()
            headers["Content-Type"] = ctype
            headers["Content-Length"] = str(len(body))
            return Response(200, "OK", headers, body)

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
                        host: str, port: int, token: str,
                        web_dir: Path | None = None) -> None:
    """Run the server until cancelled: WebSocket (Origin + token gated) plus the
    static app shell over HTTP on the same port."""
    allowed = {"null", f"http://{host}:{port}",
               f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    assets = _load_assets(web_dir)
    try:  # show the idle status board from the moment we're up (parity with Telegram)
        await engine._refresh_dashboard()
    except Exception:  # noqa: BLE001
        pass
    async with serve(make_handler(engine, transport), host, port,
                     process_request=_gatekeeper(token, allowed, assets)):
        log.info("websocket transport listening on ws://%s:%s", host, port)
        await asyncio.Future()  # run forever
