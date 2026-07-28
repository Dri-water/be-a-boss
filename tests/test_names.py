from beaboss.core.names import (
    DEFAULT_WORKER_NAMES,
    parse_worker_names,
    pick_name,
    worker_id_for,
)


def test_default_pool_retains_the_neutral_fallback_names():
    assert len(DEFAULT_WORKER_NAMES) == 16
    assert len({name.casefold() for name in DEFAULT_WORKER_NAMES}) == 16
    assert DEFAULT_WORKER_NAMES[:3] == ("Nova", "Kite", "Juno")


def test_full_display_name_gets_git_safe_worker_id():
    assert worker_id_for("Alice Smith") == "alice-smith"
    assert worker_id_for("Charlie Lee") == "charlie-lee"


def test_pick_name_checks_legacy_names_and_new_slugs():
    pool = ("Alice Smith", "Bob Jones")
    assert pick_name({"nova", "alice-smith"}, pool) == "Bob Jones"
    assert pick_name({"alice smith", "bob-jones"}, pool) == "Alice Smith2"


def test_empty_override_keeps_default_pool():
    assert parse_worker_names(" , , ") is DEFAULT_WORKER_NAMES
