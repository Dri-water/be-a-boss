"""Telegram adapter: forum topics ⇄ core threads, header-card speaker identity.

One bot token = one Telegram sender, so speaker identity is rendered in the
message body: orchestrator and worker messages open with a labelled header line;
direct sessions stay unadorned (single-speaker threads don't need cards).

The General topic is the orchestrator's office: talking there talks to the
orchestrator. /new still creates classic direct sessions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import BotCommand, ReactionTypeEmoji, Update
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram.error import BadRequest

from ..config import Settings
from ..core.engine import Engine
from ..core.ports import InboundMessage, MediaIn, Outbound, SYSTEM
from ..core.store import CoreStore
from ..rendering import chunk, to_telegram_html

log = logging.getLogger("beaboss.transport.telegram")

GENERAL = "general"

# Cap a single logical message: a runaway dump (a huge diff, a giant log) is
# truncated with a pointer rather than spammed across dozens of messages. The rate
# limiter paces whatever does go out so Telegram never 429-drops it.
MAX_MSG_CHUNKS = 5


def _thread_of(update: Update) -> str:
    """The core thread_id for an incoming update: a private DM is its own office
    'dm:<user_id>', the group's General is 'general', a group topic is its numeric id."""
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type == "private" and user:
        return f"dm:{user.id}"
    msg = update.effective_message
    tid = msg.message_thread_id if msg else None
    return GENERAL if not tid else str(tid)


