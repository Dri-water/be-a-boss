"""Persistent map of Telegram forum topic -> Claude session.

A single JSON file keyed by thread_id. Small enough that we rewrite it whole
on every change (atomic replace). Single-process, single event loop, so no lock
is required beyond not interleaving awaits mid-write (we don't).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SessionRecord:
    cwd: str
    name: str
    session_id: str | None = None
    created_at: float = 0.0


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "sessions.json"
        self._data: dict[str, SessionRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for k, v in raw.items():
            self._data[k] = SessionRecord(
                cwd=v.get("cwd", ""),
                name=v.get("name", ""),
                session_id=v.get("session_id"),
                created_at=v.get("created_at", 0.0),
            )

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        payload = {k: asdict(v) for k, v in self._data.items()}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, thread_id: int) -> SessionRecord | None:
        return self._data.get(str(thread_id))

    def all(self) -> dict[int, SessionRecord]:
        return {int(k): v for k, v in self._data.items()}

    def put(self, thread_id: int, record: SessionRecord) -> None:
        if not record.created_at:
            record.created_at = time.time()
        self._data[str(thread_id)] = record
        self._flush()

    def update_session_id(self, thread_id: int, session_id: str) -> None:
        rec = self._data.get(str(thread_id))
        if rec and rec.session_id != session_id:
            rec.session_id = session_id
            self._flush()

    def delete(self, thread_id: int) -> None:
        if self._data.pop(str(thread_id), None) is not None:
            self._flush()
