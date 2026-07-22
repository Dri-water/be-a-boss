from types import SimpleNamespace

from claude_agent_sdk import TextBlock, ThinkingBlock, ToolUseBlock

from beaboss import rendering


def A(*blocks):
    """Fake AssistantMessage — render_assistant only reads .content."""
    return SimpleNamespace(content=list(blocks))


def R(**kw):
    """Fake ResultMessage."""
    base = dict(is_error=False, subtype="success", result=None, errors=None,
                total_cost_usd=None, num_turns=1)
    base.update(kw)
    return SimpleNamespace(**base)


def test_tool_line_bash():
    assert rendering.tool_line("Bash", {"command": "npm test"}) == "🔧 Bash · npm test"


def test_tool_line_file_paths():
    assert rendering.tool_line("Edit", {"file_path": "src/x.ts"}) == "🔧 Edit · src/x.ts"
    assert rendering.tool_line("Read", {"path": "a/b"}) == "🔧 Read · a/b"


def test_tool_line_no_preview():
    assert rendering.tool_line("TodoWrite", {"todos": []}) == "🔧 TodoWrite"
    assert rendering.tool_line("Mystery", {}) == "🔧 Mystery"


def test_tool_line_flattens_newlines_and_truncates():
    line = rendering.tool_line("Bash", {"command": "echo a\necho b " + "x" * 400})
    assert "\n" not in line
    assert line.endswith("…")


def test_render_assistant_text_and_tools():
    out = rendering.render_assistant(
        A(TextBlock(text="hi"), ToolUseBlock(id="1", name="Bash", input={"command": "ls"}))
    )
    assert out == ["hi", "🔧 Bash · ls"]


def test_render_assistant_drops_thinking_and_blank():
    out = rendering.render_assistant(
        A(ThinkingBlock(thinking="secret", signature="s"), TextBlock(text="   "))
    )
    assert out == []


def test_render_result_success_footer_with_cost():
    assert rendering.render_result(R(num_turns=3, total_cost_usd=0.0123)) == [
        "— done · 3 turns · $0.0123"
    ]


def test_render_result_success_footer_no_cost():
    assert rendering.render_result(R(num_turns=2)) == ["— done · 2 turns"]


def test_render_result_error_from_result():
    out = rendering.render_result(R(is_error=True, subtype="error_max_turns", result="hit the wall"))
    assert out[0].startswith("⚠️") and "hit the wall" in out[0]


def test_render_result_error_from_errors_list():
    out = rendering.render_result(R(is_error=True, subtype="error", errors=["boom", "bang"]))
    assert "boom" in out[0]


def test_chunk_small_passthrough():
    assert rendering.chunk("hello") == ["hello"]


def test_chunk_prefers_newline_and_loses_no_content():
    body = "\n".join(["line"] * 2000)
    parts = rendering.chunk(body)
    assert all(len(p) <= 3900 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == "line" * 2000


def test_chunk_hard_split_without_newline():
    parts = rendering.chunk("x" * 9000)
    assert len(parts) == 3
    assert all(len(p) <= 3900 for p in parts)
    assert "".join(parts) == "x" * 9000
