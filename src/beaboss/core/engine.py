"""The engine: owns all sessions, routes messages, exposes the orchestrator.

Threads and roles:
- orchestrator thread ("the office", transport's main thread): human <-> orchestrator
- coder threads: a visible pair — the orchestrator drives a coder session, and
  everything both say is posted to the thread. The human may interject; the
  message reaches the coder as input and the orchestrator via its inbox.
- direct threads: the original beaboss model (human <-> session), unchanged.

Supervision is checkpoint-based: coder turn-ends and human interjections land in
an inbox; each coder turn-end wakes the orchestrator with the accumulated digest.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, create_sdk_mcp_server, tool

from .names import pick_name
from .ports import InboundMessage, MediaIn, Outbound, Speaker, SYSTEM, Transport
from .session import CoreSession
from .store import CoreStore, ThreadRecord
from . import worktrees

log = logging.getLogger("beaboss.core.engine")

ORCHESTRATOR_EMOJI = "🧭"
CODER_EMOJI = "⚙️"

ORCHESTRATOR_APPEND = (
    "You are the ORCHESTRATOR of a small software organisation, talking with your "
    "boss in a chat thread. You do not write project code yourself — you run a "
    "crew of coder agents and use your fleet tools to get work done:\n"
    "- mcp__fleet__list_repos() — see the available repositories\n"
    "- mcp__fleet__spawn_coder(repo, task) — hire a coder: creates a visible "
    "thread and an isolated git worktree, briefs them, and they start working\n"
    "- mcp__fleet__message_coder(coder_id, text) — speak to a coder (your message "
    "is visible in their thread)\n"
    "- mcp__fleet__coder_status(coder_id?) — current fleet state\n"
    "- mcp__fleet__dismiss_coder(coder_id) — end a coder: clean worktrees are "
    "removed, dirty ones are preserved and reported\n\n"
    "Prime directives:\n"
    "1. Never modify a project yourself — coders change projects, you read and "
    "direct.\n"
    "2. Never merge or discard work without the boss's explicit word.\n"
    "3. Never dismiss a coder whose work isn't committed/landed — a refused "
    "teardown is a stop-and-investigate, not an obstacle.\n"
    "4. Report outcomes faithfully. If work failed, say so plainly with the "
    "evidence.\n\n"
    "How to brief a coder:\n"
    "- A brief must be SELF-CONTAINED: the coder knows nothing about this chat. "
    "State the repo context, the concrete goal, constraints, acceptance criteria "
    "and the definition of done (tests pass, build works, committed).\n"
    "- One coder = one task. Split independent work across coders; they run in "
    "parallel in isolated worktrees, so same-repo parallelism is safe.\n"
    "- Steer with short messages; put long instructions in the brief, not drip-fed.\n\n"
    "Supervision:\n"
    "- You are woken with [fleet inbox] digests when a coder finishes a turn, "
    "gets blocked, needs a decision, or the boss interjects in a coder thread. "
    "React to the digest: answer the coder, re-brief, dismiss, or report to the "
    "boss. Do not poll; do not micro-manage a working coder.\n"
    "- A coder's 'STATUS: blocked' means they need YOUR help now. A "
    "'needs-decision' belongs to the boss — relay it with options and your "
    "recommendation.\n"
    "- Escalate to the boss: decisions that are theirs (product choices, merges, "
    "unclear requirements), anything destructive/irreversible/security-sensitive, "
    "and finished work ready for review. Do NOT surface routine progress, "
    "retries, or internal mechanics.\n\n"
    "Talk in outcomes, not mechanics. The boss cares about the project, not your "
    "internals: say 'isolated copy' not 'worktree', 'instructions' not 'brief', "
    "'cleanup' not 'teardown', name coders only when it matters. Lead with "
    "concrete evidence, then the consequence, then options and a recommendation.\n\n"
    "The boss can see every coder thread and may talk in them directly; treat "
    "their word there as authoritative context for you and the coder both.\n"
    "Keep replies to the boss short and information-dense. No flattery. An empty "
    "queue is a healthy resting state — never invent work."
)

CODER_APPEND_EXTRA = (
    "\n\nYou are a CODER on a small team. Your manager (the orchestrator) briefs "
    "you and supervises via this thread; the boss (a human) can read everything "
    "and may interject directly — treat boss messages as authoritative.\n"
    "- First, verify isolation: run `git rev-parse --show-toplevel` — you should "
    "be in your own worktree (branch coder/<your-id>), not the primary checkout. "
    "If you find yourself in the primary checkout, STOP and report it.\n"
    "- Work autonomously toward the brief's definition of done. Commit on your "
    "branch with clear messages as you go; your branch outlives you.\n"
    "- Act only on your brief and follow-ups. Never start surveys or extra "
    "'improvements' on your own initiative.\n"
    "- If you hit the same obstacle twice, stop and report blocked — don't grind.\n"
    "- If a decision belongs to a human (product choice, destructive action, "
    "ambiguous requirement), stop and ask — do not guess.\n"
    "- End every reply with one line, chosen honestly (a wrong 'done' is worse "
    "than 'blocked'):\n"
    "  STATUS: done | working | blocked: <what you need> | "
    "needs-decision: <the options>"
)


class Engine:
    def __init__(self, settings, store: CoreStore):
        self.settings = settings
        self.store = store
        self.transport: Transport | None = None
        self.sessions: dict[str, CoreSession] = {}
        self._inbox: list[str] = []          # pending notes for the orchestrator
        self._waking = False                 # digest wake in flight

    # ---- wiring ----------------------------------------------------------

    def attach_transport(self, transport: Transport) -> None:
        self.transport = transport

    async def _post(self, out: Outbound) -> None:
        assert self.transport is not None
        await self.transport.post(out)

    async def _busy(self, thread_id: str) -> None:
        if self.transport is not None:
            await self.transport.indicate_busy(thread_id)

    # ---- speakers --------------------------------------------------------

    def orchestrator_speaker(self) -> Speaker:
        return Speaker(role="orchestrator", name=self.settings.bot_name,
                       emoji=ORCHESTRATOR_EMOJI)

    @staticmethod
    def coder_speaker(name: str) -> Speaker:
        return Speaker(role="coder", name=name, emoji=CODER_EMOJI)

    # ---- inbound routing -------------------------------------------------

    async def on_inbound(self, msg: InboundMessage) -> None:
        rec = self.store.get(msg.thread_id)

        if rec is None and msg.thread_id == (self.store.orchestrator_thread or msg.thread_id):
            # First contact in the office thread: bring the orchestrator to life.
            if self.store.orchestrator_thread in (None, msg.thread_id):
                await self._ensure_orchestrator(msg.thread_id)
                rec = self.store.get(msg.thread_id)

        if rec is None:
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text="This thread isn't active. Talk to the orchestrator in the "
                     "main thread, or use /new for a direct session.",
            ))
            return

        session = await self._ensure_session(msg.thread_id, rec)
        if session is None:
            return

        if rec.role == "coder":
            await self._interject(msg, rec, session)
            return

        # orchestrator or direct: plain turn
        if msg.media:
            await session.submit_media(msg.text, msg.media)
        else:
            await session.submit(msg.text)

    async def _interject(self, msg: InboundMessage, rec: ThreadRecord,
                         session: CoreSession) -> None:
        """Boss speaks inside a coder thread: coder hears it now, orchestrator
        sees it in the next digest."""
        who = msg.sender_name or "the boss"
        text = (f"[Interjection from {who} — visible to you and the orchestrator]: "
                f"{msg.text}")
        if msg.media:
            await session.submit_media(text, msg.media)
        else:
            await session.submit(text)
        self._note(f"{who} said in {rec.coder_id}'s thread: {msg.text}")

    # ---- session management ---------------------------------------------

    async def _ensure_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession | None:
        session = self.sessions.get(thread_id)
        if session is not None:
            return session
        try:
            if rec.role == "orchestrator":
                session = self._make_orchestrator_session(thread_id, rec)
            elif rec.role == "coder":
                session = self._make_coder_session(thread_id, rec)
            else:
                session = self._make_direct_session(thread_id, rec)
            await session.start()
        except Exception as e:  # noqa: BLE001
            log.exception("failed to start session thread=%s", thread_id)
            await self._post(Outbound(
                thread_id=thread_id, speaker=SYSTEM,
                text=f"⚠️ couldn't start this session: {e}",
            ))
            return None
        self.sessions[thread_id] = session
        return session

    def _sid_saver(self, thread_id: str):
        def save(sid: str) -> None:
            self.store.update(thread_id, session_id=sid)
        return save

    def _make_direct_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        return CoreSession(
            thread_id=thread_id, cwd=Path(rec.cwd),
            speaker=Speaker(role="direct", name=self.settings.bot_name),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
        )

    def _make_orchestrator_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        home = self.settings.state_dir / "orchestrator-home"
        home.mkdir(parents=True, exist_ok=True)
        base = (self.settings.session_system_append
                if self.settings.session_system_append is not None else "")
        append = (base + "\n\n" if base else "") + ORCHESTRATOR_APPEND
        return CoreSession(
            thread_id=thread_id, cwd=home,
            speaker=self.orchestrator_speaker(),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
            system_append=append,
            extra_mcp_servers={"fleet": self._build_fleet_server()},
        )

    def _make_coder_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        session = CoreSession(
            thread_id=thread_id, cwd=Path(rec.cwd),
            speaker=self.coder_speaker(rec.name),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
            system_append=None,  # default env note…
        )
        # …plus the coder role note appended onto it
        session._system_append = session._resolve_append() + CODER_APPEND_EXTRA
        session.on_turn_done = self._on_coder_turn_done
        return session

    async def _ensure_orchestrator(self, thread_id: str) -> None:
        if self.store.get(thread_id) is None:
            self.store.put(thread_id, ThreadRecord(
                role="orchestrator", name="orchestrator"))
        self.store.set_orchestrator_thread(thread_id)

    # ---- supervision inbox ----------------------------------------------

    def _note(self, text: str) -> None:
        self._inbox.append(text)
        log.info("inbox note: %s", text[:160])

    async def _on_coder_turn_done(self, session: CoreSession, result: ResultMessage) -> None:
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        tail = (result.result or "").strip()
        if len(tail) > 600:
            tail = tail[:600] + "…"
        # track the coder's self-reported status line if present
        low = tail.lower()
        if "status: done" in low:
            self.store.update(session.thread_id, coder_status="done")
        elif "status: blocked" in low or "status: needs-decision" in low:
            self.store.update(session.thread_id, coder_status="blocked")
        status = "errored" if result.is_error else "finished a turn"
        self._note(f"coder {rec.coder_id} ({rec.name}, task: {rec.task[:80]}) "
                   f"{status}: {tail or '(no text)'}")
        await self._wake_orchestrator()

    # Coalescing window: near-simultaneous coder events (e.g. two coders finish
    # together, or a boss interjection followed by the coder's reply) become one
    # orchestrator turn instead of several. (Firstmate's SIGNAL_GRACE idea.)
    WAKE_COALESCE_SECS = 2.0

    async def _wake_orchestrator(self) -> None:
        """Deliver the accumulated inbox to the orchestrator as one digest turn."""
        if self._waking or not self._inbox:
            return
        othread = self.store.orchestrator_thread
        if othread is None:
            return
        rec = self.store.get(othread)
        if rec is None:
            return
        session = await self._ensure_session(othread, rec)
        if session is None:
            return
        self._waking = True
        try:
            await asyncio.sleep(self.WAKE_COALESCE_SECS)
            notes, self._inbox = self._inbox, []
            if not notes:
                return
            digest = "[fleet inbox]\n" + "\n".join(f"- {n}" for n in notes)
            await session.submit(digest)
        finally:
            self._waking = False

    # ---- fleet tools (the orchestrator's powers) -------------------------

    def _build_fleet_server(self):
        engine = self

        def ok(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}]}

        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        @tool(
            "list_repos",
            "List the repositories available under the projects root.",
            {"type": "object", "properties": {}},
        )
        async def list_repos(args: dict[str, Any]) -> dict[str, Any]:
            root = engine.settings.projects_root
            try:
                rows = []
                for p in sorted(root.iterdir()):
                    if p.is_dir() and not p.name.startswith("."):
                        tag = " (git)" if (p / ".git").exists() else ""
                        rows.append(f"- {p.name}{tag}")
                return ok("\n".join(rows) or "(no repositories found)")
            except OSError as e:
                return err(f"could not list {root}: {e}")

        @tool(
            "spawn_coder",
            "Hire a coder for one task. Creates a visible thread and an isolated "
            "git worktree of the repo, briefs the coder, and they start working. "
            "The brief must be self-contained (goal, constraints, definition of "
            "done). Returns the coder's id.",
            {"type": "object",
             "properties": {
                 "repo": {"type": "string",
                          "description": "repo name under the projects root, or absolute path"},
                 "task": {"type": "string", "description": "the self-contained brief"},
             },
             "required": ["repo", "task"]},
        )
        async def spawn_coder(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._spawn_coder(str(args.get("repo", "")),
                                             str(args.get("task", "")))

        @tool(
            "message_coder",
            "Say something to a coder. Your message is posted in their thread "
            "(visible to the boss) and becomes the coder's next input.",
            {"type": "object",
             "properties": {
                 "coder_id": {"type": "string"},
                 "text": {"type": "string"},
             },
             "required": ["coder_id", "text"]},
        )
        async def message_coder(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._message_coder(str(args.get("coder_id", "")),
                                               str(args.get("text", "")))

        @tool(
            "coder_status",
            "Current fleet state. Pass coder_id for one coder, omit for all.",
            {"type": "object",
             "properties": {"coder_id": {"type": "string"}}},
        )
        async def coder_status(args: dict[str, Any]) -> dict[str, Any]:
            rows = []
            for tid, rec in engine.store.coders().items():
                cid = rec.coder_id
                want = str(args.get("coder_id", "")).strip()
                if want and want != cid:
                    continue
                live = engine.sessions.get(tid)
                state = live.status if live else "dormant"
                rows.append(f"- {cid} ({rec.name}) [{state}] repo={rec.repo} "
                            f"status={rec.coder_status or 'working'} task={rec.task[:100]}")
            return ok("\n".join(rows) or "(no coders)")

        @tool(
            "dismiss_coder",
            "End a coder's engagement. Their worktree is removed if clean; a "
            "dirty worktree is preserved and its path reported.",
            {"type": "object",
             "properties": {"coder_id": {"type": "string"}},
             "required": ["coder_id"]},
        )
        async def dismiss_coder(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._dismiss_coder(str(args.get("coder_id", "")))

        return create_sdk_mcp_server(
            "fleet",
            tools=[list_repos, spawn_coder, message_coder, coder_status, dismiss_coder],
        )

    # ---- fleet operations ------------------------------------------------

    def _find_coder(self, coder_id: str) -> tuple[str, ThreadRecord] | None:
        for tid, rec in self.store.coders().items():
            if rec.coder_id == coder_id:
                return tid, rec
        return None

    async def _spawn_coder(self, repo_raw: str, task: str) -> dict[str, Any]:
        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        if not repo_raw.strip() or not task.strip():
            return err("repo and task are both required")
        repo = Path(repo_raw)
        if not repo.is_absolute():
            repo = self.settings.projects_root / repo_raw
        repo = repo.resolve()
        if not repo.is_dir():
            return err(f"no such repo: {repo}")

        taken = {r.coder_id for r in self.store.coders().values()}
        taken |= {r.name.lower() for r in self.store.coders().values()}
        name = pick_name(taken)
        coder_id = name.lower()

        # workspace: isolated worktree when the repo is git, else the repo itself
        if await worktrees.is_git_repo(repo):
            try:
                wt = await worktrees.create_worktree(
                    repo, self.settings.state_dir / "worktrees", coder_id)
                cwd, isolated = wt, True
            except worktrees.WorktreeError as e:
                return err(f"worktree creation failed: {e}")
        else:
            cwd, isolated = repo, False

        assert self.transport is not None
        thread_id = await self.transport.create_thread(
            f"{CODER_EMOJI} {name} · {repo.name}")

        rec = ThreadRecord(
            role="coder", name=name, cwd=str(cwd), coder_id=coder_id,
            repo=str(repo), task=task.strip(), coder_status="working",
        )
        self.store.put(thread_id, rec)

        iso_note = ("isolated worktree, branch coder/" + coder_id if isolated
                    else "⚠️ not a git repo — working directly in the project dir")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{name} hired for {repo.name} ({iso_note}).",
        ))
        # the orchestrator's brief, visible in the thread:
        await self._post(Outbound(
            thread_id=thread_id, speaker=self.orchestrator_speaker(), text=task.strip(),
        ))

        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("coder session failed to start (see thread)")
        await session.submit(
            f"[Brief from the orchestrator]\n{task.strip()}"
        )
        return {"content": [{"type": "text", "text":
                f"spawned coder {coder_id} ({name}) in {iso_note}; "
                f"thread created. They will report back via the fleet inbox."}]}

    async def _message_coder(self, coder_id: str, text: str) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_coder(coder_id.strip())
        if found is None:
            return err(f"no such coder: {coder_id}")
        if not text.strip():
            return err("text is required")
        thread_id, rec = found
        await self._post(Outbound(
            thread_id=thread_id, speaker=self.orchestrator_speaker(), text=text.strip(),
        ))
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("coder session unavailable")
        await session.submit(f"[From the orchestrator]: {text.strip()}")
        return {"content": [{"type": "text", "text": "delivered"}]}

    async def _dismiss_coder(self, coder_id: str) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_coder(coder_id.strip())
        if found is None:
            return err(f"no such coder: {coder_id}")
        thread_id, rec = found

        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()

        detail = ""
        wt = Path(rec.cwd)
        if rec.repo and wt != Path(rec.repo):
            removed, detail = await worktrees.remove_worktree(Path(rec.repo), wt)
            if not removed:
                detail = f" ({detail})"
            else:
                detail = ""

        self.store.update(thread_id, coder_status="dismissed")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{rec.name} dismissed by the orchestrator.{detail}",
        ))
        if self.transport is not None:
            try:
                await self.transport.close_thread(thread_id)
            except Exception:  # noqa: BLE001
                pass
        return {"content": [{"type": "text", "text": f"dismissed {coder_id}{detail}"}]}

    # ---- direct sessions (pre-orchestrator model, unchanged) -------------

    async def new_direct(self, path_raw: str, name: str | None) -> tuple[str, str] | str:
        """Create a direct session thread. Returns (thread_id, name) or error str."""
        p = Path(path_raw).expanduser()
        if not p.is_absolute():
            p = self.settings.projects_root / path_raw
        p = p.resolve()
        if not p.is_dir():
            return f"❌ Not a directory: {p}"
        title = name or p.name
        assert self.transport is not None
        thread_id = await self.transport.create_thread(title)
        rec = ThreadRecord(role="direct", name=title, cwd=str(p))
        self.store.put(thread_id, rec)
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return "❌ session failed to start"
        return thread_id, title

    async def interrupt(self, thread_id: str) -> bool:
        session = self.sessions.get(thread_id)
        if session is None:
            return False
        await session.interrupt()
        return True

    async def kill(self, thread_id: str) -> bool:
        rec = self.store.get(thread_id)
        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()
        if rec is None:
            return session is not None
        if rec.role == "coder" and rec.repo and rec.cwd != rec.repo:
            await worktrees.remove_worktree(Path(rec.repo), Path(rec.cwd))
        if rec.role == "orchestrator":
            self.store.set_orchestrator_thread(None)
        self.store.delete(thread_id)
        return True

    def listing(self) -> list[tuple[str, ThreadRecord, str]]:
        rows = []
        for tid, rec in self.store.all().items():
            live = self.sessions.get(tid)
            rows.append((tid, rec, live.status if live else "dormant"))
        return rows

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()
