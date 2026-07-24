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
    def __init__(self, reject_html: bool = False):
        self.calls: list[dict] = []
        self.reject_html = reject_html

    async def send_message(self, **kwargs):
        if kwargs.get("parse_mode") == "HTML" and self.reject_html:
            from telegram.error import BadRequest
            raise BadRequest("can't parse entities")
        self.calls.append(kwargs)


def test_post_renders_html_with_plain_fallback():
    """Messages go out as Telegram HTML (real code formatting); if Telegram rejects
    the entities, the exact same text is re-sent plain — never dropped."""
    bot = RecordingBot()
    t = TelegramTransport(bot, _settings())
    asyncio.run(t.post(Outbound(
        thread_id="general", speaker=Speaker(role="direct", name="d"),
        text="see `x<y` here")))
    assert bot.calls[0]["parse_mode"] == "HTML"
    assert "<code>x&lt;y</code>" in bot.calls[0]["text"]

    picky = RecordingBot(reject_html=True)
    t2 = TelegramTransport(picky, _settings())
    asyncio.run(t2.post(Outbound(
        thread_id="general", speaker=Speaker(role="direct", name="d"),
        text="plain please")))
    assert picky.calls[0].get("parse_mode") is None   # fell back
    assert picky.calls[0]["text"] == "plain please"


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


def test_dashboard_not_modified_does_not_duplicate(tmp_path):
    """An edit that Telegram answers 'message is not modified' is success — it must
    NOT recreate + re-pin a duplicate board (that stacked pins on every restart)."""
    from telegram.error import BadRequest

    class NotModifiedBot(DashBot):
        async def edit_message_text(self, **kw):
            raise BadRequest("Message is not modified")

    store = CoreStore(tmp_path / "state")
    store.set_dashboard_msg_id(101)
    bot = NotModifiedBot()
    t = TelegramTransport(bot, _settings(), store)
    asyncio.run(t.update_dashboard("same text"))
    assert bot.sent == []                      # no new message created
    assert store.dashboard_msg_id == 101       # board id unchanged


# ---- factory-reset message deletion (#general + DMs have no topic to drop) --------

class _MsgFull:
    def __init__(self, mid, tid=None):
        self.message_id = mid
        self.message_thread_id = tid


class IdBot:
    """Returns Message-like objects with ids (so office sends are trackable) and
    records deletions, so reset() can be asserted."""
    def __init__(self):
        self._next = 1000
        self.deleted: list[tuple] = []

    async def send_message(self, **kw):
        self._next += 1
        return type("M", (), {"chat_id": kw["chat_id"], "message_id": self._next})()

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.append(("bulk", chat_id, list(message_ids)))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(("one", chat_id, message_id))


def test_store_office_message_ids_persist_cap_clear(tmp_path, monkeypatch):
    from beaboss.core import store as store_mod
    s = CoreStore(tmp_path / "state")
    s.record_office_message(1, 10)
    s.record_office_message(1, 11)
    s.record_office_message(2, 20)
    assert s.office_message_ids == {"1": [10, 11], "2": [20]}
    assert CoreStore(tmp_path / "state").office_message_ids == {"1": [10, 11], "2": [20]}

    monkeypatch.setattr(store_mod, "OFFICE_MSG_CAP", 5)
    for i in range(8):
        s.record_office_message(3, i)
    assert s.office_message_ids["3"] == [3, 4, 5, 6, 7]   # capped, oldest dropped

    s.clear_office_messages()
    assert s.office_message_ids == {}


def test_office_messages_tracked_on_post_and_deleted_on_reset(tmp_path):
    bot = IdBot()
    store = CoreStore(tmp_path / "state")
    t = TelegramTransport(bot, _settings(), store)  # group chat_id=1
    sp = Speaker(role="orchestrator", name="Orc", emoji="🧭")

    asyncio.run(t.post(Outbound(thread_id="general", speaker=sp, text="office hello")))
    asyncio.run(t.post(Outbound(thread_id="55", speaker=sp, text="worker note")))

    assert list(store.office_message_ids) == ["1"]        # only the office chat
    assert len(store.office_message_ids["1"]) == 1        # the worker-topic post is not tracked

    tracked = store.office_message_ids["1"][0]
    asyncio.run(t.reset())
    assert bot.deleted == [("bulk", 1, [tracked])]         # bulk-deleted the office message
    assert store.office_message_ids == {}                  # tracking cleared


def test_record_incoming_tracks_only_office_messages(tmp_path):
    from beaboss.transports.telegram import record_incoming
    settings = _settings()
    settings.allowed_user_ids = {42}                       # avoid the group-id/user-id clash
    store = CoreStore(tmp_path / "state")
    ctx = _Ctx(settings, TelegramTransport(RecordingBot(), settings, store))  # group id=1

    def rec(update):
        asyncio.run(record_incoming(update, ctx))

    rec(_Upd(_U(42), _C(1, "supergroup"), _MsgFull(101)))          # #general → tracked
    rec(_Upd(_U(42), _C(1, "supergroup"), _MsgFull(102, tid=7)))   # worker topic → skipped
    rec(_Upd(_U(42), _C(42, "private"), _MsgFull(201)))            # allowlisted DM → tracked
    rec(_Upd(_U(7), _C(7, "private"), _MsgFull(202)))             # stranger DM → skipped

    assert store.office_message_ids.get("1") == [101]              # general only, not the topic
    assert store.office_message_ids.get("42") == [201]             # the allowlisted DM
    assert "7" not in store.office_message_ids                     # stranger dropped
