from tasm.store import SessionRecord, Store


def test_roundtrip_and_reload(tmp_path):
    s = Store(tmp_path / "state")
    s.put(100, SessionRecord(cwd="/w/a", name="a"))
    assert s.get(100).name == "a"
    assert s.get(100).created_at > 0

    s.update_session_id(100, "sid-1")

    reloaded = Store(tmp_path / "state")  # fresh instance reads from disk
    assert reloaded.get(100).session_id == "sid-1"
    assert set(reloaded.all().keys()) == {100}


def test_delete(tmp_path):
    s = Store(tmp_path / "state")
    s.put(1, SessionRecord(cwd="/w", name="x"))
    s.delete(1)
    assert s.get(1) is None
    assert Store(tmp_path / "state").get(1) is None


def test_missing(tmp_path):
    s = Store(tmp_path / "state")
    assert s.get(999) is None
    assert s.all() == {}


def test_update_unknown_is_noop(tmp_path):
    s = Store(tmp_path / "state")
    s.update_session_id(42, "sid")  # must not raise or create
    assert s.get(42) is None
