"""Environment-backed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import DEFAULT_BOT_NAME


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise SystemExit(
                f"TELEGRAM_ALLOWED_USER_IDS contains an invalid entry: {part!r}. "
                "It must be a comma-separated list of numeric Telegram user ids, "
                "e.g. 123456789,987654321 (DM the running bot /whoami to get yours)."
            ) from None
    return ids


def _parse_int_env(name: str, raw: str, example: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"{name} must be a whole number, but got {raw!r}. "
            f"For example: {name}={example}."
        ) from None


@dataclass
class Settings:
    bot_token: str | None
    allowed_user_ids: set[int]
    chat_id: int | None
    permission_mode: str
    projects_root: Path
    cli_path: str | None
    model: str | None
    max_turns: int | None
    state_dir: Path
    bot_name: str
    session_system_append: str | None
    agent_backend: str = "claude"  # "claude" (default) | "codex"

    @classmethod
    def from_env(cls, env_path: str | os.PathLike[str] | None = None) -> "Settings":
        load_dotenv(env_path)

        # Telegram-specific fields are optional at the config layer: the web surface
        # needs neither, so requiring them here would force a Telegram token on a
        # user who only wants the browser/VS Code surface. Each surface validates its
        # own needs instead — the Telegram surface requires a token and treats an
        # empty allowlist as setup mode (see transports/telegram.build_application).
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
        allowed = _parse_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))

        chat_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        max_turns_raw = os.getenv("CLAUDE_MAX_TURNS", "").strip()

        return cls(
            bot_token=token,
            allowed_user_ids=allowed,
            chat_id=_parse_int_env("TELEGRAM_CHAT_ID", chat_raw, "-100123456789")
            if chat_raw
            else None,
            permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
            or "bypassPermissions",
            projects_root=Path(
                os.getenv("PROJECTS_ROOT", str(Path.home()))
            ).expanduser(),
            cli_path=(os.getenv("CLAUDE_CLI_PATH", "").strip() or None),
            model=(os.getenv("CLAUDE_MODEL", "").strip() or None),
            max_turns=_parse_int_env("CLAUDE_MAX_TURNS", max_turns_raw, "12")
            if max_turns_raw
            else None,
            state_dir=Path(os.getenv("STATE_DIR", "state")).expanduser(),
            bot_name=os.getenv("BOT_NAME", "").strip() or DEFAULT_BOT_NAME,
            # None => use the built-in default note; empty string => no note at all.
            session_system_append=(
                os.environ["SESSION_SYSTEM_APPEND"]
                if "SESSION_SYSTEM_APPEND" in os.environ
                else None
            ),
            agent_backend=(os.getenv("BEABOSS_BACKEND", "").strip().lower() or "claude"),
        )
