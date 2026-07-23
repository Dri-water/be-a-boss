import json

from beaboss.core.store import CoreStore, ThreadRecord


def test_roundtrip_and_reload(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("100", ThreadRecord(role="direct", name="a", cwd="/w/a"))
    assert s.get("100").name == "a"
    assert s.get("100").created_at > 0

    s.update("100", session_id="sid-1")

    reloaded = CoreStore(tmp_path / "state")
    assert reloaded.get("100").session_id == "sid-1"
    assert set(reloaded.all().keys()) == {"100"}


def test_orchestrator_thread_persists(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    s.set_orchestrator_thread("general")
    reloaded = CoreStore(tmp_path / "state")
    assert reloaded.orchestrator_thread == "general"
    assert reloaded.get("general").role == "orchestrator"


def test_workers_filter_and_fields(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="d", cwd="/r"))
    s.put("2", ThreadRecord(role="worker", name="Nova", cwd="/wt", worker_id="nova",
                            repo="/r", task="fix bug", worker_status="working"))
    workers = s.workers()
    assert list(workers.keys()) == ["2"]
    rec = CoreStore(tmp_path / "state").get("2")
    assert rec.worker_id == "nova" and rec.task == "fix bug"


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
