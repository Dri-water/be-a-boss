"""Owns the set of live sessions and routes messages to them.

Sessions are resumed lazily: on restart the store still holds each topic's
session_id + cwd, but we don't respawn a Claude process until a message actually
arrives in that topic (or /list is asked to show it).
"""

from __future__ import annotations

import logging
from pathlib import Path

from .claude_session import ClaudeSession, Emitter
from .config import Settings
from .store import SessionRecord, Store

log = logging.getLogger("tasm.manager")


class SessionManager:
    def __init__(self, settings: Settings, store: Store, emitter: Emitter):
        self.settings = settings
        self.store = store
        self.emitter = emitter
        self.sessions: dict[int, ClaudeSession] = {}

    def _make(self, thread_id: int, rec: SessionRecord) -> ClaudeSession:
        def on_sid(sid: str) -> None:
            self.store.update_session_id(thread_id, sid)

        return ClaudeSession(
            thread_id=thread_id,
            cwd=Path(rec.cwd),
            name=rec.name,
            settings=self.settings,
            emitter=self.emitter,
            on_session_id=on_sid,
            session_id=rec.session_id,
        )

    async def create(self, thread_id: int, cwd: Path, name: str) -> ClaudeSession:
        rec = SessionRecord(cwd=str(cwd), name=name, session_id=None)
        self.store.put(thread_id, rec)
        session = self._make(thread_id, rec)
        self.sessions[thread_id] = session
        await session.start()
        return session

    async def _ensure_live(self, thread_id: int) -> ClaudeSession | None:
        """Return the live session for a topic, lazily resuming it if dormant."""
        session = self.sessions.get(thread_id)
        if session is not None:
            return session
        rec = self.store.get(thread_id)
        if rec is None:
            return None
        session = self._make(thread_id, rec)
        self.sessions[thread_id] = session
        await session.start()  # resumes via rec.session_id
        return session

    async def route(self, thread_id: int, text: str) -> bool:
        """True if the topic is a session (message handled), False otherwise."""
        session = await self._ensure_live(thread_id)
        if session is None:
            return False
        if session.status == "busy":
            await self.emitter.send(
                thread_id, f"⏳ busy — queued (#{session.pending + 1})"
            )
        await session.submit(text)
        return True

    async def route_media(self, thread_id: int, caption: str, items: list) -> bool:
        """Route downloaded media (images/files) to a session. True if handled."""
        session = await self._ensure_live(thread_id)
        if session is None:
            return False
        if session.status == "busy":
            await self.emitter.send(
                thread_id, f"⏳ busy — queued (#{session.pending + 1})"
            )
        await session.submit_media(caption, items)
        return True

    async def interrupt(self, thread_id: int) -> bool:
        session = self.sessions.get(thread_id)
        if session is None:
            return False
        await session.interrupt()
        return True

    async def kill(self, thread_id: int) -> bool:
        session = self.sessions.pop(thread_id, None)
        existed = session is not None or self.store.get(thread_id) is not None
        if session is not None:
            await session.stop()
        self.store.delete(thread_id)
        return existed

    def listing(self) -> list[tuple[int, SessionRecord, str]]:
        """(thread_id, record, live-status) for every known topic."""
        rows: list[tuple[int, SessionRecord, str]] = []
        for thread_id, rec in self.store.all().items():
            live = self.sessions.get(thread_id)
            status = live.status if live else "dormant"
            rows.append((thread_id, rec, status))
        return rows

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()
