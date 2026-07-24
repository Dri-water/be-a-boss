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

    # No cost figure: the agent runs on a Claude subscription (the mounted CLI auth),
    # not the pay-per-token API, so the SDK's total_cost_usd is an irrelevant estimate,
    # not a real charge — showing it just confuses. Status-neutral on purpose too:
    # "done" here would contradict a worker that just reported "STATUS: blocked".
    return [f"— turn ended · {msg.num_turns} turns"]


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
    """Convert links/bold/headers in already-escaped text (code spans stashed)."""
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    return text


def _inline_line(line: str) -> str:
    """One prose line → HTML. Code spans are stashed as placeholders first, so
    markdown AROUND them still converts (**bold with `code` inside** works) and
    markdown INSIDE them never does."""
    line = _html.escape(line)
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    line = re.sub(r"`([^`\n]+)`", stash, line)
    line = _md_inline(line)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{codes[int(m.group(1))]}</code>", line)


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _inline_html(text: str) -> str:
    """Escape a non-code segment and convert what we can match with certainty.
    Consecutive markdown-table lines render as <pre> — aligned columns instead of
    proportional-font pipe soup."""
    out: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if _is_table_line(lines[i]) and i + 1 < len(lines) and _is_table_line(lines[i + 1]):
            j = i
            while j < len(lines) and _is_table_line(lines[j]):
                j += 1
            out.append("<pre>" + _html.escape("\n".join(lines[i:j])) + "</pre>")
            i = j
            continue
        out.append(_inline_line(lines[i]))
        i += 1
    return "\n".join(out)


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
