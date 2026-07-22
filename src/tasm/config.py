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
        if part:
            ids.add(int(part))
    return ids


@dataclass
class Settings:
    bot_token: str
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

    @classmethod
    def from_env(cls, env_path: str | os.PathLike[str] | None = None) -> "Settings":
        load_dotenv(env_path)

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN is required. Copy .env.example to .env and fill it in."
            )

        allowed = _parse_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
        if not allowed:
            # Fail loud rather than silently accepting everyone or no one.
            raise SystemExit(
                "TELEGRAM_ALLOWED_USER_IDS is empty. Set at least your own Telegram user id "
                "(get it from @userinfobot) so the bot knows who may command it."
            )

        chat_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        max_turns_raw = os.getenv("CLAUDE_MAX_TURNS", "").strip()

        return cls(
            bot_token=token,
            allowed_user_ids=allowed,
            chat_id=int(chat_raw) if chat_raw else None,
            permission_mode=os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
            or "bypassPermissions",
            projects_root=Path(
                os.getenv("PROJECTS_ROOT", str(Path.home()))
            ).expanduser(),
            cli_path=(os.getenv("CLAUDE_CLI_PATH", "").strip() or None),
            model=(os.getenv("CLAUDE_MODEL", "").strip() or None),
            max_turns=int(max_turns_raw) if max_turns_raw else None,
            state_dir=Path(os.getenv("STATE_DIR", "state")).expanduser(),
            bot_name=os.getenv("BOT_NAME", "").strip() or DEFAULT_BOT_NAME,
            # None => use the built-in default note; empty string => no note at all.
            session_system_append=(
                os.environ["SESSION_SYSTEM_APPEND"]
                if "SESSION_SYSTEM_APPEND" in os.environ
                else None
            ),
        )
