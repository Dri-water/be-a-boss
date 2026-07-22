import pytest

from beaboss.config import Settings

_KEYS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_CHAT_ID", "BOT_NAME",
    "CLAUDE_PERMISSION_MODE", "PROJECTS_ROOT", "CLAUDE_CLI_PATH", "CLAUDE_MODEL",
    "CLAUDE_MAX_TURNS", "STATE_DIR", "SESSION_SYSTEM_APPEND",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    # point load_dotenv at a nonexistent file so the real .env is never read
    return tmp_path / "nonexistent.env"


def test_missing_token_exits(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    with pytest.raises(SystemExit):
        Settings.from_env(clean_env)


def test_empty_allowlist_exits(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    with pytest.raises(SystemExit):
        Settings.from_env(clean_env)


def test_defaults_and_allowlist_parsing(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", " 1, 2 ,3, ")
    s = Settings.from_env(clean_env)
    assert s.allowed_user_ids == {1, 2, 3}
    assert s.bot_name == "Orchestrator"
    assert s.permission_mode == "bypassPermissions"
    assert s.session_system_append is None
    assert s.chat_id is None
    assert s.max_turns is None


def test_blank_token_exits(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    with pytest.raises(SystemExit) as excinfo:
        Settings.from_env(clean_env)
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_malformed_allowlist_exits(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,abc")
    with pytest.raises(SystemExit) as excinfo:
        Settings.from_env(clean_env)
    msg = str(excinfo.value)
    assert "TELEGRAM_ALLOWED_USER_IDS" in msg
    assert "abc" in msg


def test_bad_separator_allowlist_exits(clean_env, monkeypatch):
    # Space-separated instead of comma-separated is a common mistake.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123 456")
    with pytest.raises(SystemExit) as excinfo:
        Settings.from_env(clean_env)
    assert "TELEGRAM_ALLOWED_USER_IDS" in str(excinfo.value)


def test_non_numeric_chat_id_exits(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not-a-number")
    with pytest.raises(SystemExit) as excinfo:
        Settings.from_env(clean_env)
    assert "TELEGRAM_CHAT_ID" in str(excinfo.value)


def test_non_numeric_max_turns_exits(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("CLAUDE_MAX_TURNS", "lots")
    with pytest.raises(SystemExit) as excinfo:
        Settings.from_env(clean_env)
    assert "CLAUDE_MAX_TURNS" in str(excinfo.value)


def test_overrides(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("BOT_NAME", "My Bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("CLAUDE_MAX_TURNS", "12")
    monkeypatch.setenv("SESSION_SYSTEM_APPEND", "")  # explicit empty disables the note
    s = Settings.from_env(clean_env)
    assert s.bot_name == "My Bot"
    assert s.chat_id == -100123
    assert s.max_turns == 12
    assert s.session_system_append == ""
