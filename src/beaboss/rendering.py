"""Turn Claude Agent SDK messages into Telegram-ready text.

Kept as pure functions (SDK message -> list[str]) so they're trivially testable
and decoupled from the transport. We send plain text (no parse_mode) on purpose:
Claude's output is full of backticks/underscores/asterisks that routinely break
Telegram's Markdown/HTML entity parser and cause 400s. Robustness > prettiness.
"""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

TELEGRAM_LIMIT = 4096
_CHUNK = 3900  # headroom under the hard limit


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def tool_line(name: str, tool_input: dict | None) -> str:
    """A compact one-liner describing a tool call, e.g. '🔧 Bash · npm test'."""
    ti = tool_input or {}

    def val(*keys: str) -> str | None:
        for k in keys:
            v = ti.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if v not in (None, "", [], {}):
                return str(v)
        return None

    preview: str | None
    if name in ("Bash", "bash", "shell"):
        preview = val("command")
    elif name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        preview = val("file_path", "path", "notebook_path")
    elif name in ("Grep", "Glob"):
        preview = val("pattern")
    elif name in ("WebFetch", "WebSearch"):
        preview = val("url", "query")
    elif name in ("Task", "Agent"):
        preview = val("description", "subagent_type")
    elif name == "TodoWrite":
        preview = None
    else:
        preview = val("description", "command", "query", "path")

    if preview:
        preview = _truncate(preview.replace("\n", " "), 220)
        return f"🔧 {name} · {preview}"
    return f"🔧 {name}"


def render_assistant(msg: AssistantMessage) -> list[str]:
    """Text blocks become chat messages; tool_use blocks become compact lines.

    Thinking blocks are intentionally dropped (too noisy for a phone).
    """
    out: list[str] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            text = block.text.strip()
            if text:
                out.append(text)
        elif isinstance(block, ToolUseBlock):
            out.append(tool_line(block.name, block.input))
    return out


def render_result(msg: ResultMessage) -> list[str]:
    """A subtle footer per turn, or a surfaced error."""
    if msg.is_error or (msg.subtype and msg.subtype != "success"):
        detail = msg.result
        if not detail and msg.errors:
            detail = "; ".join(str(e) for e in msg.errors)
        if not detail:
            detail = msg.subtype or "error"
        return [f"⚠️ {_truncate(str(detail), 1000)}"]

    cost = ""
    if msg.total_cost_usd:
        cost = f" · ${msg.total_cost_usd:.4f}"
    return [f"— done · {msg.num_turns} turns{cost}"]


def chunk(text: str, size: int = _CHUNK) -> list[str]:
    """Split a long string into Telegram-sized pieces, preferring newline breaks."""
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        pieces.append(remaining)
    return pieces
