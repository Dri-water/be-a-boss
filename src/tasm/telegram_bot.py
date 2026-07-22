"""Telegram front end: forum-topic routing + control commands.

Every non-General topic maps 1:1 to a live Claude session. Messages in a topic
are the session's next turn; /new (in General) mints a topic + session.
"""

from __future__ import annotations

import logging

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

from pathlib import Path

from .claude_session import MediaItem
from .config import Settings
from .manager import SessionManager
from .rendering import chunk
from .store import Store

log = logging.getLogger("tasm.bot")


class TelegramEmitter:
    """How sessions push text back into their topic."""

    def __init__(self, bot):
        self.bot = bot
        self.chat_id: int | None = None

    def ensure_chat(self, chat_id: int) -> None:
        if self.chat_id is None:
            self.chat_id = chat_id
            log.info("bound to chat_id=%s (pin this in TELEGRAM_CHAT_ID)", chat_id)

    async def send(self, thread_id: int, text: str) -> None:
        if self.chat_id is None or not text.strip():
            return
        for part in chunk(text):
            await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=thread_id or None,
                text=part,
            )

    async def typing(self, thread_id: int) -> None:
        if self.chat_id is None:
            return
        try:
            await self.bot.send_chat_action(
                chat_id=self.chat_id,
                action=ChatAction.TYPING,
                message_thread_id=thread_id or None,
            )
        except Exception:  # noqa: BLE001
            pass

    async def send_photo(self, thread_id: int, path: Path, caption: str | None = None) -> None:
        await self._send_media("photo", thread_id, path, caption)

    async def send_video(self, thread_id: int, path: Path, caption: str | None = None) -> None:
        await self._send_media("video", thread_id, path, caption)

    async def send_document(self, thread_id: int, path: Path, caption: str | None = None) -> None:
        await self._send_media("document", thread_id, path, caption)

    async def _send_media(self, kind: str, thread_id: int, path: Path, caption: str | None) -> None:
        if self.chat_id is None:
            return
        cap = (caption or "")[:1024] or None
        common = dict(chat_id=self.chat_id, message_thread_id=thread_id or None, caption=cap)
        if kind == "photo":
            await self.bot.send_photo(photo=Path(path), **common)
        elif kind == "video":
            await self.bot.send_video(video=Path(path), supports_streaming=True, **common)
        else:
            await self.bot.send_document(document=Path(path), **common)


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
        ctx.bot_data["emitter"].ensure_chat(chat.id)
    return True


