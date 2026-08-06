"""Codex app-server translation tests — no binary, subprocess, or network."""

import asyncio
from pathlib import Path

import pytest

from beaboss.core.agent_backend import (
    AgentInit,
    AgentOutput,
    AgentResult,
    AgentTool,
    CodexBackend,
    ToolNamespace,
)
from beaboss.core.session import Turn


def _drain(backend: CodexBackend, events: list[dict]) -> list:
    async def go():
        for event in events:
            await backend._events.put(event)
        return [message async for message in backend.receive()]
    return asyncio.run(go())


def test_translation_yields_neutral_events():
    backend = CodexBackend(Path("."), resume_id="019-abc")
    backend._emit_init = True
    messages = _drain(backend, [
        {"method": "item/completed", "params": {"item": {
            "type": "reasoning", "text": "thinking"}}},
        {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer", "text": "PONG"}}},
        {"method": "turn/completed", "params": {"turn": {
            "status": "completed", "id": "turn-1"}}},
    ])

    assert [type(message) for message in messages] == [
        AgentInit, AgentOutput, AgentResult]
    assert messages[0].session_id == "019-abc"
    assert messages[1].text == "PONG"
    assert messages[2].is_error is False
    assert messages[2].result == "PONG"


def test_receive_surfaces_app_server_exit():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    messages = _drain(backend, [{
        "method": "backend/eof", "params": {"error": "authentication failed"}}])
    assert len(messages) == 1
    assert isinstance(messages[0], AgentResult)
    assert messages[0].is_error is True
    assert "authentication failed" in (messages[0].result or "")


def test_missing_tool_result_exit_rotates_desynchronized_thread():
    backend = CodexBackend(Path("."), resume_id="thread-stale")
    messages = _drain(backend, [{
        "method": "backend/eof", "params": {"error": (
            "Codex app-server exited -7: Custom tool call output is missing "
            "for call id: call_123"
        )}}])

    assert messages[-1].is_error is True
    assert backend._rotate_before_next_send is True


def test_receive_stops_at_turn_completed():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    messages = _drain(backend, [
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": "leaked"}}},
    ])
    assert [type(message) for message in messages] == [AgentResult]


def test_exhausted_stream_reconnect_rotates_thread_and_reaches_retry_layer():
    backend = CodexBackend(Path("."), resume_id="thread-stale")
    message = (
        "Reconnecting... 5/5 · responseStreamDisconnected · "
        "stream disconnected before completion: Broken pipe"
    )
    messages = _drain(backend, [
        {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer", "text": message}}},
        {"method": "turn/completed", "params": {"turn": {
            "status": "completed", "id": "turn-1"}}},
    ])

    result = messages[-1]
    assert isinstance(result, AgentResult) and result.is_error
    assert result.subtype == "stream_disconnected"
    assert "responseStreamDisconnected" in (result.result or "")
    assert backend._rotate_before_next_send is True


def test_nonterminal_reconnect_progress_is_not_treated_as_failure():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    messages = _drain(backend, [
        {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "phase": "final_answer",
            "text": "Reconnecting... 1/5"}}},
        {"method": "turn/completed", "params": {"turn": {
            "status": "completed"}}},
    ])
    assert messages[-1].is_error is False
    assert backend._rotate_before_next_send is False


