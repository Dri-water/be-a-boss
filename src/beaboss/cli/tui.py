"""The be-a-boss cockpit — a Textual TUI that assembles itself as work happens.

Idle, it's just a clean conversation with the orchestrator. Hire a worker and the
thread sidebar slides in; the fleet starts moving and the dashboard strip appears.
The UI grows to match the work instead of showing empty scaffolding.

Requires the [tui] extra (textual). Agents drive with `--json` and need none of it.
The transport feeds this app the same event dicts every surface speaks, so the
cockpit is a pure view over the shared engine.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static

from ..transports.cli import OFFICE

# Speaker colours — one identity system across the whole cockpit.
_ROLE = {"orchestrator": "#5fafff", "worker": "#d787ff", "you": "#5fd787",
         "system": "#8a8a8a", "direct": "#bcbcbc"}

EngineBuilder = Callable[[Callable], Awaitable[tuple]]


class Cockpit(App):
    CSS = """
    Screen { layout: vertical; background: $surface; }
    #titlebar { height: 1; background: $boost; color: $text-muted; padding: 0 1; }
    #body { height: 1fr; }
    #sidebar { width: 28; display: none; padding: 0; background: $panel; }
    #sidebar.show { display: block; }
    #sidebar-title { height: 1; color: $text-muted; padding: 0 1; }
    #threads { background: $panel; }
    #convo { width: 1fr; padding: 0 1; background: $surface; }
    #dash { height: auto; max-height: 10; background: $panel; color: $text;
            padding: 0 1; display: none; border-top: solid $boost; }
    #dash.show { display: block; }
    #prompt { border: none; border-top: solid $boost; height: 3; background: $surface; }
    ListItem { padding: 0 1; color: $text; }
    ListItem.-active { background: $accent 40%; text-style: bold; }
    """

    BINDINGS = [Binding("ctrl+q", "quit", "quit"),
                Binding("ctrl+c", "quit", "quit")]

    def __init__(self, bot_name: str = "be-a-boss",
                 engine_builder: EngineBuilder | None = None,
                 demo_events: list[dict] | None = None):
        super().__init__()
        self.bot_name = bot_name
        self._engine_builder = engine_builder
        self._demo_events = demo_events or []
        self.msgs: dict[str, list[tuple[dict, str]]] = {OFFICE: []}
        self.titles: dict[str, str] = {OFFICE: "🧭 Orchestrator"}
        self.unread: dict[str, int] = {}
        self._dash_text = ""
        self.active = OFFICE
        self.engine = self.transport = self.state = None

    def compose(self) -> ComposeResult:
        yield Static(f" be-a-boss · [b]{self.bot_name}[/b]", id="titlebar")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("THREADS", id="sidebar-title")
                yield ListView(id="threads")
            yield RichLog(id="convo", wrap=True, markup=True, highlight=False)
        yield Static("", id="dash")
        yield Input(placeholder="Message the orchestrator…   (/help for commands)",
                    id="prompt")

    async def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        if self._engine_builder is not None:
            self.engine, self.transport, self.state = \
                await self._engine_builder(self.apply_event)
        for ev in self._demo_events:
            await self.apply_event(ev)

    # ---- event intake (the transport's emit target) ---------------------

    async def apply_event(self, event: dict) -> None:
        t = event.get("type")
        if t in ("message", "media"):
            self._ingest_message(event)
        elif t == "thread":
            self._ingest_thread(event)
        elif t == "dashboard":
            self._ingest_dashboard(event.get("text", ""))
        elif t == "threads":
            # connect/rehydrate snapshot: seed the sidebar so restarted workers show
            for th in event.get("threads", []):
                if th["id"] != OFFICE:
                    self.titles[th["id"]] = th["title"]
                    self.msgs.setdefault(th["id"], [])
            self._reveal_frames()
            self._refresh_sidebar()
        # "busy": nothing to paint in this view

    def _ingest_message(self, event: dict) -> None:
        tid = event.get("thread_id", OFFICE)
        text = event.get("text", "")
        if event.get("type") == "media":
            cap = f" — {event['caption']}" if event.get("caption") else ""
            text = f"🖼  [{event.get('kind')}: {event.get('filename')}]{cap}"
        if tid != OFFICE and tid not in self.titles:
            # a message for a thread we never got a `thread` event for → still list it
            self.titles[tid] = tid
            self._reveal_frames()
            self._refresh_sidebar()
        self.msgs.setdefault(tid, []).append((event.get("speaker", {}), text))
        if tid == self.active:
            self._write(event.get("speaker", {}), text)
        else:
            self.unread[tid] = self.unread.get(tid, 0) + 1
            self._refresh_sidebar()

    def _write(self, speaker: dict, text: str) -> None:
        log = self.query_one("#convo", RichLog)
        color = _ROLE.get(speaker.get("role"), "white")
        who = f"{speaker.get('emoji', '')} {speaker.get('name', '')}".strip()
        log.write(Text(who or "system", style=f"bold {color}"))
        if text.strip():
            log.write(Markdown(text))
        log.write(Text(""))  # a breath between messages

    def _ingest_thread(self, event: dict) -> None:
        tid = event["id"]
        if event.get("removed"):
            self.titles.pop(tid, None)
            self.msgs.pop(tid, None)
            if self.active == tid:
                self.active = OFFICE
                self._render_active()
        else:
            self.titles[tid] = event["title"]
            self.msgs.setdefault(tid, [])
        self._reveal_frames()
        self._refresh_sidebar()

    def _reveal_frames(self) -> None:
        # the cockpit reveals its frames once there's more than the orchestrator —
        # keyed on real state, not by parsing the dashboard text.
        show = len(self.titles) > 1
        self.query_one("#sidebar").set_class(show, "show")
        self.query_one("#dash").set_class(show and bool(self._dash_text), "show")

    def _ingest_dashboard(self, text: str) -> None:
        self._dash_text = text
        self.query_one("#dash", Static).update(text)
        self._reveal_frames()

    def _refresh_sidebar(self) -> None:
        lv = self.query_one("#threads", ListView)
        lv.clear()
        for tid, title in self.titles.items():
            badge = f"  [b]●{self.unread[tid]}[/b]" if self.unread.get(tid) else ""
            item = ListItem(Label(f"{title}{badge}"))
            item._tid = tid
            if tid == self.active:
                item.add_class("-active")
            lv.append(item)

    def _render_active(self) -> None:
        log = self.query_one("#convo", RichLog)
        log.clear()
        for speaker, text in self.msgs.get(self.active, []):
            self._write(speaker, text)

    # ---- interaction -----------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        tid = getattr(event.item, "_tid", None)
        if tid:
            self.active = tid
            self.unread.pop(tid, None)
            if self.state is not None:
                self.state.active = tid
            self._render_active()
            self._refresh_sidebar()
            self.query_one("#prompt", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        # echo your own line locally — the engine doesn't send it back
        if not line.startswith("/") and not line.lstrip().startswith("{"):
            you = {"role": "you", "name": "You", "emoji": ""}
            self.msgs.setdefault(self.active, []).append((you, line))
            self._write(you, line)
        if self.engine is not None:
            from .__main__ import handle_line
            # self.active is the source of truth: route by what's on screen, then
            # follow any thread switch the dispatch made (/new, /kill, /reset) so the
            # view and the input target never drift apart.
            self.state.active = self.active
            await handle_line(self.engine, self.transport, self.state, line)
            if self.state.active != self.active:
                self.active = self.state.active
                self.unread.pop(self.active, None)
                self._render_active()
                self._refresh_sidebar()
