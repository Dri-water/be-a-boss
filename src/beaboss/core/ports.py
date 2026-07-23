"""Contracts between the core engine and any chat transport.

A transport (Telegram, web, CLI, …) implements Transport and forwards every
human message to the engine's `on_inbound`. The core never formats
platform-specific text; the transport never holds session state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

Role = Literal["orchestrator", "worker", "direct", "system"]


@dataclass(frozen=True)
class Speaker:
    """Who is talking. Transports decide how to render this (header card,
    username, avatar…) — one bot account can carry many speakers."""

    role: Role
    name: str
    emoji: str = ""

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip()


SYSTEM = Speaker(role="system", name="system", emoji="ℹ️")


@dataclass
class MediaIn:
    """A file a human sent into a thread."""

    kind: str  # "image" | "file"
    filename: str
    mime: str | None
    data: bytes


@dataclass
class InboundMessage:
    """A human message, normalized by the transport."""

    thread_id: str
    text: str
    media: list[MediaIn] = field(default_factory=list)
    sender_name: str = ""


@dataclass
class Outbound:
    """Something the core wants shown in a thread."""

    thread_id: str
    speaker: Speaker
    text: str = ""
    media_path: Path | None = None
    media_kind: str | None = None  # "photo" | "video" | "document"
    caption: str | None = None


@runtime_checkable
class Transport(Protocol):
    """What the core needs from a chat platform."""

    async def create_thread(self, title: str) -> str: ...

    async def close_thread(self, thread_id: str) -> None: ...

    async def post(self, out: Outbound) -> None: ...

    async def indicate_busy(self, thread_id: str) -> None:
        """Best-effort 'typing…' hint; may be a no-op."""
        ...

    # Optional (getattr-gated by the engine): the turn-end counterpart to
    # indicate_busy. A surface that holds a persistent "working" indicator (the web
    # and CLI cockpits) implements this to clear it even when the turn posted nothing;
    # a surface whose typing hint self-expires (Telegram) can omit it entirely.
    # async def indicate_idle(self, thread_id: str) -> None: ...


class Engine(Protocol):
    """What a transport needs from the core."""

    async def on_inbound(self, msg: InboundMessage) -> None: ...
