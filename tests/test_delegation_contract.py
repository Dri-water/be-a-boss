from beaboss.config import Settings
from beaboss.core.engine import Engine
from beaboss.core.prompts import (
    ORCHESTRATOR_APPEND,
    PROJECT_MANAGER_APPEND,
    WORKER_APPEND_EXTRA,
)
from beaboss.core.store import CoreStore


def _engine(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    settings = Settings(
        bot_token=None,
        allowed_user_ids=set(),
        chat_id=None,
        permission_mode="bypassPermissions",
        projects_root=projects,
        cli_path=None,
        model=None,
        max_turns=None,
        state_dir=tmp_path / "state",
        bot_name="Boss",
        session_system_append=None,
    )
    return Engine(settings, CoreStore(settings.state_dir))


def test_role_prompts_share_one_scope_and_proof_contract():
    orchestrator = ORCHESTRATOR_APPEND.lower()
    manager = PROJECT_MANAGER_APPEND.lower()
    worker = WORKER_APPEND_EXTRA.lower()

    assert "boss's request defines the outcome and scope" in orchestrator
    assert "direct worker is the default" in orchestrator
    assert "several steps or is technically difficult" in orchestrator
    assert "essential behavior that could not be exercised is incomplete" in orchestrator

    assert "must not enlarge" in manager
    assert "never overlapping alternatives" in manager
    assert "missing essential proof means incomplete" in manager

    assert "delegated explanation cannot broaden" in worker
    assert "smallest coherent change" in worker
    assert "central requirement cannot be exercised" in worker


def test_tool_descriptions_make_direct_delegation_the_default(tmp_path):
    engine = _engine(tmp_path)
    tools = {tool.name: tool for tool in engine._build_fleet_tools().tools}

    project = tools["create_project"].description.lower()
    worker = tools["spawn_worker"].description.lower()

    assert "multiple genuinely independent worker tracks" in project
    assert "use spawn_worker directly" in project
    assert "default delegation for one coherent outcome" in worker
    assert "not new scope" in worker


def test_handoff_keeps_agent_interpretation_subordinate_to_boss_intent():
    prompt = Engine._brief_with_upstream_context(
        [
            {"speaker": "Jon", "text": "Make the existing service persistent."},
            {"speaker": "Manager", "text": "Build a generalized service platform."},
        ],
        "[Brief from the orchestrator]",
        "Implement the persistent service.",
    )

    assert "Boss messages define intent and scope" in prompt
    assert "Agent messages are context only" in prompt
    assert "Keep this delegation within the boss-defined outcome" in prompt
    assert prompt.index("Make the existing service persistent.") < prompt.index(
        "Implement the persistent service."
    )
