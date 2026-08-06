"""Prompt invariants that protect stateful fleet workflows."""

from beaboss.core.prompts import DELIVERY_CONSERVATIVE, PROJECT_MANAGER_APPEND


def test_conservative_ready_work_must_create_persisted_approval_request():
    prompt = DELIVERY_CONSERVATIVE.lower()
    assert "must call deliver_worker in that same turn" in prompt
    assert "merely telling the boss it is ready does not create" in prompt


def test_project_manager_prompt_enforces_project_scope_and_authority_boundary():
    prompt = PROJECT_MANAGER_APPEND.lower()

    assert "project manager" in prompt
    assert "one repository" in prompt
    assert "do not edit project code" in prompt
    assert "another manager's worker" in prompt
    assert "report upward" in prompt
    assert "cannot deliver" in prompt
    assert "global orchestrator must use its delivery controls" in prompt


def test_project_manager_prompt_explains_transparent_hibernation():
    prompt = PROJECT_MANAGER_APPEND.lower()

    assert "hibernat" in prompt
    assert "persist" in prompt
    assert "session" in prompt
    assert "continue" in prompt
