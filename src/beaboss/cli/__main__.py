"""`python -m beaboss.cli` — drive the whole org from a terminal, or a pipe.

Default: an interactive, coloured session for a human.
`--json`:  newline-delimited JSON events on stdout; commands on stdin. Built for
           agents and scripts — the event shapes match the web surface exactly.

Input lines (both modes):
  plain text          → a message to the ACTIVE thread (starts on the orchestrator)
  /command [args]      → /help /threads /thread <id> /new <path> [name]
                         /approve <id> /reject <id> /stop /kill /reset [confirm] /quit
  {"type": "...", ...} → a raw JSON command (the web/CLI protocol)

The command dispatch here is the single definition of what each control DOES, so a
human typing `/approve nova` and an agent piping {"type":"approve",...} behave
identically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from dataclasses import dataclass

from ..config import Settings
from ..core.engine import Engine
from ..core.ports import InboundMessage, Outbound, SYSTEM
from ..core.store import CoreStore
from ..transports.cli import OFFICE, CLITransport

log = logging.getLogger("beaboss.cli")

HELP = (
    "type to talk to the orchestrator · /threads · /thread <id> · /new <path> [name]"
    " · /approve <id> · /reject <id> · /stop · /kill · /reset [confirm] · /quit")


@dataclass
class State:
    active: str = OFFICE
    quit: bool = False


# ---- command dispatch (shared by typed slash-commands and JSON) --------------


async def handle_line(engine: Engine, transport: CLITransport,
                      state: State, line: str) -> None:
    line = line.rstrip("\r\n")
    if not line.strip():
        return
    if line.lstrip().startswith("{"):
        try:
            msg = json.loads(line)
        except ValueError:
            await _sys(transport, state, "⚠️ not valid JSON")
            return
        await _dispatch(engine, transport, state, msg)
    elif line.startswith("/"):
        parts = line[1:].split()
        cmd = parts[0].lower() if parts else ""
        await _slash(engine, transport, state, cmd, parts[1:])
    else:
        await engine.on_inbound(InboundMessage(
            thread_id=state.active, text=line, sender_name="the boss"))


async def _sys(transport: CLITransport, state: State, text: str) -> None:
    await transport.post(Outbound(thread_id=state.active, speaker=SYSTEM, text=text))


async def _slash(engine, transport, state, cmd, rest) -> None:
    if cmd in ("quit", "exit", "q"):
        state.quit = True
    elif cmd in ("help", "?"):
        await _sys(transport, state, HELP)
    elif cmd in ("threads", "list", "ls"):
        rows = "\n".join(
            f"  {tid:<10} {t['title']}{'' if t['open'] else '  (closed)'}"
            f"{'  ← active' if tid == state.active else ''}"
            for tid, t in transport.threads.items())
        await _sys(transport, state, "threads:\n" + rows)
    elif cmd == "thread":
        tid = rest[0] if rest else ""
        if tid in transport.threads:
            state.active = tid
            await _sys(transport, state, f"→ now on: {transport.threads[tid]['title']}")
        else:
            await _sys(transport, state, f"no such thread '{tid}' — /threads to list")
    elif cmd in ("stop", "interrupt"):
        await _dispatch(engine, transport, state,
                        {"type": "interrupt", "thread_id": state.active})
    elif cmd == "kill":
        await _dispatch(engine, transport, state,
                        {"type": "kill", "thread_id": state.active})
    elif cmd == "new":
        await _dispatch(engine, transport, state, {
            "type": "new", "path": rest[0] if rest else "",
            "name": " ".join(rest[1:])})
    elif cmd in ("approve", "reject"):
        await _dispatch(engine, transport, state,
                        {"type": cmd, "worker_id": rest[0] if rest else ""})
    elif cmd == "reset":
        await _dispatch(engine, transport, state,
                        {"type": "reset", "confirm": rest[:1] == ["confirm"]})
    else:
        await _sys(transport, state, f"unknown /{cmd} — /help")


async def _dispatch(engine, transport, state, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "message":
        tid = str(msg.get("thread_id") or state.active).strip()
        text = str(msg.get("text") or "")
        if tid and text.strip():
            await engine.on_inbound(InboundMessage(
                thread_id=tid, text=text, sender_name="the boss"))
    elif mtype == "interrupt":
        tid = str(msg.get("thread_id") or state.active).strip()
        ok = await engine.interrupt(tid)
        await transport.post(Outbound(
            thread_id=tid, speaker=SYSTEM,
            text="⏹ interrupting…" if ok else "Nothing running here."))
    elif mtype == "kill":
        tid = str(msg.get("thread_id") or state.active).strip()
        if tid == OFFICE or tid.startswith("dm:"):
            await transport.post(Outbound(
                thread_id=OFFICE, speaker=SYSTEM,
                text="Can't kill the orchestrator's office — /reset confirm wipes all."))
        elif tid:
            await engine.kill(tid)
            await transport.post(Outbound(
                thread_id=tid, speaker=SYSTEM, text="🗑 Session ended."))
            await transport.close_thread(tid)
            if state.active == tid:
                state.active = OFFICE
    elif mtype == "new":
        path = str(msg.get("path") or "").strip()
        if not path:
            await transport.post(Outbound(
                thread_id=OFFICE, speaker=SYSTEM, text="usage: /new <path> [name]"))
        else:
            result = await engine.new_direct(
                path, str(msg.get("name") or "").strip() or None)
            if isinstance(result, str):
                await transport.post(Outbound(
                    thread_id=OFFICE, speaker=SYSTEM, text=result))
            else:
                tid, title = result
                state.active = tid
                await transport.post(Outbound(
                    thread_id=tid, speaker=SYSTEM,
                    text=f"✅ direct session ready: {title}"))
    elif mtype in ("approve", "reject"):
        wid = str(msg.get("worker_id") or "").strip()
        if wid:
            fn = engine.approve_delivery if mtype == "approve" else engine.reject_delivery
            await transport.post(Outbound(
                thread_id=OFFICE, speaker=SYSTEM, text=await fn(wid)))
    elif mtype == "reset":
        if msg.get("confirm") is True:
            await transport.post(Outbound(
                thread_id=OFFICE, speaker=SYSTEM, text=await engine.factory_reset()))
            state.active = OFFICE
        else:
            await transport.post(Outbound(
                thread_id=OFFICE, speaker=SYSTEM,
                text="🏭 wipes ALL memory + state — irreversible. Send /reset confirm."))


# ---- renderers ---------------------------------------------------------------

_C = {"orchestrator": "\033[38;5;75m", "worker": "\033[38;5;177m",
      "you": "\033[38;5;78m", "system": "\033[38;5;244m", "direct": "\033[38;5;250m"}
_RESET, _DIM, _BOLD = "\033[0m", "\033[2m", "\033[1m"


def _pretty(event: dict) -> str | None:
    t = event.get("type")
    if t == "message":
        sp = event.get("speaker", {})
        color = _C.get(sp.get("role"), "")
        who = (f"{sp.get('emoji','')} {sp.get('name','')}").strip()
        return f"{color}{_BOLD}{who}{_RESET}  {event['text']}"
    if t == "media":
        cap = f" — {event['caption']}" if event.get("caption") else ""
        return f"{_DIM}🖼  [{event.get('kind')}: {event.get('filename')}]{cap}{_RESET}"
    if t == "thread":
        if event.get("removed"):
            return f"{_DIM}— thread {event['id']} removed{_RESET}"
        return f"{_DIM}+ thread [{event['id']}] {event['title']}{_RESET}"
    if t == "dashboard" and event.get("text"):
        body = "\n".join("  " + ln for ln in event["text"].splitlines())
        return f"{_DIM}┄┄┄ dashboard ┄┄┄\n{body}\n{_RESET}"
    return None  # busy / threads snapshot: nothing to print in the simple mode


# ---- stdin loop --------------------------------------------------------------


def _reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    try:
        for line in sys.stdin:      # blocking read on a daemon thread (cross-platform)
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)  # EOF
    except RuntimeError:
        pass  # the loop closed during shutdown (e.g. after /quit) — nothing to deliver


async def run(json_mode: bool) -> None:
    settings = Settings.from_env()
    store = CoreStore(settings.state_dir)

    if json_mode:
        async def emit(event: dict) -> None:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    else:
        async def emit(event: dict) -> None:
            line = _pretty(event)
            if line is not None:
                print(line, flush=True)

    transport = CLITransport(emit, store)
    engine = Engine(settings, store)
    engine.attach_transport(transport)
    engine.rehydrate()

    await emit(transport.snapshot())
    try:
        await engine._refresh_dashboard()
    except Exception:  # noqa: BLE001
        pass

    if not json_mode:
        print(f"{_BOLD}{settings.bot_name}{_RESET} — {HELP}\n", flush=True)

    state = State()
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=_reader, args=(loop, queue), daemon=True).start()

    while not state.quit:
        line = await queue.get()
        if line is None:            # stdin closed
            break
        try:
            await handle_line(engine, transport, state, line)
        except Exception as e:  # noqa: BLE001 — never let one bad line kill the loop
            await _sys(transport, state, f"⚠️ {e}")

    await engine.shutdown()


async def _build_engine(emit):
    """Wire the engine + CLI transport for a fresh cockpit and hand it the emit sink."""
    settings = Settings.from_env()
    store = CoreStore(settings.state_dir)
    transport = CLITransport(emit, store)
    engine = Engine(settings, store)
    engine.attach_transport(transport)
    engine.rehydrate()
    await emit(transport.snapshot())
    try:
        await engine._refresh_dashboard()
    except Exception:  # noqa: BLE001
        pass
    return engine, transport, State()


def _run_tui() -> None:
    from .tui import Cockpit
    bot_name = Settings.from_env().bot_name
    Cockpit(bot_name=bot_name, engine_builder=_build_engine).run()


def main() -> None:
    # UTF-8 in/out everywhere: agents and humans both emit emoji, and Windows
    # consoles/pipes default to a legacy codepage that can't encode them.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # Logs to stderr so stdout stays clean (JSON in --json mode, chat in interactive).
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    args = sys.argv[1:]
    if "--json" in args:
        mode = "json"
    elif "--plain" in args:
        mode = "plain"
    elif "--tui" in args or sys.stdout.isatty():
        mode = "tui"
    else:
        mode = "plain"  # piped/redirected → line mode by default

    try:
        if mode == "tui":
            try:
                _run_tui()
            except ImportError:
                print("The cockpit needs the TUI extra:  pip install be-a-boss[tui]\n"
                      "Falling back to plain mode.", file=sys.stderr)
                asyncio.run(run(json_mode=False))
        else:
            asyncio.run(run(json_mode=(mode == "json")))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
