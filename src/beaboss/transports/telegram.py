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

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import Settings
from ..core.engine import Engine
from ..core.ports import InboundMessage, MediaIn, Outbound, SYSTEM
from ..core.store import CoreStore
from ..rendering import chunk

log = logging.getLogger("beaboss.transport.telegram")

GENERAL = "general"


def _to_thread_id(message_thread_id: int | None) -> str:
    return GENERAL if not message_thread_id else str(message_thread_id)


def _to_topic_id(thread_id: str) -> int | None:
    return None if thread_id == GENERAL else int(thread_id)


class TelegramTransport:
    """Implements core.ports.Transport over a Telegram supergroup with Topics."""

    def __init__(self, bot, settings: Settings):
        self.bot = bot
        self.settings = settings
        self.chat_id: int | None = settings.chat_id

    def ensure_chat(self, chat_id: int) -> None:
        if self.chat_id is None:
            self.chat_id = chat_id
            log.info("bound to chat_id=%s (pin this in TELEGRAM_CHAT_ID)", chat_id)

    # ---- Transport interface --------------------------------------------

    async def create_thread(self, title: str) -> str:
        assert self.chat_id is not None, "no chat bound yet"
        topic = await self.bot.create_forum_topic(
            chat_id=self.chat_id, name=title[:128])
        return str(topic.message_thread_id)

    async def rename_thread(self, thread_id: str, title: str) -> None:
        if self.chat_id is None or thread_id == GENERAL:
            return
        try:
            await self.bot.edit_forum_topic(
                chat_id=self.chat_id,
                message_thread_id=_to_topic_id(thread_id),
                name=title[:128],
            )
        except Exception:  # noqa: BLE001
            pass

    async def close_thread(self, thread_id: str) -> None:
        if self.chat_id is None or thread_id == GENERAL:
            return
        try:
            await self.bot.close_forum_topic(
                chat_id=self.chat_id,
                message_thread_id=_to_topic_id(thread_id),
            )
        except Exception:  # noqa: BLE001
            pass

    async def post(self, out: Outbound) -> None:
        if self.chat_id is None:
            return
        header = self._header(out)
        if out.media_path is not None:
            cap = (out.caption or "")
            if header:
                cap = f"{header}\n{cap}".strip()
            cap = cap[:1024] or None
            common = dict(chat_id=self.chat_id,
                          message_thread_id=_to_topic_id(out.thread_id),
                          caption=cap)
            p = Path(out.media_path)
            if out.media_kind == "photo":
                await self.bot.send_photo(photo=p, **common)
            elif out.media_kind == "video":
                await self.bot.send_video(video=p, supports_streaming=True, **common)
            else:
                await self.bot.send_document(document=p, **common)
            return
        text = out.text
        if not text.strip():
            return
        body = f"{header}\n{text}" if header else text
        for part in chunk(body):
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=_to_topic_id(out.thread_id),
                text=part,
            )

    async def indicate_busy(self, thread_id: str) -> None:
        if self.chat_id is None:
            return
        try:
            await self.bot.send_chat_action(
                chat_id=self.chat_id, action=ChatAction.TYPING,
                message_thread_id=_to_topic_id(thread_id),
            )
        except Exception:  # noqa: BLE001
            pass

    # ---- rendering -------------------------------------------------------

    @staticmethod
    def _header(out: Outbound) -> str:
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
    if settings.chat_id is not None and (not chat or chat.id != settings.chat_id):
        return False
    if chat:
        ctx.bot_data["transport"].ensure_chat(chat.id)
    return True


# --- commands -----------------------------------------------------------------


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    settings: Settings = ctx.bot_data["settings"]
    await update.effective_message.reply_text(
        f"👋 {settings.bot_name} — your agent org in this group.\n\n"
        "🧭 Talk to me in General: I'm the orchestrator. Give me goals "
        "(\"fix the login bug in myapp, then audit deps\") and I hire worker "
        "agents, brief them, and supervise. Every worker gets its own topic — "
        "watch us work, and type there anytime to steer us both.\n\n"
        "🗂 Commands (General):\n"
        "  /new <path> [name] — classic direct session (no orchestrator)\n"
        "  /list — all threads + status\n"
        "  /status — bot health\n"
        "  /setup — verify this group is configured right\n"
        "  /whoami — your Telegram id + this chat's id\n\n"
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


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    engine: Engine = ctx.bot_data["engine"]
    msg = update.effective_message
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
        message_thread_id=_to_topic_id(thread_id),
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
    thread_id = _to_thread_id(msg.message_thread_id)
    if thread_id == GENERAL:
        await msg.reply_text("Run /kill inside a session topic.")
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
    thread_id = _to_thread_id(update.effective_message.message_thread_id)
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
        thread_id=_to_thread_id(msg.message_thread_id),
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
        BotCommand("new", "direct session: /new <path> [name]"),
        BotCommand("list", "list all threads"),
        BotCommand("status", "bot health"),
        BotCommand("stop", "interrupt this thread's turn"),
        BotCommand("kill", "end this thread's session"),
        BotCommand("help", "usage"),
    ])
    me = await app.bot.get_me()
    log.info("%s online as @%s", app.bot_data["settings"].bot_name, me.username)


async def _post_shutdown(app: Application) -> None:
    engine: Engine = app.bot_data.get("engine")
    if engine:
        await engine.shutdown()


def build_application(settings: Settings, store: CoreStore) -> Application:
    if not settings.bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is required to run the Telegram surface. Copy "
            ".env.example to .env and set it (get a token from @BotFather). "
            "To drive be-a-boss from the browser or VS Code instead — no Telegram "
            "token needed — run `python -m beaboss.web`."
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
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    engine = Engine(settings, store)
    transport = TelegramTransport(app.bot, settings)
    engine.attach_transport(transport)

    app.bot_data["settings"] = settings
    app.bot_data["engine"] = engine
    app.bot_data["transport"] = transport

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("setup", cmd_setup))
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
