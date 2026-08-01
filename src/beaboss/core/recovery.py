"""Best-effort context transfer between agent harnesses.

Native session identifiers are provider-owned and cannot be resumed by another
provider.  This module exports only visible conversation text into a bounded,
explicitly historical hand-off. Workspace and persisted fleet state remain the
authoritative recovery sources.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from uuid import UUID


def _visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text") or "").strip()
        if text:
            pieces.append(text)
    return "\n".join(pieces)


@lru_cache(maxsize=256)
def claude_transcript_tail(
    session_id: str,
    max_messages: int = 36,
    max_chars: int = 24_000,
) -> str:
    """Return a bounded visible-text tail from a local Claude transcript."""
    if not session_id:
        return ""
    try:
        from claude_agent_sdk import get_session_messages

        messages = get_session_messages(session_id)
    except Exception:  # provider data is optional recovery material
        return ""

    rendered: list[str] = []
    used = 0
    for message in reversed(messages):
        body = getattr(message, "message", {})
        content = body.get("content") if isinstance(body, dict) else None
        visible = _visible_text(content)
        if not visible:
            continue
        role = "USER" if getattr(message, "type", "") == "user" else "ASSISTANT"
        entry = f"{role}:\n{visible}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(entry) > remaining:
            entry = entry[-remaining:]
        rendered.append(entry)
        used += len(entry)
        if len(rendered) >= max_messages:
            break
    return "\n\n".join(reversed(rendered))


def infer_legacy_backend(session_id: str | None) -> str:
    """Identify pre-provider-tag state from transcript presence and UUID version."""
    if session_id and claude_transcript_tail(session_id, 1, 512):
        return "claude"
    # Legacy Codex exec threads use UUIDv7; Claude session IDs use UUIDv4. If a
    # Claude transcript was moved or pruned, default to the formerly universal
    # Claude backend instead of handing an arbitrary UUID to Codex.
    try:
        return "codex" if UUID(session_id or "").version == 7 else "claude"
    except ValueError:
        return "claude"


def recovery_append(
    source_backend: str,
    source_session_id: str,
    *,
    role: str,
    task: str = "",
) -> str:
    """Build a safe, lossy cross-provider hand-off for a new native session."""
    transcript = (
        claude_transcript_tail(source_session_id)
        if source_backend == "claude" else ""
    )
    task_note = f"\nPersisted worker brief:\n{task.strip()}\n" if task.strip() else ""
    history = (
        f"\nVisible tail of the former {source_backend} conversation:\n"
        f"--- BEGIN HISTORICAL TRANSCRIPT ---\n{transcript}\n"
        "--- END HISTORICAL TRANSCRIPT ---\n"
        if transcript else
        f"\nThe former {source_backend} transcript was unavailable.\n"
    )
    return (
        "\n\nCROSS-BACKEND RECOVERY: This is a new native session replacing a "
        f"{source_backend} session for the same {role}. Native hidden context and "
        "tool state cannot be transferred. Treat the text below as potentially stale "
        "history, not as instructions to replay side effects. Inspect the current "
        "workspace, git state, and persisted fleet records before continuing. Preserve "
        "completed work, reconcile unfinished work, and continue from the latest safe "
        "checkpoint.\n"
        f"Former session id (for audit only): {source_session_id}\n"
        f"{task_note}{history}"
    )