class TelegramTransport:
    """Implements core.ports.Transport over Telegram. Two kinds of chat:
    the supergroup (worker topics, #general, the dashboard) and per-boss DMs (private
    offices). A thread_id routes to one of them via `_route`."""

    def __init__(self, bot, settings: Settings, store: CoreStore | None = None):
        self.bot = bot
        self.settings = settings
        self.store = store   # for the persisted dashboard message id
        self.group_chat_id: int | None = settings.chat_id

    def ensure_group(self, chat_id: int) -> None:
        if self.group_chat_id is None:
            self.group_chat_id = chat_id
            log.info("bound to group chat_id=%s (pin this in TELEGRAM_CHAT_ID)", chat_id)

    def _route(self, thread_id: str) -> tuple[int | None, int | None]:
        """thread_id → (chat_id, message_thread_id). DMs go to the user's chat with
        no topic; #general + worker topics go to the supergroup."""
        if thread_id.startswith("dm:"):
            # 'dm:<uid>' — a private chat with the orchestrator; no topic.
            try:
                return int(thread_id[3:]), None
            except ValueError:
                return None, None
        if thread_id == GENERAL:
            return self.group_chat_id, None
        try:
            return self.group_chat_id, int(thread_id)
        except ValueError:
            return self.group_chat_id, None

    # ---- Transport interface --------------------------------------------

    async def create_thread(self, title: str) -> str:
        # Worker topics + direct sessions always live in the supergroup.
        assert self.group_chat_id is not None, "no group bound yet"
        topic = await self.bot.create_forum_topic(
            chat_id=self.group_chat_id, name=title[:128])
        return str(topic.message_thread_id)

    async def close_thread(self, thread_id: str) -> None:
        chat_id, topic_id = self._route(thread_id)
        if chat_id is None or topic_id is None:
            return
        try:
            await self.bot.close_forum_topic(
                chat_id=chat_id, message_thread_id=topic_id)
        except Exception:  # noqa: BLE001
            pass

    async def post(self, out: Outbound) -> None:
        chat_id, topic_id = self._route(out.thread_id)
        if chat_id is None:
            return
        header = self._header(out)
        if out.media_path is not None:
            cap_text = (out.caption or "").strip()
            combined = f"{header}\n{cap_text}".strip() if header else cap_text
            overflow = None
            if len(combined) > 1024:
                # Telegram hard-caps captions; never silently truncate — send a
                # stub caption and the full text as a follow-up message.
                overflow = cap_text
                combined = f"{header}\n…caption follows ⬇".strip() if header else "…caption follows ⬇"
            p = Path(out.media_path)
            common = dict(chat_id=chat_id, message_thread_id=topic_id)
            kw = dict(common)
            if combined:
                kw["caption"] = to_telegram_html(combined)
                kw["parse_mode"] = "HTML"
            try:
                await self._send_media(p, out.media_kind, kw)
            except BadRequest:
                kw = dict(common)
                if combined:
                    kw["caption"] = combined
                await self._send_media(p, out.media_kind, kw)
            if overflow:
                await self.post(Outbound(
                    thread_id=out.thread_id, speaker=out.speaker, text=overflow))
            return
        text = out.text
        if not text.strip():
            return
        body = f"{header}\n{text}" if header else text
        parts = chunk(body)
        if len(parts) > MAX_MSG_CHUNKS:
            omitted = len(parts) - (MAX_MSG_CHUNKS - 1)
            parts = parts[:MAX_MSG_CHUNKS - 1] + [
                f"… ✂️ truncated — {omitted} more part(s) omitted (too long for chat). "
                f"If you need the whole thing, ask for it as a file."]
        for part in parts:
            # Pretty by default (Telegram HTML: real code blocks, bold, links),
            # plain-text fallback if Telegram rejects the entities — never drop
            # a message over formatting.
            try:
                await self.bot.send_message(
                    chat_id=chat_id, message_thread_id=topic_id,
                    text=to_telegram_html(part), parse_mode="HTML")
            except BadRequest:
                await self.bot.send_message(
                    chat_id=chat_id, message_thread_id=topic_id, text=part)

    async def _send_media(self, p: Path, kind: str | None, kw: dict) -> None:
        if kind == "photo":
            await self.bot.send_photo(photo=p, **kw)
        elif kind == "video":
            await self.bot.send_video(video=p, supports_streaming=True, **kw)
        else:
            await self.bot.send_document(document=p, **kw)

    async def indicate_busy(self, thread_id: str) -> None:
        chat_id, topic_id = self._route(thread_id)
        if chat_id is None:
            return
        try:
            await self.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING,
                message_thread_id=topic_id)
        except Exception:  # noqa: BLE001
            pass

    async def update_dashboard(self, text: str) -> None:
        """Keep a single pinned status board in #general current — edit it in place,
        or create+pin one the first time. A status board must never break the flow,
        so every failure is swallowed."""
        if self.group_chat_id is None:
            return
        mid = self.store.dashboard_msg_id if self.store else None
        if mid:
            try:
                await self.bot.edit_message_text(
                    chat_id=self.group_chat_id, message_id=mid, text=text)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return  # identical content — the board is already right
                # message gone — unpin the corpse and recreate below
                try:
                    await self.bot.unpin_chat_message(
                        chat_id=self.group_chat_id, message_id=mid)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                return  # transient — retry on the next state change, don't duplicate
        try:
            msg = await self.bot.send_message(chat_id=self.group_chat_id, text=text)
            try:
                await self.bot.pin_chat_message(
                    chat_id=self.group_chat_id, message_id=msg.message_id,
                    disable_notification=True)
            except Exception:  # noqa: BLE001
                pass  # pinning may be denied; the message is still the board
            if self.store:
                self.store.set_dashboard_msg_id(msg.message_id)
        except Exception:  # noqa: BLE001
            pass

    async def delete_thread(self, thread_id: str) -> None:
        """Factory reset: delete a worker/direct topic outright — the topic and all
        its messages vanish. Group topics only; #general and DMs have no topic."""
        chat_id, topic_id = self._route(thread_id)
        if chat_id is None or topic_id is None:
            return
        try:
            await self.bot.delete_forum_topic(
                chat_id=chat_id, message_thread_id=topic_id)
        except Exception:  # noqa: BLE001
            pass

    async def delete_dashboard(self) -> None:
        """Factory reset: unpin + delete the status board."""
        if self.group_chat_id is None:
            return
        mid = self.store.dashboard_msg_id if self.store else None
        if not mid:
            return
        for op in ("unpin_chat_message", "delete_message"):
            try:
                await getattr(self.bot, op)(chat_id=self.group_chat_id, message_id=mid)
            except Exception:  # noqa: BLE001
                pass
        if self.store:
            self.store.set_dashboard_msg_id(None)

    # ---- rendering -------------------------------------------------------

    @staticmethod
    def _header(out: Outbound) -> str:
        if out.thread_id.startswith("dm:"):
            return ""  # a 1:1 chat — a name card on every message is noise
        if out.speaker.role in ("orchestrator", "worker"):
            return f"{out.speaker.label}:"
        if out.speaker.role == "system":
            return ""  # system lines carry their own tone
        return ""  # direct sessions stay unadorned


# --- guards -------------------------------------------------------------------


