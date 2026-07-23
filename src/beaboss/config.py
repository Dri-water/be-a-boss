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


def _agent_setting(knob: str, backend: str) -> tuple[str, str]:
    """Resolve a backend-neutral agent tuning knob.

    The canonical name is ``AGENT_<KNOB>`` (e.g. AGENT_MODEL) — the backend
    distinction is never first-class. A harness-specific ``<BACKEND>_<KNOB>``
    (e.g. CLAUDE_MODEL, CODEX_MODEL) overrides it, but only when that backend is
    active. Returns (value, source_var) so errors can name the var actually used.
    """
    specific_name = f"{backend.upper()}_{knob}"
    specific = os.getenv(specific_name, "").strip()
    if specific:
        return specific, specific_name
    return os.getenv(f"AGENT_{knob}", "").strip(), f"AGENT_{knob}"


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
    # How work lands. "balanced" (default): the orchestrator may merge/PR directly
    # once the boss has clearly told it to ("merge it", "ship it") — a soft,
    # conversational gate, right for greenfield/solo. "conservative": nothing lands
    # without an explicit programmatic /approve the LLM can't forge.
    deploy_braveness: str = "balanced"

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

        # Agent tuning is backend-neutral (AGENT_*), with optional per-backend
        # overrides (CLAUDE_*/CODEX_*) that win only when that backend is active.
        backend = os.getenv("BEABOSS_BACKEND", "").strip().lower() or "claude"
        model, _ = _agent_setting("MODEL", backend)
        cli_path, _ = _agent_setting("CLI_PATH", backend)
        perm, _ = _agent_setting("PERMISSION_MODE", backend)
        max_turns_raw, max_turns_var = _agent_setting("MAX_TURNS", backend)

        return cls(
            bot_token=token,
            allowed_user_ids=allowed,
            chat_id=_parse_int_env("TELEGRAM_CHAT_ID", chat_raw, "-100123456789")
            if chat_raw
            else None,
            permission_mode=perm or "bypassPermissions",
            projects_root=Path(
                os.getenv("PROJECTS_ROOT", str(Path.home()))
            ).expanduser(),
            cli_path=(cli_path or None),
            model=(model or None),
            max_turns=_parse_int_env(max_turns_var, max_turns_raw, "12")
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
            agent_backend=backend,
            deploy_braveness=(
                "conservative"
                if os.getenv("DEPLOY_BRAVENESS", "").strip().lower() == "conservative"
                else "balanced"),
        )
