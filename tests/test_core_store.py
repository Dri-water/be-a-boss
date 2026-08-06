import json

from beaboss.core.store import CoreStore, ProjectRecord, ThreadRecord


def test_roundtrip_and_reload(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("100", ThreadRecord(role="direct", name="a", cwd="/w/a"))
    assert s.get("100").name == "a"
    assert s.get("100").created_at > 0

    s.update("100", session_id="sid-1")

    reloaded = CoreStore(tmp_path / "state")
    assert reloaded.get("100").session_id == "sid-1"
    assert set(reloaded.all().keys()) == {"100"}


def test_provider_native_sessions_and_models_roundtrip(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("100", ThreadRecord(
        role="worker", name="Nova", backend="codex", session_id="cx-2",
        session_ids={"claude": "cl-1", "codex": "cx-2"},
        tier="balanced", model="gpt-current",
        models={"claude": "opus", "codex": "gpt-current"},
        reasoning_effort="medium",
        reasoning_efforts={"codex": "medium"}))

    rec = CoreStore(tmp_path / "state").get("100")
    assert rec.backend == "codex"
    assert rec.session_ids == {"claude": "cl-1", "codex": "cx-2"}
    assert rec.models == {"claude": "opus", "codex": "gpt-current"}
    assert rec.tier == "balanced"
    assert rec.reasoning_effort == "medium"
    assert rec.reasoning_efforts == {"codex": "medium"}


def test_orchestrator_thread_persists(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    s.set_orchestrator_thread("general")
    reloaded = CoreStore(tmp_path / "state")
    assert reloaded.orchestrator_thread == "general"
    assert reloaded.get("general").role == "orchestrator"


def test_last_boss_thread_persists(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.set_last_boss_thread("dm:42")
    assert CoreStore(tmp_path / "state").last_boss_thread == "dm:42"


def test_workers_filter_and_fields(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="d", cwd="/r"))
    s.put("2", ThreadRecord(role="worker", name="Nova", cwd="/wt", worker_id="nova",
                            repo="/r", task="fix bug", worker_status="working"))
    workers = s.workers()
    assert list(workers.keys()) == ["2"]
    rec = CoreStore(tmp_path / "state").get("2")
    assert rec.worker_id == "nova" and rec.task == "fix bug"


def test_project_manager_hierarchy_fields_roundtrip_and_filters(tmp_path):
    """Project ownership must survive a restart without changing worker lookup."""
    s = CoreStore(tmp_path / "state")
    s.put("20", ThreadRecord(
        role="project_manager", name="Maya", cwd="/state/managers/maya-home",
        manager_id="maya", manager_status="active", repo="/r/app",
        task="Own the app project", last_summary="Release candidate is green",
    ))
    s.put("21", ThreadRecord(
        role="worker", name="Nova", cwd="/wt/nova", worker_id="nova",
        repo="/r/app", task="Fix checkout", worker_status="working",
        supervisor_id="maya",
    ))
    s.put("22", ThreadRecord(role="direct", name="Scratch", cwd="/r/app"))

    reloaded = CoreStore(tmp_path / "state")
    manager = reloaded.get("20")
    worker = reloaded.get("21")

    assert manager.manager_id == "maya"
    assert manager.manager_status == "active"
    assert manager.last_summary == "Release candidate is green"
    assert worker.supervisor_id == "maya"
    assert list(reloaded.managers()) == ["20"]
    assert list(reloaded.workers()) == ["21"]
    # Additive migration preserves every thread while introducing stable projects.
    assert manager.project_id == "maya"
    assert worker.project_id == "maya"
    assert reloaded.projects()["maya"].repos == ["/r/app"]


def test_outcome_project_roundtrip_supports_multiple_repositories(tmp_path):
    store = CoreStore(tmp_path / "state")
    store.put_project(ProjectRecord(
        project_id="checkout", name="Checkout", charter="Ship unified checkout",
        repos=["/r/web", "/r/api"], manager_id="checkout",
        manager_thread="20",
    ))

    project = CoreStore(tmp_path / "state").projects()["checkout"]
    assert project.name == "Checkout"
    assert project.repos == ["/r/web", "/r/api"]
    assert project.charter == "Ship unified checkout"


def test_organization_projection_is_separate_atomic_json(tmp_path):
    store = CoreStore(tmp_path / "state")
    organization = {"version": 1, "projects": [{"id": "checkout"}]}
    store.write_organization(organization)

    assert json.loads(store.organization_path.read_text(encoding="utf-8")) == organization
    assert not store.organization_path.with_suffix(".json.tmp").exists()


def test_legacy_records_default_project_hierarchy_fields(tmp_path):
    """The additive hierarchy migration must load a v1 record written pre-manager."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "core.json").write_text(json.dumps({
        "version": 1,
        "threads": {
            "7": {
                "role": "worker",
                "name": "Nova",
                "worker_id": "nova",
                "repo": "/r/app",
                "worker_status": "working",
            },
        },
    }))

    rec = CoreStore(state).get("7")

    assert rec.supervisor_id == ""
    assert rec.manager_id == ""
    assert rec.manager_status == ""
    assert rec.last_summary == ""


def test_delete_and_update_unknown(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="x", cwd="/w"))
    s.delete("1")
    assert s.get("1") is None
    s.update("missing", session_id="sid")  # no raise, no create
    assert s.get("missing") is None


def test_flush_writes_schema_version(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="x"))
    raw = json.loads((tmp_path / "state" / "core.json").read_text())
    assert raw["version"] == 1


def test_corrupt_state_is_quarantined_not_wiped(tmp_path):
    """A corrupt core.json is preserved (not silently overwritten with empty
    state) and the store starts fresh, so the org can be recovered by hand."""
    d = tmp_path / "state"
    d.mkdir()
    (d / "core.json").write_text("{ this is not valid json")
    s = CoreStore(d)
    assert s.all() == {}                                  # started fresh
    assert len(list(d.glob("core.json.corrupt-*"))) == 1  # bad file preserved


def test_newer_schema_is_refused_not_mangled(tmp_path):
    """State written by a newer (self-developed) version isn't loaded with older
    code — it's quarantined, not silently misread."""
    d = tmp_path / "state"
    d.mkdir()
    (d / "core.json").write_text(
        '{"version": 999, "threads": {"1": {"role": "worker", "name": "X"}}}')
    s = CoreStore(d)
    assert s.all() == {}
    assert list(d.glob("core.json.corrupt-*"))


def test_wipe_clears_everything_and_persists(tmp_path):
    from beaboss.core.store import CoreStore, ThreadRecord
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="d"))
    s.set_orchestrator_thread("1")
    s.set_last_boss_thread("dm:42")
    s.set_dashboard_msg_id(42)
    s.wipe()
    reloaded = CoreStore(tmp_path / "state")
    assert reloaded.all() == {}
    assert reloaded.orchestrator_thread is None
    assert reloaded.last_boss_thread is None
    assert reloaded.dashboard_msg_id is None


def test_pending_delivery_persists(tmp_path):
    from beaboss.core.store import CoreStore
    s = CoreStore(tmp_path / "state")
    pending = {
        "nova": {"method": "merge", "sha": "abc123", "base_sha": "def456"}}
    s.set_pending_delivery(pending)
    reloaded = CoreStore(tmp_path / "state")
    assert reloaded.pending_delivery == pending   # /approve survives restart


def test_pending_boss_turns_persist_until_acknowledged(tmp_path):
    s = CoreStore(tmp_path / "state")
    first = s.enqueue_boss_turn("dm:42", "build it")
    second = s.enqueue_boss_turn("general", "and test it")

    reloaded = CoreStore(tmp_path / "state")
    assert [v["id"] for v in reloaded.pending_boss_turns] == [first, second]
    assert reloaded.pending_boss_turns[0]["text"] == "build it"

    reloaded.acknowledge_boss_turns([first])
    assert [v["id"] for v in CoreStore(tmp_path / "state").pending_boss_turns] == [second]