def _ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = ctx.bot_data["settings"]
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.id not in settings.allowed_user_ids:
        return False
    # A private DM from an allowlisted user is always allowed — it's that user's
    # own office. Group messages must come from the bound supergroup (the shared
    # office + shop floor), which we bind on first sight if not pinned yet.
    if chat and chat.type == "private":
        return True
    if settings.chat_id is not None and (not chat or chat.id != settings.chat_id):
        return False
    if chat:
        ctx.bot_data["transport"].ensure_group(chat.id)
    return True


# --- commands -----------------------------------------------------------------


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = ctx.bot_data["settings"]
    if not _ok(update, ctx):
        # Don't leave a first-timer's /start or /help in silence: if we're in setup
        # mode (no allowlist yet), guide them to bootstrap instead of ignoring them.
        msg = update.effective_message
        if msg and not settings.allowed_user_ids:
            await msg.reply_text(
                f"👋 {settings.bot_name} isn't configured yet. DM me /whoami to get "
                "your Telegram id, add it to TELEGRAM_ALLOWED_USER_IDS, and restart. "
                "Then add me to a Topics-enabled supergroup as admin and run /setup "
                "there. (/setup anywhere gives the full checklist.)")
        return
    await update.effective_message.reply_text(
        f"👋 {settings.bot_name} — your agent org, in one place.\n\n"
        "🧭 I'm the orchestrator. DM me for a private 1:1, or talk to me here in the "
        "group — either way, give me goals (\"fix the login bug in myapp, then audit "
        "deps\") and I hire worker agents, brief them, and supervise. Each worker gets "
        "its own topic in the group — watch, and type in to steer us both.\n"
        "📋 #general is a live status board — what's running, blocked, and waiting on "
        "you, always current.\n\n"
        "🗂 Commands:\n"
        "  /new <path> [name] — classic direct session, in the group (no orchestrator)\n"
        "  /list — all threads + status\n"
        "  /status — bot health\n"
        "  /approve <id> · /reject <id> — land or decline a worker's delivery\n"
        "  /setup — verify the group is configured right\n"
        "  /whoami — your Telegram id + this chat's id\n"
        "  /reset — factory reset: wipe all my memory and state (asks to confirm)\n\n"
        "💬 In any session topic:\n"
        "  • text / photos / files → that session (images are seen as vision)\n"
        "  /stop — interrupt · /kill — end the session\n\n"
        "Sessions can send images, files & messages back to you."
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Not allowlist-gated: reveals only the caller's own ids (needed for setup).
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not msg:
        return
    allowed = ctx.bot_data["settings"].allowed_user_ids
    status = "✅ allowlisted" if user.id in allowed else "🚫 not allowlisted"
    lines = [f"you: id={user.id}  @{user.username or '—'}  ({status})"]
    if chat:
        lines.append(f"chat: id={chat.id}  type={chat.type}")
    await msg.reply_text("\n".join(lines))


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Not allowlist-gated: this is the first-time "is my group configured right?"
    # check, so it must work before you've added yourself to the allowlist. It only
    # reveals the current chat's own configuration.
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    if chat.type == "private":
        await msg.reply_text(
            "Run /setup inside your group (not this DM) so I can check that group. "
            "A bot can't create the group for you — Telegram only lets a person do "
            "that — but I'll tell you exactly what to fix.")
        return

    lines = [f"Setup check for “{chat.title or chat.id}”:"]
    ok = True

    is_forum = bool(getattr(chat, "is_forum", False))
    lines.append(("✅" if is_forum else "❌") + " Topics enabled"
                 + ("" if is_forum else " — open group Settings and turn on Topics"))
    ok &= is_forum

    try:
        me = await ctx.bot.get_me()
        member = await ctx.bot.get_chat_member(chat.id, me.id)
        is_admin = member.status == "administrator"
        can_topics = is_admin and bool(getattr(member, "can_manage_topics", False))
        lines.append(("✅" if is_admin else "❌") + " I'm an admin here"
                     + ("" if is_admin else " — add me as an Admin"))
        lines.append(("✅" if can_topics else "❌") + " I can manage topics"
                     + ("" if can_topics else " — grant me the “Manage Topics” admin right"))
        ok &= is_admin and can_topics
    except Exception as e:  # noqa: BLE001
        lines.append(f"⚠️ couldn't read my own membership ({e})")
        ok = False

    settings: Settings = ctx.bot_data["settings"]
    if not settings.allowed_user_ids:
        lines.append("⚠️ Setup mode — no allowlist yet. DM me /whoami, put your id in "
                     "TELEGRAM_ALLOWED_USER_IDS, and restart.")
        ok = False

    lines.append("")
    lines.append("✅ All set — talk to me in the General topic to get started."
                 if ok else "Fix the ❌ / ⚠️ items above, then run /setup again.")
    await msg.reply_text("\n".join(lines))


async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
    if not ctx.args:
        await msg.reply_text("Usage: /approve <worker_id> — authorize a pending delivery.")
        return
    await msg.reply_text(await engine.approve_delivery(ctx.args[0]))


async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
    if not ctx.args:
        await msg.reply_text("Usage: /reject <worker_id> — decline a pending delivery.")
        return
    await msg.reply_text(await engine.reject_delivery(ctx.args[0]))


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
    if not ctx.args or ctx.args[0].lower() != "confirm":
        await msg.reply_text(
            "🏭 Factory reset wipes EVERYTHING I know: all conversation memory, "
            "every worker (topics deleted, worktrees discarded — committed "
            "worker/* branches stay in your repos), the dashboard, all records. "
            "Old #general/DM messages stay visible to you, but I can't read them — "
            "a fresh me starts truly blank.\n\nThis cannot be undone. "
            "Run /reset confirm to do it.")
        return
    await msg.reply_text(await engine.factory_reset())


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
    chat = update.effective_chat
    if chat and chat.type == "private":
        await msg.reply_text("Run /new in the group — a direct session lives in its "
                             "own topic there. In a DM, just tell me what you need.")
        return
    if not ctx.args:
        await msg.reply_text("Usage: /new <path> [name] — path relative to "
                             "PROJECTS_ROOT, or absolute.")
        return
    name = " ".join(ctx.args[1:]).strip() or None
    result = await engine.new_direct(ctx.args[0], name)
    if isinstance(result, str):
        await msg.reply_text(result)
        return
    thread_id, title = result
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=int(thread_id),
        text=(f"✅ direct session ready: {title}\n"
              "Type to talk to it. /stop interrupts, /kill ends it."),
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    rows = engine.listing()
    if not rows:
        await update.effective_message.reply_text(
            "Nothing yet. Talk to me here to put me to work, or /new <path> "
            "for a direct session.")
        return
    lines = ["Threads:"]
    for _tid, rec, status in rows:
        extra = f" · {rec.worker_status}" if rec.role == "worker" else ""
        lines.append(f"• [{rec.role}] {rec.name} [{status}]{extra} — {rec.cwd or '—'}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    settings: Settings = ctx.bot_data["settings"]
    engine: Engine = ctx.bot_data["engine"]
    rows = engine.listing()
    live = sum(1 for _t, _r, s in rows if s != "dormant")
    await update.effective_message.reply_text(
        f"{settings.bot_name} up. {live} live / {len(rows)} known thread(s).")


async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
    thread_id = _thread_of(update)
    if thread_id == GENERAL or thread_id.startswith("dm:"):
        await msg.reply_text("Run /kill inside a session topic, not the office.")
        return
    existed = await engine.kill(thread_id)
    if not existed:
        await msg.reply_text("This topic isn't a tracked session.")
        return
    transport: TelegramTransport = ctx.bot_data["transport"]
    await transport.post(Outbound(
        thread_id=thread_id, speaker=SYSTEM, text="🗑 Session ended.",
    ))
    await transport.close_thread(thread_id)


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    thread_id = _thread_of(update)
    ok = await engine.interrupt(thread_id)
    await update.effective_message.reply_text(
        "⏹ interrupting…" if ok else "Nothing running here.")


# --- inbound ------------------------------------------------------------------


async def _collect_media(msg) -> tuple[list[MediaIn], str | None]:
    items: list[MediaIn] = []

    async def grab(obj, filename: str, mime: str | None, kind: str) -> None:
        tg_file = await obj.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        items.append(MediaIn(kind=kind, filename=filename, mime=mime, data=data))

    try:
        if msg.photo:
            ph = msg.photo[-1]
            await grab(ph, f"photo_{ph.file_unique_id}.jpg", "image/jpeg", "image")
        if msg.document:
            d = msg.document
            mime = d.mime_type or ""
            kind = "image" if mime.startswith("image/") else "file"
            await grab(d, d.file_name or f"doc_{d.file_unique_id}", mime or None, kind)
        if msg.video:
            v = msg.video
            await grab(v, v.file_name or f"video_{v.file_unique_id}.mp4",
                       v.mime_type or "video/mp4", "file")
        if msg.animation:
            a = msg.animation
            await grab(a, a.file_name or f"anim_{a.file_unique_id}.mp4",
                       a.mime_type or "video/mp4", "file")
        if msg.audio:
            au = msg.audio
            await grab(au, au.file_name or f"audio_{au.file_unique_id}.mp3",
                       au.mime_type or "audio/mpeg", "file")
        if msg.voice:
            vo = msg.voice
            await grab(vo, f"voice_{vo.file_unique_id}.ogg",
                       vo.mime_type or "audio/ogg", "file")
        if msg.video_note:
            vn = msg.video_note
            await grab(vn, f"videonote_{vn.file_unique_id}.mp4", "video/mp4", "file")
    except Exception as e:  # noqa: BLE001
        return items, str(e)
    return items, None


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
    if not msg:
        return

    # Instant "received" acknowledgement: an agent turn can take a while before its
    # first reply, so react the moment the message lands — you're never left
    # wondering whether it got through. Best-effort; never blocks the message.
    try:
        await msg.set_reaction(ReactionTypeEmoji(emoji="👀"))
    except Exception:  # noqa: BLE001
        pass

    media: list[MediaIn] = []
    if (msg.photo or msg.document or msg.video or msg.animation or msg.audio
            or msg.voice or msg.video_note):
        media, err = await _collect_media(msg)
        if err and not media:
            await msg.reply_text(
                f"⚠️ couldn't fetch that ({err}). Telegram bots can only "
                "download files up to 20MB.")
            return

    text = msg.text or msg.caption or ""
    if not text and not media:
        return

    user = update.effective_user
    await engine.on_inbound(InboundMessage(
        thread_id=_thread_of(update),
        text=text,
        media=media,
        sender_name=(user.first_name or user.username or "the boss") if user else "",
    ))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=ctx.error)


# --- lifecycle ----------------------------------------------------------------


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("whoami", "show your Telegram id (for setup)"),
        BotCommand("setup", "check this group is configured right"),
        BotCommand("approve", "approve a worker's delivery: /approve <id>"),
        BotCommand("reject", "decline a worker's delivery: /reject <id>"),
        BotCommand("new", "direct session: /new <path> [name]"),
        BotCommand("reset", "factory reset: wipe all bot memory/state"),
        BotCommand("list", "list all threads"),
        BotCommand("status", "bot health"),
        BotCommand("stop", "interrupt this thread's turn"),
        BotCommand("kill", "end this thread's session"),
        BotCommand("help", "usage"),
    ])
    me = await app.bot.get_me()
    log.info("%s online as @%s", app.bot_data["settings"].bot_name, me.username)
    try:  # make the pinned board current from the moment we're online
        await app.bot_data["engine"]._refresh_dashboard()
    except Exception:  # noqa: BLE001
        pass