def test_dynamic_tools_are_namespaced_and_dispatched():
    called: list[dict] = []

    async def echo(args):
        called.append(args)
        return {"content": [{"type": "text", "text": f"echo:{args['value']}"}]}

    namespace = ToolNamespace(
        "fleet", "fleet operations",
        (AgentTool("echo", "echo input", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"]}, echo),))
    backend = CodexBackend(Path("."), tool_namespaces=(namespace,))
    writes: list[dict] = []

    async def capture(payload):
        writes.append(payload)

    backend._write = capture
    asyncio.run(backend._handle_tool_request({
        "id": 7,
        "params": {"namespace": "fleet", "tool": "echo",
                   "arguments": {"value": "ok"}},
    }))

    assert backend._dynamic_tools()[0]["name"] == "fleet"
    assert called == [{"value": "ok"}]
    assert writes == [{"id": 7, "result": {
        "contentItems": [{"type": "inputText", "text": "echo:ok"}],
        "success": True}}]

    # Installed app-server accepts definitions on start and restores them from the
    # rollout on resume; ThreadResumeParams does not accept a dynamicTools override.
    fresh = CodexBackend(Path("."), tool_namespaces=(namespace,))
    resumed = CodexBackend(Path("."), resume_id="thread-1",
                           tool_namespaces=(namespace,))
    assert fresh._thread_options(
        include_dynamic_tools=True)["dynamicTools"][0]["name"] == "fleet"
    assert "dynamicTools" not in resumed._thread_options(
        include_dynamic_tools=False)


def test_steer_uses_active_turn_precondition():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    backend._proc = object()  # only liveness is consulted; no subprocess I/O here
    backend._active_turn_id = "turn-7"
    requests: list[tuple[str, dict]] = []

    async def request(method, params):
        requests.append((method, params))
        return {"turnId": "turn-7"}

    backend._request = request
    accepted = asyncio.run(backend.steer(Turn("change direction")))

    assert accepted is True
    assert requests == [("turn/steer", {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "change direction"}],
        "expectedTurnId": "turn-7",
    })]


def test_reasoning_effort_is_applied_as_sticky_turn_override():
    backend = CodexBackend(
        Path("."), resume_id="thread-1", model="gpt-5.6-terra",
        reasoning_effort="medium")
    requests: list[tuple[str, dict]] = []

    async def request(method, params):
        requests.append((method, params))
        return {"turn": {"id": "turn-1"}}

    backend._request = request
    asyncio.run(backend.send(Turn("do it")))

    method, params = requests[0]
    assert method == "turn/start"
    assert params["effort"] == "medium"
    assert "effort" not in backend._thread_options(include_dynamic_tools=True)


def test_profile_validation_rejects_unavailable_or_unsupported_config():
    backend = CodexBackend(
        Path("."), model="gpt-good", reasoning_effort="high")

    async def unavailable(_method, _params):
        return {"data": [{"id": "gpt-other"}]}

    backend._request = unavailable
    with pytest.raises(RuntimeError, match="gpt-good.*unavailable"):
        asyncio.run(backend._validate_profile())

    async def unsupported(_method, _params):
        return {"data": [{
            "id": "gpt-good",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low"}, {"reasoningEffort": "medium"}],
        }]}

    backend._request = unsupported
    with pytest.raises(RuntimeError, match="high.*not supported"):
        asyncio.run(backend._validate_profile())


def test_steer_race_falls_back_without_losing_input():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    backend._proc = object()
    backend._active_turn_id = "turn-old"

    async def request(_method, _params):
        raise RuntimeError("expectedTurnId does not match active turn")

    backend._request = request
    assert asyncio.run(backend.steer(Turn("do not lose me"))) is False
    assert backend._rotate_before_next_send is True


def test_active_turn_mismatch_rotates_before_queued_input():
    backend = CodexBackend(Path("."), resume_id="thread-stale")
    backend._rotate_before_next_send = True
    starts: list[str | None] = []
    requests: list[tuple[str, dict]] = []

    async def start():
        starts.append(backend._thread_id)
        backend._thread_id = "thread-fresh"

    async def request(method, params):
        requests.append((method, params))
        return {"turn": {"id": "turn-fresh"}}

    backend.start = start
    backend._request = request
    asyncio.run(backend.send(Turn("answer the latest request")))

    assert starts == [None]
    assert backend.session_id == "thread-fresh"
    assert backend._rotate_before_next_send is False
    method, params = requests[0]
    assert method == "turn/start" and params["threadId"] == "thread-fresh"
    text = params["input"][0]["text"]
    assert "THREAD RESYNCHRONIZATION" in text
    assert text.endswith("answer the latest request")


def test_transient_steer_failure_keeps_existing_thread():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    backend._proc = object()
    backend._active_turn_id = "turn-1"

    async def request(_method, _params):
        raise RuntimeError("connection timed out")

    backend._request = request
    assert asyncio.run(backend.steer(Turn("keep me queued"))) is False
    assert backend._rotate_before_next_send is False


def test_structured_codex_error_kind_reaches_retry_layer():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    messages = _drain(backend, [
        {"method": "error", "params": {"error": {
            "message": "Please try later",
            "codexErrorInfo": {"type": "UsageLimitExceeded"},
        }}},
        {"method": "turn/completed", "params": {"turn": {
            "status": "failed", "error": None,
        }}},
    ])
    result = messages[-1]
    assert isinstance(result, AgentResult) and result.is_error
    assert "UsageLimitExceeded" in (result.result or "")


def test_unknown_server_request_is_rejected_instead_of_hanging():
    backend = CodexBackend(Path("."), resume_id="thread-1")
    writes: list[dict] = []

    async def capture(payload):
        writes.append(payload)

    backend._write = capture
    asyncio.run(backend._reject_unsupported_request({
        "id": 9, "method": "item/tool/requestUserInput", "params": {}}))
    assert writes[0]["id"] == 9
    assert writes[0]["error"]["code"] == -32601


def test_approval_policy_uses_current_app_server_wire_values():
    assert CodexBackend(Path("."), permission_mode="bypassPermissions")._approval_policy() == "never"
    assert CodexBackend(Path("."), permission_mode="acceptEdits")._approval_policy() == "on-request"
    assert CodexBackend(Path("."), permission_mode="default")._approval_policy() == "untrusted"
