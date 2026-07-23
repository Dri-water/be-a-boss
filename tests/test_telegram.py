import asyncio
from pathlib import Path

from telegram.ext import AIORateLimiter

from beaboss.config import Settings
from beaboss.core.ports import Outbound, Speaker
from beaboss.core.store import CoreStore
from beaboss.transports.telegram import (
    MAX_MSG_CHUNKS,
    TelegramTransport,
    build_application,
)


def _settings() -> Settings:
    return Settings(
        bot_token="123:abc", allowed_user_ids={1}, chat_id=1,
        permission_mode="bypassPermissions", projects_root=Path("."),
        cli_path=None, model=None, max_turns=None, state_dir=Path("state"),
        bot_name="Bot", session_system_append=None,
    )


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs["text"])


def test_rate_limiter_is_wired(tmp_path):
    # A burst of messages (a chunked diff, a chatty worker) must be paced + retried
    # on Telegram's 429, not dropped — so the app must carry a rate limiter.
    app = build_application(_settings(), CoreStore(tmp_path / "state"))
    assert isinstance(app.bot.rate_limiter, AIORateLimiter)


def test_post_truncates_runaway_output():
    # A massive dump is capped to a few messages with a pointer, not spammed as
    # dozens — and nothing exceeds Telegram's hard 4096 limit.
    bot = FakeBot()
    transport = TelegramTransport(bot, _settings())
    huge = "x" * 100_000  # ~26 chunks' worth, no newlines to break on
    out = Outbound(thread_id="general",
                   speaker=Speaker(role="direct", name="d"), text=huge)
    asyncio.run(transport.post(out))
    assert len(bot.sent) == MAX_MSG_CHUNKS
    assert "truncated" in bot.sent[-1]
    assert all(len(m) <= 4096 for m in bot.sent)


def test_post_normal_message_not_truncated():
    bot = FakeBot()
    transport = TelegramTransport(bot, _settings())
    out = Outbound(thread_id="general",
                   speaker=Speaker(role="direct", name="d"), text="a short reply")
    asyncio.run(transport.post(out))
    assert bot.sent == ["a short reply"]
