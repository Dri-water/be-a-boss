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


def test_coders_filter_and_fields(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="d", cwd="/r"))
    s.put("2", ThreadRecord(role="coder", name="Nova", cwd="/wt", coder_id="nova",
                            repo="/r", task="fix bug", coder_status="working"))
    coders = s.coders()
    assert list(coders.keys()) == ["2"]
    rec = CoreStore(tmp_path / "state").get("2")
    assert rec.coder_id == "nova" and rec.task == "fix bug"


def test_delete_and_update_unknown(tmp_path):
    s = CoreStore(tmp_path / "state")
    s.put("1", ThreadRecord(role="direct", name="x", cwd="/w"))
    s.delete("1")
    assert s.get("1") is None
    s.update("missing", session_id="sid")  # no raise, no create
    assert s.get("missing") is None