async def _post_shutdown(app: Application) -> None:
    engine: Engine = app.bot_data.get("engine")
    if engine:
        await engine.shutdown()


def build_application(settings: Settings, store: CoreStore) -> Application:
    if not settings.bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is required to run the Telegram surface. Copy "
            ".env.example to .env and set it (get a token from @BotFather). "
            "To drive be-a-boss from the browser or terminal instead — no Telegram "
            "token needed — run `python -m beaboss.web` or `python -m beaboss.cli`."
        )
    if not settings.allowed_user_ids:
        # Empty allowlist is not fatal: _ok() already refuses everyone, so the bot
        # is fail-closed. Start in setup mode so the operator can bootstrap their id
        # via /whoami (which is deliberately not allowlist-gated) instead of needing
        # a third-party bot to discover it.
        log.warning(
            "No users allowlisted (TELEGRAM_ALLOWED_USER_IDS is empty) — running in "
            "SETUP MODE: every command except /whoami is ignored. DM the bot /whoami "
            "to get your numeric id, add it to TELEGRAM_ALLOWED_USER_IDS, and restart."
        )
    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        # Throttle + auto-retry on Telegram's 429 flood control, so a burst of
        # messages (a chunked diff, a chatty worker) is paced, never dropped.
        .rate_limiter(AIORateLimiter())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    engine = Engine(settings, store)
    transport = TelegramTransport(app.bot, settings, store)
    engine.attach_transport(transport)
    engine.rehydrate()  # re-surface unfinished workers after a restart

    app.bot_data["settings"] = settings
    app.bot_data["engine"] = engine
    app.bot_data["transport"] = transport

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("stop", cmd_stop))
    media_filter = (
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL
        | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | media_filter, on_message))
    app.add_error_handler(on_error)

    return app
