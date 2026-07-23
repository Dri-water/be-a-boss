"""Turn Claude Agent SDK messages into Telegram-ready text.

Kept as pure functions (SDK message -> list[str]) so they're trivially testable
and decoupled from the transport.

Formatting: Claude's output is markdown-ish. Telegram's MarkdownV2 is a minefield
(unescaped `_`/`*` anywhere → 400), but its HTML mode is forgiving: only <, > and &
need escaping, and stray markdown characters are harmless literals. So the
transport renders with `to_telegram_html` (code fences → <pre>, inline code →
<code>, **bold** → <b>, headers → bold, links → <a>) and falls back to plain text
if Telegram still rejects a message — pretty by default, robust always.
"""

from __future__ import annotations

import html as _html
import re

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
    # Status-neutral on purpose: "done" here would contradict a worker that just
    # reported "STATUS: blocked" in the same breath.
    return [f"— turn ended · {msg.num_turns} turns{cost}"]


def chunk(text: str, size: int = _CHUNK) -> list[str]:
    """Split a long string into Telegram-sized pieces, preferring newline breaks.

    Fence-aware: a chunk that would end inside an open ``` block gets the fence
    closed, and the next chunk reopens it — so every piece renders as valid
    formatting on its own.
    """
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        piece, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        if piece.count("```") % 2 == 1:  # split fell inside a code block
            piece += "\n```"
            remaining = "```\n" + remaining
        pieces.append(piece)
    if remaining:
        pieces.append(remaining)
    return pieces


# --- Telegram HTML rendering ---------------------------------------------------


def _md_inline(text: str) -> str:
    """Convert bold/headers/links in already-HTML-escaped text."""
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    return text


def _inline_html(text: str) -> str:
    """Escape a non-code segment, then convert inline code + simple markdown."""
    text = _html.escape(text)
    out: list[str] = []
    last = 0
    for m in re.finditer(r"`([^`\n]+)`", text):
        out.append(_md_inline(text[last:m.start()]))
        out.append(f"<code>{m.group(1)}</code>")
        last = m.end()
    out.append(_md_inline(text[last:]))
    return "".join(out)


_FENCE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.DOTALL)


def to_telegram_html(text: str) -> str:
    """Render markdown-ish agent output as Telegram HTML.

    Only constructs we can match with certainty are converted; everything else is
    escaped literal text, so a stray `_` or `*` can never break the message. The
    transport still falls back to plain text if Telegram rejects the result.
    """
    parts: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        parts.append(_inline_html(text[pos:m.start()]))
        code = _html.escape(m.group(2).rstrip("\n"))
        lang = m.group(1).strip()
        if lang:
            parts.append(f'<pre><code class="language-{_html.escape(lang)}">{code}</code></pre>')
        else:
            parts.append(f"<pre>{code}</pre>")
        pos = m.end()
    parts.append(_inline_html(text[pos:]))
    return "".join(parts)