# --- commands -----------------------------------------------------------------


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    settings: Settings = ctx.bot_data["settings"]
    await update.effective_message.reply_text(
        f"👋 {settings.bot_name} — I run one live Claude Code session per topic.\n"
        "A message in a session topic is that session's next turn, so you can run "
        "several sessions in parallel across your repos.\n\n"
        "🗂 In the General topic:\n"
        "  /new <path> [name] — start a session in a repo\n"
        "       (path is relative to PROJECTS_ROOT, or absolute)\n"
        "  /list — list sessions and their status\n"
        "  /status — bot health\n"
        "  /whoami — your Telegram id + this chat's id\n\n"
        "💬 In a session topic:\n"
        "  • type anything → it becomes the session's next turn\n"
        "  • send a photo / file / video → handed to the session\n"
        "       (images are seen as vision; files land in ./.tg-inbox/)\n"
        "  • the session can send images, files & messages back to you\n"
        "  /stop — interrupt the current turn\n"
        "  /kill — end the session and close the topic\n\n"
        "Tip: two topics can point at the same repo for parallel work."
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Deliberately NOT allowlist-gated: it reveals only the caller's own ids, which
    # is exactly what you need while first filling in TELEGRAM_ALLOWED_USER_IDS /
    # TELEGRAM_CHAT_ID. Reveals nothing about other users or the bot's internals.
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


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    settings: Settings = ctx.bot_data["settings"]
    manager: SessionManager = ctx.bot_data["manager"]
    msg = update.effective_message

    if not ctx.args:
        await msg.reply_text(
            "Usage: /new <path> [name]\n"
            "Path is relative to PROJECTS_ROOT or absolute."
        )
        return

    raw = ctx.args[0]
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (settings.projects_root / raw)
    p = p.resolve()
    if not p.is_dir():
        await msg.reply_text(f"❌ Not a directory: {p}")
        return

    name = " ".join(ctx.args[1:]).strip() or p.name
    try:
        topic = await ctx.bot.create_forum_topic(
            chat_id=update.effective_chat.id, name=name[:128]
        )
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(
            f"❌ Couldn't create a topic ({e}).\n"
            "The group must have Topics enabled and I must be an admin with "
            "'Manage Topics'."
        )
        return

    thread_id = topic.message_thread_id
    await manager.create(thread_id, p, name)
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=thread_id,
        text=(
            f"✅ {settings.bot_name} ready.\n"
            f"cwd: {p}\n\n"
            "Type to talk to this session. /stop interrupts, /kill ends it."
        ),
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    manager: SessionManager = ctx.bot_data["manager"]
    rows = manager.listing()
    if not rows:
        await update.effective_message.reply_text("No sessions yet. /new <path> to start one.")
        return
    lines = ["Sessions:"]
    for _thread_id, rec, status in rows:
        lines.append(f"• {rec.name} [{status}] — {rec.cwd}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    settings: Settings = ctx.bot_data["settings"]
    manager: SessionManager = ctx.bot_data["manager"]
    live = sum(1 for _t, _r, s in manager.listing() if s != "dormant")
    total = len(manager.listing())
    await update.effective_message.reply_text(
        f"{settings.bot_name} up. {live} live / {total} known session(s)."
    )


async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    manager: SessionManager = ctx.bot_data["manager"]
    msg = update.effective_message
    thread_id = msg.message_thread_id
    if not thread_id:
        await msg.reply_text("Run /kill inside the session's topic.")
        return
    existed = await manager.kill(thread_id)
    if not existed:
        await msg.reply_text("This topic isn't a tracked session.")
        return
    try:
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            message_thread_id=thread_id,
            text="🗑 Session ended.",
        )
        await ctx.bot.close_forum_topic(
            chat_id=update.effective_chat.id, message_thread_id=thread_id
        )
    except Exception:  # noqa: BLE001
        pass


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    manager: SessionManager = ctx.bot_data["manager"]
    thread_id = update.effective_message.message_thread_id
    if not thread_id:
        await update.effective_message.reply_text("Run /stop inside a session topic.")
        return
    ok = await manager.interrupt(thread_id)
    await update.effective_message.reply_text("⏹ interrupting…" if ok else "Nothing running here.")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    manager: SessionManager = ctx.bot_data["manager"]
    msg = update.effective_message
    if not msg or not msg.text:
        return
    thread_id = msg.message_thread_id
    if not thread_id:
        return  # General topic — nothing to route
    handled = await manager.route(thread_id, msg.text)
    if not handled:
        await msg.reply_text("This topic isn't a live session. Use /new in General to start one.")


async def _collect_media(msg) -> tuple[list[MediaItem], str | None]:
    """Download every attachment on a message. Returns (items, error_string)."""
    items: list[MediaItem] = []

    async def grab(obj, filename: str, mime: str | None, kind: str) -> None:
        tg_file = await obj.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        items.append(MediaItem(kind=kind, filename=filename, mime=mime, data=data))

    try:
        if msg.photo:
            ph = msg.photo[-1]  # largest size
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
            await grab(vo, f"voice_{vo.file_unique_id}.ogg", vo.mime_type or "audio/ogg", "file")
        if msg.video_note:
            vn = msg.video_note
            await grab(vn, f"videonote_{vn.file_unique_id}.mp4", "video/mp4", "file")
    except Exception as e:  # noqa: BLE001
        return items, str(e)
    return items, None


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ok(update, ctx):
        return
    manager: SessionManager = ctx.bot_data["manager"]
    msg = update.effective_message
    if not msg:
        return
    thread_id = msg.message_thread_id
    if not thread_id:
        return  # media in General — nothing to route
    items, err = await _collect_media(msg)
    if err and not items:
        await msg.reply_text(
            f"⚠️ couldn't fetch that ({err}). "
            "Telegram bots can only download files up to 20MB."
        )
        return
    if not items:
        return
    handled = await manager.route_media(thread_id, msg.caption or "", items)
    if not handled:
        await msg.reply_text("This topic isn't a live session. Use /new in General to start one.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=ctx.error)


# --- lifecycle ----------------------------------------------------------------


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("whoami", "show your Telegram id (for setup)"),
            BotCommand("new", "start a session: /new <path> [name]"),
            BotCommand("list", "list sessions"),
            BotCommand("status", "bot health"),
            BotCommand("stop", "interrupt this session's turn"),
            BotCommand("kill", "end this session + close topic"),
            BotCommand("help", "usage"),
        ]
    )
    me = await app.bot.get_me()
    log.info("%s online as @%s", app.bot_data["settings"].bot_name, me.username)


async def _post_shutdown(app: Application) -> None:
    manager: SessionManager = app.bot_data.get("manager")
    if manager:
        await manager.shutdown()


def build_application(settings: Settings, store: Store) -> Application:
    app = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    emitter = TelegramEmitter(app.bot)
    if settings.chat_id:
        emitter.chat_id = settings.chat_id
    manager = SessionManager(settings, store, emitter)

    app.bot_data["settings"] = settings
    app.bot_data["emitter"] = emitter
    app.bot_data["manager"] = manager

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("stop", cmd_stop))
    media_filter = (
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL
        | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(media_filter, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    return app
