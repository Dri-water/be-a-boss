import asyncio
from pathlib import Path

from telegram.ext import AIORateLimiter

from beaboss.config import Settings
from beaboss.core.ports import Outbound, Speaker
from beaboss.core.store import CoreStore
from beaboss.transports.telegram import (
    MAX_MSG_CHUNKS,
    TelegramTransport,
    _ok,
    _thread_of,
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


class RecordingBot:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)


def test_route_maps_dm_general_and_topic():
    t = TelegramTransport(RecordingBot(), _settings())  # group chat_id=1
    assert t._route("dm:42") == (42, None)     # a DM → the user's chat, no topic
    assert t._route("general") == (1, None)    # #general → the group, no topic
    assert t._route("777") == (1, 777)         # a worker topic → the group + topic id


def test_post_to_dm_routes_to_user_chat():
    bot = RecordingBot()
    t = TelegramTransport(bot, _settings())
    asyncio.run(t.post(Outbound(
        thread_id="dm:42",
        speaker=Speaker(role="orchestrator", name="Lim", emoji="🧭"), text="hi")))
    assert bot.calls[0]["chat_id"] == 42                 # went to the user, not group 1
    assert bot.calls[0]["message_thread_id"] is None


class _U:
    def __init__(self, uid): self.id = uid; self.username = "u"; self.first_name = "U"


class _C:
    def __init__(self, cid, ctype): self.id = cid; self.type = ctype


class _M:
    def __init__(self, tid=None): self.message_thread_id = tid


class _Upd:
    def __init__(self, user, chat, msg):
        self.effective_user = user; self.effective_chat = chat
        self.effective_message = msg


class _Ctx:
    def __init__(self, settings, transport):
        self.bot_data = {"settings": settings, "transport": transport}


class DashBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.pinned: list[dict] = []
        self._next = 100

    async def send_message(self, **kw):
        self._next += 1
        self.sent.append(kw)
        return type("M", (), {"message_id": self._next})()

    async def edit_message_text(self, **kw):
        self.edited.append(kw)

    async def pin_chat_message(self, **kw):
        self.pinned.append(kw)


def test_dashboard_pins_once_then_edits_in_place(tmp_path):
    """First update creates + pins the board (id persisted); later updates edit the
    same message instead of spamming new ones."""
    store = CoreStore(tmp_path / "state")
    bot = DashBot()
    t = TelegramTransport(bot, _settings(), store)  # group chat_id=1

    asyncio.run(t.update_dashboard("board v1"))
    assert len(bot.sent) == 1 and bot.pinned          # created + pinned
    assert store.dashboard_msg_id == 101

    asyncio.run(t.update_dashboard("board v2"))
    assert len(bot.sent) == 1                          # no second message spawned
    assert bot.edited and bot.edited[-1]["text"] == "board v2"


def test_guard_accepts_allowlisted_dm_but_not_foreign_group():
    settings = _settings()  # allowed={1}, group chat_id=1
    ctx = _Ctx(settings, TelegramTransport(RecordingBot(), settings))

    # allowlisted user's DM → allowed; its office thread is 'dm:<uid>'
    dm = _Upd(_U(1), _C(999, "private"), _M())
    assert _ok(dm, ctx) is True
    assert _thread_of(dm) == "dm:1"

    # a stranger's DM → refused
    assert _ok(_Upd(_U(2), _C(2, "private"), _M()), ctx) is False

    # allowlisted user in some OTHER group → refused (only the bound group counts)
    assert _ok(_Upd(_U(1), _C(555, "supergroup"), _M()), ctx) is False

    # allowlisted user in the bound group → allowed; #general / topic threads
    grp = _Upd(_U(1), _C(1, "supergroup"), _M())
    assert _ok(grp, ctx) is True
    assert _thread_of(grp) == "general"
    assert _thread_of(_Upd(_U(1), _C(1, "supergroup"), _M(7))) == "7"
